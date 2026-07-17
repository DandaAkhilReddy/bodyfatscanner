"""
Reddy-Fit Body Scanner — camera AI body composition tracker with accounts.

- Passwordless login: email -> 6-digit OTP -> session token
- Per-user daily entries: one selfie + weight per day (upsert)
- Before/after comparison across any two days
- SQLite storage (+ optional Azure Blob mirror for selfies)

OTP delivery: SMTP if SMTP_* env vars are set, otherwise "dev mode"
returns the code in the API response (fine for personal/friends use;
add SMTP creds for real email delivery).
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
import smtplib
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

APP_NAME = "Reddy-Fit Body Scanner"
DATA_DIR = os.environ.get("DATA_DIR", "./data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "reddyfit.db")
IMAGES_DIR = os.path.join(DATA_DIR, "selfies")
os.makedirs(IMAGES_DIR, exist_ok=True)

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)

AZURE_CONN = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
AZURE_CONTAINER = os.environ.get("AZURE_BLOB_CONTAINER", "selfies")

SESSION_DAYS = 90
OTP_TTL_MIN = 10
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

app = FastAPI(title=APP_NAME, version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------------- db
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
                media_type TEXT DEFAULT 'photo',
                azure_blob_url TEXT,
                notes TEXT,
                UNIQUE(user_id, entry_date)
            );
            """
        )


init_db()


def _migrate() -> None:
    with db() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(entries)").fetchall()]
        if "video_path" not in cols:
            conn.execute("ALTER TABLE entries ADD COLUMN video_path TEXT")
        if "media_type" not in cols:
            conn.execute("ALTER TABLE entries ADD COLUMN media_type TEXT DEFAULT 'photo'")


_migrate()


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# ---------------------------------------------------------------- email
def send_otp_email(to_email: str, code: str) -> bool:
    """Send OTP via SMTP. Returns True if actually emailed."""
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


# ---------------------------------------------------------------- auth
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
        # Dev mode: no SMTP configured — hand the code back so login still works.
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


# ---------------------------------------------------------------- entries
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


def upload_to_azure(filename: str, raw: bytes) -> str | None:
    if not AZURE_CONN:
        return None
    try:
        from azure.storage.blob import BlobServiceClient

        service = BlobServiceClient.from_connection_string(AZURE_CONN)
        try:
            service.create_container(AZURE_CONTAINER)
        except Exception:
            pass
        blob = service.get_blob_client(container=AZURE_CONTAINER, blob=filename)
        blob.upload_blob(raw, overwrite=True)
        return blob.url
    except Exception:
        return None


