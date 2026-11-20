import pytest

from validation.assertions import (aggregate, check_invariants, compare, evaluate_check, match, percentile,
                                   select)


def rec(**kw):
    base = dict(group="g", idx=0, status=200, route="replica", reason="packed", replica="r1", request_id="x",
                predicted_tpot_ms=20.0, cand_capacity=2, cand_budget=2, cand_tpot=1, complete=True,
                simulated=True, direct=False)
    base.update(kw)
    return base


def test_compare_ops_and_tol():
    assert compare(5, "<=", 4, tol=1)
    assert not compare(5, "<=", 4)
    assert compare(3, ">=", 4, tol=1)
    assert compare(2, "between", [1, 3])
    assert compare("a", "in", ["a", "b"])
    assert compare(None, "==", None)
    assert not compare(None, "<", 3)
    assert compare([1, 2], "contains", 2)
    assert compare(0, "exists", None)
    with pytest.raises(ValueError):
        compare(1, "~", 1)


def test_match_forms():
    r = rec(status=429)
    assert match(r, {"status": 429})
    assert match(r, {"status": [200, 429]})
    assert match(r, {"status": {">=": 400, "<": 500}})
    assert not match(r, {"route": "overflow"})


def test_percentile_aggregate():
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert aggregate([1, 2, 3], "mean") == 2
    assert aggregate([1, 2, 3], "p100") == 3
    assert aggregate([], "count") == 0
    with pytest.raises(ValueError):
        aggregate([], "min")


def test_select_slice_and_star_excludes_direct():
    groups = {"a": [rec(idx=i) for i in range(5)], "d": [rec(direct=True)]}
    assert len(select(groups, "*")) == 5
    assert [r["idx"] for r in select(groups, "a", slice_="-2:")] == [3, 4]
    assert len(select(groups, ["a", "d"])) == 6
    with pytest.raises(KeyError):
        select(groups, "nope")


def chk(check, groups, facts=None):
    return evaluate_check(check, groups, facts or {})


def test_count_none_all():
    g = {"a": [rec(), rec(route="overflow", reason="overflow", replica=None)]}
    assert chk({"type": "count", "on": "a", "where": {"route": "overflow"}, "value": 1}, g).ok
    assert not chk({"type": "none", "on": "a", "where": {"route": "overflow"}}, g).ok
    assert not chk({"type": "all", "on": "a", "expect": {"route": "replica"}}, g).ok
    assert chk({"type": "all", "on": "a", "expect": {"status": 200}}, g).ok
    assert not chk({"type": "all", "on": "a", "where": {"status": 500}, "expect": {"status": 200}}, g).ok


def test_stat_distinct_share_max_per_value():
    g = {"a": [rec(replica="r1", decision_us=u) for u in (100, 200, 300)] + [rec(replica="r2", decision_us=900)]}
    assert chk({"type": "stat", "on": "a", "field": "decision_us", "agg": "p50", "op": "<", "value": 500}, g).ok
    assert not chk({"type": "stat", "on": "a", "field": "decision_us", "agg": "max", "op": "<", "value": 500}, g).ok
    assert chk({"type": "distinct", "on": "a", "field": "replica", "value": 2}, g).ok
    assert chk({"type": "share", "on": "a", "field": "replica", "op": ">=", "value": 0.75}, g).ok
    assert chk({"type": "max_per_value", "on": "a", "field": "replica", "op": "<=", "value": 2, "tol": 1}, g).ok
    assert not chk({"type": "max_per_value", "on": "a", "field": "replica", "op": "<=", "value": 2}, g).ok


def test_in_set_negate_and_missing_group():
    g = {"a": [rec(replica="r1"), rec(replica="r2")]}
    assert chk({"type": "in_set", "on": "a", "field": "replica", "values": ["r3"], "negate": True}, g).ok
    assert not chk({"type": "in_set", "on": "a", "field": "replica", "values": ["r1"], "negate": True}, g).ok
    bad = chk({"type": "all", "on": "missing", "expect": {}}, g)
    assert not bad.ok and "KeyError" in bad.detail


def test_fact_and_compare_stat_and_warn():
    g = {"s": [rec(predicted_tpot_ms=v) for v in (10, 10, 20, 20)]}
    assert chk({"type": "fact", "path": "x.y[0]", "value": 3}, g, {"x": {"y": [3]}}).ok
    assert chk({"type": "fact", "path": "x", "op": "exists"}, g, {"x": 0}).ok
    cs = {"type": "compare_stat", "a": {"on": "s", "slice": "-2:", "field": "predicted_tpot_ms"},
          "b": {"on": "s", "slice": ":2", "field": "predicted_tpot_ms"}, "op": ">=", "value": 1.9}
    assert chk(cs, g).ok
    r = chk({"type": "count", "on": "s", "value": 99, "severity": "warn"}, g)
    assert not r.ok and r.status == "WARN"


def test_invariants_pass_and_fail():
    good = [rec(), rec(route="overflow", reason="overflow", replica=None, cand_capacity=0, cand_budget=0, cand_tpot=0,
                       predicted_tpot_ms=None),
            rec(status=429, route="rejected", reason="burst_disabled", replica=None, retry_after=1,
                cand_capacity=0, cand_budget=0, cand_tpot=0)]
    res = check_invariants(good, {}, {})
    assert res and all(c.ok for c in res), [c.detail for c in res if not c.ok]
    bad = [rec(status=200, route="rejected", reason="burst_disabled", replica=None)]
    res = check_invariants(bad, {}, {})
    assert any(not c.ok for c in res)
