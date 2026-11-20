"""PackServe gateway: OpenAI-compatible front door, scheduler, streaming proxy, overflow."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import statistics
import time
import uuid
from collections import deque
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from packserve.common import config
from packserve.common.schemas import (
    Candidates, CompletionRecord, ReplicaState, RouteDecision, Tenant,
)
from packserve.common.tokens import count_tokens, messages_text, prefix_block_hashes

from .pool import Pool
from .scheduler import Decision, Features, schedule

TENANT_TTL_S = 5.0
NEG_TENANT_TTL_S = 1.0
RETRY_AFTER_S = "2"
DEFAULT_MAX_TOKENS = 256
TELEMETRY_BACKOFF_S = 5.0


def _err(message: str, etype: str, code: str) -> dict:
    return {"error": {"message": message, "type": etype, "code": code}}


class Gateway:
    def __init__(self, gateway_id: str, client: httpx.AsyncClient, *, metadata_url: str = config.METADATA_URL,
                 telemetry_url: str | None = config.TELEMETRY_URL,
                 serverless_url: str = config.SERVERLESS_BASE_URL, refresh_interval: float = 0.25,
                 catalog: dict[str, bool] | None = None):
        self.id = gateway_id
        self.client = client
        self.metadata_url = metadata_url.rstrip("/")
        self.telemetry_url = telemetry_url.rstrip("/") if telemetry_url else None
        self.serverless_url = serverless_url.rstrip("/")
        self.refresh_interval = refresh_interval
        self.catalog = catalog or dict(config.MODELS)
        self.pool = Pool(gateway_id)
        self.decisions: deque[RouteDecision] = deque(maxlen=1000)
        self.decision_us: deque[float] = deque(maxlen=5000)
        self.out_tokens: deque[int] = deque(maxlen=200)
        self._tenants: dict[str, tuple[Tenant | None, float]] = {}
        self._tel_down_until = 0.0
        self._tasks: set[asyncio.Task] = set()
        self._bg: list[asyncio.Task] = []
        self.last_refresh_ok: float | None = None

    # ---------------- lifecycle / sync ----------------
    def start(self) -> None:
        self._bg.append(asyncio.create_task(self._refresh_loop()))

    async def stop(self) -> None:
        for t in self._bg:
            t.cancel()
        for t in list(self._tasks):
            t.cancel()
        await self.client.aclose()

    async def _refresh_loop(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(self.refresh_interval)

    async def refresh(self) -> bool:
        """Flush our deltas to metadata, then merge a fresh snapshot."""
        deltas, taken = self.pool.take_deltas()
        if deltas:
            try:
                await asyncio.gather(*[self.client.post(f"{self.metadata_url}/v1/state/delta",
                                                         json=d.model_dump(), timeout=1.0) for d in deltas])
            except httpx.HTTPError:
                self.pool.restore(taken)
        try:
            r = await self.client.get(f"{self.metadata_url}/v1/replicas", timeout=1.0)
            r.raise_for_status()
            self.pool.merge([ReplicaState.model_validate(x) for x in r.json()])
            self.last_refresh_ok = time.time()
            return True
        except (httpx.HTTPError, ValueError):
            return False

    # ---------------- tenants ----------------
    async def tenant_for(self, api_key: str) -> tuple[Tenant | None, bool]:
        """Returns (tenant|None, metadata_ok)."""
        k = hashlib.sha256(api_key.encode()).hexdigest()
        now = time.monotonic()
        hit = self._tenants.get(k)
        if hit and hit[1] > now:
            return hit[0], True
        try:
            r = await self.client.get(f"{self.metadata_url}/v1/tenants/by-key",
                                      headers={"Authorization": f"Bearer {api_key}"}, timeout=1.5)
        except httpx.HTTPError:
            if hit:  # stale entry beats an outage
                return hit[0], True
            return None, False
        if r.status_code == 200:
            t = Tenant.model_validate(r.json())
            self._tenants[k] = (t, now + TENANT_TTL_S)
            return t, True
        if r.status_code == 401:
            self._tenants[k] = (None, now + NEG_TENANT_TTL_S)
            return None, True
        return (hit[0], True) if hit else (None, False)

    # ---------------- telemetry ----------------
    def _fire(self, path: str, payload: dict) -> None:
        if not self.telemetry_url or time.monotonic() < self._tel_down_until or len(self._tasks) > 500:
            return

        async def go() -> None:
            try:
                await self.client.post(f"{self.telemetry_url}{path}", json=payload, timeout=1.0)
            except Exception:
                self._tel_down_until = time.monotonic() + TELEMETRY_BACKOFF_S

        t = asyncio.create_task(go())
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def record(self, d: RouteDecision) -> None:
        self.decisions.append(d)
        self.decision_us.append(d.decision_us)
        self._fire("/v1/decisions", d.model_dump(mode="json"))

    def record_completion(self, c: CompletionRecord) -> None:
        self._fire("/v1/completions", c.model_dump(mode="json"))

    # ---------------- headers ----------------
    @staticmethod
    def headers(rid: str, route: str, reason: str, replica: str | None = None,
                tpot: float | None = None, cands: Candidates | None = None) -> dict[str, str]:
        c = cands or Candidates()
        h = {"X-PackServe-Route": route, "X-PackServe-Reason": reason, "X-PackServe-Request-Id": rid,
             "X-PackServe-Candidates": f"capacity={c.capacity},budget={c.budget},tpot={c.tpot}",
             "X-PackServe-Simulated": "true" if config.SIMULATED else "false"}
        if replica:
            h["X-PackServe-Replica"] = replica
        if tpot is not None:
            h["X-PackServe-Predicted-TPOT-Ms"] = f"{tpot:.3f}"
        return h

    def reject(self, status: int, rid: str, reason: str, message: str, etype: str, *, tenant_id: str | None = None,
               model: str = "", cands: Candidates | None = None, retry_after: str | None = None,
               decision_us: float = 0.0, prompt_tokens: int = 0, max_tokens: int = 0) -> JSONResponse:
        h = self.headers(rid, "rejected", reason, cands=cands)
        if status == 429:
            h["Retry-After"] = retry_after or RETRY_AFTER_S
        self.record(RouteDecision(request_id=rid, gateway_id=self.id, tenant_id=tenant_id, model=model,
                                  route="rejected", reason=reason, candidates=cands or Candidates(),
                                  decision_us=decision_us, prompt_tokens=prompt_tokens,
                                  max_tokens=max_tokens, simulated=config.SIMULATED))
        return JSONResponse(_err(message, etype, reason), status_code=status, headers=h)

    # ---------------- main path ----------------
    async def chat(self, request: Request) -> Response:
        t_arrive = time.perf_counter()
        rid = str(uuid.uuid4())
        key = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not key:
            return self.reject(401, rid, "unauthorized", "Missing API key", "invalid_request_error")
        tenant, md_ok = await self.tenant_for(key)
        if tenant is None:
            if not md_ok:
                return self.reject(503, rid, "no_capacity", "Metadata service unavailable", "server_error",
                                   retry_after="1")
            return self.reject(401, rid, "unauthorized", "Invalid API key", "invalid_request_error")
        try:
            body = await request.json()
            assert isinstance(body, dict)
        except Exception:
            return self.reject(400, rid, "unknown_model", "Body must be a JSON object", "invalid_request_error",
                               tenant_id=tenant.id)
        model = str(body.get("model", ""))
        text = messages_text(body.get("messages") or [])
        prompt_tokens = count_tokens(text)
        max_tokens = int(body.get("max_completion_tokens") or body.get("max_tokens") or DEFAULT_MAX_TOKENS)
        hashes = prefix_block_hashes(text, 64)
        med = int(statistics.median(self.out_tokens)) if self.out_tokens else min(max_tokens, 128)
        feats = Features(model, prompt_tokens, max_tokens, hashes, med)
        need = prompt_tokens + max_tokens
        stream = bool(body.get("stream"))
        t_start = t_arrive

        exclude: set[str] = set()
        for attempt in range(2):
            views = [v for v in self.pool.views.values() if v.id not in exclude]
            t0 = time.perf_counter_ns()
            dec = schedule(feats, views, tenant, self.catalog)
            dus = (time.perf_counter_ns() - t0) / 1000.0
            common = dict(tenant_id=tenant.id, model=model, cands=dec.candidates, decision_us=dus,
                          prompt_tokens=prompt_tokens, max_tokens=max_tokens)

            if dec.route == "rejected":
                if dec.reason == "unknown_model":
                    return self.reject(404, rid, "unknown_model",
                                       f"The model '{model}' does not exist or is not enabled for this key",
                                       "invalid_request_error", **common)
                return self.reject(429, rid, dec.reason, _REJECT_MSG[dec.reason], "rate_limit_error", **common)

            if dec.route == "overflow":
                resp = await self._overflow(rid, tenant, model, body, stream, dec, dus, feats, t_start)
                return resp

            view = self.pool.views[dec.replica_id]
            url = f"{view.url}/v1/chat/completions"
            self.pool.on_dispatch(view.id, prompt_tokens, need, hashes)
            up = None
            try:
                req = self.client.build_request("POST", url, json=body, headers={"X-PackServe-Request-Id": rid})
                up = await self.client.send(req, stream=True)
                if up.status_code >= 500 and attempt == 0:
                    await up.aclose()
                    raise httpx.HTTPError("upstream 5xx")
            except httpx.HTTPError:
                self.pool.on_complete(view.id, prompt_tokens, need)
                exclude.add(view.id)
                if up is not None:
                    with contextlib.suppress(Exception):
                        await up.aclose()
                if attempt == 0:
                    continue
                return self.reject(502, rid, "no_capacity", "Replica unreachable", "server_error", **common)
            return await self._relay(up, rid, tenant, model, stream, dec, dus, feats, t_start,
                                     on_done=lambda: self.pool.on_complete(view.id, prompt_tokens, need))
        raise AssertionError("unreachable")

    async def _overflow(self, rid, tenant, model, body, stream, dec: Decision, dus, feats: Features, t_start):
        headers = {"X-PackServe-Request-Id": rid}
        key = os.environ.get(config.SERVERLESS_API_KEY_ENV)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        common = dict(tenant_id=tenant.id, model=model, cands=dec.candidates, decision_us=dus,
                      prompt_tokens=feats.prompt_tokens, max_tokens=feats.max_tokens)
        try:
            req = self.client.build_request("POST", f"{self.serverless_url}/chat/completions", json=body,
                                            headers=headers)
            up = await self.client.send(req, stream=True)
        except httpx.HTTPError:
            return self.reject(429, rid, "overflow_rejected", "Serverless endpoint unreachable",
                               "rate_limit_error", **common)
        if not 200 <= up.status_code < 300:
            ra = up.headers.get("retry-after")
            await up.aclose()
            return self.reject(429, rid, "overflow_rejected", "Serverless overflow rejected the request",
                               "rate_limit_error", retry_after=ra, **common)
        return await self._relay(up, rid, tenant, model, stream, dec, dus, feats, t_start, on_done=lambda: None)

    async def _relay(self, up: httpx.Response, rid: str, tenant: Tenant, model: str, stream: bool,
                     dec: Decision, dus: float, feats: Features, t_start: float, on_done) -> Response:
        route = dec.route
        hdrs = self.headers(rid, route, dec.reason, dec.replica_id, dec.predicted_tpot_ms, dec.candidates)
        self.record(RouteDecision(
            request_id=rid, gateway_id=self.id, tenant_id=tenant.id, model=model, route=route,
            reason=dec.reason, replica_id=dec.replica_id, predicted_tpot_ms=dec.predicted_tpot_ms,
            predicted_prefill_ms=dec.predicted_prefill_ms, cache_hit_tokens=dec.cache_hit_tokens,
            prompt_tokens=feats.prompt_tokens, max_tokens=feats.max_tokens, candidates=dec.candidates,
            decision_us=dus, simulated=config.SIMULATED))
        ctype = up.headers.get("content-type", "application/json")
        status = up.status_code
        state = {"first": None, "last": None, "n": 0, "usage": None}

        def finish() -> None:
            on_done()
            n = state["usage"] or state["n"]
            ttft = (state["first"] - t_start) * 1000 if state["first"] else None
            tpot = None
            if state["n"] > 1 and state["first"] is not None and state["last"] > state["first"]:
                tpot = (state["last"] - state["first"]) * 1000 / (state["n"] - 1)
            if n:
                self.out_tokens.append(n)
            if (tpot is not None and dec.replica_id and dec.predicted_tpot_ms and dec.segment is not None
                    and status == 200):
                v = self.pool.views.get(dec.replica_id)
                if v is not None:
                    v.correction.update(dec.segment, tpot, dec.predicted_tpot_ms)
            self.record_completion(CompletionRecord(
                request_id=rid, tenant_id=tenant.id, model=model, route=route, replica_id=dec.replica_id,
                ttft_ms=ttft, tpot_ms=tpot, output_tokens=n, status=status, simulated=config.SIMULATED))

        def observe(ev: bytes) -> None:
            if not ev.startswith(b"data:") or b"[DONE]" in ev:
                return
            try:
                j = json.loads(ev[5:])
            except ValueError:
                return
            u = j.get("usage")
            if u and u.get("completion_tokens"):
                state["usage"] = u["completion_tokens"]
            ch = j.get("choices") or []
            if ch and (ch[0].get("delta") or {}).get("content"):
                now = time.perf_counter()
                if state["first"] is None:
                    state["first"] = now
                state["last"] = now
                state["n"] += 1

        if stream and status == 200:
            async def gen() -> AsyncIterator[bytes]:
                buf = b""
                try:
                    async for chunk in up.aiter_bytes():
                        buf += chunk
                        while b"\n\n" in buf:
                            ev, buf = buf.split(b"\n\n", 1)
                            observe(ev.strip())
                        yield chunk
                finally:
                    finish()
                    with contextlib.suppress(Exception):
                        await up.aclose()

            return StreamingResponse(gen(), status_code=status, headers=hdrs, media_type=ctype)

        try:
            content = await up.aread()
        finally:
            await up.aclose()
        now = time.perf_counter()
        try:
            u = (json.loads(content) or {}).get("usage") or {}
            state["usage"] = u.get("completion_tokens")
        except ValueError:
            pass
        state["first"] = now if state["usage"] else None
        finish()
        return Response(content, status_code=status, headers=hdrs, media_type=ctype)

    # ---------------- admin ----------------
    def state(self) -> dict:
        us = sorted(self.decision_us)
        stats = {"count": len(us)}
        if us:
            stats.update(mean=sum(us) / len(us), p50=us[len(us) // 2], p99=us[min(len(us) - 1, int(len(us) * 0.99))],
                         max=us[-1])
        return {"gateway_id": self.id, "replicas": self.pool.snapshot(),
                "correction": {rid: list(c.f) for rid, c in self.pool.corrections.items()},
                "decision_us": stats, "last_refresh_ok": self.last_refresh_ok,
                "median_output_tokens": int(statistics.median(self.out_tokens)) if self.out_tokens else None}


_REJECT_MSG = {
    "no_capacity": "No pool capacity available",
    "pool_only_model": "No pool capacity and this model cannot burst to serverless",
    "burst_disabled": "No pool capacity and burst is disabled for this tenant",
    "overflow_rejected": "Serverless overflow rejected the request",
}


def create_app(gateway_id: str = "gw1", *, client: httpx.AsyncClient | None = None,
               metadata_url: str = config.METADATA_URL, telemetry_url: str | None = config.TELEMETRY_URL,
               serverless_url: str = config.SERVERLESS_BASE_URL, start_background: bool = True,
               refresh_interval: float = 0.25) -> FastAPI:
    client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(connect=2.0, read=300.0, write=10.0, pool=None),
        limits=httpx.Limits(max_connections=2000, max_keepalive_connections=200))
    gw = Gateway(gateway_id, client, metadata_url=metadata_url, telemetry_url=telemetry_url,
                 serverless_url=serverless_url, refresh_interval=refresh_interval)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_background:
            await gw.refresh()
            gw.start()
        yield
        await gw.stop()

    app = FastAPI(title=f"PackServe gateway {gateway_id} (SIMULATED pool)", lifespan=lifespan)
    app.state.gateway = gw

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        return await gw.chat(request)

    @app.get("/v1/models")
    async def models():
        return {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "packserve",
                                            "burst_eligible": b} for m, b in gw.catalog.items()]}

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "gateway_id": gw.id}

    @app.get("/admin/decisions")
    async def decisions(limit: int = 100):
        items = list(gw.decisions)[::-1][:max(limit, 0)]
        return [d.model_dump(mode="json") for d in items]

    @app.get("/admin/state")
    async def admin_state():
        return gw.state()

    @app.post("/admin/reset")
    async def reset():
        gw.decisions.clear()
        gw.decision_us.clear()
        gw.out_tokens.clear()
        gw.pool.reset()
        gw._tenants.clear()
        return {"ok": True}

    @app.post("/admin/refresh")
    async def refresh():
        return {"ok": await gw.refresh()}

    return app
