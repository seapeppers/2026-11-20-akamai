import asyncio
import json
import statistics
import time

import httpx
import pytest
from openai import AsyncOpenAI

from packserve.common.latency import BUILTIN, LatencyModel

PROFILE = BUILTIN["virtual-blackwell-x4"]
LM = LatencyModel(PROFILE)


def body(content="hi", max_tokens=8, **kw):
    return {"model": "qwen3-8-27b", "messages": [{"role": "user", "content": content}], "max_tokens": max_tokens, **kw}


async def stream_times(c, content, max_tokens):
    t0 = time.perf_counter()
    ts = []
    async with c.stream("POST", "/v1/chat/completions", json=body(content, max_tokens, stream=True)) as r:
        async for line in r.aiter_lines():
            if line.startswith("data: ") and line != "data: [DONE]":
                ev = json.loads(line[6:])
                if ev["choices"] and ev["choices"][0]["delta"].get("content"):
                    ts.append(time.perf_counter())
    return t0, ts


async def test_nonstream_and_headers(make_replica):
    r = make_replica()
    async with httpx.AsyncClient(base_url=r.url) as c:
        resp = await c.post("/v1/chat/completions", json=body(max_tokens=5), headers={"X-PackServe-Request-Id": "abc"})
    assert resp.status_code == 200
    assert resp.headers["x-packserve-simulated"] == "true"
    j = resp.json()
    assert j["usage"]["completion_tokens"] == 5 and j["choices"][0]["message"]["content"]


async def test_openai_client_streaming_usage(make_replica):
    r = make_replica()
    client = AsyncOpenAI(base_url=r.url + "/v1", api_key="x")
    s = await client.chat.completions.create(**body(max_tokens=6), stream=True, stream_options={"include_usage": True})
    chunks = [ch async for ch in s]
    text = "".join(ch.choices[0].delta.content or "" for ch in chunks if ch.choices)
    assert len(text.split()) == 6
    assert chunks[-1].usage.completion_tokens == 6 and chunks[-1].choices == []
    assert chunks[-2].choices[0].finish_reason == "length"


async def test_state_and_fault(make_replica):
    r = make_replica()
    async with httpx.AsyncClient(base_url=r.url) as c:
        st = (await c.get("/state")).json()
        assert st["id"] == "r1" and st["healthy"] and st["max_running"] == 64
        assert (await c.get("/healthz")).json()["ok"] is True
        await c.post("/admin/fault", json={"fail": True})
        assert (await c.get("/healthz")).status_code == 503
        assert (await c.get("/state")).json()["healthy"] is False
        assert (await c.post("/v1/chat/completions", json=body())).status_code == 503
        await c.post("/admin/fault", json={"fail": False, "slowdown": 3.0})
        t = time.perf_counter()
        await c.post("/v1/chat/completions", json=body(max_tokens=10))
        slow = time.perf_counter() - t
    assert slow > 10 * LM.decode_iter_ms(1, 10) * 3 / 1000 * 0.8


async def test_16_concurrent_streams_match_decode_model(make_replica):
    r = make_replica(noise_sigma=0.05)
    n, out = 16, 150
    async with httpx.AsyncClient(base_url=r.url, timeout=60) as c:
        res = await asyncio.gather(*[stream_times(c, f"unique prompt {i} " * 20, out) for i in range(n)])
    t0 = max(ts[0] for _, ts in res)
    t1 = min(ts[-1] for _, ts in res)
    assert t1 > t0
    gaps = []
    for _, ts in res:
        w = [t for t in ts if t0 <= t <= t1]
        assert len(w) > 20
        gaps.append((w[-1] - w[0]) / (len(w) - 1) * 1000)
    mean_gap = statistics.mean(gaps)
    ctx = n * (len("unique prompt 0 " * 20) // 4 + 80)
    expected = LM.decode_iter_ms(16, ctx)
    assert expected * 0.85 < mean_gap < expected * 1.2, (mean_gap, expected)
    # all streams share the same iterations, so their mean gaps agree closely
    assert max(gaps) / min(gaps) < 1.1


async def test_prefix_cache_hit_lowers_ttft(make_replica):
    r = make_replica(noise_sigma=0.02)
    prompt = "shared system prompt about packing. " * 800  # ~7k tokens
    async with httpx.AsyncClient(base_url=r.url, timeout=30) as c:
        async def ttft(extra):
            t0, ts = await stream_times(c, prompt + extra, 2)
            return (ts[0] - t0) * 1000
        first = await ttft("q1")
        second = await ttft("q2")
        usage = (await c.post("/v1/chat/completions", json=body(prompt + "q3", 2))).json()["usage"]
    assert usage["prompt_tokens_details"]["cached_tokens"] > 6000
    assert second < first * 0.5, (first, second)
    # first request paid the a3*N^2 term; cached tokens are exempt from it
    assert first > LM.prefill_ms(6500, 0) * 0.8


async def test_queueing_beyond_max_running(make_replica):
    r = make_replica()
    async with httpx.AsyncClient(base_url=r.url, timeout=60) as c:
        tasks = [asyncio.create_task(c.post("/v1/chat/completions", json=body(f"p{i}", 40))) for i in range(70)]
        await asyncio.sleep(0.5)
        st = (await c.get("/state")).json()
        assert st["running"] == 64 and st["queued"] == 6
        resps = await asyncio.gather(*tasks)
    assert all(x.status_code == 200 for x in resps)
    st = None
    async with httpx.AsyncClient(base_url=r.url) as c:
        st = (await c.get("/state")).json()
    assert st["running"] == 0 and st["queued"] == 0 and st["kv_used_tokens"] == 0


async def test_client_disconnect_frees_slot(make_replica):
    r = make_replica()
    async with httpx.AsyncClient(base_url=r.url, timeout=10) as c:
        async with c.stream("POST", "/v1/chat/completions", json=body(max_tokens=400, stream=True)) as resp:
            async for _ in resp.aiter_lines():
                break
        await asyncio.sleep(0.5)
        st = (await c.get("/state")).json()
    assert st["running"] == 0


async def test_register_tolerates_metadata_down(make_replica):
    r = make_replica(register=True, metadata_url="http://127.0.0.1:1")
    async with httpx.AsyncClient(base_url=r.url) as c:
        assert (await c.get("/healthz")).status_code == 200
