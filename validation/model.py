"""Expected-behaviour thresholds derived from the *spec* latency model (packserve.common.latency),
independent of the gateway implementation. Scenario files reference these as `{{ calc.<name> }}`.

Gateway rule (implementation-plan.md, Scheduler step 3): a request is TPOT-admissible on a replica with
A active requests and S total context iff
    decode_iter_ms(A+1, S+ctx) * correction + [A>0] * prefill_ms / median_output_tokens  <=  target.
Unknowns (context actually counted per request, "recent median output length") are covered by
evaluating a small grid of assumptions and reporting the (lo, hi) range of the admissible batch.
"""
from __future__ import annotations

from itertools import product

from packserve.common.latency import LatencyModel, load_profile
from packserve.common.tokens import CHARS_PER_TOKEN

HOLD_PROMPT_CHARS = 400          # prompt used for "held" requests in scenarios (=100 tokens)
HOLD_MAX_TOKENS = 500            # long-running decode so requests stay in flight while we probe
ETA = 0.5


def admissible_batch(profile_name: str, target_ms: float, prompt_tokens: int, max_tokens: int,
                     median_outputs=(16, 64, 256)) -> tuple[int, int]:
    """(lo, hi): largest number of identical requests packable on ONE idle replica at `target_ms`,
    over the assumption grid. Capped at the profile's max_running (capacity guard)."""
    p = load_profile(profile_name)
    lm = LatencyModel(p)
    pf = lm.prefill_ms(prompt_tokens, 0)
    caps = []
    for ctx, med in product((prompt_tokens, prompt_tokens + max_tokens), median_outputs):
        cap = 0
        for b in range(1, p.max_running + 1):
            a = b - 1
            if lm.predicted_tpot_ms(a, a * ctx, ctx, pf, med) <= target_ms:
                cap = b
            else:
                break
        caps.append(cap)
    return min(caps), max(caps)


def recompute_ratio(profile_name: str, total_tokens: int, cached_tokens: int, best_cached_tokens: int) -> float:
    """(pf_c - pf_best) / max(pf_nocache - pf_best, eps) for a candidate with `cached_tokens` cached."""
    lm = LatencyModel(load_profile(profile_name))
    pf = lambda cached: lm.prefill_ms(max(total_tokens - cached, 0), cached)  # noqa: E731
    best, nocache = pf(best_cached_tokens), pf(0)
    return (pf(cached_tokens) - best) / max(nocache - best, 1e-9)


def build_calc(qwen_profile: str = "virtual-blackwell-x4") -> dict:
    p = load_profile(qwen_profile)
    ptok = HOLD_PROMPT_CHARS // CHARS_PER_TOKEN
    c30 = admissible_batch(qwen_profile, 30, ptok, HOLD_MAX_TOKENS)
    c50 = admissible_batch(qwen_profile, 50, ptok, HOLD_MAX_TOKENS)
    # eta scenario: 2048-token prompt = 32 blocks of 64 tokens
    total, blk = 32 * 64, 64
    b_blocks, c_blocks = 21, 8
    out = {
        "hold_prompt_chars": HOLD_PROMPT_CHARS,
        "hold_max_tokens": HOLD_MAX_TOKENS,
        "max_running": p.max_running,
        "cap30_lo": c30[0], "cap30_hi": c30[1],
        "cap50_lo": c50[0], "cap50_hi": c50[1],
        # direct load that is certainly over the 30 ms TPOT limit but under max_running
        "over30": p.max_running - 4,
        "over30_ok": c30[1] < p.max_running - 4,
        "eta": ETA,
        "prefix_blocks": 32,
        "b_blocks": b_blocks,
        "c_blocks": c_blocks,
        "ratio_b": recompute_ratio(qwen_profile, total, b_blocks * blk, total),
        "ratio_c": recompute_ratio(qwen_profile, total, c_blocks * blk, total),
    }
    out["eta_ok"] = out["ratio_b"] <= ETA - 0.1 and out["ratio_c"] >= ETA + 0.1
    return out
