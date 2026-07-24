"""
Reddy-Fit Body Scanner — camera AI body composition tracker with accounts.

- Passwordless login: email -> 6-digit OTP -> session token
- Per-user daily entries: one selfie OR video + weight per day (upsert)
- Before/after comparison + 15-day transformation reel
- DURABLE STORAGE: every photo, every video, and the whole database are
  mirrored to Azure Blob Storage so nothing is lost when the container
  is redeployed (Railway disks are ephemeral). Media is read-through:
  served from local cache if present, otherwise streamed from Blob.

Storage layout (Azure), container per media class:
    photos/{user_id}/{date}.jpg
    videos/{user_id}/{date}.webm
    backups/reddyfit.db          (consistent SQLite snapshot)

If AZURE_STORAGE_CONNECTION_STRING is unset, the app runs fully locally
(useful for tests / dev) — every blob call is a safe no-op.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import secrets
import smtplib
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime
from email.mime.text import MIMEText

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

APP_NAME = "Reddy-Fit Body Scanner"
DATA_DIR = os.environ.get("DATA_DIR", "./data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "reddyfit.db")
MEDIA_DIR = os.path.join(DATA_DIR, "media")
os.makedirs(MEDIA_DIR, exist_ok=True)

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)

AZURE_CONN = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
PHOTOS_CONTAINER = os.environ.get("AZURE_PHOTOS_CONTAINER", "photos")
VIDEOS_CONTAINER = os.environ.get("AZURE_VIDEOS_CONTAINER", "videos")
BACKUPS_CONTAINER = os.environ.get("AZURE_BACKUPS_CONTAINER", "backups")
DB_BLOB_NAME = "reddyfit.db"

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "").lower()
SESSION_DAYS = 90
OTP_TTL_MIN = 10
MAX_IMAGE_BYTES = 8_000_000
MAX_VIDEO_BYTES = 25_000_000
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

app = FastAPI(title=APP_NAME, version="3.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ================================================================ blob storage
_blob_service = None
_blob_lock = threading.Lock()


def blob_service():
    """Cached BlobServiceClient, or None when Azure isn't configured."""
    global _blob_service
    if not AZURE_CONN:
        return None
    if _blob_service is None:
        with _blob_lock:
            if _blob_service is None:
                from azure.storage.blob import BlobServiceClient

                _blob_service = BlobServiceClient.from_connection_string(AZURE_CONN)
                for c in (PHOTOS_CONTAINER, VIDEOS_CONTAINER, BACKUPS_CONTAINER):
                    try:
                        _blob_service.create_container(c)
                    except Exception:
                        pass
    return _blob_service


def blob_put(container: str, name: str, data: bytes, content_type: str) -> bool:
    """Best-effort upload. Never raises — storage must not break a save."""
    svc = blob_service()
    if not svc:
        return False
    try:
        from azure.storage.blob import ContentSettings

        svc.get_blob_client(container, name).upload_blob(
            data, overwrite=True, content_settings=ContentSettings(content_type=content_type)
        )
        return True
    except Exception:
        return False


def blob_get(container: str, name: str) -> bytes | None:
    svc = blob_service()
    if not svc:
        return None
    try:
        return svc.get_blob_client(container, name).download_blob().readall()
    except Exception:
        return None


def blob_delete(container: str, name: str) -> None:
    svc = blob_service()
    if not svc or not name:
        return
    try:
        svc.get_blob_client(container, name).delete_blob()
    except Exception:
        pass


# --- durable DB: consistent snapshot -> backups container, restore on boot ---
def snapshot_db_bytes() -> bytes:
    """A consistent SQL dump of the SQLite DB using the online backup API."""
    src = sqlite3.connect(DB_PATH)
    mem = sqlite3.connect(":memory:")
    try:
        src.backup(mem)
        buf = io.BytesIO()
        for line in mem.iterdump():
            buf.write((line + "\n").encode())
        return buf.getvalue()
    finally:
        src.close()
        mem.close()


_last_backup = 0.0


def backup_db(force: bool = False) -> None:
    """Push a SQL dump of the DB to Blob. Throttled to avoid write storms."""
    global _last_backup
    if not AZURE_CONN:
        return
    now = time.time()
    if not force and now - _last_backup < 3:
        return
    _last_backup = now
    try:
        blob_put(BACKUPS_CONTAINER, DB_BLOB_NAME, snapshot_db_bytes(), "application/sql")
    except Exception:
        pass


