"""
BodyFat Scanner — AI body composition tracker.

FastAPI backend:
  - Serves the camera-based frontend (static/)
  - Stores daily entries (selfie + weight + body-fat estimate) in SQLite
  - Optionally mirrors selfies to Azure Blob Storage when
    AZURE_STORAGE_CONNECTION_STRING is set.

Every saved entry is a labeled data point (image + weight + estimate),
building a personal dataset that improves calibration over time.
"""

import base64
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

DATA_DIR = os.environ.get("DATA_DIR", "./data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "bodyfat.db")
IMAGES_DIR = os.path.join(DATA_DIR, "selfies")
os.makedirs(IMAGES_DIR, exist_ok=True)

AZURE_CONN = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
AZURE_CONTAINER = os.environ.get("AZURE_BLOB_CONTAINER", "selfies")

app = FastAPI(title="BodyFat Scanner", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------- storage
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS entries (
                id TEXT PRIMARY KEY,
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
                azure_blob_url TEXT,
                notes TEXT
            )
            """
        )


init_db()


def upload_to_azure(filename: str, raw: bytes) -> Optional[str]:
    """Mirror a selfie to Azure Blob Storage. Returns blob URL or None."""
    if not AZURE_CONN:
        return None
    try:
        from azure.storage.blob import BlobServiceClient

        service = BlobServiceClient.from_connection_string(AZURE_CONN)
        try:
            service.create_container(AZURE_CONTAINER)
        except Exception:
            pass  # container already exists
        blob = service.get_blob_client(container=AZURE_CONTAINER, blob=filename)
        blob.upload_blob(raw, overwrite=True)
        return blob.url
    except Exception:
        return None  # Azure mirroring is best-effort; local copy is source of truth


# ---------------------------------------------------------------- models
class EntryIn(BaseModel):
    entry_date: str = Field(default_factory=lambda: date.today().isoformat())
    weight_lbs: Optional[float] = None
    height_in: Optional[float] = None
    age: Optional[int] = None
    sex: Optional[str] = None
    bf_percent: Optional[float] = None
    bmi: Optional[float] = None
    waist_shoulder_ratio: Optional[float] = None
    image_base64: Optional[str] = None  # data-URL or raw base64 JPEG
    notes: Optional[str] = None


# ---------------------------------------------------------------- routes
@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "azure_enabled": bool(AZURE_CONN),
        "entries": count_entries(),
    }


def count_entries() -> int:
    with db() as conn:
        return conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]


@app.post("/api/entries")
def create_entry(entry: EntryIn):
    entry_id = str(uuid.uuid4())
    image_path = None
    azure_url = None

    if entry.image_base64:
        b64 = entry.image_base64.split(",")[-1]
        try:
            raw = base64.b64decode(b64)
        except Exception:
            raise HTTPException(400, "Invalid image data")
        filename = f"{entry.entry_date}_{entry_id[:8]}.jpg"
        image_path = os.path.join(IMAGES_DIR, filename)
        with open(image_path, "wb") as f:
            f.write(raw)
        azure_url = upload_to_azure(filename, raw)

    with db() as conn:
        conn.execute(
            """INSERT INTO entries
               (id, entry_date, created_at, weight_lbs, height_in, age, sex,
                bf_percent, bmi, waist_shoulder_ratio, image_path,
                azure_blob_url, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                entry_id,
                entry.entry_date,
                datetime.utcnow().isoformat(),
                entry.weight_lbs,
                entry.height_in,
                entry.age,
                entry.sex,
                entry.bf_percent,
                entry.bmi,
                entry.waist_shoulder_ratio,
                image_path,
                azure_url,
                entry.notes,
            ),
        )
    return {"id": entry_id, "azure_blob_url": azure_url}


@app.get("/api/entries")
def list_entries(limit: int = 365):
    with db() as conn:
        rows = conn.execute(
            """SELECT id, entry_date, created_at, weight_lbs, height_in, age,
                      sex, bf_percent, bmi, waist_shoulder_ratio,
                      azure_blob_url, notes
               FROM entries ORDER BY entry_date ASC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.delete("/api/entries/{entry_id}")
def delete_entry(entry_id: str):
    with db() as conn:
        row = conn.execute(
            "SELECT image_path FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Entry not found")
        conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    if row["image_path"] and os.path.exists(row["image_path"]):
        os.remove(row["image_path"])
    return {"deleted": entry_id}


# ---------------------------------------------------------------- static
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
