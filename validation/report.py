"""PASS/FAIL table, JUnit XML and Markdown report writers."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from .engine import ScenarioResult


def table(results: list[ScenarioResult]) -> str:
    w = max([len(r.id) for r in results] + [8])
    lines = [f"{'SCENARIO'.ljust(w)}  {'STATUS':6}  {'TIME':>6}  CHECKS  DETAIL", "-" * (w + 60)]
    for r in results:
        bad = [c for c in r.checks if c.status == "FAIL"]
        warn = [c for c in r.checks if c.status == "WARN"]
        detail = r.error.splitlines()[0] if r.error else (bad[0].name + ": " + bad[0].detail[:80] if bad else "")
        lines.append(f"{r.id.ljust(w)}  {r.status:6}  {r.duration_s:5.1f}s  "
                     f"{len(r.checks) - len(bad) - len(warn)}/{len(r.checks)}"
                     f"{'+' + str(len(warn)) + 'w' if warn else ''}".ljust(w + 26) + f"  {detail}")
    counts = {s: sum(1 for r in results if r.status == s) for s in ("PASS", "FAIL", "ERROR", "SKIP")}
    lines.append("-" * (w + 60))
    lines.append("  ".join(f"{k}={v}" for k, v in counts.items()) + "   (stack is SIMULATED: no real GPUs)")
    return "\n".join(lines)


def junit_xml(results: list[ScenarioResult], suite: str = "packserve-validation") -> str:
    tests = len(results)
    fails = sum(1 for r in results if r.status == "FAIL")
    errs = sum(1 for r in results if r.status == "ERROR")
    skips = sum(1 for r in results if r.status == "SKIP")
    t = sum(r.duration_s for r in results)
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           f'<testsuites><testsuite name={quoteattr(suite)} tests="{tests}" failures="{fails}" errors="{errs}" '
           f'skipped="{skips}" time="{t:.3f}" timestamp="{dt.datetime.now().isoformat(timespec="seconds")}">']
    for r in results:
        out.append(f'<testcase classname="validation" name={quoteattr(r.id + " " + r.title)} time="{r.duration_s:.3f}">')
        if r.status == "SKIP":
            out.append(f"<skipped message={quoteattr(r.error or '')}/>")
        elif r.status == "ERROR":
            out.append(f"<error message={quoteattr((r.error or '').splitlines()[0][:300])}>{escape(r.error or '')}</error>")
        elif r.status == "FAIL":
            bad = [c for c in r.checks if c.status == "FAIL"]
            body = "\n".join(f"{c.name}: {c.detail}" for c in bad)
            out.append(f"<failure message={quoteattr(f'{len(bad)} check(s) failed: ' + bad[0].name)}>{escape(body)}</failure>")
        notes = "\n".join(r.notes)
        if notes:
            out.append(f"<system-out>{escape(notes)}</system-out>")
        out.append("</testcase>")
    out.append("</testsuite></testsuites>")
    return "\n".join(out)


def markdown(results: list[ScenarioResult], meta: dict) -> str:
    L = ["# PackServe routing validation report", "",
         "> **SIMULATED**: replicas are simulated (PackServe latency model + noise); serverless is a local mock "
         "unless `AKAMAI_BASE_URL` points elsewhere. Nothing here measures real GPU performance.", "",
         f"- Run: {meta.get('started')}  |  duration {meta.get('duration_s', 0):.0f}s",
         f"- Gateway: `{meta.get('gateway')}`  |  services up: {meta.get('alive')}",
         f"- Replicas at start: {meta.get('replicas')}", ""]
    L += ["| Scenario | Status | Time | Failed checks | Notes |", "|---|---|---|---|---|"]
    for r in results:
        bad = [c.name for c in r.checks if c.status == "FAIL"]
        L.append(f"| {r.id} {r.title} | **{r.status}** | {r.duration_s:.1f}s | {', '.join(bad) or '-'} | "
                 f"{(r.error or '').splitlines()[0][:80] if r.error else ''} |")
    L.append("")
    for r in results:
        L += [f"## {r.id}: {r.title} - {r.status}", "", f"*Intent:* {r.intent.strip()}", ""]
        if r.error:
            L += [f"**{r.status}:** `{r.error.splitlines()[0]}`", ""]
        if r.checks:
            L += ["| Check | Result | Detail |", "|---|---|---|"]
            for c in r.checks:
                L.append(f"| {c.name} | {c.status} | {c.detail.replace('|', '/')[:300]} |")
            L.append("")
        if r.groups:
            L += ["Traffic groups:", ""]
            for n, g in r.groups.items():
                L.append(f"- `{n}` n={g['n']}{' (direct to replica)' if g['direct'] else ''} status={g['status']} "
                         f"route={g['route']} reason={g['reason']} replica={g['replica']} complete={g['complete']}"
                         + (f" errors={g['errors']}" if g["errors"] else ""))
            L.append("")
        if r.notes:
            L += ["Notes:", ""] + [f"- {n}" for n in r.notes] + [""]
    return "\n".join(L)


def write_reports(results: list[ScenarioResult], meta: dict, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {"junit": out_dir / "junit.xml", "markdown": out_dir / "report.md", "json": out_dir / "results.json"}
    paths["junit"].write_text(junit_xml(results))
    paths["markdown"].write_text(markdown(results, meta))
    paths["json"].write_text(json.dumps(
        [{"id": r.id, "title": r.title, "status": r.status, "duration_s": round(r.duration_s, 2), "error": r.error,
          "notes": r.notes, "groups": r.groups,
          "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in r.checks]} for r in results],
        indent=2, default=str))
    return paths
