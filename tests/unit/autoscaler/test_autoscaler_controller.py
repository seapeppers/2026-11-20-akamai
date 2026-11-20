import httpx
import respx

from packserve.autoscaler.controller import Controller
from packserve.autoscaler.policy import PoolConfig, ScalingParams
from packserve.autoscaler.providers import Provisioned

META, TELE = "http://meta.test", "http://tele.test"


class FakeProvider:
    name, hour_s = "fake", None

    def __init__(self):
        self.created, self.deleted = [], []

    async def create(self, model, profile):
        self.created.append(model)
        return Provisioned(f"new-{len(self.created)}", "http://x")

    async def delete(self, rid):
        self.deleted.append(rid)

    async def list(self):
        return []

    async def cleanup(self):
        return []


def rs(id, running=0, queued=0, draining=False, created=1000.0):
    return {"id": id, "url": "http://x", "model": "m", "profile": "p", "running": running,
            "queued": queued, "draining": draining, "created_at": created, "max_running": 10}


def make(params=None, pool=None):
    prov = FakeProvider()
    c = Controller(prov, [pool or PoolConfig("m", 1, 4)], params or ScalingParams(billing_hold=False),
                   metadata_url=META, telemetry_url=TELE)
    return c, prov


def mocks(replicas, by_reason=None):
    respx.get(f"{META}/v1/replicas").mock(return_value=httpx.Response(200, json=replicas))
    respx.get(f"{TELE}/v1/rollup").mock(return_value=httpx.Response(200, json={"by_reason": by_reason or {}}))
    drain = respx.post(url__regex=rf"{META}/v1/replicas/.+/drain").mock(return_value=httpx.Response(200, json={}))
    dele = respx.delete(url__regex=rf"{META}/v1/replicas/.+").mock(return_value=httpx.Response(200, json={}))
    return drain, dele


@respx.mock
async def test_scale_up_on_overflow_pressure():
    mocks([rs("a", running=3)], {"packed": 40, "overflow": 10})
    c, prov = make()
    await c.tick(now=100)
    assert prov.created == ["m"]
    assert "scale_up" in c.last_action


@respx.mock
async def test_pending_replica_blocks_double_scale_up():
    mocks([rs("a", running=3)], {"packed": 40, "overflow": 10})
    c, prov = make(ScalingParams(billing_hold=False, up_cooldown_s=0))
    await c.tick(now=100)
    await c.tick(now=102)   # new-1 not registered yet: still counted, but pressure persists
    assert len(prov.created) == 2  # allowed up to max; each tick adds one, never a burst
    assert c.snapshot().pools["m"]["pending"]


@respx.mock
async def test_scale_down_drains_least_loaded_after_sustained_low_util():
    drain, dele = mocks([rs("busy", running=2), rs("idle", running=0)])
    c, prov = make()
    for t in range(0, 70, 2):
        await c.tick(now=100 + t)
    assert drain.call_count == 1
    assert drain.calls[0].request.url.path.endswith("/idle/drain")
    assert not dele.called


@respx.mock
async def test_never_deletes_replica_with_running_requests():
    drain, dele = mocks([rs("a", running=1, draining=True), rs("b")])
    c, prov = make()
    for t in range(5):
        await c.tick(now=5000 + t)
    assert prov.deleted == [] and not dele.called


@respx.mock
async def test_delete_after_idle_ticks_provider_then_metadata():
    drain, dele = mocks([rs("a", draining=True), rs("b")])
    c, prov = make()
    await c.tick(now=5000)
    assert prov.deleted == []                      # only one idle observation
    await c.tick(now=5002)
    assert prov.deleted == ["a"] and dele.call_count == 1


@respx.mock
async def test_billing_hold_defers_delete_until_boundary():
    drain, dele = mocks([rs("a", draining=True, created=1000), rs("b")])
    c, prov = make(ScalingParams(billing_hold=True, sim_hour_s=120))
    await c.tick(now=1030)
    await c.tick(now=1032)
    assert prov.deleted == [] and "a" in c.snapshot().held_for_billing
    await c.tick(now=1116)
    assert prov.deleted == ["a"]


@respx.mock
async def test_pause_stops_scaling_and_manual_scale_overrides():
    mocks([rs("a", running=9)], {"overflow": 20})
    c, prov = make()
    c.paused = True
    await c.tick(now=100)
    assert prov.created == []
    c.paused = False
    c.set_manual(3)
    await c.tick(now=102)
    assert prov.created == ["m"]


@respx.mock
async def test_scale_up_revives_drained_replica_before_creating():
    mocks([rs("a", running=3), rs("held", draining=True, running=2)], {"packed": 40, "overflow": 10})
    undrain = respx.post(f"{META}/v1/replicas/held/undrain").mock(return_value=httpx.Response(200, json={}))
    c, prov = make()
    await c.tick(now=100)
    assert undrain.call_count == 1 and prov.created == []
