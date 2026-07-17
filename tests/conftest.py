"""Test fixtures for Reddy-Fit Body Scanner."""

from __future__ import annotations

import base64
import os
import sys
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="reddyfit-test-")
os.environ["DATA_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402


@pytest.fixture()
def client() -> TestClient:
    return TestClient(main.app)


_counter = {"n": 0}


@pytest.fixture()
def login(client):
    """Factory: returns (headers, email) for a fresh logged-in user."""

    def _login(email: str | None = None):
        _counter["n"] += 1
        email = email or f"user{_counter['n']}@example.org"
        r = client.post("/api/auth/request-otp", json={"email": email})
        assert r.status_code == 200, r.text
        code = r.json()["dev_otp"]
        r = client.post("/api/auth/verify-otp", json={"email": email, "code": code})
        assert r.status_code == 200, r.text
        return {"Authorization": "Bearer " + r.json()["token"]}, email

    return _login


@pytest.fixture()
def fake_jpeg() -> str:
    return "data:image/jpeg;base64," + base64.b64encode(
        b"\xff\xd8\xff\xe0" + b"selfie" * 40).decode()


@pytest.fixture()
def fake_webm():
    """Factory for distinct fake videos."""

    def _make(tag: bytes = b"a") -> str:
        return "data:video/webm;base64," + base64.b64encode(
            b"\x1aE\xdf\xa3" + tag * 500).decode()

    return _make
