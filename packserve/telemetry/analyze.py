"""Trace analysis: deterministic stats first, then ONE LLM call that only narrates them.

    uv run python -m packserve.telemetry.analyze --window 3600 [--dry-run]

The stats tables are authoritative. The LLM narrative is labeled as model-generated and gets
a number cross-check; if the call fails the stats report is still written.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path

import httpx

from packserve.common import config
from packserve.telemetry.store import DEFAULT_DB, TraceStore, percentile

LLM_MODEL = "qwen3-8-27b"
FALLBACK_TARGET_MS = 50.0


def tenant_targets() -> dict[str, float]:
    """tenant_id -> tpot_target_ms. Seed tenants first, then metadata if reachable."""
    targets = {t["id"]: float(t["tpot_target_ms"]) for t in config.SEED_TENANTS}
    try:
        r = httpx.get(f"{config.METADATA_URL}/v1/tenants", timeout=1.5)
        r.raise_for_status()
        for t in r.json():
            targets[t["id"]] = float(t["tpot_target_ms"])
    except Exception:
        pass
    return targets


def _pcts(xs: list[float]) -> dict[str, float | None]:
    return {"p50": percentile(xs, 50), "p90": percentile(xs, 90), "p99": percentile(xs, 99)}


def compute_stats(store: TraceStore, window_s: float, targets: dict[str, float],
                  now: float | None = None) -> dict:
    now = now if now is not None else time.time()
    since = now - window_s
    decisions = store._query(
        "SELECT route, reason, model, tenant_id FROM decisions WHERE ts >= ?", (since,))
    comps = store.completions_since(since)

    by_reason: dict[str, int] = defaultdict(int)
    by_route: dict[str, int] = defaultdict(int)
    for route, reason, _m, _t in decisions:
        by_reason[reason] += 1
        by_route[route] += 1
    n = len(decisions)

    # tier = the tenant's TPOT target; tenants with the same target share a tier
    tiers: dict[float, list[dict]] = defaultdict(list)
    for c in comps:
        tiers[targets.get(c["tenant_id"], FALLBACK_TARGET_MS)].append(c)

    tier_stats = []
    for target in sorted(tiers):
        rows = tiers[target]
        tpot = [r["tpot_ms"] for r in rows if r["tpot_ms"] is not None]
        ttft = [r["ttft_ms"] for r in rows if r["ttft_ms"] is not None]
        # SLO-miss minutes: a minute where the median TPOT of that tier exceeded its target
        minutes: dict[int, list[float]] = defaultdict(list)
        for r in rows:
            if r["tpot_ms"] is not None:
                minutes[int(r["ts"] // 60)].append(r["tpot_ms"])
        missed = sum(1 for xs in minutes.values() if percentile(xs, 50) > target)
        tier_stats.append({
            "tpot_target_ms": target,
            "requests": len(rows),
            "errors": sum(1 for r in rows if r["status"] >= 400),
            "tpot_ms": _pcts(tpot),
            "ttft_ms": _pcts(ttft),
            "within_target_share": (sum(1 for x in tpot if x <= target) / len(tpot)) if tpot else None,
            "minutes_observed": len(minutes),
            "slo_miss_minutes": missed,
        })

    return {
        "window_s": window_s,
        "generated_at": now,
        "simulated": config.SIMULATED,
        "decisions": n,
        "completions": len(comps),
        "by_reason": dict(sorted(by_reason.items(), key=lambda kv: -kv[1])),
        "by_route": dict(by_route),
        "overflow_share": by_route.get("overflow", 0) / n if n else 0.0,
        "reject_share": by_route.get("rejected", 0) / n if n else 0.0,
        "tiers": tier_stats,
    }


def _f(x: float | None, nd: int = 1) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def stats_markdown(s: dict) -> str:
    lines = [
        f"- Window: {s['window_s']:.0f} s; decisions: {s['decisions']}; completions: {s['completions']}",
        f"- Overflow share: {s['overflow_share']:.1%}; reject share: {s['reject_share']:.1%}",
        "",
        "### Routes by reason",
        "",
        "| reason | count |", "|---|---|",
    ]
    lines += [f"| {k} | {v} |" for k, v in s["by_reason"].items()] or ["| (none) | 0 |"]
    lines += ["", "### Latency by tenant tier (TPOT target)", "",
              "| target ms | reqs | errors | TPOT p50 | p90 | p99 | TTFT p50 | p90 | p99 | within target | SLO-miss min |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for t in s["tiers"]:
        w = t["within_target_share"]
        lines.append(
            f"| {t['tpot_target_ms']:.0f} | {t['requests']} | {t['errors']} | "
            f"{_f(t['tpot_ms']['p50'])} | {_f(t['tpot_ms']['p90'])} | {_f(t['tpot_ms']['p99'])} | "
            f"{_f(t['ttft_ms']['p50'])} | {_f(t['ttft_ms']['p90'])} | {_f(t['ttft_ms']['p99'])} | "
            f"{'n/a' if w is None else f'{w:.1%}'} | {t['slo_miss_minutes']}/{t['minutes_observed']} |")
    if not s["tiers"]:
        lines.append("| (no completions in window) |||||||||||")
    lines += ["", "SLO-miss minute: a one-minute bucket whose median TPOT exceeded the tier target. "
                  "Tail latency (p90/p99) is reported but is not what the scheduler targets."]
    return "\n".join(lines)


def llm_request(stats: dict) -> dict:
    return {
        "model": LLM_MODEL,
        "temperature": 0.2,
        "max_tokens": 400,
        "messages": [
            {"role": "system", "content": (
                "You narrate serving statistics for an operator. Use ONLY numbers that appear in the "
                "JSON given. Do not compute new numbers, do not invent causes you cannot see. "
                "Write 3-6 short bullets: SLO misses, overflow/reject pressure, and one or two tuning "
                "notes. Say the data is simulated if simulated=true.")},
            {"role": "user", "content": json.dumps(stats, sort_keys=True)},
        ],
    }


def call_llm(stats: dict, timeout_s: float = 30.0) -> str:
    headers = {}
    key = os.environ.get(config.SERVERLESS_API_KEY_ENV)  # never logged
    if key:
        headers["Authorization"] = f"Bearer {key}"
    r = httpx.post(f"{config.SERVERLESS_BASE_URL}/chat/completions", json=llm_request(stats),
                   headers=headers, timeout=timeout_s)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


_NUM = re.compile(r"\d+(?:\.\d+)?")


def unverified_numbers(narrative: str, stats_md: str, stats: dict) -> list[str]:
    known = set(_NUM.findall(stats_md)) | set(_NUM.findall(json.dumps(stats)))
    # tolerate percent forms of shares ("12.5%" printed as 12.5) already in the table
    return sorted({m for m in _NUM.findall(narrative) if m not in known})


def render_report(stats: dict, narrative: str | None, llm_note: str) -> str:
    sim = "SIMULATED data (replicas are simulated). " if stats["simulated"] else ""
    out = [
        "# PackServe trace analysis",
        "",
        f"Generated {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stats['generated_at']))}. {sim}",
        "",
        "## Statistics (authoritative, computed deterministically from data/traces.db)",
        "",
        stats_markdown(stats),
        "",
        "## LLM narrative (model-generated, NOT authoritative)",
        "",
    ]
    if narrative is None:
        out.append(f"No narrative: {llm_note}")
    else:
        out += [f"_Written by `{LLM_MODEL}` from the statistics above. If it disagrees with the tables, "
                f"the tables are right. Endpoint: {config.SERVERLESS_BASE_URL}_", "", narrative]
        if "127.0.0.1" in config.SERVERLESS_BASE_URL or "localhost" in config.SERVERLESS_BASE_URL:
            out += ["", "> SIMULATED: the endpoint is the local serverless mock, which returns placeholder text, not analysis."]
        bad = unverified_numbers(narrative, stats_markdown(stats), stats)
        if bad:
            out += ["", f"> Check: the narrative contains numbers not found in the statistics: {', '.join(bad)}."]
    return "\n".join(out) + "\n"


def run(window_s: float, db: str, out_dir: str, dry_run: bool) -> Path:
    store = TraceStore(db)
    stats = compute_stats(store, window_s, tenant_targets())
    narrative, note = None, ""
    if dry_run:
        note = "dry-run: LLM call skipped. Request that would be sent:\n\n```json\n" + \
               json.dumps(llm_request(stats), indent=2) + "\n```"
    else:
        try:
            narrative = call_llm(stats)
        except Exception as e:  # report still gets written
            note = f"LLM call failed ({type(e).__name__}); statistics above are unaffected."
    report = render_report(stats, narrative, note)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"analysis-{time.strftime('%Y%m%d-%H%M%S')}.md"
    path.write_text(report)
    return path


def main() -> None:
    p = argparse.ArgumentParser(description="Analyze PackServe traces")
    p.add_argument("--window", type=float, default=3600, help="seconds of history")
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--out-dir", default="reports")
    p.add_argument("--dry-run", action="store_true", help="compute stats, skip the LLM call")
    a = p.parse_args()
    path = run(a.window, a.db, a.out_dir, a.dry_run)
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
