"""Scenario file loading and static validation (schema documented in validation/SCENARIOS.md)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .assertions import CHECK_TYPES

SCENARIO_DIR = Path(__file__).parent / "scenarios"

# action -> required keys
ACTIONS: dict[str, set[str]] = {
    "traffic": {"name", "model"},
    "direct_load": {"name"},
    "direct_prime": {"replica", "prompt"},
    "await": set(),
    "cancel": set(),
    "wait": set(),
    "fault": {"replica"},
    "metadata": {"op", "replica"},
    "autoscaler": {"op"},
    "serverless": set(),
    "observe": {"name"},
    "custom": {"hook"},
    "check": {"checks"},
}
CHECK_REQUIRED: dict[str, set[str]] = {
    "count": {"value"}, "none": set(), "all": {"expect"}, "stat": {"field", "value"},
    "distinct": {"field", "value"}, "share": {"field", "value"}, "max_per_value": {"field", "value"},
    "in_set": {"field", "values"}, "fact": {"path"}, "compare_stat": {"a", "b", "value"},
}
REQUIRES_KEYS = {"qwen_replicas", "gemma_replicas", "large_replicas", "gateway2", "autoscaler", "serverless"}


@dataclass
class Scenario:
    id: str
    title: str
    intent: str
    steps: list[dict]
    requires: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    invariants: bool = True
    timeout_s: float = 240
    path: Path | None = None
    raw: dict = field(default_factory=dict)


class ScenarioError(ValueError):
    pass


def validate(d: dict, where: str = "") -> list[str]:
    errs: list[str] = []
    for k in ("id", "title", "intent", "steps"):
        if k not in d:
            errs.append(f"{where}: missing '{k}'")
    for k in d.get("requires", {}) or {}:
        if k not in REQUIRES_KEYS:
            errs.append(f"{where}: unknown requires key {k!r}")
    for i, st in enumerate(d.get("steps", []) or []):
        a = st.get("action")
        if a not in ACTIONS:
            errs.append(f"{where}: step {i}: unknown action {a!r}")
            continue
        missing = ACTIONS[a] - set(st)
        if missing:
            errs.append(f"{where}: step {i} ({a}): missing {sorted(missing)}")
        if a == "check":
            for j, c in enumerate(st.get("checks", [])):
                t = c.get("type")
                if t not in CHECK_TYPES:
                    errs.append(f"{where}: step {i} check {j}: unknown type {t!r}")
                    continue
                miss = CHECK_REQUIRED[t] - set(c)
                if miss:
                    errs.append(f"{where}: step {i} check {j} ({t}): missing {sorted(miss)}")
    return errs


def _fix_on(x):
    """YAML 1.1 parses an unquoted `on:` key as boolean True; map it back to "on"."""
    if isinstance(x, dict):
        return {("on" if k is True else k): _fix_on(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_fix_on(v) for v in x]
    return x


def load_file(path: Path) -> Scenario:
    d = _fix_on(yaml.safe_load(path.read_text()))
    errs = validate(d, path.name)
    if errs:
        raise ScenarioError("; ".join(errs))
    return Scenario(id=d["id"], title=d["title"], intent=d["intent"], steps=d["steps"],
                    requires=d.get("requires") or {}, tags=d.get("tags") or [],
                    invariants=d.get("invariants", True), timeout_s=d.get("timeout_s", 240),
                    path=path, raw=d)


def load_all(directory: Path = SCENARIO_DIR) -> list[Scenario]:
    out = [load_file(p) for p in sorted(directory.glob("*.yaml"))]
    ids = [s.id for s in out]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise ScenarioError(f"duplicate scenario ids: {sorted(dup)}")
    return out
