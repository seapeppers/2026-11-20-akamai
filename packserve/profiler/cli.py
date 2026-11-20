"""`python -m packserve.profiler`: prefill grid + decode sweep + least-squares fit."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import numpy as np

from packserve.common.latency import BUILTIN, PROFILES_DIR, LatencyModel, load_profile
from packserve.common.schemas import LatencyProfile

from . import measure

SEGMENT_BOUNDS = [8, 32, 64]
BATCHES = [1, 2, 4, 8, 12, 16, 24, 32, 40, 48, 64]
QUICK_BATCHES = [1, 4, 8, 16, 32, 48, 64]
PROMPT_LENS = [128, 2048]
NEW_GRID = [64, 256, 1024, 2048, 4096, 8192]
CACHED_GRID = [0, 1024, 4096, 8192]  # multiples of the 64-token prefix block


def fit_decode(points: list[tuple[int, float, float]], bounds: list[int]) -> list[tuple[int, float, float, float]]:
    """points: (batch, total_context, iter_ms). Per segment: iter = b0 + b1*B + b2*S."""
    segs, lo = [], 0
    for hi in bounds:
        pts = [p for p in points if lo < p[0] <= hi]
        lo = hi
        if len(pts) < 3:
            raise SystemExit(f"need >=3 decode points for segment up to batch {hi}, got {len(pts)}")
        A = np.array([[1.0, b, s] for b, s, _ in pts])
        y = np.array([t for _, _, t in pts])
        A, y = A / y[:, None], np.ones_like(y)  # relative-error fit (multiplicative noise)
        # scale columns so the tiny S coefficient is conditioned
        scale = np.abs(A).max(axis=0)
        coef = np.linalg.lstsq(A / scale, y, rcond=None)[0] / scale
        segs.append((hi, float(coef[0]), float(coef[1]), float(coef[2])))
    return segs


def fit_prefill(points: list[tuple[float, int, int]]) -> tuple[float, float, float, float]:
    """points: (ms, new, cached). ms = a0 + a1*N + a2*N*C + a3*N^2."""
    A = np.array([[1.0, n, n * c, n * n] for _, n, c in points])
    y = np.array([ms for ms, _, _ in points])
    # weighted least squares: noise is multiplicative, so fit relative error (else big points dominate)
    A, y = A / y[:, None], np.ones_like(y)
    scale = np.abs(A).max(axis=0)
    coef = np.linalg.lstsq(A / scale, y, rcond=None)[0] / scale
    return tuple(float(c) for c in coef)  # type: ignore[return-value]


async def profile(url: str, model: str, quick: bool, api_key: str | None, log=print):
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(base_url=url.rstrip("/"), headers=headers, timeout=120) as c:
        # decode sweep first: its fit is needed to remove the first decode iteration from TTFT
        probe = await measure.stream_once(c, model, measure.make_text(PROMPT_LENS[0]), 16)
        tpot1 = (probe.token_times[-1] - probe.token_times[0]) * 1000 / max(1, len(probe.token_times) - 1)
        log(f"probe: ttft={probe.ttft_ms:.1f}ms tpot={tpot1:.1f}ms")
        dpoints = []
        for L in PROMPT_LENS:
            ttft_L = (await measure.stream_once(c, model, measure.make_text(L), 1)).ttft_ms
            for B in (QUICK_BATCHES if quick else BATCHES):
                if quick and L == PROMPT_LENS[1] and B not in (1, 8, 32, 64):
                    continue
                max_tokens = min(500, 40 + math.ceil(1.5 * B * ttft_L / max(tpot1, 1)))
                pt = await measure.decode_point(c, model, B, L, max_tokens)
                if pt is None:
                    log(f"decode B={B} L={L}: window too short, skipped")
                    continue
                dpoints.append((B, pt[1], pt[0]))
                log(f"decode B={B:>2} L={L:>4}: iter={pt[0]:.1f}ms ctx={pt[1]:.0f}")
        beta = fit_decode(dpoints, SEGMENT_BOUNDS)
        lm = LatencyModel(LatencyProfile(name="tmp", model=model, alpha=(0, 0, 0, 0), beta_segments=beta))
        ppoints = []
        for N in NEW_GRID:
            for C in CACHED_GRID:
                if quick and (N, C) not in {(64, 0), (1024, 0), (4096, 0), (8192, 0), (1024, 4096), (4096, 4096), (4096, 8192), (256, 1024)}:
                    continue
                reps = 5 if N <= 1024 else 3
                samples = []
                for _ in range(reps):
                    ms, n, cc = await measure.prefill_point(c, model, N, C)
                    samples.append(ms - lm.decode_iter_ms(1, n + cc))
                ppoints.append((sum(samples) / len(samples), n, cc))
                log(f"prefill new={n:>5} cached={cc:>5}: {ppoints[-1][0]:.1f}ms")
        alpha = fit_prefill(ppoints)
        # noise estimate: relative residual std of the decode fit
        res = [t / lm.decode_iter_ms(b, s) for b, s, t in dpoints]
        sigma = float(np.std(np.log(res))) if len(res) > 2 else 0.08
    return alpha, beta, sigma


def compare(fit: LatencyProfile, ref: LatencyProfile) -> list[str]:
    lines = ["coefficient            fitted         reference      rel.err"]
    def row(name, a, b):
        err = abs(a - b) / abs(b) if b else float("nan")
        lines.append(f"{name:<20} {a:>12.6g} {b:>14.6g} {err:>9.1%}")
    for i, (a, b) in enumerate(zip(fit.alpha, ref.alpha)):
        row(f"alpha{i}", a, b)
    for s, (f, r) in enumerate(zip(fit.beta_segments, ref.beta_segments)):
        for j, n in enumerate(("b0", "b1", "b2"), start=1):
            row(f"seg{s}(<= {r[0]}) {n}", f[j], r[j])
    # prediction error is what matters to the scheduler
    errs = []
    fm, rm = LatencyModel(fit), LatencyModel(ref)
    for B in (1, 4, 8, 16, 32, 64):
        for S in (B * 256, B * 1500):
            errs.append(abs(fm.decode_iter_ms(B, S) - rm.decode_iter_ms(B, S)) / rm.decode_iter_ms(B, S))
    lines.append(f"decode_iter prediction error over grid: mean {np.mean(errs):.1%}, max {np.max(errs):.1%}")
    errs = []
    for N in (256, 1024, 4096):
        for C in (0, 2048, 8192):
            errs.append(abs(fm.prefill_ms(N, C) - rm.prefill_ms(N, C)) / rm.prefill_ms(N, C))
    lines.append(f"prefill prediction error over grid: mean {np.mean(errs):.1%}, max {np.max(errs):.1%}")
    return lines


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_sim(reference: str):
    import uvicorn
    from packserve.replica_sim.app import create_app
    port = _free_port()
    app = create_app("selftest", "qwen3-8-27b", reference, port, register=False, seed=1)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}/v1", server


def main() -> None:
    ap = argparse.ArgumentParser(description="Profile an OpenAI-compatible endpoint and fit a LatencyProfile")
    ap.add_argument("--url", help="base URL incl. /v1, e.g. http://127.0.0.1:9001/v1")
    ap.add_argument("--model", default="qwen3-8-27b")
    ap.add_argument("--name", default="my-profile")
    ap.add_argument("--api-key", default=None, help="bearer token (prefer env PROFILER_API_KEY)")
    ap.add_argument("--max-running", type=int, default=64)
    ap.add_argument("--kv-capacity-tokens", type=int, default=400_000)
    ap.add_argument("--reference", help="built-in/known profile name to compare the fit against")
    ap.add_argument("--quick", action="store_true", help="fewer sweep points")
    ap.add_argument("--self-test", action="store_true", help="start an in-process SIMULATED replica and profile it")
    ap.add_argument("--out-dir", default=str(PROFILES_DIR))
    a = ap.parse_args()
    import os
    api_key = a.api_key or os.environ.get("PROFILER_API_KEY")

    server = None
    url, ref = a.url, a.reference
    if a.self_test:
        ref = ref or "virtual-blackwell-x4"
        url, server = _start_sim(ref)
        print(f"self-test against in-process SIMULATED replica ({ref})")
    if not url:
        ap.error("--url is required (or --self-test)")
    t0 = time.time()
    alpha, beta, sigma = asyncio.run(profile(url, a.model, a.quick, api_key))
    prof = LatencyProfile(name=a.name, model=a.model, alpha=alpha, beta_segments=beta, max_running=a.max_running,
                          kv_capacity_tokens=a.kv_capacity_tokens, noise_sigma=round(sigma, 4))
    out = Path(a.out_dir) / f"{a.name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(prof.model_dump_json(indent=2))
    print(f"\nwrote {out} in {time.time() - t0:.0f}s")
    if ref:
        print("\n".join(compare(prof, load_profile(ref))))
    if server:
        server.should_exit = True


if __name__ == "__main__":
    main()
