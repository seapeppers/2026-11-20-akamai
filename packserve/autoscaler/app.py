"""Autoscaler admin API (port 8070)."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from packserve.autoscaler.controller import AutoscalerStateExt, Controller


class ScaleRequest(BaseModel):
    replicas: int | None = None   # null clears the manual override (back to automatic rules)
    model: str | None = None      # default: the first configured pool


class BillingHoldRequest(BaseModel):
    enabled: bool


def create_app(controller: Controller, run_loop: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if run_loop:
            controller.start()
        try:
            yield
        finally:
            if run_loop:
                await controller.stop()
            # cleanup runs on every exit path: stops spawned sim replicas / deletes claude-probe-* Linodes
            leftovers = await controller.provider.cleanup() if controller.cleanup_on_exit else []
            if leftovers:
                import logging
                logging.getLogger("packserve.autoscaler").warning("cleanup left: %s", leftovers)

    app = FastAPI(title="packserve-autoscaler", lifespan=lifespan)
    app.state.controller = controller

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/admin/state", response_model=AutoscalerStateExt)
    async def state():
        return controller.snapshot()

    @app.post("/admin/scale", response_model=AutoscalerStateExt)
    async def scale(req: ScaleRequest):
        try:
            controller.set_manual(req.replicas, req.model)
        except KeyError:
            raise HTTPException(404, f"no pool for model {req.model!r}")
        return controller.snapshot()

    @app.post("/admin/pause", response_model=AutoscalerStateExt)
    async def pause():
        controller.paused = True
        return controller.snapshot()

    @app.post("/admin/resume", response_model=AutoscalerStateExt)
    async def resume():
        controller.paused = False
        return controller.snapshot()

    @app.post("/admin/billing_hold", response_model=AutoscalerStateExt)
    async def billing_hold(req: BillingHoldRequest):
        controller.params.billing_hold = req.enabled
        return controller.snapshot()

    return app
