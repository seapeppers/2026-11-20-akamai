"""Scheduler is a pure function: each reason path, packing, eta, tiers, exclusions."""
import pytest

from packserve.common import config
from packserve.common.latency import BUILTIN, CorrectionFactor, LatencyModel
from packserve.common.schemas import Tenant
from packserve.gateway.scheduler import Features, ReplicaView, schedule

LM = LatencyModel(BUILTIN["virtual-blackwell-x4"])
HASHES = [f"h{i}" for i in range(30)]  # 30 blocks x 64 = 1920 tokens


def view(rid, *, running=0, queued=0, kv_used=0, ctx=0, model="qwen3-8-27b", healthy=True, draining=False,
         cached=0, max_running=64, kv_cap=400_000, correction=1.0):
    cf = CorrectionFactor(3)
    cf.f = [correction] * 3
    return ReplicaView(id=rid, model=model, lm=LM, correction=cf, healthy=healthy, draining=draining,
                       running=running, queued=queued, kv_used=kv_used, kv_capacity=kv_cap,
                       max_running=max_running, total_context=ctx, cache=set(HASHES[:cached]))


def tenant(tpot=50, burst=True, models=("qwen3-8-27b", "gemma-4-26b-a4b-it", "packserve-large-230b")):
    return Tenant(id="t", name="t", tpot_target_ms=tpot, burst_enabled=burst, models=list(models))


def feats(model="qwen3-8-27b", prompt=2000, max_tokens=100, hashes=HASHES, med=128):
    return Features(model, prompt, max_tokens, hashes, med)


def test_packing_prefers_most_decoding():
    d = schedule(feats(hashes=[]), [view("a", running=2), view("b", running=5), view("c", running=0)], tenant())
    assert (d.route, d.reason, d.replica_id) == ("replica", "packed", "b")
    assert (d.candidates.capacity, d.candidates.budget, d.candidates.tpot) == (3, 3, 3)


def test_packing_tiebreak_cached_tokens_then_queued_then_id():
    # equal decoding: more cached tokens wins (both within the budget)
    d = schedule(feats(), [view("a", running=3, cached=20), view("b", running=3, cached=29)], tenant())
    assert d.replica_id == "b"
    # equal decoding and cache: fewer queued wins
    d = schedule(feats(hashes=[]), [view("a", running=3, queued=2), view("b", running=3, queued=0)], tenant())
    assert d.replica_id == "b"
    # full tie: lowest id, deterministic
    d = schedule(feats(hashes=[]), [view("b", running=3), view("a", running=3)], tenant())
    assert d.replica_id == "a"


def test_decoding_beats_cache_when_both_inside_budget():
    # a: 15 blocks cached (inside eta), b: 29 blocks cached but less decoding -> packing prefers a
    d = schedule(feats(), [view("a", running=4, cached=15), view("b", running=1, cached=29)], tenant())
    assert (d.replica_id, d.candidates.budget) == ("a", 2)
    # a cold replica is cut by the budget even though it has the most decoding
    d = schedule(feats(), [view("a", running=4, cached=0), view("b", running=1, cached=29)], tenant())
    assert (d.replica_id, d.candidates.budget) == ("b", 1)


def test_eta_budget_excludes_cold_replica():
    reps = [view("hot", running=1, cached=29), view("cold", running=9, cached=0)]
    d = schedule(feats(), reps, tenant())
    assert d.replica_id == "hot" and d.candidates.budget == 1
    # a lax budget lets the busier cold replica pack
    d = schedule(feats(), reps, tenant(), eta=1.0)
    assert d.replica_id == "cold" and d.candidates.budget == 2


def test_eta_partial_hit_inside_budget():
    # 15 blocks cached: ratio ~0.49 -> inside eta=0.5, outside eta=0.3
    reps = [view("best", running=1, cached=29), view("mid", running=6, cached=15)]
    assert schedule(feats(), reps, tenant()).replica_id == "mid"
    assert schedule(feats(), reps, tenant(), eta=0.3).replica_id == "best"


def test_tpot_tiers_30_vs_50():
    # running=40, ctx=300k: ~37ms -> fits 50 tier, not 30 tier
    heavy = view("a", running=40, ctx=300_000)
    d50 = schedule(feats(hashes=[], prompt=100), [heavy], tenant(tpot=50))
    assert (d50.reason, d50.candidates.tpot) == ("packed", 1)
    d30 = schedule(feats(hashes=[], prompt=100), [heavy], tenant(tpot=30))
    assert d30.reason == "fallback" and d30.candidates.tpot == 0 and d30.candidates.budget == 1
    assert d30.predicted_tpot_ms > 30


