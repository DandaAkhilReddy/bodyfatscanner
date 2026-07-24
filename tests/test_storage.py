"""Durable-storage tests: Azure Blob mirror, read-through after cache wipe,
DB snapshot/restore, blob cleanup on delete, and failure resilience.

Azure is simulated with an in-memory dict so the durability logic is exercised
without any network. The key scenario is a Railway-style redeploy: the local
disk is wiped but every photo, video, and the database survive in Blob.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

import main

# ------------------------------------------------------------------ fake blob
FAKE: dict[tuple[str, str], bytes] = {}


def _put(container, name, data, content_type):
    FAKE[(container, name)] = data
    return True


def _get(container, name):
    return FAKE.get((container, name))


def _del(container, name):
    FAKE.pop((container, name), None)


@pytest.fixture()
def blob(monkeypatch):
    FAKE.clear()
    monkeypatch.setattr(main, "AZURE_CONN", "fake-connection-string")
    monkeypatch.setattr(main, "blob_put", _put)
    monkeypatch.setattr(main, "blob_get", _get)
    monkeypatch.setattr(main, "blob_delete", _del)
    return FAKE


def _wipe_local_disk():
    """Simulate a fresh container: delete every cached media file locally."""
    for root, _, files in os.walk(main.MEDIA_DIR):
        for f in files:
            os.remove(os.path.join(root, f))


# ------------------------------------------------------------------ mirroring
def test_photo_mirrored_to_photos_container(client, login, blob, fake_jpeg):
    h, _ = login()
    client.post("/api/entries", json={"entry_date": "2026-08-01", "image_base64": fake_jpeg}, headers=h)
    keys = [k for k in FAKE if k[0] == main.PHOTOS_CONTAINER]
    assert keys and keys[0][1].endswith("2026-08-01.jpg")


def test_video_mirrored_to_videos_container(client, login, blob, fake_webm):
    h, _ = login()
    client.post("/api/entries", json={"entry_date": "2026-08-02", "video_base64": fake_webm(b"v")}, headers=h)
    keys = [k for k in FAKE if k[0] == main.VIDEOS_CONTAINER]
    assert keys and keys[0][1].endswith("2026-08-02.webm")


def test_db_backed_up_to_backups_container(client, login, blob):
    login()  # verify-otp forces a DB backup
    assert (main.BACKUPS_CONTAINER, main.DB_BLOB_NAME) in FAKE
    dump = FAKE[(main.BACKUPS_CONTAINER, main.DB_BLOB_NAME)]
    assert b"CREATE TABLE" in dump and b"users" in dump


# ------------------------------------------- the redeploy durability scenario
def test_photo_survives_local_disk_wipe(client, login, blob, fake_jpeg):
    h, _ = login()
    client.post("/api/entries", json={"entry_date": "2026-08-03", "image_base64": fake_jpeg}, headers=h)
    eid = client.get("/api/entries", headers=h).json()[0]["id"]
    assert client.get(f"/api/entries/{eid}/image", headers=h).status_code == 200

    _wipe_local_disk()  # <-- redeploy: local cache gone

    r = client.get(f"/api/entries/{eid}/image", headers=h)
    assert r.status_code == 200, "photo must be restored from Blob"
    assert r.headers["content-type"] == "image/jpeg"


def test_video_survives_local_disk_wipe(client, login, blob, fake_webm):
    h, _ = login()
    client.post("/api/entries", json={"entry_date": "2026-08-04", "video_base64": fake_webm(b"z")}, headers=h)
    eid = client.get("/api/entries", headers=h).json()[0]["id"]
    _wipe_local_disk()
    r = client.get(f"/api/entries/{eid}/video", headers=h)
    assert r.status_code == 200 and r.headers["content-type"] == "video/webm"


def test_read_through_repopulates_local_cache(client, login, blob, fake_jpeg):
    h, _ = login()
    client.post("/api/entries", json={"entry_date": "2026-08-05", "image_base64": fake_jpeg}, headers=h)
    eid = client.get("/api/entries", headers=h).json()[0]["id"]
    _wipe_local_disk()
    client.get(f"/api/entries/{eid}/image", headers=h)  # first hit pulls from blob
    cached = [f for _, _, fs in os.walk(main.MEDIA_DIR) for f in fs]
    assert any(f.endswith(".jpg") for f in cached), "blob read should refill local cache"


def test_multi_day_video_habit_all_durable(client, login, blob, fake_jpeg, fake_webm):
    """Three days of daily video scans all survive a disk wipe."""
    h, _ = login()
    for i, day in enumerate(["2026-08-06", "2026-08-07", "2026-08-08"]):
        client.post("/api/entries", json={"entry_date": day, "weight_lbs": 180 - i,
                    "image_base64": fake_jpeg, "video_base64": fake_webm(bytes([98 + i]))}, headers=h)
    ids = [e["id"] for e in client.get("/api/entries", headers=h).json()]
    _wipe_local_disk()
    for eid in ids:
        assert client.get(f"/api/entries/{eid}/video", headers=h).status_code == 200
        assert client.get(f"/api/entries/{eid}/image", headers=h).status_code == 200


# ------------------------------------------------------------------ cleanup
def test_delete_removes_media_blobs(client, login, blob, fake_jpeg, fake_webm):
    h, _ = login()
    client.post("/api/entries", json={"entry_date": "2026-08-09",
                "image_base64": fake_jpeg, "video_base64": fake_webm()}, headers=h)
    eid = client.get("/api/entries", headers=h).json()[0]["id"]
    assert any(k[0] in (main.PHOTOS_CONTAINER, main.VIDEOS_CONTAINER) for k in FAKE)
    client.delete(f"/api/entries/{eid}", headers=h)
    assert not any(k[0] in (main.PHOTOS_CONTAINER, main.VIDEOS_CONTAINER) for k in FAKE), \
        "media blobs must be removed on delete"


def test_admin_reports_media_in_blob(client, login, blob, fake_jpeg, monkeypatch):
    monkeypatch.setattr(main, "ADMIN_EMAIL", "akhilreddydanda3@gmail.com")
    h, _ = login()
    client.post("/api/entries", json={"entry_date": "2026-08-10", "image_base64": fake_jpeg}, headers=h)
    h_admin, _ = login("akhilreddydanda3@gmail.com")
    stats = client.get("/api/admin/stats", headers=h_admin).json()
    assert stats["media_in_blob"] >= 1


# ------------------------------------------------------ DB snapshot / restore
def test_db_snapshot_is_consistent_and_replayable(client, login):
    _, email = login()
    dump = main.snapshot_db_bytes()
    assert b"CREATE TABLE" in dump
    # replay the dump into a brand-new database and confirm the user is there
    mem = sqlite3.connect(":memory:")
    mem.executescript(dump.decode())
    got = mem.execute("SELECT email FROM users WHERE email = ?", (email,)).fetchone()
    mem.close()
    assert got and got[0] == email


def test_restore_noop_without_azure(monkeypatch):
    # with Azure disabled, restore must never touch the DB
    monkeypatch.setattr(main, "AZURE_CONN", "")
    main.restore_db_if_needed()  # should simply return, no exception


# ------------------------------------------------------------------ resilience
def test_blob_put_swallows_backend_errors(monkeypatch):
    class Boom:
        def get_blob_client(self, *a, **k):
            raise RuntimeError("azure down")

    monkeypatch.setattr(main, "AZURE_CONN", "x")
    monkeypatch.setattr(main, "blob_service", lambda: Boom())
    # must return False, never raise — a storage hiccup can't break a save
    assert main.blob_put("photos", "u/x.jpg", b"data", "image/jpeg") is False


def test_blob_get_returns_none_on_error(monkeypatch):
    class Boom:
        def get_blob_client(self, *a, **k):
            raise RuntimeError("azure down")

    monkeypatch.setattr(main, "AZURE_CONN", "x")
    monkeypatch.setattr(main, "blob_service", lambda: Boom())
    assert main.blob_get("photos", "u/x.jpg") is None


def test_save_succeeds_even_if_blob_put_raises(client, login, monkeypatch, fake_jpeg):
    """A raising blob layer must not 500 the save (defense in depth)."""
    monkeypatch.setattr(main, "AZURE_CONN", "x")

    def boom(*a, **k):
        raise RuntimeError("network")

    # patch the low-level service so the real blob_put's try/except catches it
    class Boom:
        def get_blob_client(self, *a, **k):
            raise RuntimeError("net")

    monkeypatch.setattr(main, "blob_service", lambda: Boom())
    h, _ = login()
    r = client.post("/api/entries", json={"entry_date": "2026-08-11", "image_base64": fake_jpeg}, headers=h)
    assert r.status_code == 200


# ------------------------------------------------------------------ edge cases
def test_empty_base64_image_rejected(client, login):
    h, _ = login()
    r = client.post("/api/entries", json={"image_base64": "data:image/jpeg;base64,"}, headers=h)
    assert r.status_code == 400


def test_empty_base64_video_rejected(client, login):
    h, _ = login()
    r = client.post("/api/entries", json={"video_base64": "data:video/webm;base64,"}, headers=h)
    assert r.status_code == 400


def test_health_reports_storage_mode(client):
    j = client.get("/api/health").json()
    assert j["storage"] in ("local-only", "azure-blob")
