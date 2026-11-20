import numpy as np

from packserve.common.latency import BUILTIN, LatencyModel
from packserve.profiler.cli import SEGMENT_BOUNDS, compare, fit_decode, fit_prefill

REF = BUILTIN["virtual-blackwell-x4"]
LM = LatencyModel(REF)


def test_fit_decode_exact_recovery_noiseless():
    pts = [(b, b * L, LM.decode_iter_ms(b, b * L)) for b in (1, 2, 4, 8, 12, 16, 24, 32, 40, 48, 64) for L in (200, 2000)]
    segs = fit_decode(pts, SEGMENT_BOUNDS)
    for got, want in zip(segs, REF.beta_segments):
        np.testing.assert_allclose(got, want, rtol=1e-6)


def test_fit_prefill_exact_recovery_noiseless():
    pts = [(LM.prefill_ms(n, c), n, c) for n in (64, 256, 1024, 2048, 4096, 8192) for c in (0, 1024, 4096, 8192)]
    np.testing.assert_allclose(fit_prefill(pts), REF.alpha, rtol=1e-6)


def test_fit_with_noise_predicts_within_tolerance():
    rng = np.random.default_rng(0)
    pts = [(b, b * L, LM.decode_iter_ms(b, b * L) * rng.lognormal(0, 0.05)) for b in (1, 2, 4, 8, 12, 16, 24, 32, 40, 48, 64) for L in (200, 2000)]
    pp = [(LM.prefill_ms(n, c) * rng.lognormal(0, 0.05), n, c) for n in (64, 256, 1024, 2048, 4096, 8192) for c in (0, 1024, 4096, 8192)]
    from packserve.common.schemas import LatencyProfile
    fit = LatencyProfile(name="f", model="m", alpha=fit_prefill(pp), beta_segments=fit_decode(pts, SEGMENT_BOUNDS))
    text = "\n".join(compare(fit, REF))
    assert "prediction error" in text
    fm = LatencyModel(fit)
    for B in (1, 8, 16, 64):
        want = LM.decode_iter_ms(B, B * 1000)
        assert abs(fm.decode_iter_ms(B, B * 1000) - want) / want < 0.12