def test_tpot_filter_prefers_lighter_replica_over_packing():
    heavy = view("a", running=40, ctx=300_000)
    light = view("b", running=2)
    d = schedule(feats(hashes=[], prompt=100), [heavy, light], tenant(tpot=30))
    assert (d.replica_id, d.reason) == ("b", "packed") and d.candidates.tpot == 1


def test_correction_factor_changes_prediction():
    r = view("a", running=10, correction=2.0)
    base = schedule(feats(hashes=[]), [view("a", running=10)], tenant())
    corr = schedule(feats(hashes=[]), [r], tenant())
    assert corr.predicted_tpot_ms > base.predicted_tpot_ms * 1.5
    assert corr.segment == base.segment


def test_fallback_is_cache_aware_least_loaded():
    both_slow = [view("a", running=45, ctx=400_000, queued=3), view("b", running=45, ctx=400_000, queued=0)]
    d = schedule(feats(hashes=[]), both_slow, tenant(tpot=30))
    assert (d.reason, d.replica_id) == ("fallback", "b")
    # cache pulls the choice toward a slightly busier replica
    reps = [view("a", running=45, ctx=400_000, cached=29), view("b", running=44, ctx=400_000, cached=0)]
    d = schedule(feats(), reps, tenant(tpot=30), eta=1.0)
    assert (d.reason, d.replica_id) == ("fallback", "a")


@pytest.mark.parametrize("kw", [dict(healthy=False), dict(draining=True), dict(model="gemma-4-26b-a4b-it"),
                                dict(running=64), dict(kv_used=399_950)])
def test_capacity_guard_excludes(kw):
    d = schedule(feats(hashes=[]), [view("a", **kw)], tenant(burst=False))
    assert d.route == "rejected" and d.candidates.capacity == 0
    d = schedule(feats(hashes=[]), [view("a", **kw), view("ok")], tenant(burst=False))
    assert d.replica_id == "ok" and d.candidates.capacity == 1


def test_draining_and_unhealthy_never_selected_even_if_best():
    reps = [view("drain", running=30, cached=29, draining=True), view("sick", running=30, healthy=False),
            view("ok", running=0)]
    assert schedule(feats(), reps, tenant()).replica_id == "ok"


def test_overflow_when_pool_full_and_burst_enabled():
    d = schedule(feats(hashes=[]), [view("a", running=64)], tenant(burst=True))
    assert (d.route, d.reason) == ("overflow", "overflow")


def test_no_replicas_overflows():
    assert schedule(feats(hashes=[]), [], tenant()).route == "overflow"


def test_burst_disabled_rejects():
    d = schedule(feats(hashes=[]), [view("a", running=64)], tenant(burst=False))
    assert (d.route, d.reason) == ("rejected", "burst_disabled")


def test_pool_only_model_never_overflows_even_if_burst_enabled():
    f = feats(model="packserve-large-230b", hashes=[])
    d = schedule(f, [view("a", model="packserve-large-230b", running=64)], tenant(burst=True))
    assert (d.route, d.reason) == ("rejected", "pool_only_model")
    d = schedule(f, [], tenant(burst=True))
    assert d.reason == "pool_only_model"


def test_overflow_unavailable_gives_no_capacity():
    d = schedule(feats(hashes=[]), [], tenant(burst=True), overflow_available=False)
    assert (d.route, d.reason) == ("rejected", "no_capacity")


def test_unknown_model_and_entitlement():
    assert schedule(feats(model="nope"), [view("a")], tenant()).reason == "unknown_model"
    d = schedule(feats(model="gemma-4-26b-a4b-it"), [view("a")], tenant(models=("qwen3-8-27b",)))
    assert d.reason == "unknown_model"


def test_cache_hit_reported_and_capped_at_prompt():
    d = schedule(feats(prompt=1000), [view("a", cached=29)], tenant())
    assert d.cache_hit_tokens == 1000


def test_decision_is_fast():
    import time
    reps = [view(f"r{i}", running=i, cached=i % 30, ctx=i * 1000) for i in range(16)]
    f, t = feats(), tenant()
    schedule(f, reps, t)
    t0 = time.perf_counter()
    n = 500
    for _ in range(n):
        schedule(f, reps, t)
    per_ms = (time.perf_counter() - t0) * 1000 / n
    assert per_ms < 2.0, per_ms
