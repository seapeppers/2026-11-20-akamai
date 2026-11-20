"""SIMULATED serverless endpoint standing in for Akamai Inference. No real text is generated."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import time
import uuid

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from packserve.common import config
from packserve.common.schemas import ServerlessMockConfig
from packserve.common.tokens import count_tokens, messages_text

SIM_HEADERS = {"X-PackServe-Simulated": "true"}
WORDS = "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor".split()
DEFAULT_MAX_TOKENS = 128
NOISE_SIGMA = 0.15


def _err(status: int, msg: str, typ: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": {"message": msg, "type": typ, "code": status}}, status_code=status,
                        headers={**SIM_HEADERS, **(headers or {})})


class MockState:
    def __init__(self, seed: int | None = None):
        self.rng = random.Random(seed)
        self.reset()

    def reset(self) -> None:
        self.cfg = ServerlessMockConfig()
        self._refill_bucket()
        self.last_request: float | None = None
        self.stats = {"requests": 0, "ok": 0, "rate_limited": 0, "failed": 0, "cold_starts": 0,
                      "prompt_tokens": 0, "completion_tokens": 0}

    def _refill_bucket(self) -> None:
        self.bucket = float(self.cfg.tokens_per_minute)
        self.bucket_ts = time.monotonic()

    def set_config(self, patch: dict) -> None:
        self.cfg = self.cfg.model_copy(update=patch)
        self._refill_bucket()

    def take(self, cost: int) -> float:
        """Charge the bucket; return 0 if admitted, else seconds until it could be."""
        rate = self.cfg.tokens_per_minute / 60.0
        now = time.monotonic()
        self.bucket = min(float(self.cfg.tokens_per_minute), self.bucket + (now - self.bucket_ts) * rate)
        self.bucket_ts = now
        if self.bucket >= cost:
            self.bucket -= cost
            return 0.0
        return (cost - self.bucket) / rate

    def token_delay(self) -> float:
        return self.cfg.tpot_ms / 1000 * self.rng.lognormvariate(-NOISE_SIGMA ** 2 / 2, NOISE_SIGMA)


def create_app(seed: int | None = None) -> FastAPI:
    app = FastAPI(title="SIMULATED serverless mock")
    st = MockState(seed)
    app.state.mock = st

    @app.get("/healthz")
    async def healthz():
        return JSONResponse({"ok": True, "simulated": True}, headers=SIM_HEADERS)

    @app.get("/v1/models")
    async def models():
        return JSONResponse({"object": "list", "data": [{"id": m, "object": "model", "owned_by": "akamai-simulated"}
                                                         for m in config.SERVERLESS_CATALOG]}, headers=SIM_HEADERS)

    @app.post("/admin/config")
    async def admin_config(request: Request):
        patch = await request.json()
        ServerlessMockConfig(**{**st.cfg.model_dump(), **patch})  # validate
        st.set_config(patch)
        return JSONResponse(st.cfg.model_dump(), headers=SIM_HEADERS)

    @app.post("/admin/reset")
    async def admin_reset():
        st.reset()
        return JSONResponse({"ok": True, "config": st.cfg.model_dump()}, headers=SIM_HEADERS)

    @app.get("/admin/stats")
    async def admin_stats():
        return JSONResponse({**st.stats, "bucket_tokens": round(st.bucket, 1), "config": st.cfg.model_dump(),
                             "simulated": True}, headers=SIM_HEADERS)

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        try:
            body = await request.json()
            messages = body["messages"]
        except Exception:
            return _err(400, "invalid request body", "invalid_request_error")
        model = body.get("model")
        if model not in config.SERVERLESS_CATALOG:
            return _err(404, f"model '{model}' not found in serverless catalog", "invalid_request_error")
        st.stats["requests"] += 1
        if st.cfg.fail_rate > 0 and st.rng.random() < st.cfg.fail_rate:
            st.stats["failed"] += 1
            return _err(503, "serverless backend unavailable (SIMULATED)", "server_error")
        prompt_tokens = count_tokens(messages_text(messages))
        max_tokens = max(1, int(body.get("max_completion_tokens") or body.get("max_tokens") or DEFAULT_MAX_TOKENS))
        cost = prompt_tokens + max_tokens
        if cost > st.cfg.tokens_per_minute:
            return _err(400, "request larger than the per-minute token limit", "invalid_request_error")
        wait = st.take(cost)
        if wait > 0:
            st.stats["rate_limited"] += 1
            return _err(429, "rate limit exceeded: tokens per minute (SIMULATED)", "rate_limit_error",
                        {"Retry-After": str(max(1, math.ceil(wait)))})
        now = time.monotonic()
        cold = st.last_request is None or now - st.last_request > st.cfg.idle_s
        st.last_request = now
        if cold:
            st.stats["cold_starts"] += 1
        st.stats["prompt_tokens"] += prompt_tokens
        rid = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())
        headers = {**SIM_HEADERS, "X-PackServe-Cold-Start": str(cold).lower()}

        async def tokens():
            if cold:
                await asyncio.sleep(st.cfg.cold_start_ms / 1000)
            for i in range(1, max_tokens + 1):
                await asyncio.sleep(st.token_delay())
                st.stats["completion_tokens"] += 1
                yield ("" if i == 1 else " ") + WORDS[(i - 1) % len(WORDS)]

        usage = {"prompt_tokens": prompt_tokens, "completion_tokens": max_tokens, "total_tokens": cost}

        if body.get("stream"):
            include_usage = bool((body.get("stream_options") or {}).get("include_usage"))

            def chunk(delta, finish=None):
                return "data: " + json.dumps({"id": rid, "object": "chat.completion.chunk", "created": created, "model": model,
                                              "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"

            async def gen():
                yield chunk({"role": "assistant", "content": ""})
                async for t in tokens():
                    yield chunk({"content": t})
                st.stats["ok"] += 1
                yield chunk({}, "length")
                if include_usage:
                    yield "data: " + json.dumps({"id": rid, "object": "chat.completion.chunk", "created": created,
                                                 "model": model, "choices": [], "usage": usage}) + "\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)

        content = "".join([t async for t in tokens()])
        st.stats["ok"] += 1
        return JSONResponse({"id": rid, "object": "chat.completion", "created": created, "model": model,
                             "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                          "finish_reason": "length"}], "usage": usage}, headers=headers)

    return app


def main() -> None:
    ap = argparse.ArgumentParser(description="SIMULATED Akamai Inference mock")
    ap.add_argument("--port", type=int, default=config.SERVERLESS_MOCK_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--seed", type=int, default=None)
    a = ap.parse_args()
    print(f"SIMULATED serverless mock on {a.host}:{a.port}", flush=True)
    uvicorn.run(create_app(a.seed), host=a.host, port=a.port, log_level="warning")
