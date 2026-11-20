import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

WEB = Path(__file__).resolve().parents[3] / "onboarding-web"
sys.path.insert(0, str(WEB))
import estimator  # noqa: E402


@pytest.fixture()
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("ONBOARDING_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ONBOARDING_PERSONAS", str(tmp_path / "missing.json"))
    spec = importlib.util.spec_from_file_location("onboarding_app", WEB / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Point at ports nothing listens on so demo mode is deterministic.
    mod.GATEWAY_URL, mod.METADATA_URL, mod.TELEMETRY_URL = ("http://127.0.0.1:1",) * 3
    return mod, TestClient(mod.app)


@pytest.mark.parametrize("path", ["/", "/use-cases", "/plan", "/signup", "/playground"])
def test_pages(web, path):
    r = web[1].get(path)
    assert r.status_code == 200 and "PackServe" in r.text


def test_status_demo_mode(web):
    s = web[1].get("/api/status").json()
    assert s["demo_mode"] is True and not s["metadata"] and not s["gateway"]


def test_models_burst_flags(web):
    m = {x["id"]: x["burst_eligible"] for x in web[1].get("/api/models").json()}
    assert m == {"qwen3-8-27b": True, "gemma-4-26b-a4b-it": True, "packserve-large-230b": False}


def test_personas_fallback_then_file(web, tmp_path):
    mod, c = web
    assert c.get("/api/personas").json()["source"] == "fallback"
    p = tmp_path / "p.json"
    p.write_text(json.dumps([{"id": "x", "name": "X", "recommended": {}, "traffic_shape": {}}]))
    mod.PERSONAS_PATH = p
    d = c.get("/api/personas").json()
    assert d["source"] == "personas.json" and d["personas"][0]["id"] == "x"


def test_signup_validation(web):
    r = web[1].post("/api/signup", json={"name": "A", "email": "bad", "company": "C"})
    assert r.status_code == 422


def test_signup_demo_fallback_and_simulated_chat(web):
    c = web[1]
    r = c.post("/api/signup", json={"name": "A", "email": "a@b.co", "company": "Co", "model": "qwen3-8-27b"})
    j = r.json()
    assert r.status_code == 200 and j["mode"] == "demo"
    key = j["tenant"]["api_key"]
    assert key.startswith("ps_local_")
    stored = (Path(web[0].DATA_DIR) / "tenants.json").read_text()
    assert key not in stored  # only a hash is persisted
    resp = c.post("/api/chat", json={"api_key": key, "model": "qwen3-8-27b", "prompt": "hi"})
    assert resp.status_code == 200
    assert resp.headers["x-packserve-simulated"] == "true" and resp.headers["x-packserve-route"] == "replica"
    assert "SIMULATED" in resp.text


def test_signup_live_posts_to_metadata(web, monkeypatch):
    mod, c = web
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "t1", "api_key": "ps_live_abc", **seen["body"]})
    real = httpx.AsyncClient
    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    j = c.post("/api/signup", json={"name": "A", "email": "a@b.co", "company": "Co", "model": "packserve-large-230b",
                                    "burst_enabled": True, "tpot_target_ms": 30}).json()
    assert j["mode"] == "live" and j["tenant"]["api_key"] == "ps_live_abc"
    assert seen["body"]["burst_enabled"] is False  # pool-only model can't burst
    assert seen["body"]["models"] == ["packserve-large-230b"] and seen["body"]["tpot_target_ms"] == 30


def test_chat_requires_key_and_unknown_demo_key(web):
    c = web[1]
    assert c.post("/api/chat", json={"prompt": "x"}).status_code == 401
    assert c.post("/api/chat", json={"api_key": "ps_local_nope"}).status_code == 401


def test_burst_demo_tally_burst_enabled_and_pool_only(web):
    c = web[1]

    def run(model, burst):
        r = c.post("/api/burst", json={"api_key": "ps_demo_x", "model": model, "n": 10, "burst_enabled": burst})
        lines = [json.loads(x) for x in r.text.strip().splitlines()]
        return lines[-1]["tally"]
    t = run("qwen3-8-27b", True)
    assert t["replica"] == 4 and t["overflow"] == 6 and t["rejected"] == 0
    t = run("qwen3-8-27b", False)
    assert t["overflow"] == 0 and t["rejected"] == 6
    t = run("packserve-large-230b", True)
    assert t["overflow"] == 0 and t["rejected"] == 6


def test_rollup_unavailable(web):
    assert web[1].get("/api/rollup").json() == {"available": False, "rollup": None}


# ---- estimator math ----
def test_estimator_peak_vs_burst():
    r = estimator.estimate({"tpot_target_ms": 50, "baseline_rps": 7, "peak_rps": 28, "peak_hours_per_day": 2.4,
                            "tokens_per_request": 1000, "serverless_usd_per_m_tokens": 1.0})
    assert r["rps_per_replica"] == 7.0
    assert r["peak"]["replicas"] == 4 and r["peak"]["usd_month"] == 4 * 12 * 730
    assert r["burst"]["reserved_replicas"] == 1 and r["burst"]["reserved_usd_month"] == 12 * 730
    # excess 21 rps for 10% of 730h = 21*2,628,000*0.1 requests * 1000 tokens / 1e6 = 5518.8 M tokens
    assert r["burst"]["serverless_m_tokens_month"] == pytest.approx(5518.8, rel=1e-3)
    assert r["burst"]["serverless_usd_month"] == pytest.approx(5518.8, rel=1e-3)
    assert r["burst"]["usd_month"] == pytest.approx(8760 + 5518.8, abs=1)


def test_estimator_no_burst_for_pool_only():
    r = estimator.estimate({"baseline_rps": 1, "peak_rps": 20, "burst_enabled": True, "burst_eligible": False})
    assert r["burst_available"] is False
    assert r["burst"]["serverless_usd_month"] == 0 and r["burst"]["unserved_rps_at_peak"] > 0


def test_estimator_tier_30_is_pricier():
    a = estimator.estimate({"tpot_target_ms": 30, "baseline_rps": 14, "peak_rps": 14})
    b = estimator.estimate({"tpot_target_ms": 50, "baseline_rps": 14, "peak_rps": 14})
    assert a["peak"]["replicas"] > b["peak"]["replicas"]
