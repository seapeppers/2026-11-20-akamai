"""Control loop: reads metadata + telemetry, applies policy, drives a Provider."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field

import httpx

from packserve.common import config
from packserve.common.schemas import AutoscalerState, ReplicaState
from packserve.autoscaler import policy
from packserve.autoscaler.policy import PoolConfig, ScalingParams
from packserve.autoscaler.providers import Provider
from packserve.telemetry.store import pressure_rate

log = logging.getLogger("packserve.autoscaler")

PENDING_TIMEOUT_S = 90.0


class AutoscalerStateExt(AutoscalerState):
    pools: dict[str, dict] = {}
    billing_hold: bool = True
    sim_hour_s: float = 120.0
    simulated: bool = True


@dataclass
class PoolRuntime:
    util_samples: deque = field(default_factory=lambda: deque(maxlen=600))   # (ts, util)
    low_since: float | None = None
    last_up_ts: float | None = None
    pending: dict[str, float] = field(default_factory=dict)                 # id -> created ts
    manual_target: int | None = None
    last_signals: dict = field(default_factory=dict)


class Controller:
    def __init__(self, provider: Provider, pools: list[PoolConfig], params: ScalingParams | None = None,
                 metadata_url: str | None = None, telemetry_url: str | None = None,
                 interval_s: float = 2.0, client: httpx.AsyncClient | None = None):
        self.provider = provider
        self.pools = {p.model: p for p in pools}
        self.params = params or ScalingParams()
        if provider.hour_s is not None:
            self.params.sim_hour_s = provider.hour_s
        self.metadata_url = metadata_url or config.METADATA_URL
        self.telemetry_url = telemetry_url or config.TELEMETRY_URL
        self.interval_s = interval_s
        self.http = client or httpx.AsyncClient(timeout=3.0)
        self.paused = False
        self.rt = {m: PoolRuntime() for m in self.pools}
        self.idle_ticks: dict[str, int] = {}
        self.held: set[str] = set()
        self.cleanup_on_exit = True
        self.last_action: str | None = None
        self.last_action_ts: float | None = None
        self.last_replicas: list[ReplicaState] = []
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ I/O
    async def _replicas(self) -> list[ReplicaState] | None:
        try:
            r = await self.http.get(f"{self.metadata_url}/v1/replicas")
            r.raise_for_status()
            return [ReplicaState.model_validate(x) for x in r.json()]
        except Exception as e:
            log.warning("metadata unavailable: %s", type(e).__name__)
            return None

    async def _pressure(self, model: str) -> tuple[float, int]:
        try:
            r = await self.http.get(f"{self.telemetry_url}/v1/rollup",
                                    params={"window_s": self.params.window_s, "model": model})
            r.raise_for_status()
            return pressure_rate(r.json().get("by_reason", {}))
        except Exception:
            return 0.0, 0

    def _record(self, msg: str, now: float) -> None:
        self.last_action, self.last_action_ts = msg, now
        log.info("autoscaler: %s", msg)

    # --------------------------------------------------------------- actions
    async def scale_up(self, pool: PoolConfig, reason: str, now: float,
                       draining: list[ReplicaState] | None = None) -> None:
        rt = self.rt[pool.model]
        # Prefer reviving a drained replica (already paid for, cache still warm) over creating one.
        if draining:
            r = max(draining, key=lambda x: (x.running + x.queued, x.created_at))
            try:
                resp = await self.http.post(f"{self.metadata_url}/v1/replicas/{r.id}/undrain")
                resp.raise_for_status()
                rt.last_up_ts = now
                self.held.discard(r.id)
                self._record(f"undrain {r.id} ({reason})", now)
                return
            except Exception as e:
                log.warning("undrain %s failed (%s); creating a new replica", r.id, type(e).__name__)
        try:
            prov = await self.provider.create(pool.model, pool.profile)
        except Exception as e:
            log.error("provider.create failed: %s", e)
            rt.last_up_ts = now  # back off via the cooldown
            return
        rt.pending[prov.id] = now
        rt.last_up_ts = now
        self._record(f"scale_up {pool.model} +{prov.id} ({reason})", now)

    async def drain(self, r: ReplicaState, why: str, now: float) -> None:
        try:
            resp = await self.http.post(f"{self.metadata_url}/v1/replicas/{r.id}/drain")
            resp.raise_for_status()
        except Exception as e:
            log.error("drain %s failed: %s", r.id, e)
            return
        self._record(f"drain {r.id} ({why})", now)

    async def release(self, r: ReplicaState, now: float) -> None:
        """provider.delete(), then deregister from metadata. Both are idempotent so a retry is safe."""
        try:
            await self.provider.delete(r.id)
            resp = await self.http.delete(f"{self.metadata_url}/v1/replicas/{r.id}")
            if resp.status_code not in (200, 204, 404):
                resp.raise_for_status()
        except Exception as e:
            log.error("release %s failed (will retry): %s", r.id, e)
            return
        self.idle_ticks.pop(r.id, None)
        self.held.discard(r.id)
        self._record(f"delete {r.id}", now)

    # ------------------------------------------------------------------ tick
    async def tick(self, now: float | None = None) -> None:
        async with self._lock:
            await self._tick(time.time() if now is None else now)

    async def _tick(self, now: float) -> None:
        replicas = await self._replicas()
        if replicas is None:
            return
        self.last_replicas = replicas
        known = {r.id for r in replicas}

        # 1. release drained replicas whose hold has elapsed (runs even while paused: it only
        #    removes capacity that is already out of rotation).
        self.held.clear()
        for r in replicas:
            if not r.draining:
                self.idle_ticks.pop(r.id, None)
                continue
            self.idle_ticks[r.id] = self.idle_ticks.get(r.id, 0) + 1 if policy.is_idle_draining(r) else 0
            if not policy.is_idle_draining(r):
                continue
            if policy.can_delete(r, now, self.idle_ticks[r.id], self.params):
                await self.release(r, now)
            else:
                self.held.add(r.id)

        # 2. per-pool scaling
        for model, pool in self.pools.items():
            rt = self.rt[model]
            for rid in list(rt.pending):
                if rid in known or now - rt.pending[rid] > PENDING_TIMEOUT_S:
                    del rt.pending[rid]
            mine = [r for r in replicas if r.model == model]
            active = [r for r in mine if not r.draining]
            draining = [r for r in mine if r.draining]
            n_active = len(active) + len(rt.pending)

            healthy = [r for r in active if r.healthy]
            util = (sum(r.running / max(r.max_running, 1) for r in healthy) / len(healthy)) if healthy else None
            if util is not None:
                rt.util_samples.append((now, util))
            while rt.util_samples and now - rt.util_samples[0][0] > self.params.window_s:
                rt.util_samples.popleft()
            win_util = (sum(u for _, u in rt.util_samples) / len(rt.util_samples)) if rt.util_samples else None
            pressure, requests = await self._pressure(model)
            rt.last_signals = {"util": win_util, "pressure": pressure, "requests": requests}

            if self.paused:
                continue

            if rt.manual_target is not None:
                await self._reconcile_manual(pool, rt, active, draining, n_active, now)
                continue

            reason = policy.should_scale_up(pool, n_active, pressure, requests, win_util,
                                            self.params, now, rt.last_up_ts)
            if reason:
                rt.low_since = None
                await self.scale_up(pool, reason, now, draining)
                continue

            if win_util is not None and win_util < self.params.down_util:
                rt.low_since = rt.low_since if rt.low_since is not None else now
            else:
                rt.low_since = None
            if not rt.pending and policy.should_scale_down(pool, n_active, win_util, rt.low_since,
                                                           pressure, now, self.params):
                victim = policy.pick_drain_victim(active)
                if victim:
                    rt.low_since = None   # require a fresh low period before the next one
                    await self.drain(victim, f"util {win_util:.0%} < {self.params.down_util:.0%}", now)

    async def _reconcile_manual(self, pool: PoolConfig, rt: PoolRuntime, active: list[ReplicaState],
                                draining: list[ReplicaState], n_active: int, now: float) -> None:
        target = max(0, min(rt.manual_target, pool.max))
        if n_active < target:
            await self.scale_up(pool, f"manual target {target}", now, draining)
        elif n_active > target and not rt.pending:
            victim = policy.pick_drain_victim(active)
            if victim:
                await self.drain(victim, f"manual target {target}", now)

    # ----------------------------------------------------------------- admin
    def set_manual(self, replicas: int | None, model: str | None = None) -> str:
        model = model or next(iter(self.pools))
        if model not in self.pools:
            raise KeyError(model)
        self.rt[model].manual_target = replicas
        return model

    def snapshot(self) -> AutoscalerStateExt:
        by_model: dict[str, dict] = {}
        active_ids, draining_ids = [], []
        for r in self.last_replicas:
            (draining_ids if r.draining else active_ids).append(r.id)
        for model, pool in self.pools.items():
            rt = self.rt[model]
            mine = [r for r in self.last_replicas if r.model == model]
            by_model[model] = {
                "min": pool.min, "max": pool.max, "profile": pool.profile,
                "active": sum(1 for r in mine if not r.draining),
                "draining": sum(1 for r in mine if r.draining),
                "pending": list(rt.pending),
                "manual_target": rt.manual_target,
                "signals": rt.last_signals,
            }
        target = sum(rt.manual_target if rt.manual_target is not None else
                     sum(1 for r in self.last_replicas if r.model == m and not r.draining)
                     for m, rt in self.rt.items())
        return AutoscalerStateExt(
            paused=self.paused, provider=self.provider.name, replicas=active_ids,
            draining=draining_ids, held_for_billing=sorted(self.held), target=target,
            min_replicas=sum(p.min for p in self.pools.values()),
            max_replicas=sum(p.max for p in self.pools.values()),
            last_action=self.last_action, last_action_ts=self.last_action_ts,
            pools=by_model, billing_hold=self.params.billing_hold,
            sim_hour_s=self.params.sim_hour_s, simulated=self.provider.name == "sim",
        )

    # ------------------------------------------------------------------ loop
    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("tick failed")
            await asyncio.sleep(self.interval_s)

    def start(self) -> None:
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.http.aclose()
