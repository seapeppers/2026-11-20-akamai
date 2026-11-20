"""SQLite trace store and rollup math. Pure helpers are importable without a server."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from packserve.common.schemas import CompletionRecord, RouteDecision, Rollup

DEFAULT_DB = Path("data/traces.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
  request_id TEXT PRIMARY KEY, ts REAL NOT NULL, gateway_id TEXT, tenant_id TEXT,
  model TEXT, route TEXT, reason TEXT, replica_id TEXT,
  predicted_tpot_ms REAL, predicted_prefill_ms REAL, cache_hit_tokens INTEGER,
  prompt_tokens INTEGER, max_tokens INTEGER,
  cand_capacity INTEGER, cand_budget INTEGER, cand_tpot INTEGER,
  decision_us REAL, simulated INTEGER
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);
CREATE TABLE IF NOT EXISTS completions (
  request_id TEXT PRIMARY KEY, ts REAL NOT NULL, tenant_id TEXT, model TEXT,
  route TEXT, replica_id TEXT, ttft_ms REAL, tpot_ms REAL,
  output_tokens INTEGER, status INTEGER, simulated INTEGER
);
CREATE INDEX IF NOT EXISTS idx_completions_ts ON completions(ts);
"""

# Reasons that mean "the pool had no room for this request" (the autoscaler's pressure signal).
# unauthorized / unknown_model are client errors, not capacity.
PRESSURE_REASONS = ("overflow", "overflow_rejected", "no_capacity", "pool_only_model", "burst_disabled")
NON_CAPACITY_REASONS = ("unauthorized", "unknown_model")


def percentile(values: list[float], q: float) -> float | None:
    """Linear-interpolation percentile, q in [0, 100]. None for an empty list."""
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def pressure_rate(by_reason: dict[str, int]) -> tuple[float, int]:
    """(share of capacity-relevant requests that overflowed or were turned away, denominator)."""
    total = sum(n for r, n in by_reason.items() if r not in NON_CAPACITY_REASONS)
    if total == 0:
        return 0.0, 0
    return sum(by_reason.get(r, 0) for r in PRESSURE_REASONS) / total, total


def build_rollup(
    window_s: float,
    decisions: list[tuple[str, str]],      # (route, reason)
    tpots: list[float],
    ttfts: list[float],
    pool_utilization: float | None,
) -> Rollup:
    n = len(decisions)
    by_reason: dict[str, int] = {}
    overflow = rejected = 0
    for route, reason in decisions:
        by_reason[reason] = by_reason.get(reason, 0) + 1
        overflow += route == "overflow"
        rejected += route == "rejected"
    return Rollup(
        window_s=window_s,
        requests=n,
        by_reason=by_reason,
        overflow_share=overflow / n if n else 0.0,
        reject_share=rejected / n if n else 0.0,
        tpot_p50_ms=percentile(tpots, 50),
        tpot_p90_ms=percentile(tpots, 90),
        ttft_p50_ms=percentile(ttfts, 50),
        ttft_p90_ms=percentile(ttfts, 90),
        pool_utilization=pool_utilization,
    )


class TraceStore:
    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL") if self.path != ":memory:" else None
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def add_decision(self, d: RouteDecision) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (d.request_id, d.ts, d.gateway_id, d.tenant_id, d.model, d.route, d.reason,
                 d.replica_id, d.predicted_tpot_ms, d.predicted_prefill_ms, d.cache_hit_tokens,
                 d.prompt_tokens, d.max_tokens, d.candidates.capacity, d.candidates.budget,
                 d.candidates.tpot, d.decision_us, int(d.simulated)),
            )
            self._db.commit()

    def add_completion(self, c: CompletionRecord) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO completions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (c.request_id, c.ts, c.tenant_id, c.model, c.route, c.replica_id,
                 c.ttft_ms, c.tpot_ms, c.output_tokens, c.status, int(c.simulated)),
            )
            self._db.commit()

    def _query(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def decisions_since(self, since: float, model: str | None = None) -> list[tuple[str, str]]:
        sql, args = "SELECT route, reason FROM decisions WHERE ts >= ?", (since,)
        if model:
            sql, args = sql + " AND model = ?", (since, model)
        return self._query(sql, args)

    def completions_since(self, since: float, model: str | None = None) -> list[dict]:
        sql = ("SELECT ts, tenant_id, model, route, replica_id, ttft_ms, tpot_ms, output_tokens, status "
               "FROM completions WHERE ts >= ?")
        args: tuple = (since,)
        if model:
            sql, args = sql + " AND model = ?", (since, model)
        cols = ("ts", "tenant_id", "model", "route", "replica_id", "ttft_ms", "tpot_ms", "output_tokens", "status")
        return [dict(zip(cols, r)) for r in self._query(sql, args)]

    def rollup(self, window_s: float, pool_utilization: float | None = None,
               model: str | None = None, now: float | None = None) -> Rollup:
        since = (now if now is not None else time.time()) - window_s
        comps = self.completions_since(since, model)
        return build_rollup(
            window_s,
            self.decisions_since(since, model),
            [c["tpot_ms"] for c in comps if c["tpot_ms"] is not None],
            [c["ttft_ms"] for c in comps if c["ttft_ms"] is not None],
            pool_utilization,
        )


def mean_utilization(replicas: list[dict], model: str | None = None) -> float | None:
    """Mean running/max_running over healthy replicas (optionally for one model)."""
    xs = [r["running"] / max(r.get("max_running") or 1, 1) for r in replicas
          if r.get("healthy", True) and (model is None or r.get("model") == model)]
    return sum(xs) / len(xs) if xs else None
