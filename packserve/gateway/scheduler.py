"""Pure admission/packing scheduler (PackServe, arXiv 2609.33224 section Admission).

No I/O, no clocks: inputs are request features, replica views, the tenant and the model
catalog; output is a Decision. Seven steps, see docs/implementation-plan.md (Gateway)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Container, Mapping

from packserve.common import config
from packserve.common.latency import CorrectionFactor, LatencyModel
from packserve.common.schemas import Candidates, Tenant
from packserve.common.tokens import cached_prefix_tokens

EPS = 1e-6
FALLBACK_ALPHA = 4.0  # weight of cache-hit ratio vs request count in the LMetric-style score


@dataclass
class ReplicaView:
    id: str
    model: str
    lm: LatencyModel
    correction: CorrectionFactor
    url: str = ""
    profile: str = ""
    healthy: bool = True
    draining: bool = False
    running: int = 0
    queued: int = 0
    kv_used: int = 0
    kv_capacity: int = 0
    max_running: int = 64
    total_context: int = 0
    cache: Container[str] = field(default_factory=set)  # block-hash set (chained hashes)

    @property
    def block_tokens(self) -> int:
        return self.lm.p.prefix_block_tokens


@dataclass
class Features:
    model: str
    prompt_tokens: int
    max_tokens: int
    block_hashes: list[str] = field(default_factory=list)
    median_output_tokens: int = 128


@dataclass
class Decision:
    route: str  # replica | overflow | rejected
    reason: str
    replica_id: str | None = None
    predicted_tpot_ms: float | None = None
    predicted_prefill_ms: float | None = None
    cache_hit_tokens: int = 0
    candidates: Candidates = field(default_factory=Candidates)
    segment: int | None = None  # batch segment used for the correction factor update


@dataclass
class _Cand:
    v: ReplicaView
    cached: int
    pf_ms: float
    tpot_ms: float = 0.0
    seg: int = 0


def schedule(f: Features, replicas: list[ReplicaView], tenant: Tenant,
             catalog: Mapping[str, bool] = config.MODELS, *,
             eta: float = config.RECOMPUTE_BUDGET_ETA, alpha: float = FALLBACK_ALPHA,
             overflow_available: bool = True) -> Decision:
    # unknown model, or one this tenant is not entitled to
    if f.model not in catalog or f.model not in tenant.models:
        return Decision("rejected", "unknown_model")
    burst_eligible = catalog[f.model]
    need = f.prompt_tokens + f.max_tokens

    # 1. capacity guard
    cands: list[_Cand] = []
    for v in replicas:
        if (v.healthy and not v.draining and v.model == f.model and v.running < v.max_running
                and v.kv_capacity - v.kv_used >= need):
            cached = cached_prefix_tokens(f.block_hashes, v.cache, v.block_tokens) if f.block_hashes else 0
            cached = min(cached, f.prompt_tokens)
            cands.append(_Cand(v, cached, v.lm.prefill_ms(f.prompt_tokens - cached, cached)))
    n_capacity = len(cands)

    if not cands:
        return _no_pool(tenant, burst_eligible, overflow_available, Candidates(capacity=0))

    # 2. recompute budget: keep candidates whose extra prefill vs the best is within eta of the
    #    full no-cache penalty
    pf_best = min(c.pf_ms for c in cands)
    budget = []
    for c in cands:
        pf_nocache = c.v.lm.prefill_ms(f.prompt_tokens, 0)
        if (c.pf_ms - pf_best) / max(pf_nocache - pf_best, EPS) <= eta:
            budget.append(c)
    n_budget = len(budget)

    # 3. TPOT filter against the tenant tier
    ok = []
    for c in budget:
        v = c.v
        c.seg = v.lm.segment(v.running + 1)
        c.tpot_ms = v.lm.predicted_tpot_ms(v.running, v.total_context, f.prompt_tokens, c.pf_ms,
                                           f.median_output_tokens, v.correction.get(c.seg))
        if c.tpot_ms <= tenant.tpot_target_ms:
            ok.append(c)
    counts = Candidates(capacity=n_capacity, budget=n_budget, tpot=len(ok))

    if ok:
        # 4. packing: most decoding, then most cached tokens, then fewest queued; id for determinism
        best = max(ok, key=lambda c: (c.v.running, c.cached, -c.v.queued, _neg_id(c.v.id)))
        return _pick(best, "packed", counts)

    # 5. cache-aware least-loaded fallback within the budget set (LMetric-style)
    def score(c: _Cand) -> float:
        ratio = c.cached / max(f.prompt_tokens, 1)
        return c.v.queued + c.v.running - alpha * ratio

    best = min(budget, key=lambda c: (score(c), c.v.id))
    c = best
    c.seg = c.v.lm.segment(c.v.running + 1)
    c.tpot_ms = c.v.lm.predicted_tpot_ms(c.v.running, c.v.total_context, f.prompt_tokens, c.pf_ms,
                                         f.median_output_tokens, c.v.correction.get(c.seg))
    return _pick(c, "fallback", counts)


def _neg_id(s: str) -> tuple:
    # max() with lowest id winning ties
    return tuple(-ord(ch) for ch in s)


def _pick(c: _Cand, reason: str, counts: Candidates) -> Decision:
    return Decision("replica", reason, c.v.id, c.tpot_ms, c.pf_ms, c.cached, counts, c.seg)


def _no_pool(tenant: Tenant, burst_eligible: bool, overflow_available: bool, counts: Candidates) -> Decision:
    # 6. overflow / 7. reject
    if not burst_eligible:
        return Decision("rejected", "pool_only_model", candidates=counts)
    if not tenant.burst_enabled:
        return Decision("rejected", "burst_disabled", candidates=counts)
    if not overflow_available:
        return Decision("rejected", "no_capacity", candidates=counts)
    return Decision("overflow", "overflow", candidates=counts)