@app.post("/api/entries")
def upsert_entry(entry: EntryIn, user=Depends(current_user)):
    """One entry per user per day — saving again replaces that day's data point."""
    uid = user["id"]
    entry_id = secrets.token_hex(16)
    image_path = None
    video_path = None
    media_type = "photo"
    azure_url = None

    if entry.video_base64:
        b64v = entry.video_base64.split(",")[-1]
        try:
            rawv = base64.b64decode(b64v)
        except Exception:
            raise HTTPException(400, "Invalid video data")
        if len(rawv) > 25_000_000:
            raise HTTPException(400, "Video too large (25MB max)")
        user_dir = os.path.join(IMAGES_DIR, uid)
        os.makedirs(user_dir, exist_ok=True)
        video_path = os.path.join(user_dir, f"{entry.entry_date}.webm")
        with open(video_path, "wb") as f:
            f.write(rawv)
        upload_to_azure(f"{uid}/{entry.entry_date}.webm", rawv)
        media_type = "video"

    if entry.image_base64:
        b64 = entry.image_base64.split(",")[-1]
        try:
            raw = base64.b64decode(b64)
        except Exception:
            raise HTTPException(400, "Invalid image data")
        if len(raw) > 8_000_000:
            raise HTTPException(400, "Image too large")
        user_dir = os.path.join(IMAGES_DIR, uid)
        os.makedirs(user_dir, exist_ok=True)
        image_path = os.path.join(user_dir, f"{entry.entry_date}.jpg")
        with open(image_path, "wb") as f:
            f.write(raw)
        azure_url = upload_to_azure(f"{uid}/{entry.entry_date}.jpg", raw)

    with db() as conn:
        old = conn.execute(
            "SELECT id, image_path, video_path, media_type FROM entries WHERE user_id = ? AND entry_date = ?",
            (uid, entry.entry_date),
        ).fetchone()
        if old:
            entry_id = old["id"]
            image_path = image_path or old["image_path"]
            video_path = video_path or old["video_path"]
            if not entry.video_base64 and not entry.image_base64:
                media_type = old["media_type"] or "photo"
        conn.execute(
            """INSERT INTO entries
               (id, user_id, entry_date, created_at, weight_lbs, height_in, age, sex,
                bf_percent, bmi, waist_shoulder_ratio, image_path, video_path, media_type,
                azure_blob_url, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(user_id, entry_date) DO UPDATE SET
                 created_at=excluded.created_at, weight_lbs=excluded.weight_lbs,
                 height_in=excluded.height_in, age=excluded.age, sex=excluded.sex,
                 bf_percent=excluded.bf_percent, bmi=excluded.bmi,
                 waist_shoulder_ratio=excluded.waist_shoulder_ratio,
                 image_path=excluded.image_path, video_path=excluded.video_path,
                 media_type=excluded.media_type, azure_blob_url=excluded.azure_blob_url,
                 notes=excluded.notes""",
            (
                entry_id, uid, entry.entry_date, datetime.utcnow().isoformat(),
                entry.weight_lbs, entry.height_in, entry.age, entry.sex,
                entry.bf_percent, entry.bmi, entry.waist_shoulder_ratio,
                image_path, video_path, media_type, azure_url, entry.notes,
            ),
        )
    return {"id": entry_id, "entry_date": entry.entry_date, "replaced": bool(old)}


@app.get("/api/entries")
def list_entries(user=Depends(current_user), limit: int = 730):
    with db() as conn:
        rows = conn.execute(
            """SELECT id, entry_date, weight_lbs, height_in, age, sex, bf_percent,
                      bmi, waist_shoulder_ratio, notes, media_type,
                      CASE WHEN image_path IS NOT NULL THEN 1 ELSE 0 END AS has_image,
                      CASE WHEN video_path IS NOT NULL THEN 1 ELSE 0 END AS has_video
               FROM entries WHERE user_id = ? ORDER BY entry_date ASC LIMIT ?""",
            (user["id"], limit),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/entries/{entry_id}/image")
def entry_image(entry_id: str, user=Depends(current_user)):
    with db() as conn:
        row = conn.execute(
            "SELECT image_path FROM entries WHERE id = ? AND user_id = ?",
            (entry_id, user["id"]),
        ).fetchone()
    if not row or not row["image_path"] or not os.path.exists(row["image_path"]):
        raise HTTPException(404, "No photo for this day")
    return FileResponse(row["image_path"], media_type="image/jpeg")


@app.get("/api/entries/{entry_id}/video")
def entry_video(entry_id: str, user=Depends(current_user)):
    with db() as conn:
        row = conn.execute(
            "SELECT video_path FROM entries WHERE id = ? AND user_id = ?",
            (entry_id, user["id"]),
        ).fetchone()
    if not row or not row["video_path"] or not os.path.exists(row["video_path"]):
        raise HTTPException(404, "No video for this day")
    return FileResponse(row["video_path"], media_type="video/webm")


@app.delete("/api/entries/{entry_id}")
def delete_entry(entry_id: str, user=Depends(current_user)):
    with db() as conn:
        row = conn.execute(
            "SELECT image_path, video_path FROM entries WHERE id = ? AND user_id = ?",
            (entry_id, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Entry not found")
        conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    for p in (row["image_path"], row["video_path"]):
        if p and os.path.exists(p):
            os.remove(p)
    return {"deleted": entry_id}


# ---------------------------------------------------------------- misc
@app.get("/api/health")
def health():
    with db() as conn:
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        entries = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    return {"status": "ok", "app": APP_NAME, "users": users, "entries": entries,
            "email_configured": bool(SMTP_HOST and SMTP_USER and SMTP_PASS),
            "azure_enabled": bool(AZURE_CONN)}


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