def restore_db_if_needed() -> None:
    """On a fresh container, rebuild the DB from the last Blob snapshot."""
    if not AZURE_CONN:
        return
    if os.path.exists(DB_PATH):
        try:
            c = sqlite3.connect(DB_PATH)
            has = c.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='users'"
            ).fetchone()[0]
            users = c.execute("SELECT count(*) FROM users").fetchone()[0] if has else 0
            c.close()
            if users > 0:
                return  # local DB already has data
        except Exception:
            pass
    dump = blob_get(BACKUPS_CONTAINER, DB_BLOB_NAME)
    if not dump:
        return
    try:
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
        c = sqlite3.connect(DB_PATH)
        c.executescript(dump.decode())
        c.commit()
        c.close()
    except Exception:
        pass


# ================================================================ db
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS otps (
                email TEXT PRIMARY KEY,
                code_hash TEXT NOT NULL,
                expires_at REAL NOT NULL,
                attempts INTEGER DEFAULT 0,
                last_sent REAL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS entries (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                entry_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                weight_lbs REAL,
                height_in REAL,
                age INTEGER,
                sex TEXT,
                bf_percent REAL,
                bmi REAL,
                waist_shoulder_ratio REAL,
                image_path TEXT,
                video_path TEXT,
                image_blob TEXT,
                video_blob TEXT,
                media_type TEXT DEFAULT 'photo',
                azure_blob_url TEXT,
                notes TEXT,
                UNIQUE(user_id, entry_date)
            );
            """
        )


def _migrate() -> None:
    with db() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(entries)").fetchall()]
        for col in ("video_path", "image_blob", "video_blob", "azure_blob_url"):
            if col not in cols:
                conn.execute(f"ALTER TABLE entries ADD COLUMN {col} TEXT")
        if "media_type" not in cols:
            conn.execute("ALTER TABLE entries ADD COLUMN media_type TEXT DEFAULT 'photo'")


restore_db_if_needed()
init_db()
_migrate()


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# ================================================================ email
def send_otp_email(to_email: str, code: str) -> bool:
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
        return False
    msg = MIMEText(
        f"Your {APP_NAME} login code is: {code}\n\n"
        f"It expires in {OTP_TTL_MIN} minutes. If you didn't request this, ignore this email."
    )
    msg["Subject"] = f"{code} — your {APP_NAME} login code"
    msg["From"] = f"{APP_NAME} <{SMTP_FROM}>"
    msg["To"] = to_email
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
    return True


# ================================================================ auth
class OtpRequest(BaseModel):
    email: str


class OtpVerify(BaseModel):
    email: str
    code: str


def current_user(authorization: str = Header(default="")) -> sqlite3.Row:
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "Login required")
    with db() as conn:
        row = conn.execute(
            """SELECT u.id, u.email FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token = ? AND s.expires_at > ?""",
            (token, time.time()),
        ).fetchone()
    if not row:
        raise HTTPException(401, "Session expired — log in again")
    return row


@app.post("/api/auth/request-otp")
def request_otp(body: OtpRequest):
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "Enter a valid email address")
    now = time.time()
    with db() as conn:
        prev = conn.execute("SELECT last_sent FROM otps WHERE email = ?", (email,)).fetchone()
        if prev and now - prev["last_sent"] < 30:
            raise HTTPException(429, "Wait a moment before requesting another code")
        code = f"{secrets.randbelow(1000000):06d}"
        conn.execute(
            """INSERT INTO otps (email, code_hash, expires_at, attempts, last_sent)
               VALUES (?,?,?,0,?)
               ON CONFLICT(email) DO UPDATE SET code_hash=excluded.code_hash,
                 expires_at=excluded.expires_at, attempts=0, last_sent=excluded.last_sent""",
            (email, sha(code), now + OTP_TTL_MIN * 60, now),
        )
    emailed = False
    try:
        emailed = send_otp_email(email, code)
    except Exception:
        emailed = False
    resp = {"sent": True, "emailed": emailed}
    if not emailed:
        resp["dev_otp"] = code
        resp["note"] = "Email delivery not configured; use this code."
    return resp


@app.post("/api/auth/verify-otp")
def verify_otp(body: OtpVerify):
    email = body.email.strip().lower()
    code = body.code.strip()
    now = time.time()
    with db() as conn:
        row = conn.execute("SELECT * FROM otps WHERE email = ?", (email,)).fetchone()
        if not row or row["expires_at"] < now:
            raise HTTPException(400, "Code expired — request a new one")
        if row["attempts"] >= 5:
            raise HTTPException(429, "Too many attempts — request a new code")
        if sha(code) != row["code_hash"]:
            conn.execute("UPDATE otps SET attempts = attempts + 1 WHERE email = ?", (email,))
            conn.commit()  # persist the counter even though we raise next
            raise HTTPException(400, "Wrong code — try again")
        conn.execute("DELETE FROM otps WHERE email = ?", (email,))
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not user:
            uid = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO users (id, email, created_at) VALUES (?,?,?)",
                (uid, email, datetime.utcnow().isoformat()),
            )
        else:
            uid = user["id"]
        token = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO sessions (token, user_id, expires_at) VALUES (?,?,?)",
            (token, uid, now + SESSION_DAYS * 86400),
        )
    backup_db(force=True)
    return {"token": token, "email": email}


@app.post("/api/auth/logout")
def logout(authorization: str = Header(default="")):
    token = authorization.removeprefix("Bearer ").strip()
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    return {"ok": True}


@app.get("/api/me")
def me(user=Depends(current_user)):
    return {"email": user["email"]}


# ================================================================ entries
class EntryIn(BaseModel):
    entry_date: str = Field(default_factory=lambda: date.today().isoformat())
    weight_lbs: float | None = None
    height_in: float | None = None
    age: int | None = None
    sex: str | None = None
    bf_percent: float | None = None
    bmi: float | None = None
    waist_shoulder_ratio: float | None = None
    image_base64: str | None = None
    video_base64: str | None = None
    notes: str | None = None


def _decode(b64: str) -> bytes:
    raw = base64.b64decode(b64.split(",")[-1], validate=True)
    if not raw:
        raise ValueError("empty")
    return raw


@app.post("/api/entries")
def upsert_entry(entry: EntryIn, user=Depends(current_user)):
    """One entry per user per day — saving again replaces that day's data point."""
    uid = user["id"]
    entry_id = secrets.token_hex(16)
    image_path = video_path = image_blob = video_blob = None
    media_type = "photo"
    user_dir = os.path.join(MEDIA_DIR, uid)

    if entry.video_base64:
        try:
            rawv = _decode(entry.video_base64)
        except Exception as exc:
            raise HTTPException(400, "Invalid video data") from exc
        if len(rawv) > MAX_VIDEO_BYTES:
            raise HTTPException(400, "Video too large (25MB max)")
        os.makedirs(user_dir, exist_ok=True)
        video_path = os.path.join(user_dir, f"{entry.entry_date}.webm")
        with open(video_path, "wb") as f:
            f.write(rawv)
        video_blob = f"{uid}/{entry.entry_date}.webm"
        blob_put(VIDEOS_CONTAINER, video_blob, rawv, "video/webm")
        media_type = "video"

    if entry.image_base64:
        try:
            raw = _decode(entry.image_base64)
        except Exception as exc:
            raise HTTPException(400, "Invalid image data") from exc
        if len(raw) > MAX_IMAGE_BYTES:
            raise HTTPException(400, "Image too large")
        os.makedirs(user_dir, exist_ok=True)
        image_path = os.path.join(user_dir, f"{entry.entry_date}.jpg")
        with open(image_path, "wb") as f:
            f.write(raw)
        image_blob = f"{uid}/{entry.entry_date}.jpg"
        blob_put(PHOTOS_CONTAINER, image_blob, raw, "image/jpeg")

    with db() as conn:
        old = conn.execute(
            "SELECT id, image_path, video_path, image_blob, video_blob, media_type "
            "FROM entries WHERE user_id = ? AND entry_date = ?",
            (uid, entry.entry_date),
        ).fetchone()
        if old:
            entry_id = old["id"]
            image_path = image_path or old["image_path"]
            video_path = video_path or old["video_path"]
            image_blob = image_blob or old["image_blob"]
            video_blob = video_blob or old["video_blob"]
            if not entry.video_base64 and not entry.image_base64:
                media_type = old["media_type"] or "photo"
        conn.execute(
            """INSERT INTO entries
               (id, user_id, entry_date, created_at, weight_lbs, height_in, age, sex,
                bf_percent, bmi, waist_shoulder_ratio, image_path, video_path,
                image_blob, video_blob, media_type, azure_blob_url, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(user_id, entry_date) DO UPDATE SET
                 created_at=excluded.created_at, weight_lbs=excluded.weight_lbs,
                 height_in=excluded.height_in, age=excluded.age, sex=excluded.sex,
                 bf_percent=excluded.bf_percent, bmi=excluded.bmi,
                 waist_shoulder_ratio=excluded.waist_shoulder_ratio,
                 image_path=excluded.image_path, video_path=excluded.video_path,
                 image_blob=excluded.image_blob, video_blob=excluded.video_blob,
                 media_type=excluded.media_type, notes=excluded.notes""",
            (
                entry_id, uid, entry.entry_date, datetime.utcnow().isoformat(),
                entry.weight_lbs, entry.height_in, entry.age, entry.sex,
                entry.bf_percent, entry.bmi, entry.waist_shoulder_ratio,
                image_path, video_path, image_blob, video_blob, media_type, None, entry.notes,
            ),
        )
    backup_db()
    return {"id": entry_id, "entry_date": entry.entry_date, "replaced": bool(old)}


@app.get("/api/entries")
def list_entries(user=Depends(current_user), limit: int = 730):
    with db() as conn:
        rows = conn.execute(
            """SELECT id, entry_date, weight_lbs, height_in, age, sex, bf_percent,
                      bmi, waist_shoulder_ratio, notes, media_type,
                      CASE WHEN image_path IS NOT NULL OR image_blob IS NOT NULL THEN 1 ELSE 0 END AS has_image,
                      CASE WHEN video_path IS NOT NULL OR video_blob IS NOT NULL THEN 1 ELSE 0 END AS has_video
               FROM entries WHERE user_id = ? ORDER BY entry_date ASC LIMIT ?""",
            (user["id"], limit),
        ).fetchall()
    return [dict(r) for r in rows]


def _serve_media(row, path_col, blob_col, container, mime):
    path = row[path_col]
    if path and os.path.exists(path):
        return FileResponse(path, media_type=mime)
    data = blob_get(container, row[blob_col]) if row[blob_col] else None
    if data:
        # repopulate local cache so subsequent reads are fast
        try:
            if path:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(data)
        except Exception:
            pass
        return Response(content=data, media_type=mime)
    return None


@app.get("/api/entries/{entry_id}/image")
def entry_image(entry_id: str, user=Depends(current_user)):
    with db() as conn:
        row = conn.execute(
            "SELECT image_path, image_blob FROM entries WHERE id = ? AND user_id = ?",
            (entry_id, user["id"]),
        ).fetchone()
    resp = _serve_media(row, "image_path", "image_blob", PHOTOS_CONTAINER, "image/jpeg") if row else None
    if not resp:
        raise HTTPException(404, "No photo for this day")
    return resp


@app.get("/api/entries/{entry_id}/video")
def entry_video(entry_id: str, user=Depends(current_user)):
    with db() as conn:
        row = conn.execute(
            "SELECT video_path, video_blob FROM entries WHERE id = ? AND user_id = ?",
            (entry_id, user["id"]),
        ).fetchone()
    resp = _serve_media(row, "video_path", "video_blob", VIDEOS_CONTAINER, "video/webm") if row else None
    if not resp:
        raise HTTPException(404, "No video for this day")
    return resp


@app.delete("/api/entries/{entry_id}")
def delete_entry(entry_id: str, user=Depends(current_user)):
    with db() as conn:
        row = conn.execute(
            "SELECT image_path, video_path, image_blob, video_blob FROM entries WHERE id = ? AND user_id = ?",
            (entry_id, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Entry not found")
        conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    for p in (row["image_path"], row["video_path"]):
        if p and os.path.exists(p):
            os.remove(p)
    blob_delete(PHOTOS_CONTAINER, row["image_blob"])
    blob_delete(VIDEOS_CONTAINER, row["video_blob"])
    backup_db()
    return {"deleted": entry_id}


# ================================================================ admin (env only)
@app.get("/api/admin/stats")
def admin_stats(user=Depends(current_user)):
    if not ADMIN_EMAIL or user["email"].lower() != ADMIN_EMAIL:
        raise HTTPException(403, "Not authorized")
    with db() as conn:
        users = [dict(r) for r in conn.execute(
            "SELECT email, created_at FROM users ORDER BY created_at DESC LIMIT 200").fetchall()]
        entries = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        media = conn.execute(
            "SELECT COUNT(*) FROM entries WHERE image_blob IS NOT NULL OR video_blob IS NOT NULL").fetchone()[0]
    return {"total_users": len(users), "total_entries": entries,
            "media_in_blob": media, "recent_users": users}


@app.delete("/api/admin/users/{email}")
def admin_delete_user(email: str, user=Depends(current_user)):
    if not ADMIN_EMAIL or user["email"].lower() != ADMIN_EMAIL:
        raise HTTPException(403, "Not authorized")
    email = email.lower()
    with db() as conn:
        r = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if not r:
            raise HTTPException(404, "No such user")
        uid = r["id"]
        blobs = conn.execute(
            "SELECT image_blob, video_blob FROM entries WHERE user_id = ?", (uid,)).fetchall()
        for t in ("sessions", "entries"):
            conn.execute(f"DELETE FROM {t} WHERE user_id = ?", (uid,))
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    for b in blobs:
        blob_delete(PHOTOS_CONTAINER, b["image_blob"])
        blob_delete(VIDEOS_CONTAINER, b["video_blob"])
    backup_db(force=True)
    return {"deleted": email}


# ================================================================ misc
@app.get("/api/health")
def health():
    with db() as conn:
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        entries = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    return {"status": "ok", "app": APP_NAME, "users": users, "entries": entries,
            "email_configured": bool(SMTP_HOST and SMTP_USER and SMTP_PASS),
            "azure_enabled": bool(AZURE_CONN),
            "storage": "azure-blob" if AZURE_CONN else "local-only"}


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
