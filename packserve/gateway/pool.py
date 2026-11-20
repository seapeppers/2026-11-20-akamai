"""Gateway-local view of the replica pool: optimistic accounting on dispatch/completion,
local prefix cache per replica, correction factors, merged with metadata snapshots."""
from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field

from packserve.common import config
from packserve.common.latency import BUILTIN, CorrectionFactor, LatencyModel, load_profile
from packserve.common.schemas import ReplicaState, StateDelta

from .scheduler import ReplicaView

CACHE_CAP = 8192  # blocks per replica


@dataclass
class _Pending:
    running: int = 0
    queued: int = 0
    kv: int = 0
    ctx: int = 0
    hashes: list[str] = field(default_factory=list)

    def empty(self) -> bool:
        return not (self.running or self.queued or self.kv or self.ctx or self.hashes)

    def merge(self, o: "_Pending") -> None:
        self.running += o.running
        self.queued += o.queued
        self.kv += o.kv
        self.ctx += o.ctx
        self.hashes += o.hashes


class Pool:
    def __init__(self, gateway_id: str):
        self.gateway_id = gateway_id
        self.views: dict[str, ReplicaView] = {}
        self.pending: dict[str, _Pending] = {}
        self.corrections: dict[str, CorrectionFactor] = {}
        self._lms: dict[str, LatencyModel] = {}

    def _lm(self, profile: str) -> LatencyModel:
        lm = self._lms.get(profile)
        if lm is None:
            try:
                p = load_profile(profile)
            except (KeyError, ValueError):
                p = BUILTIN[config.DEFAULT_PROFILE]
            lm = self._lms[profile] = LatencyModel(p)
        return lm

    def reset(self) -> None:
        self.corrections.clear()
        for v in self.views.values():
            v.cache = OrderedDict()
            v.correction = self._correction(v.id, v.lm)

    def _correction(self, rid: str, lm: LatencyModel) -> CorrectionFactor:
        c = self.corrections.get(rid)
        if c is None:
            c = self.corrections[rid] = CorrectionFactor(len(lm.p.beta_segments))
        return c

    # ---- merge with metadata snapshot ----
    def merge(self, states: list[ReplicaState]) -> None:
        seen = set()
        for s in states:
            seen.add(s.id)
            v = self.views.get(s.id)
            lm = self._lm(s.profile)
            if v is None:
                v = self.views[s.id] = ReplicaView(id=s.id, model=s.model, lm=lm,
                                                   correction=self._correction(s.id, lm), cache=OrderedDict())
            p = self.pending.get(s.id)
            v.url, v.model, v.profile, v.lm = s.url, s.model, s.profile, lm
            v.healthy, v.draining = s.healthy, s.draining
            v.max_running = s.max_running
            v.kv_capacity = s.kv_capacity_tokens
            # snapshot already contains flushed deltas; add what we dispatched since
            v.running = s.running + (p.running if p else 0)
            v.queued = s.queued + (p.queued if p else 0)
            v.kv_used = s.kv_used_tokens + (p.kv if p else 0)
            v.total_context = s.total_context_tokens + (p.ctx if p else 0)
            for h in s.cached_block_hashes:
                self._touch(v, h)
        for rid in list(self.views):
            if rid not in seen:
                del self.views[rid]
                self.pending.pop(rid, None)

    @staticmethod
    def _touch(v: ReplicaView, h: str) -> None:
        c = v.cache
        c[h] = None
        c.move_to_end(h)
        while len(c) > CACHE_CAP:
            c.popitem(last=False)

    # ---- optimistic accounting ----
    def on_dispatch(self, rid: str, prompt_tokens: int, need_tokens: int, hashes: list[str]) -> None:
        v = self.views.get(rid)
        if v is None:
            return
        v.running += 1
        v.kv_used += need_tokens
        v.total_context += prompt_tokens
        for h in hashes:
            self._touch(v, h)
        p = self.pending.setdefault(rid, _Pending())
        p.running += 1
        p.kv += need_tokens
        p.ctx += prompt_tokens
        p.hashes += hashes

    def on_complete(self, rid: str, prompt_tokens: int, need_tokens: int) -> None:
        p = self.pending.setdefault(rid, _Pending())
        p.running -= 1
        p.kv -= need_tokens
        p.ctx -= prompt_tokens
        v = self.views.get(rid)
        if v is not None:
            v.running = max(v.running - 1, 0)
            v.kv_used = max(v.kv_used - need_tokens, 0)
            v.total_context = max(v.total_context - prompt_tokens, 0)

    def take_deltas(self) -> tuple[list[StateDelta], dict[str, _Pending]]:
        taken = {rid: p for rid, p in self.pending.items() if not p.empty()}
        self.pending = {}
        now = time.time()
        return [StateDelta(gateway_id=self.gateway_id, replica_id=rid, running_delta=p.running,
                           queued_delta=p.queued, kv_delta_tokens=p.kv, context_delta_tokens=p.ctx,
                           added_block_hashes=list(dict.fromkeys(p.hashes)), ts=now)
                for rid, p in taken.items()], taken

    def restore(self, taken: dict[str, _Pending]) -> None:
        for rid, p in taken.items():
            self.pending.setdefault(rid, _Pending()).merge(p)

    def snapshot(self) -> list[dict]:
        return [{"id": v.id, "url": v.url, "model": v.model, "profile": v.profile, "healthy": v.healthy,
                 "draining": v.draining, "running": v.running, "queued": v.queued, "kv_used": v.kv_used,
                 "kv_capacity": v.kv_capacity, "max_running": v.max_running,
                 "total_context": v.total_context, "cached_blocks": len(v.cache),
                 "correction": list(v.correction.f)} for v in self.views.values()]
