import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from packserve.common.schemas import Candidates, CompletionRecord, RouteDecision
from packserve.telemetry import analyze
from packserve.telemetry.app import create_app
from packserve.telemetry.store import TraceStore, mean_utilization, percentile, pressure_rate


def dec(i, route="replica", reason="packed", model="qwen3-8-27b", ts=1000.0, tenant="t-interactive"):
    return RouteDecision(request_id=f"r{i}", ts=ts, gateway_id="gw1", tenant_id=tenant, model=model,
                         route=route, reason=reason, candidates=Candidates(capacity=1))


def comp(i, tpot, ttft=100.0, ts=1000.0, tenant="t-interactive", model="qwen3-8-27b", status=200):
    return CompletionRecord(request_id=f"r{i}", ts=ts, tenant_id=tenant, model=model, route="replica",
                            ttft_ms=ttft, tpot_ms=tpot, output_tokens=10, status=status)


def test_percentile():
    assert percentile([], 50) is None
    assert percentile([5], 90) == 5
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([0, 10], 90) == 9


def test_pressure_rate_ignores_client_errors():
    rate, n = pressure_rate({"packed": 8, "overflow": 1, "no_capacity": 1, "unauthorized": 50})
    assert (rate, n) == (0.2, 10)
    assert pressure_rate({}) == (0.0, 0)


def test_mean_utilization_filters_model_and_health():
    reps = [{"model": "a", "running": 32, "max_running": 64, "healthy": True},
            {"model": "a", "running": 64, "max_running": 64, "healthy": True},
            {"model": "a", "running": 64, "max_running": 64, "healthy": False},
            {"model": "b", "running": 0, "max_running": 64, "healthy": True}]
    assert mean_utilization(reps, "a") == 0.75
    assert mean_utilization(reps, "zzz") is None


def test_store_rollup_math_and_window():
    s = TraceStore(":memory:")
    for i, (route, reason) in enumerate([("replica", "packed")] * 6 + [("overflow", "overflow")] * 3 + [("rejected", "no_capacity")]):
        s.add_decision(dec(i, route, reason, ts=1000))
    s.add_decision(dec(99, ts=100))                     # outside window
    for i, t in enumerate([10, 20, 30, 40]):
        s.add_completion(comp(i, tpot=t, ttft=t * 10, ts=1000))
    r = s.rollup(60, pool_utilization=0.5, now=1010)
    assert r.requests == 10 and r.by_reason == {"packed": 6, "overflow": 3, "no_capacity": 1}
    assert r.overflow_share == 0.3 and r.reject_share == 0.1
    assert r.tpot_p50_ms == 25 and r.ttft_p50_ms == 250 and r.pool_utilization == 0.5
    assert s.rollup(60, model="gemma-4-26b-a4b-it", now=1010).requests == 0


def test_http_endpoints_and_utilization_from_metadata(tmp_path):
    with respx.mock:
        respx.get(url__regex=r".*/v1/replicas").mock(return_value=httpx.Response(200, json=[
            {"model": "qwen3-8-27b", "running": 16, "max_running": 64, "healthy": True}]))
        app = create_app(str(tmp_path / "t.db"), metadata_url="http://meta.test")
        with TestClient(app) as c:
            import time
            assert c.post("/v1/decisions", json=dec(1, ts=time.time()).model_dump()).status_code == 202
            assert c.post("/v1/completions", json=comp(1, 33.0, ts=time.time()).model_dump()).status_code == 202
            r = c.get("/v1/rollup", params={"window_s": 30}).json()
    assert r["requests"] == 1 and r["tpot_p50_ms"] == 33.0 and r["pool_utilization"] == 0.25


def test_rollup_without_metadata_has_null_utilization(tmp_path):
    with respx.mock:
        respx.get(url__regex=r".*/v1/replicas").mock(side_effect=httpx.ConnectError("down"))
        with TestClient(create_app(str(tmp_path / "t.db"), metadata_url="http://meta.test")) as c:
            assert c.get("/v1/rollup").json()["pool_utilization"] is None


# ---- analysis
def test_stats_tiers_and_slo_miss_minutes():
    s = TraceStore(":memory:")
    targets = {"t-interactive": 30.0, "t-batch": 50.0}
    # minute 0: interactive median 40 > 30 (miss); minute 1: median 20 (ok)
    for i, t in enumerate([40, 40, 40]):
        s.add_completion(comp(i, t, ts=1000 + i, tenant="t-interactive"))
    for i, t in enumerate([20, 20, 20]):
        s.add_completion(comp(10 + i, t, ts=1070 + i, tenant="t-interactive"))
    s.add_completion(comp(20, 45, ts=1000, tenant="t-batch"))
    s.add_decision(dec(1, "overflow", "overflow", ts=1000))
    s.add_decision(dec(2, ts=1000))
    st = analyze.compute_stats(s, 3600, targets, now=1100)
    t30 = next(t for t in st["tiers"] if t["tpot_target_ms"] == 30)
    assert t30["requests"] == 6 and t30["slo_miss_minutes"] == 1 and t30["minutes_observed"] == 2
    assert t30["within_target_share"] == 0.5
    assert next(t for t in st["tiers"] if t["tpot_target_ms"] == 50)["slo_miss_minutes"] == 0
    assert st["overflow_share"] == 0.5


def test_report_survives_llm_failure(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    s = TraceStore(db)
    s.add_decision(dec(1, ts=__import__("time").time()))
    s.close()
    monkeypatch.setattr(analyze, "call_llm", lambda stats: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(analyze, "tenant_targets", lambda: {})
    path = analyze.run(3600, db, str(tmp_path / "reports"), dry_run=False)
    text = path.read_text()
    assert "Statistics (authoritative" in text and "LLM call failed" in text and "NOT authoritative" in text


def test_narrative_is_labeled_and_checked(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    TraceStore(db).add_decision(dec(1, ts=__import__("time").time()))
    monkeypatch.setattr(analyze, "tenant_targets", lambda: {})
    monkeypatch.setattr(analyze, "call_llm", lambda stats: "- Overflow share was 77777 which is odd.")
    text = analyze.run(3600, db, str(tmp_path / "r"), dry_run=False).read_text()
    assert "model-generated" in text and "77777" in text.split("Check:")[1]


def test_dry_run_skips_llm(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    TraceStore(db)
    monkeypatch.setattr(analyze, "tenant_targets", lambda: {})
    monkeypatch.setattr(analyze, "call_llm", lambda s: pytest.fail("LLM must not be called in dry-run"))
    text = analyze.run(60, db, str(tmp_path / "r"), dry_run=True).read_text()
    assert "dry-run" in text and json.loads(text.split("```json\n")[1].split("\n```")[0])["model"] == "qwen3-8-27b"
