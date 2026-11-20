from packserve.autoscaler import policy
from packserve.autoscaler.policy import PoolConfig, ScalingParams
from packserve.common.schemas import ReplicaState

POOL = PoolConfig("qwen3-8-27b", 1, 4)
P = ScalingParams()


def rep(id, running=0, queued=0, draining=False, created=1000.0, model="qwen3-8-27b"):
    return ReplicaState(id=id, url=f"http://x/{id}", model=model, profile="p", running=running,
                        queued=queued, draining=draining, created_at=created, max_running=64)


# ---- scale up
def test_up_on_overflow_rate():
    assert policy.should_scale_up(POOL, 2, 0.10, 50, 0.3, P, 100, None)


def test_no_up_when_rate_below_threshold_or_few_requests():
    assert not policy.should_scale_up(POOL, 2, 0.04, 100, 0.3, P, 100, None)
    assert not policy.should_scale_up(POOL, 2, 0.50, 2, 0.3, P, 100, None)  # too few requests


def test_up_on_utilization():
    assert policy.should_scale_up(POOL, 2, 0.0, 0, 0.85, P, 100, None)


def test_up_respects_max_and_cooldown():
    assert not policy.should_scale_up(POOL, 4, 0.5, 100, 0.95, P, 100, None)
    assert not policy.should_scale_up(POOL, 2, 0.5, 100, 0.95, P, 100, last_up_ts=95)
    assert policy.should_scale_up(POOL, 2, 0.5, 100, 0.95, P, 100, last_up_ts=80)


def test_up_below_min_ignores_cooldown():
    assert policy.should_scale_up(POOL, 0, 0.0, 0, None, P, 100, last_up_ts=99) == "below_min"


# ---- scale down
def test_down_needs_sustained_low_util():
    assert not policy.should_scale_down(POOL, 3, 0.2, None, 0.0, 100, P)
    assert not policy.should_scale_down(POOL, 3, 0.2, 80, 0.0, 100, P)          # only 20s
    assert policy.should_scale_down(POOL, 3, 0.2, 40, 0.0, 100, P)              # 60s


def test_down_never_below_min_or_under_pressure():
    assert not policy.should_scale_down(POOL, 1, 0.1, 0, 0.0, 100, P)
    assert not policy.should_scale_down(POOL, 3, 0.1, 0, 0.02, 100, P)
    assert not policy.should_scale_down(POOL, 3, 0.5, 0, 0.0, 100, P)


def test_victim_is_least_loaded_newest_on_tie():
    rs = [rep("a", running=10), rep("b", running=1, queued=1), rep("c", running=3), rep("d", running=3, created=2000)]
    assert policy.pick_drain_victim(rs).id == "b"
    assert policy.pick_drain_victim([rep("x", running=3), rep("y", running=3, created=2000)]).id == "y"
    assert policy.pick_drain_victim([]) is None


# ---- billing hold
def test_billed_hour_end():
    assert policy.billed_hour_end(1000, 1010, 120) == 1120
    assert policy.billed_hour_end(1000, 1120.5, 120) == 1240
    assert policy.billed_hour_end(1000, 1000, 120) == 1120


def test_hold_waits_until_near_boundary():
    assert not policy.hold_elapsed(1000, 1030, P)       # 90 s to go
    assert not policy.hold_elapsed(1000, 1100, P)       # 20 s to go
    assert policy.hold_elapsed(1000, 1116, P)           # within the 5 s margin


def test_hold_disabled_releases_immediately():
    assert policy.hold_elapsed(1000, 1030, ScalingParams(billing_hold=False))


# ---- safety
def test_never_delete_with_running_or_queued_or_not_draining():
    off = ScalingParams(billing_hold=False)
    assert not policy.can_delete(rep("a", running=1, draining=True), 5000, 10, off)
    assert not policy.can_delete(rep("a", queued=1, draining=True), 5000, 10, off)
    assert not policy.can_delete(rep("a", draining=False), 5000, 10, off)


def test_delete_requires_consecutive_idle_ticks_and_hold():
    r = rep("a", draining=True)
    assert not policy.can_delete(r, 1116, 1, P)         # seen idle once only
    assert not policy.can_delete(r, 1030, 5, P)         # hold not elapsed
    assert policy.can_delete(r, 1116, 2, P)
