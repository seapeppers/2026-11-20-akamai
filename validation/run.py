"""CLI: uv run python -m validation.run [--scenario ID ...] [--list] [--base-url URL] [--report-dir DIR]"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
import time
from pathlib import Path

from . import load
from .engine import ScenarioResult, run_scenario
from .report import table, write_reports
from .scenario import ScenarioError, load_all
from .stack import Stack, Urls


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m validation.run", description=__doc__)
    p.add_argument("--scenario", "-s", action="append", help="scenario id or prefix (repeatable), e.g. S03")
    p.add_argument("--tag", action="append", help="only scenarios with this tag")
    p.add_argument("--list", action="store_true", help="list scenarios and exit")
    p.add_argument("--validate", action="store_true", help="statically validate scenario files and exit")
    p.add_argument("--base-url", help="gateway URL (default: PACKSERVE_GATEWAY_URL or http://127.0.0.1:8080)")
    p.add_argument("--gateway2-url")
    p.add_argument("--metadata-url")
    p.add_argument("--autoscaler-url")
    p.add_argument("--serverless-url", help="serverless mock ADMIN base (no /v1)")
    p.add_argument("--telemetry-url")
    p.add_argument("--report-dir", default="reports/validation")
    p.add_argument("--sim-hour-s", type=float, default=120, help="autoscaler's compressed billing hour (default 120)")
    p.add_argument("--fast", action="store_true", help="skip slow checks (waiting for a billed-hour boundary)")
    p.add_argument("--no-reports", action="store_true")
    return p.parse_args(argv)


def pick(all_s, args):
    sel = all_s
    if args.scenario:
        sel = [s for s in all_s if any(s.id == q or s.id.startswith(q) or q.lower() in s.id.lower() for q in args.scenario)]
    if args.tag:
        sel = [s for s in sel if set(args.tag) & set(s.tags)]
    return sel


async def amain(args: argparse.Namespace) -> int:
    scenarios = load_all()
    sel = pick(scenarios, args)
    if args.list:
        for s in sel:
            print(f"{s.id:34} {s.title}  [{', '.join(s.tags)}]")
        return 0
    if not sel:
        print("no scenarios selected", file=sys.stderr)
        return 2
    urls = Urls()
    for field_, val in (("gateway", args.base_url), ("gateway2", args.gateway2_url), ("metadata", args.metadata_url),
                        ("autoscaler", args.autoscaler_url), ("serverless", args.serverless_url),
                        ("telemetry", args.telemetry_url)):
        if val:
            setattr(urls, field_, val.rstrip("/"))
    started = dt.datetime.now()
    t0 = time.time()
    run_id = started.strftime("%H%M%S")
    async with load.make_client() as client:
        stack = Stack(urls, client)
        alive = await stack.check_up()
        core = ("gateway", "metadata")
        if not all(alive[k] for k in core):
            print(f"stack is not up: {alive}\nStart it with: deploy/run-local.sh up", file=sys.stderr)
            return 3
        reps = await stack.replicas()
        print(f"stack up: {alive}\nreplicas: " + ", ".join(f"{r['id']}({r['model']})" for r in reps) + "\n")
        results: list[ScenarioResult] = []
        for s in sel:
            print(f"== {s.id}: {s.title}", flush=True)
            r = await run_scenario(s, stack, client, alive, run_id=run_id, sim_hour_s=args.sim_hour_s, fast=args.fast)
            for c in r.checks:
                if c.status != "PASS":
                    print(f"   {c.status}: {c.name}: {c.detail[:200]}")
            print(f"   -> {r.status} ({r.duration_s:.1f}s)" + (f" {r.error.splitlines()[0]}" if r.error else ""), flush=True)
            results.append(r)
    print("\n" + table(results))
    if not args.no_reports:
        meta = {"started": started.isoformat(timespec="seconds"), "duration_s": time.time() - t0,
                "gateway": urls.gateway, "alive": alive,
                "replicas": ", ".join(f"{r['id']}({r['model']})" for r in reps)}
        paths = write_reports(results, meta, Path(args.report_dir))
        print("reports: " + ", ".join(str(p) for p in paths.values()))
    return 1 if any(r.status in ("FAIL", "ERROR") for r in results) else 0


def main(argv: list[str] | None = None) -> None:
    args = parse(argv)
    if args.validate:
        try:
            ss = load_all()
        except ScenarioError as e:
            print(f"INVALID: {e}", file=sys.stderr)
            sys.exit(2)
        print(f"{len(ss)} scenario files valid")
        sys.exit(0)
    sys.exit(asyncio.run(amain(args)))


if __name__ == "__main__":
    main()
