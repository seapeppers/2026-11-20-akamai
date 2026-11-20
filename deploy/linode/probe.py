#!/usr/bin/env python3
"""Proposal 7: serverless scaling probe from Nanode load generators.

DRY-RUN BY DEFAULT. It only prints the Linode API requests and ssh commands it would run.
It goes live only when ALL of these hold:  --live  AND  PACKSERVE_LINODE_LIVE=1  AND  LINODE_TOKEN set
(and AKAMAI_API_KEY set, which is copied to each Nanode over ssh, never to user data, never printed).

    uv run python deploy/linode/probe.py --nanodes 2 --steps 1,2,4,8        # dry-run
    PACKSERVE_LINODE_LIVE=1 uv run python deploy/linode/probe.py --live ...  # needs lead approval

Everything is labeled claude-probe-* and tagged claude-probe. Cleanup runs in `finally` and verifies.
Results are client-side latency/errors only; replica counts and server-side cache are not visible.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from packserve.autoscaler.providers import LIVE_ENV, LinodeProvider  # noqa: E402

HERE = Path(__file__).resolve().parent
SSH = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10"]
INFERENCE_BASE = "https://api.akamai-inference.com/v1"   # resolves from Akamai's network, not the laptop


def sh(cmd: list[str], live: bool, stdin: bytes | None = None) -> str:
    shown = " ".join(shlex.quote(c) for c in cmd)
    if not live:
        print(f"DRY-RUN would run: {shown}")
        return ""
    return subprocess.run(cmd, input=stdin, capture_output=True, check=True, timeout=600).stdout.decode()


async def wait_running(prov: LinodeProvider, linode_id: int, live: bool) -> str:
    if not live:
        return "203.0.113.%d" % (linode_id % 250)
    for _ in range(60):
        inst = await prov._request("GET", f"/linode/instances/{linode_id}")
        if inst.get("status") == "running":
            return inst["ipv4"][0]
        await asyncio.sleep(5)
    raise TimeoutError(f"linode {linode_id} not running")


async def main(a: argparse.Namespace) -> int:
    live = a.live and os.environ.get(LIVE_ENV) == "1"
    if a.live and not live:
        print(f"--live given but {LIVE_ENV}=1 is not set: staying in DRY-RUN")
    prov = LinodeProvider(live=live)
    print(f"mode: {'LIVE' if live else 'DRY-RUN'}; nanodes={a.nanodes} region={prov.region} type={prov.plan}")
    results: list[dict] = []
    try:
        hosts = []
        for _ in range(a.nanodes):
            p = await prov.create("probe", "nanode")
            hosts.append(await wait_running(prov, prov._ids[p.id], live))
        per_node_steps = ",".join(str(max(1, int(s) // a.nanodes)) for s in a.steps.split(","))
        for ip in hosts:
            target = f"root@{ip}"
            sh([*SSH, target, "mkdir -p /root/probe"], live)
            sh(["scp", "-o", "StrictHostKeyChecking=accept-new", str(HERE / "loadgen.py"), f"{target}:/root/probe/"], live)
            # key goes over ssh stdin to a 0600 file on the Nanode; never argv, never user data
            key = os.environ.get("AKAMAI_API_KEY", "").encode() if live else b""
            sh([*SSH, target, "umask 077; cat > /root/probe/key"], live, stdin=key)
        for ip in hosts:
            remote = shlex.join(["python3", "/root/probe/loadgen.py", "--base-url", INFERENCE_BASE,
                                 "--key-file", "/root/probe/key", "--model", a.model, "--steps", per_node_steps,
                                 "--step-seconds", str(a.step_seconds), "--max-tokens", str(a.max_tokens)])
            cmd = [*SSH, f"root@{ip}", remote]
            out = sh(cmd, live)
            results.append({"host": ip, "steps": [json.loads(x) for x in out.splitlines() if x.strip()]})
        if live:
            Path("reports").mkdir(exist_ok=True)
            path = Path("reports") / f"probe-{time.strftime('%Y%m%d-%H%M%S')}.json"
            path.write_text(json.dumps({"nanodes": a.nanodes, "results": results}, indent=2))
            print(f"wrote {path}")
        return 0
    finally:
        left = await prov.cleanup()
        print(f"cleanup done; leftover claude-probe instances: {left or 'none'}")
        if live and left:
            print("WARNING: leftovers remain; delete them in Cloud Manager (tag claude-probe)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nanodes", type=int, default=2, help="2-4; account Linode cap is unknown")
    ap.add_argument("--steps", default="1,2,4,8", help="TOTAL concurrency per step, split across Nanodes")
    ap.add_argument("--step-seconds", type=float, default=20)
    ap.add_argument("--max-tokens", type=int, default=64, help="keep small: the token budget is shared")
    ap.add_argument("--model", default="qwen3-8-27b")
    ap.add_argument("--live", action="store_true")
    ns = ap.parse_args()
    if not 1 <= ns.nanodes <= 4:
        ap.error("--nanodes must be 1-4")
    sys.exit(asyncio.run(main(ns)))
