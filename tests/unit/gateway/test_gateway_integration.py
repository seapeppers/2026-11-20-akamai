"""In-process gateway integration: real metadata app + fake replica + fake serverless, wired through
an httpx transport that routes by hostname (no sockets)."""
import json

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse

from packserve.common.schemas import ReplicaRegistration, ReplicaState
from packserve.gateway.app import create_app
from packserve.metadata.app import create_app as metadata_app

KEY = "ps_demo_interactive"   # t-interactive: 30ms, burst, qwen+gemma
BATCH = "ps_demo_batch"       # burst disabled
LARGE = "ps_demo_large"       # pool-only model, burst flag on


def sse(n):
    async def gen():
        for i in range(n):
            yield f'data: {json.dumps({"choices": [{"delta": {"content": "tok"}}]})}\n\n'.encode()
        yield b"data: [DONE]\n\n"
    return gen()


def fake_backend(name, calls):
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat(req: Request):
        body = await req.json()
        calls.append((name, req.headers.get("x-packserve-request-id"), req.headers.get("authorization")))
        if app.state.fail:
            return Response(status_code=app.state.fail, headers={"Retry-After": "7"})
        if body.get("stream"):
            return StreamingResponse(sse(5), media_type="text/event-stream")
        return {"choices": [{"message": {"content": "hi"}}], "usage": {"completion_tokens": 5}}

    app.state.fail = 0
    return app


class Router(httpx.AsyncBaseTransport):
    def __init__(self, apps):
        self.t = {h: httpx.ASGITransport(a) for h, a in apps.items()}

    async def handle_async_request(self, request):
        return await self.t[request.url.host].handle_async_request(request)


@pytest.fixture
async def env(monkeypatch):
    monkeypatch.setenv("AKAMAI_API_KEY", "sk-secret-test")
    calls = []
    md = metadata_app(None)
    store = md.state.store
    r1, r2 = fake_backend("r1", calls), fake_backend("r2", calls)
    serverless = fake_backend("serverless", calls)
    tel = FastAPI()
    completions = []

    @tel.post("/v1/completions")
    async def _c(req: Request):
        completions.append(await req.json())

    @tel.post("/v1/decisions")
    async def _d(req: Request):
        pass

    transport = Router({"md": md, "r1": r1, "r2": r2, "sls": serverless, "tel": tel})
    client = httpx.AsyncClient(transport=transport)
    for rid, model in (("r1", "qwen3-8-27b"), ("r2", "qwen3-8-27b")):
        store.register(ReplicaRegistration(id=rid, url=f"http://{rid}", model=model, profile="virtual-blackwell-x4"))
    store.register(ReplicaRegistration(id="big", url="http://r1", model="packserve-large-230b",
                                       profile="virtual-blackwell-x8-large"))
    app = create_app("gw1", client=client, metadata_url="http://md", telemetry_url="http://tel",
                     serverless_url="http://sls/v1", start_background=False)
    gw = app.state.gateway
    await gw.refresh()
    api = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://gw")
    yield type("E", (), dict(api=api, gw=gw, store=store, calls=calls, r1=r1, r2=r2, sls=serverless,
                             md=md, client=client, completions=completions, transport=transport))
    await api.aclose()


def req(model="qwen3-8-27b", stream=False, content="hello", **kw):
    return {"model": model, "messages": [{"role": "user", "content": content}], "stream": stream, **kw}


async def post(env, body, key=KEY):
    return await env.api.post("/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"})


async def test_routes_to_replica_with_headers(env):
    r = await post(env, req(stream=True))
    assert r.status_code == 200 and r.text.count("data:") == 6
    h = r.headers
    assert h["x-packserve-route"] == "replica" and h["x-packserve-reason"] == "packed"
    assert h["x-packserve-replica"] in ("r1", "r2")
    assert h["x-packserve-simulated"] == "true"
    assert h["x-packserve-candidates"] == "capacity=2,budget=2,tpot=2"
    assert float(h["x-packserve-predicted-tpot-ms"]) > 0
    d = (await env.api.get("/admin/decisions?limit=1")).json()[0]
    assert d["request_id"] == h["x-packserve-request-id"] and d["decision_us"] < 2000
    # replica received the gateway's request id
    assert env.calls[-1][1] == h["x-packserve-request-id"]


async def test_non_stream_and_accounting_returns_to_zero(env):
    r = await post(env, req())
    assert r.status_code == 200 and r.json()["usage"]["completion_tokens"] == 5
    assert all(v.running == 0 for v in env.gw.pool.views.values())
    assert env.gw.out_tokens[-1] == 5


async def test_packing_sticks_to_busy_replica_and_prefix_cache(env):
    long = "x" * 4000
    first = (await post(env, req(content=long))).headers["x-packserve-replica"]
    second = await post(env, req(content=long + "more"))
    assert second.headers["x-packserve-replica"] == first
    d = (await env.api.get("/admin/decisions?limit=1")).json()[0]
    assert d["cache_hit_tokens"] > 0


async def test_unauthorized_and_unknown_model(env):
    r = await env.api.post("/v1/chat/completions", json=req())
    assert r.status_code == 401 and r.headers["x-packserve-reason"] == "unauthorized"
    assert r.headers["x-packserve-route"] == "rejected" and "x-packserve-request-id" in r.headers
    r = await post(env, req(), key="bad")
    assert r.status_code == 401
    r = await post(env, req(model="gpt-nope"))
    assert r.status_code == 404 and r.headers["x-packserve-reason"] == "unknown_model"
    r = await post(env, req(model="packserve-large-230b"))  # not in this tenant's models
    assert r.status_code == 404


