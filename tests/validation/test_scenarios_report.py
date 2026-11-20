import xml.etree.ElementTree as ET

import pytest

from validation.assertions import CheckResult
from validation.engine import ScenarioResult
from validation.report import junit_xml, markdown, table
from validation.scenario import ScenarioError, load_all, validate


def test_all_scenarios_valid_unique_and_enough():
    ss = load_all()
    assert len(ss) >= 12
    assert len({s.id for s in ss}) == len(ss)
    for s in ss:
        assert s.id.startswith("S") and s.path.stem == s.id


def test_validate_rejects_bad():
    assert validate({"id": "x", "title": "t", "intent": "i", "steps": [{"action": "nope"}]})
    assert validate({"id": "x", "title": "t", "intent": "i", "steps": [{"action": "check", "checks": [{"type": "stat"}]}]})
    assert validate({"id": "x", "title": "t", "intent": "i", "steps": [], "requires": {"bogus": 1}})
    assert not validate({"id": "x", "title": "t", "intent": "i", "steps": []})


def _results():
    def mk(id_, status, checks, err=None):
        return ScenarioResult(id=id_, title="T " + id_, intent="intent", status=status, duration_s=1.5,
                              checks=checks, error=err, notes=["n"], groups={})
    return [mk("S01", "PASS", [CheckResult("a", True, "ok")]),
            mk("S02", "FAIL", [CheckResult("b", False, "boom <&>")]),
            mk("S03", "ERROR", [], "Traceback\nValueError")]


def test_junit_and_table_and_markdown():
    rs = _results()
    root = ET.fromstring(junit_xml(rs))
    suite = root.find("testsuite")
    assert suite.get("tests") == "3" and suite.get("failures") == "1" and suite.get("errors") == "1"
    assert "S02" in table(rs) and "FAIL" in table(rs)
    assert "SIMULATED" in markdown(rs, {"started": "now"})


def test_unquoted_on_key_survives_yaml(tmp_path):
    from validation.scenario import load_file
    f = tmp_path / "S99-x.yaml"
    f.write_text("id: S99-x\ntitle: t\nintent: i\nsteps:\n  - {action: check, checks: [{type: count, on: a, value: 1}]}\n")
    assert load_file(f).steps[0]["checks"][0]["on"] == "a"
