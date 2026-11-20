"""PackServe latency model (arXiv 2609.33224): prefill, decode iteration, predicted TPOT,
and the online multiplicative correction factor. Used by the gateway to predict and by the
simulated replica to generate timings."""
from __future__ import annotations

import json
from pathlib import Path

from .config import CORRECTION_FACTOR_CLIP, CORRECTION_RATIO_CLIP, CORRECTION_WEIGHT
from .schemas import LatencyProfile

PROFILES_DIR = Path(__file__).resolve().parents[2] / "profiles"

# Built-in profiles. Coefficients are illustrative (hypothesis), not measured on Akamai GPUs.
BUILTIN: dict[str, LatencyProfile] = {
    "virtual-blackwell-x4": LatencyProfile(
        name="virtual-blackwell-x4", model="qwen3-8-27b",
        alpha=(8.0, 0.045, 0.0000012, 0.0000025),
        beta_segments=[(8, 10.0, 0.5, 0.00003), (32, 12.0, 0.4, 0.00003), (64, 14.0, 0.35, 0.00003)],
        max_running=64, kv_capacity_tokens=400_000,
    ),
    "virtual-blackwell-x8-large": LatencyProfile(
        name="virtual-blackwell-x8-large", model="packserve-large-230b",
        alpha=(20.0, 0.12, 0.000003, 0.000006),
        beta_segments=[(8, 18.0, 0.6, 0.00004), (32, 21.0, 0.4, 0.00004), (64, 25.0, 0.3, 0.00004)],
        max_running=48, kv_capacity_tokens=300_000,
    ),
}


def load_profile(name: str) -> LatencyProfile:
    path = PROFILES_DIR / f"{name}.json"
    if path.exists():
        return LatencyProfile.model_validate(json.loads(path.read_text()))
    return BUILTIN[name]


class LatencyModel:
    def __init__(self, profile: LatencyProfile):
        self.p = profile

    def prefill_ms(self, new_tokens: int, cached_tokens: int = 0) -> float:
        a0, a1, a2, a3 = self.p.alpha
        n, c = new_tokens, cached_tokens
        return a0 + a1 * n + a2 * n * c + a3 * n * n

    def segment(self, batch: int) -> int:
        for i, (max_b, *_rest) in enumerate(self.p.beta_segments):
            if batch <= max_b:
                return i
        return len(self.p.beta_segments) - 1

    def decode_iter_ms(self, batch: int, total_context: int) -> float:
        _, b0, b1, b2 = self.p.beta_segments[self.segment(batch)]
        return b0 + b1 * batch + b2 * total_context

    def predicted_tpot_ms(self, active: int, total_context: int, new_ctx: int,
                          new_prefill_ms: float, median_output_tokens: int,
                          correction: float = 1.0) -> float:
        """TPOT if a request joins: T_iter(A+1, S+ctx) plus the arriving prefill amortized
        over the recent median output length (omitted when the instance is idle)."""
        t = self.decode_iter_ms(active + 1, total_context + new_ctx) * correction
        if active > 0 and median_output_tokens > 0:
            t += new_prefill_ms / median_output_tokens
        return t


class CorrectionFactor:
    """Per-batch-segment multiplicative EMA of measured/predicted TPOT."""

    def __init__(self, segments: int):
        self.f = [1.0] * segments

    def get(self, seg: int) -> float:
        return self.f[seg]

    def update(self, seg: int, measured_ms: float, predicted_ms: float) -> float:
        if predicted_ms <= 0:
            return self.f[seg]
        lo, hi = CORRECTION_RATIO_CLIP
        ratio = min(max(measured_ms / predicted_ms, lo), hi)
        # predicted_ms already includes the current factor; ratio * f is the new target
        target = ratio * self.f[seg]
        flo, fhi = CORRECTION_FACTOR_CLIP
        self.f[seg] = min(max((1 - CORRECTION_WEIGHT) * self.f[seg] + CORRECTION_WEIGHT * target, flo), fhi)
        return self.f[seg]
