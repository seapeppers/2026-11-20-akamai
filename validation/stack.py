"""Thin wrappers over the stack's admin APIs (contracts.md) plus reset/restore helpers."""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from packserve.common import config
from packserve.common.schemas import ServerlessMockConfig


@dataclass
class Urls:
    gateway: str = config.GATEWAY_URL
    gateway2: str = os.environ.get("PACKSERVE_GATEWAY2_URL", f"http://127.0.0.1:{config.GATEWAY2_PORT}")
    metadata: str = config.METADATA_URL
    telemetry: str = config.TELEMETRY_URL
    autoscaler: str = config.AUTOSCALER_URL
    serverless: str = os.environ.get("PACKSERVE_SERVERLESS_ADMIN_URL", f"http://127.0.0.1:{config.SERVERLESS_MOCK_PORT}")

    def as_ctx(self) -> dict:
        return dict(self.__dict__)


TENANT_KEYS = {t["id"].removeprefix("t-"): t["api_key"] for t in config.SEED_TENANTS}
TENANT_TPOT = {t["id"].removeprefix("t-"): float(t["tpot_target_ms"]) for t in config.SEED_TENANTS}


def model_alias(model: str) -> str:
    return {"qwen3-8-27b": "q", "gemma-4-26b-a4b-it": "m", "packserve-large-230b": "l"}.get(model, "x")


class Stack:
    def __init__(self, urls: Urls, client: httpx.AsyncClient):
        self.u = urls
        self.c = client
        self.baseline_autoscaler: dict | None = None
        self.registrations: dict[str, dict] = {}   # replica id -> registration body (for re-register)

    # ---------------------------------------------------------------- generic http
    async def get_json(self, url: str, timeout: float = 5.0) -> Any:
        r = await self.c.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()

    async def post_json(self, url: str, body: Any = None, timeout: float = 10.0) -> httpx.Response:
        return await self.c.post(url, json=body, timeout=timeout)

    async def alive(self, url: str, path: str) -> bool:
        try:
            r = await self.c.get(url + path, timeout=2.0)
            return r.status_code < 500
        except httpx.HTTPError:
            return False

    # ---------------------------------------------------------------- discovery
    async def check_up(self) -> dict[str, bool]:
        probes = {"gateway": (self.u.gateway, "/admin/state"), "metadata": (self.u.metadata, "/v1/replicas"),
                  "serverless": (self.u.serverless, "/admin/stats"), "autoscaler": (self.u.autoscaler, "/admin/state"),
                  "telemetry": (self.u.telemetry, "/v1/rollup?window_s=1"), "gateway2": (self.u.gateway2, "/admin/state")}
        res = await asyncio.gather(*(self.alive(u, p) for u, p in probes.values()))
        return dict(zip(probes, res))

    async def replicas(self) -> list[dict]:
        return await self.get_json(f"{self.u.metadata}/v1/replicas")

    async def bind(self) -> dict[str, dict]:
        """Name replicas by model: q1,q2.. (qwen), m1.. (gemma), l1.. (large), ordered by id."""
        reps = [r for r in await self.replicas() if r.get("healthy", True) and not r.get("draining")]
        reps.sort(key=lambda r: (r["model"], _port(r["url"]), r["id"]))
        counters: dict[str, int] = {}
        out: dict[str, dict] = {}
        for r in reps:
            a = model_alias(r["model"])
            counters[a] = counters.get(a, 0) + 1
            out[f"{a}{counters[a]}"] = r
            self.registrations.setdefault(r["id"], {k: r[k] for k in ("id", "url", "model", "profile")} | {"provider": r.get("provider", "sim")})
        return out

    async def replica_state(self, url: str) -> dict:
        return await self.get_json(url + "/state")

    # ---------------------------------------------------------------- reset / restore
    async def reset_all(self, alive: dict[str, bool]) -> list[str]:
        """Bring shared state to a known baseline. Returns human-readable notes (anomalies)."""
        notes: list[str] = []
        for name in ("gateway", "gateway2"):
            if alive.get(name):
                r = await self.post_json(getattr(self.u, name) + "/admin/reset")
                if r.status_code >= 300:
                    notes.append(f"{name} /admin/reset -> {r.status_code}")
        if alive.get("serverless"):
            await self.post_json(self.u.serverless + "/admin/reset")
            await self.post_json(self.u.serverless + "/admin/config", ServerlessMockConfig().model_dump())
        for r in await self.replicas():
            try:
                await self.post_json(r["url"] + "/admin/fault", {"slowdown": 1.0, "fail": False, "drop_rate": 0.0}, timeout=3)
            except httpx.HTTPError:
                notes.append(f"could not reset faults on {r['id']}")
        return notes

    async def wait_idle(self, timeout_s: float = 90) -> tuple[bool, str]:
        """Wait until every replica reports running==0 and queued==0."""
        t0 = time.time()
        last = ""
        while time.time() - t0 < timeout_s:
            busy = []
            for r in await self.replicas():
                try:
                    s = await self.replica_state(r["url"])
                except httpx.HTTPError:
                    continue
                if s.get("running", 0) or s.get("queued", 0):
                    busy.append(f"{r['id']}:{s.get('running')}r/{s.get('queued')}q")
            if not busy:
                return True, ""
            last = ",".join(busy)
            await asyncio.sleep(1.0)
        return False, last

    async def autoscaler_state(self) -> dict | None:
        try:
            return await self.get_json(self.u.autoscaler + "/admin/state")
        except httpx.HTTPError:
            return None

    async def healthy_replicas(self, model: str | None = None) -> list[dict]:
        return [r for r in await self.replicas()
                if r.get("healthy", True) and not r.get("draining") and (model is None or r["model"] == model)]

    async def ensure_scale(self, n: int, wait_s: float = 90, model: str = "qwen3-8-27b") -> tuple[bool, str]:
        """Manual-scale one pool to n live replicas and wait for convergence.

        The controller only reconciles a manual target while NOT paused, so the autoscaler is resumed here
        (the runner re-pauses it at cleanup). The manual override pins the pool, so automatic rules do not interfere."""
        await self.post_json(self.u.autoscaler + "/admin/resume")
        await self.post_json(self.u.autoscaler + "/admin/scale", {"replicas": n, "model": model})
        t0 = time.time()
        while time.time() - t0 < wait_s:
            live = [r for r in await self.replicas()
                    if r.get("model") == model and r.get("healthy", True) and not r.get("draining")]
            if len(live) == n:
                return True, ""
            await asyncio.sleep(1.0)
        return False, f"timed out waiting for {n} healthy {model} replicas"

    async def clear_manual_scale(self, model: str = "qwen3-8-27b") -> None:
        await self.post_json(self.u.autoscaler + "/admin/scale", {"replicas": None, "model": model})


def _port(url: str) -> int:
    try:
        return int(url.rsplit(":", 1)[1].split("/")[0])
    except (IndexError, ValueError):
        return 0
