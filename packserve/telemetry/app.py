"""Telemetry service (port 8060): decisions + completions in, rollups out."""
from __future__ import annotations

import asyncio
import os

import httpx
from fastapi import FastAPI

from packserve.common import config
from packserve.common.schemas import CompletionRecord, RouteDecision, Rollup
from packserve.telemetry.store import DEFAULT_DB, TraceStore, mean_utilization


def create_app(db_path: str | None = None, metadata_url: str | None = None) -> FastAPI:
    store = TraceStore(db_path or os.environ.get("PACKSERVE_TRACES_DB", DEFAULT_DB))
    meta = metadata_url or config.METADATA_URL
    app = FastAPI(title="packserve-telemetry")
    app.state.store = store

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.post("/v1/decisions", status_code=202)
    async def post_decision(d: RouteDecision):
        await asyncio.to_thread(store.add_decision, d)
        return {"ok": True}

    @app.post("/v1/completions", status_code=202)
    async def post_completion(c: CompletionRecord):
        await asyncio.to_thread(store.add_completion, c)
        return {"ok": True}

    @app.get("/v1/rollup", response_model=Rollup)
    async def rollup(window_s: float = 30, model: str | None = None):
        util = None
        try:
            async with httpx.AsyncClient(timeout=1.5) as c:
                r = await c.get(f"{meta}/v1/replicas")
                r.raise_for_status()
                util = mean_utilization(r.json(), model)
        except Exception:
            pass  # metadata down: utilization is simply unknown
        return await asyncio.to_thread(store.rollup, window_s, util, model)

    return app