async def test_draining_replica_not_routed(env):
    env.store.drain("r1")
    await env.gw.refresh()
    for _ in range(4):
        r = await post(env, req())
        assert r.headers["x-packserve-replica"] == "r2"


async def test_overflow_when_pool_full_uses_serverless_key_without_leak(env):
    for v in env.gw.pool.views.values():
        v.running = v.max_running
    r = await post(env, req(stream=True))
    assert r.status_code == 200
    assert r.headers["x-packserve-route"] == "overflow" and r.headers["x-packserve-reason"] == "overflow"
    assert "x-packserve-replica" not in r.headers
    assert env.calls[-1][0] == "serverless" and env.calls[-1][2] == "Bearer sk-secret-test"
    assert "sk-secret" not in r.text and "sk-secret" not in json.dumps(dict(r.headers))


async def test_overflow_429_rejected_with_retry_after(env):
    for v in env.gw.pool.views.values():
        v.running = v.max_running
    env.sls.state.fail = 429
    r = await post(env, req())
    assert r.status_code == 429 and r.headers["retry-after"] == "7"
    assert r.headers["x-packserve-route"] == "rejected" and r.headers["x-packserve-reason"] == "overflow_rejected"
    env.sls.state.fail = 503
    assert (await post(env, req())).headers["x-packserve-reason"] == "overflow_rejected"


async def test_burst_disabled_and_pool_only(env):
    for v in env.gw.pool.views.values():
        v.running = v.max_running
    env.gw.pool.views["big"].running = 0
    r = await post(env, req(), key=BATCH)
    assert r.status_code == 429 and r.headers["x-packserve-reason"] == "burst_disabled" and "retry-after" in r.headers
    r = await post(env, req(model="packserve-large-230b"), key=LARGE)
    assert r.status_code == 200 and r.headers["x-packserve-replica"] == "big"
    env.gw.pool.views["big"].running = 48
    r = await post(env, req(model="packserve-large-230b"), key=LARGE)
    assert r.status_code == 429 and r.headers["x-packserve-reason"] == "pool_only_model"
    assert not any(c[0] == "serverless" for c in env.calls)


async def test_replica_failure_retries_other_replica(env):
    env.r1.state.fail = 500
    seen = set()
    for _ in range(4):
        r = await post(env, req())
        assert r.status_code == 200
        seen.add(r.headers["x-packserve-replica"])
    assert seen == {"r2"}


async def test_deltas_make_load_visible_to_second_gateway(env):
    gw2 = create_app("gw2", client=env.client, metadata_url="http://md", telemetry_url=None,
                     serverless_url="http://sls/v1", start_background=False).state.gateway
    await gw2.refresh()
    env.gw.pool.on_dispatch("r1", 100, 200, ["hh"])
    assert gw2.pool.views["r1"].running == 0
    await env.gw.refresh()   # flush delta
    await gw2.refresh()
    assert gw2.pool.views["r1"].running == 1
    assert "hh" in gw2.pool.views["r1"].cache
    env.gw.pool.on_complete("r1", 100, 200)
    await env.gw.refresh()
    await gw2.refresh()
    assert gw2.pool.views["r1"].running == 0
    # gw1 does not double count its own flushed delta
    assert env.gw.pool.views["r1"].running == 0


async def test_admin_state_reset_models(env):
    await post(env, req())
    s = (await env.api.get("/admin/state")).json()
    assert {r["id"] for r in s["replicas"]} == {"r1", "r2", "big"} and "correction" in s
    assert s["decision_us"]["count"] == 1
    assert (await env.api.post("/admin/reset")).json()["ok"]
    assert (await env.api.get("/admin/decisions")).json() == []
    m = (await env.api.get("/v1/models")).json()
    assert {x["id"] for x in m["data"]} == {"qwen3-8-27b", "gemma-4-26b-a4b-it", "packserve-large-230b"}


async def test_correction_updates_from_measured_tpot(env):
    v = env.gw.pool.views["r1"]
    before = list(v.correction.f)
    v.correction.update(0, 30.0, 10.0)  # measured 3x slower than predicted -> factor rises
    assert v.correction.f[0] > before[0]


async def test_completion_records_have_ttft_and_tpot_for_replica_and_overflow(env):
    import asyncio
    await post(env, req(stream=True))
    for v in env.gw.pool.views.values():
        v.running = v.max_running
    await post(env, req(stream=True))
    await asyncio.sleep(0.05)
    routes = {c["route"]: c for c in env.completions}
    assert set(routes) == {"replica", "overflow"}
    for c in routes.values():
        assert c["ttft_ms"] is not None and c["tpot_ms"] is not None and c["output_tokens"] == 5


async def test_overflow_400_is_overflow_rejected(env):
    for v in env.gw.pool.views.values():
        v.running = v.max_running
    env.sls.state.fail = 400
    r = await post(env, req())
    assert r.status_code == 429 and r.headers["x-packserve-reason"] == "overflow_rejected"
    assert r.headers["x-packserve-route"] == "rejected"


async def test_pool_only_beats_burst_disabled(env):
    env.gw.pool.views["big"].running = 48
    r = await post(env, req(model="packserve-large-230b"), key=BATCH)  # batch: burst off, large allowed
    assert r.headers["x-packserve-reason"] == "pool_only_model"
