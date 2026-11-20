"""In-memory metadata store: replica registry with heartbeat expiry, gateway state deltas,
and the tenant registry (API keys stored as SHA-256 hashes only)."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Callable

from packserve.common import config
from packserve.common.latency import BUILTIN, load_profile
from packserve.common.schemas import (
    ReplicaRegistration, ReplicaState, StateDelta, Tenant, TenantCreate, TenantCreated,
)

HEARTBEAT_TIMEOUT_S = 3.0
DELTA_TTL_S = 10.0
HASH_CAP = 2048


def hash_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


class _Entry:
    __slots__ = ("state", "last_hb", "deltas", "hashes")

    def __init__(self, state: ReplicaState, now: float):
        self.state = state
        self.last_hb = now
        self.deltas: list[StateDelta] = []
        self.hashes: OrderedDict[str, None] = OrderedDict()


class MetadataStore:
    def __init__(self, snapshot_path: str | Path | None = None, clock: Callable[[], float] = time.time,
                 seed: bool = True):
        self.clock = clock
        self.snapshot_path = Path(snapshot_path) if snapshot_path else None
        self._lock = threading.RLock()
        self._replicas: dict[str, _Entry] = {}
        self._tenants: dict[str, Tenant] = {}
        self._key_hash_to_tenant: dict[str, str] = {}
        self._load_snapshot()
        if seed:
            self._seed()

    # ---------------- tenants ----------------
    def _seed(self) -> None:
        for t in config.SEED_TENANTS:
            tenant = Tenant(id=t["id"], name=t["name"], tpot_target_ms=t["tpot_target_ms"],
                            burst_enabled=t["burst_enabled"], models=list(t["models"]))
            self._tenants[tenant.id] = tenant
            self._key_hash_to_tenant[hash_key(t["api_key"])] = tenant.id

    def create_tenant(self, body: TenantCreate) -> TenantCreated:
        with self._lock:
            tid = "t-" + secrets.token_hex(4)
            api_key = "ps_" + secrets.token_urlsafe(24)
            tenant = Tenant(id=tid, **body.model_dump())
            self._tenants[tid] = tenant
            self._key_hash_to_tenant[hash_key(api_key)] = tid
            self._save_snapshot()
            return TenantCreated(**tenant.model_dump(), api_key=api_key)

    def list_tenants(self) -> list[Tenant]:
        with self._lock:
            return list(self._tenants.values())

    def tenant_by_key(self, api_key: str) -> Tenant | None:
        with self._lock:
            tid = self._key_hash_to_tenant.get(hash_key(api_key))
            return self._tenants.get(tid) if tid else None

    def patch_tenant(self, tenant_id: str, patch: dict) -> Tenant | None:
        with self._lock:
            cur = self._tenants.get(tenant_id)
            if cur is None:
                return None
            allowed = set(TenantCreate.model_fields)
            data = cur.model_dump()
            data.update({k: v for k, v in patch.items() if k in allowed})
            new = Tenant.model_validate(data)
            self._tenants[tenant_id] = new
            self._save_snapshot()
            return new

    # ---------------- snapshot ----------------
    def _save_snapshot(self) -> None:
        if not self.snapshot_path:
            return
        seed_ids = {t["id"] for t in config.SEED_TENANTS}
        hash_by_tenant = {tid: h for h, tid in self._key_hash_to_tenant.items()}
        data = {"tenants": [{"tenant": t.model_dump(), "key_hash": hash_by_tenant.get(t.id)}
                            for t in self._tenants.values() if t.id not in seed_ids]}
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.snapshot_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, self.snapshot_path)

    def _load_snapshot(self) -> None:
        if not self.snapshot_path or not self.snapshot_path.exists():
            return
        try:
            data = json.loads(self.snapshot_path.read_text())
            for item in data.get("tenants", []):
                t = Tenant.model_validate(item["tenant"])
                self._tenants[t.id] = t
                if item.get("key_hash"):
                    self._key_hash_to_tenant[item["key_hash"]] = t.id
        except Exception:  # corrupt snapshot must not block startup
            pass

    # ---------------- replicas ----------------
    def register(self, reg: ReplicaRegistration) -> ReplicaState:
        now = self.clock()
        try:
            prof = load_profile(reg.profile)
        except KeyError:
            prof = BUILTIN[config.DEFAULT_PROFILE]
        state = ReplicaState(id=reg.id, url=reg.url, model=reg.model, profile=reg.profile,
                             kv_capacity_tokens=prof.kv_capacity_tokens, max_running=prof.max_running,
                             created_at=now, updated_at=now)
        with self._lock:
            self._replicas[reg.id] = _Entry(state, now)
        return state

    def heartbeat(self, replica_id: str, state: ReplicaState) -> None:
        now = self.clock()
        with self._lock:
            e = self._replicas.get(replica_id)
            draining = state.draining or (e.state.draining if e else False)
            new = state.model_copy(update={"id": replica_id, "draining": draining, "updated_at": now})
            if e is None:  # metadata restarted: accept the replica's own report
                self._replicas[replica_id] = _Entry(new, now)
                return
            e.state = new
            e.last_hb = now
            # deltas older than this report are already reflected in it
            hb_ts = state.updated_at if 0 <= now - state.updated_at <= 2.0 else now
            e.deltas = [d for d in e.deltas if d.ts > hb_ts]

    def drain(self, replica_id: str) -> ReplicaState | None:
        with self._lock:
            e = self._replicas.get(replica_id)
            if e is None:
                return None
            e.state = e.state.model_copy(update={"draining": True})
            return self._view(e, self.clock())

    def undrain(self, replica_id: str) -> ReplicaState | None:
        with self._lock:
            e = self._replicas.get(replica_id)
            if e is None:
                return None
            e.state = e.state.model_copy(update={"draining": False})
            return self._view(e, self.clock())

    def deregister(self, replica_id: str) -> bool:
        with self._lock:
            return self._replicas.pop(replica_id, None) is not None

    def apply_delta(self, d: StateDelta) -> bool:
        with self._lock:
            e = self._replicas.get(d.replica_id)
            if e is None:
                return False
            e.deltas.append(d)
            for h in d.added_block_hashes:
                e.hashes[h] = None
                e.hashes.move_to_end(h)
            while len(e.hashes) > HASH_CAP:
                e.hashes.popitem(last=False)
            return True

    def _view(self, e: _Entry, now: float) -> ReplicaState:
        s = e.state
        e.deltas = [d for d in e.deltas if now - d.ts < DELTA_TTL_S]
        run = s.running + sum(d.running_delta for d in e.deltas)
        que = s.queued + sum(d.queued_delta for d in e.deltas)
        kv = s.kv_used_tokens + sum(d.kv_delta_tokens for d in e.deltas)
        ctx = s.total_context_tokens + sum(d.context_delta_tokens for d in e.deltas)
        hashes = list(dict.fromkeys([*s.cached_block_hashes, *e.hashes]))[-HASH_CAP:]
        healthy = s.healthy and (now - e.last_hb) <= HEARTBEAT_TIMEOUT_S
        return s.model_copy(update={"running": max(run, 0), "queued": max(que, 0),
                                    "kv_used_tokens": max(kv, 0), "total_context_tokens": max(ctx, 0),
                                    "cached_block_hashes": hashes, "healthy": healthy})

    def list_replicas(self) -> list[ReplicaState]:
        now = self.clock()
        with self._lock:
            return [self._view(e, now) for e in self._replicas.values()]
