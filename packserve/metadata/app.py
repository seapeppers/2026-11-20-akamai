from __future__ import annotations

import os

from fastapi import Body, FastAPI, Header, HTTPException

from packserve.common.schemas import (
    ReplicaRegistration, ReplicaState, StateDelta, Tenant, TenantCreate, TenantCreated,
)

from .store import MetadataStore

DEFAULT_SNAPSHOT = "data/metadata.json"


def create_app(snapshot_path: str | None = None, store: MetadataStore | None = None) -> FastAPI:
    store = store or MetadataStore(snapshot_path)
    app = FastAPI(title="PackServe metadata (SIMULATED pool)")
    app.state.store = store

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.post("/v1/replicas/register", response_model=ReplicaState)
    def register(reg: ReplicaRegistration):
        return store.register(reg)

    @app.post("/v1/replicas/{replica_id}/heartbeat")
    def heartbeat(replica_id: str, state: ReplicaState):
        store.heartbeat(replica_id, state)
        return {"ok": True}

    @app.post("/v1/replicas/{replica_id}/drain", response_model=ReplicaState)
    def drain(replica_id: str):
        s = store.drain(replica_id)
        if s is None:
            raise HTTPException(404, "unknown replica")
        return s

    @app.post("/v1/replicas/{replica_id}/undrain", response_model=ReplicaState)
    def undrain(replica_id: str):
        s = store.undrain(replica_id)
        if s is None:
            raise HTTPException(404, "unknown replica")
        return s

    @app.delete("/v1/replicas/{replica_id}")
    def deregister(replica_id: str):
        if not store.deregister(replica_id):
            raise HTTPException(404, "unknown replica")
        return {"ok": True}

    @app.get("/v1/replicas", response_model=list[ReplicaState])
    def replicas():
        return store.list_replicas()

    @app.post("/v1/state/delta")
    def delta(d: StateDelta):
        return {"ok": True, "applied": store.apply_delta(d)}

    @app.post("/v1/tenants", response_model=TenantCreated)
    def create_tenant(body: TenantCreate):
        return store.create_tenant(body)

    @app.get("/v1/tenants", response_model=list[Tenant])
    def list_tenants():
        return store.list_tenants()

    @app.get("/v1/tenants/by-key", response_model=Tenant)
    def by_key(authorization: str | None = Header(default=None)):
        key = (authorization or "").removeprefix("Bearer ").strip()
        t = store.tenant_by_key(key) if key else None
        if t is None:
            raise HTTPException(401, "invalid api key")
        return t

    @app.patch("/v1/tenants/{tenant_id}", response_model=Tenant)
    def patch_tenant(tenant_id: str, patch: dict = Body(...)):
        t = store.patch_tenant(tenant_id, patch)
        if t is None:
            raise HTTPException(404, "unknown tenant")
        return t

    return app


def app_from_env() -> FastAPI:
    return create_app(os.environ.get("PACKSERVE_METADATA_SNAPSHOT", DEFAULT_SNAPSHOT) or None)
