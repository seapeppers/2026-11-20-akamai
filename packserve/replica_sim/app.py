"""SIMULATED replica FastAPI app (OpenAI-compatible). No real text is generated."""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import time
import uuid
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from packserve.common import config
from packserve.common.latency import load_profile
from packserve.common.schemas import FaultConfig, ReplicaRegistration, ReplicaState
from packserve.common.tokens import count_tokens, messages_text, prefix_block_hashes

from .engine import Engine, Req

WORDS = ("lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor "
         "incididunt ut labore et dolore magna aliqua").split()
SIM_HEADERS = {"X-PackServe-Simulated": "true"}
DEFAULT_MAX_TOKENS = 128


def _tok(i: int) -> str:
    return ("" if i == 1 else " ") + WORDS[(i - 1) % len(WORDS)]


def _err(status: int, msg: str, typ: str = "server_error") -> JSONResponse:
    return JSONResponse({"error": {"message": msg, "type": typ, "code": status}}, status_code=status, headers=SIM_HEADERS)


def create_app(replica_id: str = "r1", model: str = "qwen3-8-27b", profile: str = config.DEFAULT_PROFILE,
               port: int = 9001, register: bool = True, metadata_url: str = config.METADATA_URL,
               seed: int | None = None, noise_sigma: float | None = None, profile_obj=None) -> FastAPI:
    prof = profile_obj or load_profile(profile)
    if noise_sigma is not None:
        prof = prof.model_copy(update={"noise_sigma": noise_sigma})
    url = f"http://127.0.0.1:{port}"
    created_at = time.time()
    holder: dict = {}

    def state() -> ReplicaState:
        e: Engine = holder["engine"]
        return ReplicaState(
            id=replica_id, url=url, model=model, profile=prof.name,
            healthy=not e.fault.fail, running=e.running, queued=len(e.waiting),
            kv_used_tokens=e.total_context(), kv_capacity_tokens=prof.kv_capacity_tokens,
            max_running=prof.max_running, total_context_tokens=e.total_context(),
            cached_block_hashes=e.cached_hashes(), measured_tpot_ms_ema=e.tpot_ema,
            created_at=created_at, updated_at=time.time())

    async def registrar() -> None:
        reg = ReplicaRegistration(id=replica_id, url=url, model=model, profile=prof.name)
        registered = False
        async with httpx.AsyncClient(base_url=metadata_url, timeout=2.0) as c:
            while True:
                try:
                    if not registered:
                        r = await c.post("/v1/replicas/register", json=reg.model_dump())
                        registered = r.status_code < 300
                    else:
                        r = await c.post(f"/v1/replicas/{replica_id}/heartbeat", json=state().model_dump())
                        if r.status_code == 404:
                            registered = False  # metadata restarted; re-register
                except (httpx.HTTPError, OSError):
                    registered = False  # metadata down: retry quietly
                await asyncio.sleep(1.0)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = Engine(prof, seed)
        holder["engine"] = engine
        engine.start()
        reg_task = asyncio.create_task(registrar()) if register else None
        yield
        if reg_task:
            reg_task.cancel()
            try:
                async with httpx.AsyncClient(timeout=1.0) as c:
                    await c.delete(f"{metadata_url}/v1/replicas/{replica_id}")
            except Exception:
                pass
        await engine.stop()

    app = FastAPI(title=f"SIMULATED replica {replica_id}", lifespan=lifespan)
    app.state.holder = holder
    drop_rng = random.Random(seed)

    @app.get("/healthz")
    async def healthz():
        if holder["engine"].fault.fail:
            return JSONResponse({"ok": False}, status_code=503, headers=SIM_HEADERS)
        return JSONResponse({"ok": True, "simulated": True}, headers=SIM_HEADERS)

    @app.get("/state")
    async def get_state():
        return JSONResponse(state().model_dump(), headers=SIM_HEADERS)

    @app.post("/admin/fault")
    async def set_fault(f: FaultConfig):
        holder["engine"].fault = f
        return JSONResponse(state().model_dump(), headers=SIM_HEADERS)

    @app.get("/v1/models")
    async def models():
        return JSONResponse({"object": "list", "data": [{"id": model, "object": "model", "owned_by": "packserve-simulated"}]},
                            headers=SIM_HEADERS)

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        e: Engine = holder["engine"]
        try:
            body = await request.json()
            messages = body["messages"]
        except Exception:
            return _err(400, "invalid request body: expected JSON with messages", "invalid_request_error")
        if e.fault.fail:
            return _err(503, "replica fault injected (SIMULATED)")
        drop = e.fault.drop_rate > 0 and drop_rng.random() < e.fault.drop_rate
        text = messages_text(messages)
        prompt_tokens = count_tokens(text)
        max_tokens = int(body.get("max_completion_tokens") or body.get("max_tokens") or DEFAULT_MAX_TOKENS)
        max_tokens = max(1, max_tokens)
        if prompt_tokens + max_tokens > prof.kv_capacity_tokens:
            return _err(400, "request exceeds KV capacity", "invalid_request_error")
        req = Req(prompt_tokens, max_tokens, prefix_block_hashes(text, prof.prefix_block_tokens))
        e.submit(req)
        rid = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())
        mname = body.get("model") or model
        headers = {**SIM_HEADERS, "X-PackServe-Replica": replica_id}
        if (pid := request.headers.get("x-packserve-request-id")):
            headers["X-PackServe-Request-Id"] = pid
        usage = lambda: {"prompt_tokens": prompt_tokens, "completion_tokens": req.produced,
                         "total_tokens": prompt_tokens + req.produced,
                         "prompt_tokens_details": {"cached_tokens": req.cached_tokens}}

        def chunk(delta: dict, finish=None) -> str:
            return "data: " + json.dumps({"id": rid, "object": "chat.completion.chunk", "created": created, "model": mname,
                                          "system_fingerprint": "packserve-simulated",
                                          "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"

        if body.get("stream"):
            include_usage = bool((body.get("stream_options") or {}).get("include_usage"))

            async def gen():
                try:
                    yield chunk({"role": "assistant", "content": ""})
                    drop_at = max_tokens // 2 + 1 if drop else None
                    while (i := await req.q.get()) is not None:
                        if drop_at is not None and i >= drop_at:
                            raise ConnectionError("dropped (SIMULATED fault)")
                        yield chunk({"content": _tok(i)})
                    yield chunk({}, "length")
                    if include_usage:
                        yield "data: " + json.dumps({"id": rid, "object": "chat.completion.chunk", "created": created,
                                                     "model": mname, "choices": [], "usage": usage()}) + "\n\n"
                    yield "data: [DONE]\n\n"
                finally:
                    if req.state != "done":
                        e.cancel(req)

            return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)

        try:
            while await req.q.get() is not None:
                pass
        except asyncio.CancelledError:
            e.cancel(req)
            raise
        if drop:
            return _err(500, "dropped (SIMULATED fault)")
        content = "".join(_tok(i) for i in range(1, req.produced + 1))
        return JSONResponse({"id": rid, "object": "chat.completion", "created": created, "model": mname,
                             "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                          "finish_reason": "length"}], "usage": usage()}, headers=headers)

    return app


def main() -> None:
    ap = argparse.ArgumentParser(description="SIMULATED GPU replica")
    ap.add_argument("--port", type=int, default=9001)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--id", default="r1")
    ap.add_argument("--model", default="qwen3-8-27b")
    ap.add_argument("--profile", default=config.DEFAULT_PROFILE)
    ap.add_argument("--metadata-url", default=config.METADATA_URL)
    ap.add_argument("--no-register", action="store_true")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--noise-sigma", type=float, default=None)
    a = ap.parse_args()
    app = create_app(a.id, a.model, a.profile, a.port, not a.no_register, a.metadata_url, a.seed, a.noise_sigma)
    print(f"SIMULATED replica {a.id} model={a.model} profile={a.profile} on {a.host}:{a.port}", flush=True)
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
