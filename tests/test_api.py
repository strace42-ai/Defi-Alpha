"""Тесты локального API (без сети). Токен создаётся искусственно внутри теста."""
from fastapi.testclient import TestClient

from app import db, discovery
from app.api import app
from app.chains import CHAINS


def _client():
    return TestClient(app)


def _token():
    uid, _ = discovery.upsert_token(CHAINS["robinhood"], {"address": "0x451b42a15100c340ca12f7c66de06fac5ea2d751", "symbol": "BOW",
                                                          "name": "Longbow"}, source="test", reason="synthetic")
    return uid


def test_pages_respond():
    uid = _token()
    c = _client()
    for path in ("/", "/api/meta", "/api/dashboard", "/api/tokens", "/api/review", "/api/watchlist", "/api/alerts",
                 "/api/tokenomics", "/api/sources", "/api/settings", "/api/diagnostics", f"/api/token/{uid}"):
        r = c.get(path)
        assert r.status_code == 200, path
    assert c.get("/api/tokens").json()["total"] == 1


def test_export_csv_xlsx():
    _token()
    c = _client()
    r = c.get("/api/export.csv")
    assert r.status_code == 200 and "Тикер" in r.content.decode("utf-8-sig")
    r = c.get("/api/export.xlsx")
    assert r.status_code == 200 and r.content[:2] == b"PK"


def test_cross_origin_write_rejected():
    uid = _token()
    c = _client()
    r = c.post(f"/api/watchlist/{uid}", json={}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = c.post(f"/api/watchlist/{uid}", json={}, headers={"Origin": "http://127.0.0.1:8765"})
    assert r.status_code == 200
    r = c.get("/api/meta", headers={"Host": "attacker.example"})
    assert r.status_code == 403


def test_funding_fact_is_analytics_not_filter():
    uid = _token()
    c = _client()
    r = c.post(f"/api/token/{uid}/facts", json={"fact_key": "funding", "category": "vc_fund", "value": "Fund X",
                                               "amount_usd": 10000000, "evidence_url": "https://example.org/round"})
    assert r.status_code == 200
    assert db.q1("SELECT 1 FROM filter_current WHERE token_uid=? AND filter_key='vc'", (uid,)) is None
    d = c.get(f"/api/token/{uid}").json()
    assert d["facts"]["funding"]["status"] == "vc_present"
    assert d["token"]["status"] != "FAIL" or "VC" not in (d["token"]["status_reason"] or "")


def test_secrets_never_returned():
    c = _client()
    c.post("/api/secrets", json={"name": "etherscan_key", "value": "SECRET123"})
    s = c.get("/api/settings").text
    assert "SECRET123" not in s and '"etherscan_key":true' in s.replace(" ", "")
