"""Integration tests for Reddy-Fit: auth, photo/video entries, media auth, admin."""

from __future__ import annotations

import base64

import main


# ---------------------------------------------------------------- auth
def test_otp_flow(client):
    r = client.post("/api/auth/request-otp", json={"email": "a@example.org"})
    code = r.json()["dev_otp"]
    r = client.post("/api/auth/verify-otp", json={"email": "a@example.org", "code": code})
    assert r.status_code == 200 and r.json()["token"]


def test_brute_force_lockout_persists(client):
    email = "brute@example.org"
    code = client.post("/api/auth/request-otp", json={"email": email}).json()["dev_otp"]
    for _ in range(5):
        assert client.post("/api/auth/verify-otp",
                           json={"email": email, "code": "999999"}).status_code == 400
    assert client.post("/api/auth/verify-otp",
                       json={"email": email, "code": code}).status_code == 429


def test_rate_limit_and_bad_email(client):
    assert client.post("/api/auth/request-otp", json={"email": "bad"}).status_code == 400
    email = "rl@example.org"
    client.post("/api/auth/request-otp", json={"email": email})
    assert client.post("/api/auth/request-otp", json={"email": email}).status_code == 429


def test_auth_required(client):
    assert client.get("/api/entries").status_code == 401
    assert client.post("/api/entries", json={"weight_lbs": 170}).status_code == 401


def test_logout_kills_session(client, login):
    h, _ = login()
    assert client.get("/api/me", headers=h).status_code == 200
    client.post("/api/auth/logout", headers=h)
    assert client.get("/api/me", headers=h).status_code == 401


# ---------------------------------------------------------------- entries: photos + fake videos
def test_photo_entry_roundtrip(client, login, fake_jpeg):
    h, _ = login()
    r = client.post("/api/entries", json={
        "entry_date": "2026-07-10", "weight_lbs": 180.0, "bf_percent": 21.0,
        "image_base64": fake_jpeg}, headers=h)
    assert r.status_code == 200 and r.json()["replaced"] is False
    entries = client.get("/api/entries", headers=h).json()
    assert len(entries) == 1
    assert entries[0]["has_image"] == 1 and entries[0]["media_type"] == "photo"


def test_multiple_video_days_and_upsert(client, login, fake_jpeg, fake_webm):
    """Simulates the daily video-scan habit: distinct fake videos over several days."""
    h, _ = login()
    for i, day in enumerate(["2026-07-11", "2026-07-12", "2026-07-13"]):
        r = client.post("/api/entries", json={
            "entry_date": day, "weight_lbs": 180 - i, "bf_percent": 21 - i * 0.3,
            "image_base64": fake_jpeg, "video_base64": fake_webm(bytes([97 + i]))},
            headers=h)
        assert r.status_code == 200
    entries = client.get("/api/entries", headers=h).json()
    assert [e["has_video"] for e in entries] == [1, 1, 1]
    assert [e["media_type"] for e in entries] == ["video", "video", "video"]
    # same-day rescan replaces, count stays 3
    r = client.post("/api/entries", json={
        "entry_date": "2026-07-13", "weight_lbs": 177.5,
        "image_base64": fake_jpeg, "video_base64": fake_webm(b"z")}, headers=h)
    assert r.json()["replaced"] is True
    entries = client.get("/api/entries", headers=h).json()
    assert len(entries) == 3 and entries[-1]["weight_lbs"] == 177.5


def test_video_and_image_media_endpoints(client, login, fake_jpeg, fake_webm):
    h, _ = login()
    client.post("/api/entries", json={
        "entry_date": "2026-07-14", "weight_lbs": 176,
        "image_base64": fake_jpeg, "video_base64": fake_webm()}, headers=h)
    eid = client.get("/api/entries", headers=h).json()[0]["id"]
    img = client.get(f"/api/entries/{eid}/image", headers=h)
    vid = client.get(f"/api/entries/{eid}/video", headers=h)
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    assert vid.status_code == 200 and vid.headers["content-type"] == "video/webm"
    # no auth -> blocked
    assert client.get(f"/api/entries/{eid}/image").status_code == 401
    assert client.get(f"/api/entries/{eid}/video").status_code == 401


def test_media_isolated_between_users(client, login, fake_jpeg):
    h1, _ = login()
    h2, _ = login()
    client.post("/api/entries", json={
        "entry_date": "2026-07-15", "image_base64": fake_jpeg}, headers=h1)
    eid = client.get("/api/entries", headers=h1).json()[0]["id"]
    assert client.get(f"/api/entries/{eid}/image", headers=h2).status_code == 404
    assert client.get("/api/entries", headers=h2).json() == []


def test_weight_only_manual_entry(client, login):
    h, _ = login()
    r = client.post("/api/entries", json={"entry_date": "2026-07-16", "weight_lbs": 175,
                                          "notes": "fasted"}, headers=h)
    assert r.status_code == 200
    e = client.get("/api/entries", headers=h).json()[0]
    assert e["has_image"] == 0 and e["notes"] == "fasted"


def test_oversized_and_malformed_media_rejected(client, login):
    h, _ = login()
    big_img = "data:image/jpeg;base64," + base64.b64encode(b"x" * 8_100_000).decode()
    assert client.post("/api/entries", json={"image_base64": big_img},
                       headers=h).status_code == 400
    big_vid = "data:video/webm;base64," + base64.b64encode(b"x" * 25_100_000).decode()
    assert client.post("/api/entries", json={"video_base64": big_vid},
                       headers=h).status_code == 400
    assert client.post("/api/entries", json={"image_base64": "data:;base64,!!!"},
                       headers=h).status_code == 400


def test_delete_entry_removes_media(client, login, fake_jpeg, fake_webm):
    h, _ = login()
    client.post("/api/entries", json={"entry_date": "2026-07-17",
                "image_base64": fake_jpeg, "video_base64": fake_webm()}, headers=h)
    eid = client.get("/api/entries", headers=h).json()[0]["id"]
    assert client.delete(f"/api/entries/{eid}", headers=h).status_code == 200
    assert client.get("/api/entries", headers=h).json() == []
    assert client.get(f"/api/entries/{eid}/image", headers=h).status_code == 404


# ---------------------------------------------------------------- admin
def test_admin_stats_and_delete(client, login, monkeypatch):
    monkeypatch.setattr(main, "ADMIN_EMAIL", "akhilreddydanda3@gmail.com")
    h_user, user_email = login()
    assert client.get("/api/admin/stats", headers=h_user).status_code == 403
    h_admin, _ = login("akhilreddydanda3@gmail.com")
    stats = client.get("/api/admin/stats", headers=h_admin)
    assert stats.status_code == 200 and stats.json()["total_users"] >= 2
    r = client.request("DELETE", f"/api/admin/users/{user_email}", headers=h_admin)
    assert r.status_code == 200
    assert client.get("/api/me", headers=h_user).status_code == 401


def test_health(client):
    assert client.get("/api/health").json()["app"] == "Reddy-Fit Body Scanner"
