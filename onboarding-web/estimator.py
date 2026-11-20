"""Cost estimator math (pure functions; unit tested). All inputs are assumptions the user can edit."""
import math

BLACKWELL_X4_USD_HR = 12.0   # proposal2.md section 2: RTX PRO 6000 Blackwell x4
EGRESS_USD_PER_GB = 0.005    # GPU plans include no bundled transfer
HOURS_PER_MONTH = 730

# Assumed sustainable requests/s per x4 replica at a median-TPOT tier. SIMULATED-profile guess, not measured.
DEFAULT_RPS_PER_REPLICA = {30: 4.0, 50: 7.0}

DEFAULTS = {
    "tpot_target_ms": 50,
    "baseline_rps": 2.0,
    "peak_rps": 12.0,
    "peak_hours_per_day": 1.5,
    "tokens_per_request": 800,       # prompt + output
    "output_tokens": 300,
    "serverless_usd_per_m_tokens": 0.30,  # UNKNOWN: Akamai Inference is beta/free; per-M-token pricing not published
    "gpu_usd_hr": BLACKWELL_X4_USD_HR,
}


def estimate(p: dict) -> dict:
    """Compare 'reserve for peak' vs 'reserve for baseline (or chosen replicas) + serverless burst'."""
    d = {**DEFAULTS, **{k: v for k, v in p.items() if v is not None}}
    tier = 30 if float(d["tpot_target_ms"]) <= 30 else 50
    cap = float(d.get("rps_per_replica") or DEFAULT_RPS_PER_REPLICA[tier])
    base, peak = max(0.0, float(d["baseline_rps"])), max(float(d["baseline_rps"]), float(d["peak_rps"]))
    gpu = float(d["gpu_usd_hr"])
    burst_ok = bool(d.get("burst_enabled", True)) and bool(d.get("burst_eligible", True))

    peak_replicas = max(1, math.ceil(peak / cap))
    reserved = d.get("reserved_replicas")
    reserved = max(1, math.ceil(base / cap)) if not reserved else max(1, int(reserved))

    reserve_peak_month = peak_replicas * gpu * HOURS_PER_MONTH

    pool_rps = reserved * cap
    peak_frac = min(1.0, max(0.0, float(d["peak_hours_per_day"]) / 24.0))
    excess_rps = max(0.0, peak - pool_rps)
    # Requests above pool capacity during peak windows (only the excess, at peak duty cycle).
    month_s = HOURS_PER_MONTH * 3600
    excess_requests = excess_rps * month_s * peak_frac
    burst_tokens_m = excess_requests * float(d["tokens_per_request"]) / 1e6
    serverless_month = burst_tokens_m * float(d["serverless_usd_per_m_tokens"]) if burst_ok else 0.0
    reserved_month = reserved * gpu * HOURS_PER_MONTH
    burst_total = reserved_month + serverless_month
    rejected_rps = 0.0 if burst_ok else excess_rps

    total_requests = (base * (1 - peak_frac) + peak * peak_frac) * month_s
    egress_gb = total_requests * float(d["output_tokens"]) * 4 / 1e9
    egress_month = egress_gb * EGRESS_USD_PER_GB

    saving = reserve_peak_month - burst_total
    return {
        "tier_ms": tier,
        "rps_per_replica": cap,
        "burst_available": burst_ok,
        "peak": {"replicas": peak_replicas, "usd_month": round(reserve_peak_month, 2)},
        "burst": {
            "reserved_replicas": reserved,
            "reserved_usd_month": round(reserved_month, 2),
            "serverless_usd_month": round(serverless_month, 2),
            "serverless_m_tokens_month": round(burst_tokens_m, 2),
            "excess_rps_at_peak": round(excess_rps, 2),
            "unserved_rps_at_peak": round(rejected_rps, 2),
            "usd_month": round(burst_total, 2),
        },
        "egress_usd_month_both": round(egress_month, 4),
        "saving_usd_month": round(saving, 2),
        "saving_pct": round(100 * saving / reserve_peak_month, 1) if reserve_peak_month else 0.0,
        "assumptions": {
            "gpu_usd_hr": gpu, "hours_per_month": HOURS_PER_MONTH, "rps_per_replica": cap,
            "peak_hours_per_day": d["peak_hours_per_day"], "tokens_per_request": d["tokens_per_request"],
            "serverless_usd_per_m_tokens": d["serverless_usd_per_m_tokens"],
        },
    }
