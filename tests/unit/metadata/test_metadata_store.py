import time

import pytest
from fastapi.testclient import TestClient

from packserve.common import config
from packserve.common.schemas import ReplicaRegistration, ReplicaState, StateDelta, TenantCreate
from packserve.metadata.app import create_app
from packserve.metadata.store import MetadataStore, hash_key


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def reg(i="r1"):
    return ReplicaRegistration(id=i, url=f"http://x/{i}", model="qwen3-8-27b", profile="virtual-blackwell-x4")


def test_register_fills_profile_limits():
    s = MetadataStore(clock=Clock())
    st = s.register(reg())
    assert st.max_running == 64 and st.kv_capacity_tokens == 400_000


def test_unhealthy_after_3s_without_heartbeat():
    c = Clock()
    s = MetadataStore(clock=c)
    s.register(reg())
    assert s.list_replicas()[0].healthy
    c.t += 2.9
    assert s.list_replicas()[0].healthy
    c.t += 0.2
    assert not s.list_replicas()[0].healthy
    s.heartbeat("r1", ReplicaState(id="r1", url="u", model="qwen3-8-27b", profile="p", updated_at=c.t))
    assert s.list_replicas()[0].healthy


def test_drain_survives_heartbeat_and_deregister():
    c = Clock()
    s = MetadataStore(clock=c)
    s.register(reg())
    s.drain("r1")
    s.heartbeat("r1", ReplicaState(id="r1", url="u", model="m", profile="p", updated_at=c.t))
    assert s.list_replicas()[0].draining
    assert s.deregister("r1") and not s.list_replicas()
    assert s.drain("r1") is None


def test_deltas_merge_until_heartbeat_catches_up():
    c = Clock()
    s = MetadataStore(clock=c)
    s.register(reg())
    s.heartbeat("r1", ReplicaState(id="r1", url="u", model="m", profile="p", running=2, updated_at=c.t))
    c.t += 0.1
    s.apply_delta(StateDelta(gateway_id="gw1", replica_id="r1", running_delta=3, kv_delta_tokens=500,
                             added_block_hashes=["a", "b"], ts=c.t))
    s.apply_delta(StateDelta(gateway_id="gw2", replica_id="r1", running_delta=-1, ts=c.t))
    v = s.list_replicas()[0]
    assert v.running == 4 and v.kv_used_tokens == 500 and v.cached_block_hashes == ["a", "b"]
    c.t += 0.5  # heartbeat taken after the deltas supersedes them
    s.heartbeat("r1", ReplicaState(id="r1", url="u", model="m", profile="p", running=4, updated_at=c.t))
    assert s.list_replicas()[0].running == 4
    assert not s.apply_delta(StateDelta(gateway_id="g", replica_id="ghost"))


def test_running_never_negative():
    s = MetadataStore(clock=Clock())
    s.register(reg())
    s.apply_delta(StateDelta(gateway_id="g", replica_id="r1", running_delta=-5, ts=s.clock()))
    assert s.list_replicas()[0].running == 0


def test_seed_tenants_and_hashed_keys():
    s = MetadataStore(clock=Clock())
    for t in config.SEED_TENANTS:
        assert s.tenant_by_key(t["api_key"]).id == t["id"]
    assert s.tenant_by_key("wrong") is None
    created = s.create_tenant(TenantCreate(name="acme", tpot_target_ms=30))
    assert s.tenant_by_key(created.api_key).id == created.id
    assert created.api_key not in repr(s.__dict__)  # plaintext never stored
    assert hash_key(created.api_key) in s._key_hash_to_tenant


def test_snapshot_roundtrip_keeps_hash_not_key(tmp_path):
    p = tmp_path / "m.json"
    s = MetadataStore(p)
    created = s.create_tenant(TenantCreate(name="acme"))
    assert created.api_key not in p.read_text()
    s2 = MetadataStore(p)
    assert s2.tenant_by_key(created.api_key).name == "acme"
    assert len([t for t in s2.list_tenants() if t.id == created.id]) == 1


def test_http_api():
    c = TestClient(create_app(None))
    assert c.post("/v1/replicas/register", json=reg().model_dump()).status_code == 200
    assert c.get("/v1/replicas").json()[0]["id"] == "r1"
    r = c.post("/v1/tenants", json={"name": "web", "tpot_target_ms": 30, "burst_enabled": False})
    body = r.json()
    assert body["api_key"].startswith("ps_")
    listed = c.get("/v1/tenants").json()
    assert all("api_key" not in t for t in listed)
    me = c.get("/v1/tenants/by-key", headers={"Authorization": f"Bearer {body['api_key']}"})
    assert me.status_code == 200 and me.json()["id"] == body["id"]
    assert c.get("/v1/tenants/by-key", headers={"Authorization": "Bearer nope"}).status_code == 401
    p = c.patch(f"/v1/tenants/{body['id']}", json={"burst_enabled": True, "bogus": 1})
    assert p.json()["burst_enabled"] is True
    assert c.patch("/v1/tenants/zzz", json={}).status_code == 404
    assert c.post("/v1/replicas/r1/drain").json()["draining"] is True
    assert c.post("/v1/replicas/zzz/drain").status_code == 404
    assert c.post("/v1/state/delta", json={"gateway_id": "g", "replica_id": "r1", "running_delta": 2}).json()["applied"]
    assert c.get("/v1/replicas").json()[0]["running"] == 2
    assert c.delete("/v1/replicas/r1").status_code == 200


def test_undrain():
    c = Clock()
    s = MetadataStore(clock=c)
    s.register(reg())
    s.drain("r1")
    # heartbeat from the replica must not clear the flag; undrain does
    s.heartbeat("r1", ReplicaState(id="r1", url="u", model="m", profile="p", updated_at=c.t))
    assert s.list_replicas()[0].draining
    assert not s.undrain("r1").draining
    s.heartbeat("r1", ReplicaState(id="r1", url="u", model="m", profile="p", updated_at=c.t))
    assert not s.list_replicas()[0].draining
    assert s.undrain("zzz") is None
    cl = TestClient(create_app(None))
    cl.post("/v1/replicas/register", json=reg().model_dump())
    cl.post("/v1/replicas/r1/drain")
    assert cl.post("/v1/replicas/r1/undrain").json()["draining"] is False
    assert cl.post("/v1/replicas/zzz/undrain").status_code == 404
