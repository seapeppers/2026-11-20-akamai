"""Pure assertion logic: evaluates scenario `check:` entries against collected request records.

A *record* is a flat dict describing one request (see load.new_record): status, route, reason,
replica, predicted_tpot_ms, cand_capacity/cand_budget/cand_tpot, retry_after, ttft_ms, chunks,
complete, decision_us, cache_hit_tokens, ... Records live in named *groups*.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable

from .paths import extract

CHECK_TYPES = {"count", "none", "all", "stat", "distinct", "share", "max_per_value", "in_set",
               "fact", "compare_stat"}


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    severity: str = "fail"  # "fail" | "warn": a failed warn check does not fail the scenario

    @property
    def status(self) -> str:
        return "PASS" if self.ok else ("WARN" if self.severity == "warn" else "FAIL")


# ------------------------------------------------------------------ comparison helpers

def _isnum(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def compare(actual: Any, op: str, expected: Any, tol: float = 0.0) -> bool:
    """`tol` loosens the comparison in the favourable direction (it never tightens it)."""
    if op == "exists":
        return actual is not None
    if op == "truthy":
        return bool(actual)
    if actual is None:
        return op == "==" and expected is None
    if op == "==":
        return abs(actual - expected) <= tol if _isnum(actual) and _isnum(expected) else actual == expected
    if op == "!=":
        return abs(actual - expected) > tol if _isnum(actual) and _isnum(expected) else actual != expected
    if op == "<":
        return actual < expected + tol
    if op == "<=":
        return actual <= expected + tol
    if op == ">":
        return actual > expected - tol
    if op == ">=":
        return actual >= expected - tol
    if op == "between":
        lo, hi = expected
        return lo - tol <= actual <= hi + tol
    if op == "in":
        return actual in expected
    if op == "not_in":
        return actual not in expected
    if op == "contains":
        return expected in actual
    if op == "not_contains":
        return expected not in actual
    raise ValueError(f"unknown op {op!r}")


def match(rec: dict, where: dict | None) -> bool:
    """where: {field: scalar | [any-of] | {op: value, ...}}"""
    for k, want in (where or {}).items():
        have = rec.get(k)
        if isinstance(want, dict):
            if not all(compare(have, op, v) for op, v in want.items()):
                return False
        elif isinstance(want, list):
            if have not in want:
                return False
        elif have != want:
            return False
    return True


def percentile(vals: list[float], p: float) -> float:
    s = sorted(vals)
    if not s:
        raise ValueError("no values")
    k = (len(s) - 1) * p / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] if lo == hi else s[lo] + (s[hi] - s[lo]) * (k - lo)


def aggregate(vals: list[float], agg: str) -> float:
    if agg == "count":
        return len(vals)
    if not vals:
        raise ValueError("no values")
    if agg == "min":
        return min(vals)
    if agg == "max":
        return max(vals)
    if agg == "mean":
        return sum(vals) / len(vals)
    if agg == "sum":
        return sum(vals)
    m = re.fullmatch(r"p(\d+(?:\.\d+)?)", agg)
    if m:
        return percentile(vals, float(m.group(1)))
    raise ValueError(f"unknown agg {agg!r}")


Groups = dict[str, list[dict]]


def select(groups: Groups, on: Any = "*", where: dict | None = None, slice_: str | None = None) -> list[dict]:
    if on in ("*", None):
        names = [n for n, recs in groups.items() if not (recs and recs[0].get("direct"))]
    elif isinstance(on, str):
        names = [on]
    else:
        names = list(on)
    recs: list[dict] = []
    for n in names:
        if n not in groups:
            raise KeyError(f"unknown group {n!r} (have {sorted(groups)})")
        recs.extend(groups[n])
    recs = [r for r in recs if match(r, where)]
    if slice_:
        a, _, b = slice_.partition(":")
        recs = recs[int(a) if a else None: int(b) if b else None]
    return recs


def _values(recs: Iterable[dict], fld: str) -> list:
    return [r[fld] for r in recs if r.get(fld) is not None]


def _fmt(x: Any) -> str:
    return f"{x:.4g}" if isinstance(x, float) else str(x)


def _expect_str(op: str, value: Any, tol: float) -> str:
    return f"{op} {_fmt(value)}" + (f" (tol {tol})" if tol else "")


def _dist(recs: list[dict], fld: str) -> str:
    return ", ".join(f"{k}={v}" for k, v in Counter(r.get(fld) for r in recs).most_common(8))


# ------------------------------------------------------------------ check evaluation

def evaluate_check(check: dict, groups: Groups, facts: dict) -> CheckResult:
    """Evaluate one already-rendered check dict."""
    typ = check["type"]
    name = check.get("name") or typ
    sev = check.get("severity", "fail")
    try:
        ok, detail = _EVAL[typ](check, groups, facts)
    except Exception as e:  # missing group / no data / bad path -> a failed check, not a crash
        return CheckResult(name, False, f"{type(e).__name__}: {e}", sev)
    return CheckResult(name, bool(ok), detail, sev)


def _sel(c: dict, groups: Groups) -> list[dict]:
    return select(groups, c.get("on", "*"), c.get("where"), c.get("slice"))


def _count(c, groups, facts):
    recs = _sel(c, groups)
    op, val, tol = c.get("op", "=="), c["value"], c.get("tol", 0)
    return compare(len(recs), op, val, tol), f"count={len(recs)} expected {_expect_str(op, val, tol)}"


def _none(c, groups, facts):
    recs = _sel(c, groups)
    extra = f" ({_dist(recs, c.get('field', 'replica'))})" if recs else ""
    return len(recs) == 0, f"matching records={len(recs)} expected 0{extra}"


def _all(c, groups, facts):
    recs = _sel(c, groups)
    if not recs and not c.get("allow_empty"):
        return False, "no records selected"
    bad = [r for r in recs if not match(r, c["expect"])]
    detail = f"{len(recs) - len(bad)}/{len(recs)} match {c['expect']}"
    if bad:
        detail += f"; violators: {_dist(bad, next(iter(c['expect'])))}"
    return not bad, detail


def _stat(c, groups, facts):
    recs = _sel(c, groups)
    vals = _values(recs, c["field"])
    agg, op, val, tol = c.get("agg", "mean"), c.get("op", "=="), c["value"], c.get("tol", 0)
    if not vals:
        return False, f"no values for field {c['field']!r} in {len(recs)} records"
    got = aggregate(vals, agg)
    return compare(got, op, val, tol), f"{agg}({c['field']})={_fmt(got)} n={len(vals)} expected {_expect_str(op, val, tol)}"


def _distinct(c, groups, facts):
    recs = _sel(c, groups)
    vals = set(_values(recs, c["field"]))
    op, val, tol = c.get("op", "=="), c["value"], c.get("tol", 0)
    return compare(len(vals), op, val, tol), f"distinct {c['field']}={len(vals)} {sorted(map(str, vals))[:6]} expected {_expect_str(op, val, tol)}"


def _share(c, groups, facts):
    recs = [r for r in _sel(c, groups) if r.get(c["field"]) is not None]
    if not recs:
        return False, f"no records with {c['field']!r}"
    top = Counter(r[c["field"]] for r in recs).most_common(1)[0]
    got = top[1] / len(recs)
    op, val, tol = c.get("op", ">="), c["value"], c.get("tol", 0)
    return compare(got, op, val, tol), f"max share={got:.3f} ({top[0]}: {top[1]}/{len(recs)}) expected {_expect_str(op, val, tol)}; dist: {_dist(recs, c['field'])}"


def _max_per_value(c, groups, facts):
    recs = [r for r in _sel(c, groups) if r.get(c["field"]) is not None]
    if not recs:
        return False, f"no records with {c['field']!r}"
    top = Counter(r[c["field"]] for r in recs).most_common(1)[0]
    op, val, tol = c.get("op", "<="), c["value"], c.get("tol", 0)
    return compare(top[1], op, val, tol), f"max requests on one {c['field']}={top[1]} ({top[0]}) expected {_expect_str(op, val, tol)}; dist: {_dist(recs, c['field'])}"


def _in_set(c, groups, facts):
    recs = _sel(c, groups)
    if not recs and not c.get("allow_empty"):
        return False, "no records selected"
    allowed, negate = set(c["values"]), bool(c.get("negate"))
    bad = [r for r in recs if (r.get(c["field"]) in allowed) == negate]
    word = "outside" if negate else "inside"
    return not bad, f"{len(bad)}/{len(recs)} records have {c['field']} {word} {sorted(map(str, allowed))}; dist: {_dist(recs, c['field'])}"


def _fact(c, groups, facts):
    got = extract(facts, c["path"])
    op, tol, val = c.get("op", "=="), c.get("tol", 0), c.get("value")
    exp = op if op in ("exists", "truthy") else _expect_str(op, val, tol)
    return compare(got, op, val, tol), f"{c['path']} = {str(_fmt(got))[:200]} expected {exp}"


def _stat_spec(spec: dict, groups: Groups) -> float:
    recs = select(groups, spec.get("on", "*"), spec.get("where"), spec.get("slice"))
    return aggregate(_values(recs, spec["field"]), spec.get("agg", "mean"))


def _compare_stat(c, groups, facts):
    a, b = _stat_spec(c["a"], groups), _stat_spec(c["b"], groups)
    mode = c.get("mode", "ratio")
    got = a / b if mode == "ratio" else a - b
    op, val, tol = c.get("op", ">="), c["value"], c.get("tol", 0)
    return compare(got, op, val, tol), f"{mode}: a={_fmt(a)} b={_fmt(b)} -> {_fmt(got)} expected {_expect_str(op, val, tol)}"


_EVAL = {"count": _count, "none": _none, "all": _all, "stat": _stat, "distinct": _distinct,
         "share": _share, "max_per_value": _max_per_value, "in_set": _in_set, "fact": _fact,
         "compare_stat": _compare_stat}


# ------------------------------------------------------------------ global invariants

LEGAL = {
    ("replica", "packed"), ("replica", "fallback"), ("overflow", "overflow"),
    ("rejected", "overflow_rejected"), ("rejected", "no_capacity"), ("rejected", "pool_only_model"),
    ("rejected", "burst_disabled"), ("rejected", "unauthorized"), ("rejected", "unknown_model"),
}
STATUS_FOR = {"unauthorized": 401, "unknown_model": 404}
REJECT_429 = {"overflow_rejected", "no_capacity", "pool_only_model", "burst_disabled"}


def check_invariants(records: list[dict], tpot_targets: dict[str, float] | None = None,
                     replica_models: dict[str, str] | None = None) -> list[CheckResult]:
    """Properties that must hold for every gateway-routed request in every scenario (contracts.md)."""
    tpot_targets = tpot_targets or {}
    replica_models = replica_models or {}
    out: list[CheckResult] = []

    def rule(name: str, bad: list[str], total: int):
        out.append(CheckResult(f"invariant:{name}", not bad,
                               f"{total - len(bad)}/{total} ok" + (f"; e.g. {bad[:3]}" if bad else "")))

    recs = [r for r in records if not r.get("direct") and r.get("status") is not None and not r.get("cancelled")]
    if not recs:
        return out
    n = len(recs)

    def ident(r: dict) -> str:
        return f"{r.get('group')}#{r.get('idx')}"

    rule("headers-present", [ident(r) for r in recs
                             if r.get("route") is None or r.get("reason") is None or not r.get("request_id")], n)
    rule("route-reason-legal", [f"{ident(r)}={r.get('route')}/{r.get('reason')}" for r in recs
                                if (r.get("route"), r.get("reason")) not in LEGAL], n)
    rule("rejections-are-429-with-retry-after",
         [f"{ident(r)} status={r['status']} retry_after={r.get('retry_after')}" for r in recs
          if r.get("reason") in REJECT_429 and not (r["status"] == 429 and r.get("retry_after") is not None)], n)
    rule("auth-and-unknown-model-status",
         [f"{ident(r)} status={r['status']}" for r in recs
          if r.get("reason") in STATUS_FOR and r["status"] != STATUS_FOR[r["reason"]]], n)
    rule("replica-header-iff-route-replica",
         [ident(r) for r in recs if (r.get("route") == "replica") != bool(r.get("replica"))], n)
    rule("success-statuses",
         [f"{ident(r)} status={r['status']} route={r.get('route')}" for r in recs
          if r.get("route") in ("replica", "overflow") and r["status"] >= 400 and not r.get("tolerate_errors")], n)
    cand = [r for r in recs if r.get("cand_capacity") is not None]
    rule("candidate-counts-monotone",
         [f"{ident(r)}={r['cand_capacity']},{r['cand_budget']},{r['cand_tpot']}" for r in cand
          if not (r["cand_capacity"] >= r["cand_budget"] >= r["cand_tpot"] >= 0)], len(cand))
    rule("packed-implies-tpot-candidate",
         [ident(r) for r in cand if r["reason"] == "packed" and r["cand_tpot"] < 1], len(cand))
    rule("fallback-implies-no-tpot-candidate",
         [ident(r) for r in cand if r["reason"] == "fallback" and not (r["cand_tpot"] == 0 and r["cand_budget"] >= 1)], len(cand))
    rule("overflow-implies-no-capacity",
         [ident(r) for r in cand if r["reason"] == "overflow" and r["cand_capacity"] != 0], len(cand))
    if tpot_targets:
        pk = [r for r in recs if r.get("reason") == "packed" and r.get("predicted_tpot_ms") is not None
              and r.get("tenant") in tpot_targets]
        rule("packed-predicted-tpot-within-target",
             [f"{ident(r)} {r['predicted_tpot_ms']}>{tpot_targets[r['tenant']]}" for r in pk
              if r["predicted_tpot_ms"] > tpot_targets[r["tenant"]] + 0.01], len(pk))
    if replica_models:
        rr = [r for r in recs if r.get("route") == "replica" and r.get("replica") in replica_models]
        rule("routed-replica-serves-requested-model",
             [f"{ident(r)} {r['replica']}={replica_models[r['replica']]} asked {r.get('model')}" for r in rr
              if replica_models[r["replica"]] != r.get("model")], len(rr))
    rule("simulated-header", [ident(r) for r in recs if r.get("simulated") is not True], n)
    joined = [r for r in recs if r.get("d_route") is not None]
    rule("header-matches-decision-log",
         [f"{ident(r)} hdr={r['route']}/{r['reason']}/{r.get('replica')} dec={r['d_route']}/{r['d_reason']}/{r.get('d_replica')}"
          for r in joined
          if (r["route"], r["reason"], r.get("replica")) != (r["d_route"], r["d_reason"], r.get("d_replica"))], len(joined))
    return out
