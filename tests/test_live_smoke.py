"""Live production smoke tests. Skipped locally; run in the scheduled CI against the deployed site."""

from __future__ import annotations

import json
import os
import urllib.request

import pytest

LIVE = os.environ.get("SMOKE_LIVE") == "1"
BASE = "https://bodyfatscanner-production.up.railway.app"
UA = {"User-Agent": "Mozilla/5.0 (ReddySmoke/1.0)"}

pytestmark = pytest.mark.skipif(not LIVE, reason="set SMOKE_LIVE=1 to run against production")


def _get(path, timeout=25):
    req = urllib.request.Request(BASE + path, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def test_live_health():
    status, body = _get("/api/health")
    assert status == 200
    j = json.loads(body)
    assert j["app"] == "Reddy-Fit Body Scanner" and j["status"] == "ok"


def test_live_homepage_serves():
    status, body = _get("/")
    assert status == 200 and b"<html" in body.lower()


def test_live_otp_endpoint_reachable():
    """Liveness check with NO side effects: an invalid email must be rejected 400.
    This exercises the auth route without sending a real email or tripping rate limits."""
    import urllib.error
    data = json.dumps({"email": "not-a-valid-email"}).encode()
    req = urllib.request.Request(BASE + "/api/auth/request-otp", data=data,
                                 headers={**UA, "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=25)
        assert False, "invalid email should have been rejected"
    except urllib.error.HTTPError as e:
        assert e.code == 400, f"expected 400, got {e.code}"
