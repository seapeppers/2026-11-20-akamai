import time

import httpx
import pytest
from httpx import ASGITransport

from packserve.serverless_mock.app import create_app


@pytest.fixture
def client():
    app = create_app(seed=1)
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://m")


def body(max_tokens=5, model="qwen3-8-27b", content="hello", **kw):
    return {"model": model, "messages": [{"role": "user", "content": content}], "max_tokens": max_tokens, **kw}


async def test_catalog(client):
    ids = [m["id"] for m in (await client.get("/v1/models")).json()["data"]]
    assert ids == ["qwen3-8-27b", "gemma-4-26b-a4b-it"]
    r = await client.post("/v1/chat/completions", json=body(model="packserve-large-230b"))
    assert r.status_code == 404


async def test_completion_and_label(client):
    await client.post("/admin/config", json={"cold_start_ms": 0})
    r = await client.post("/v1/chat/completions", json=body(4))
    assert r.status_code == 200 and r.headers["x-packserve-simulated"] == "true"
    assert r.json()["usage"]["completion_tokens"] == 4


async def test_stream_format(client):
    await client.post("/admin/config", json={"cold_start_ms": 0, "tpot_ms": 1})
    r = await client.post("/v1/chat/completions", json=body(3, stream=True, stream_options={"include_usage": True}))
    lines = [l for l in r.text.split("\n") if l.startswith("data: ")]
    assert lines[-1] == "data: [DONE]" and '"usage"' in lines[-2]


async def test_rate_limit_429_retry_after(client):
    await client.post("/admin/config", json={"tokens_per_minute": 1200, "cold_start_ms": 0, "tpot_ms": 1})
    assert (await client.post("/v1/chat/completions", json=body(500))).status_code == 200
    assert (await client.post("/v1/chat/completions", json=body(500))).status_code == 200
    r = await client.post("/v1/chat/completions", json=body(500))
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) >= 1
    assert r.json()["error"]["type"] == "rate_limit_error"
    stats = (await client.get("/admin/stats")).json()
    assert stats["rate_limited"] == 1 and stats["ok"] == 2


async def test_cold_start_then_warm(client):
    await client.post("/admin/config", json={"cold_start_ms": 300, "tpot_ms": 1})
    t = time.perf_counter()
    r1 = await client.post("/v1/chat/completions", json=body(2))
    cold = time.perf_counter() - t
    t = time.perf_counter()
    r2 = await client.post("/v1/chat/completions", json=body(2))
    warm = time.perf_counter() - t
    assert r1.headers["x-packserve-cold-start"] == "true" and r2.headers["x-packserve-cold-start"] == "false"
    assert cold > 0.28 and warm < 0.15
    await client.post("/admin/config", json={"idle_s": 0})
    await client.post("/admin/config", json={"idle_s": 0.0})
    time.sleep(0.01)
    assert (await client.post("/v1/chat/completions", json=body(2))).headers["x-packserve-cold-start"] == "true"


async def test_per_token_latency(client):
    await client.post("/admin/config", json={"cold_start_ms": 0, "tpot_ms": 20})
    t = time.perf_counter()
    await client.post("/v1/chat/completions", json=body(30))
    per_tok = (time.perf_counter() - t) / 30 * 1000
    assert 17 < per_tok < 28


async def test_reset_and_fail_rate(client):
    await client.post("/admin/config", json={"fail_rate": 1.0})
    assert (await client.post("/v1/chat/completions", json=body())).status_code == 503
    await client.post("/admin/reset")
    st = (await client.get("/admin/stats")).json()
    assert st["requests"] == 0 and st["config"]["fail_rate"] == 0.0 and st["config"]["tokens_per_minute"] == 20000
