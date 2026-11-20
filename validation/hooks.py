"""Custom python steps for scenarios that need logic YAML cannot express.

Each hook is `async def hook(run, args) -> list[CheckResult]` and is referenced from a scenario as
    - action: custom
      hook: <name>
      args: {...}
"""
from __future__ import annotations

import asyncio
import time

import httpx

from .assertions import CheckResult


async def _healthz(run, url: str) -> bool:
    try:
        r = await run.client.get(url + "/healthz", timeout=2)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


async def billing_hold(run, args: dict) -> list[CheckResult]:
    """A drained replica must be kept until the billed-hour boundary (sim_hour_s), then deleted.

    args: replica (id/alias, or "auto" = whichever replica the autoscaler picked), busiest (id that
    must NOT be the one picked). Uses replica.created_at from the pre-scenario snapshot."""
    out: list[CheckResult] = []
    ref = args["replica"]
    if ref == "auto":
        picked: list[str] = []
        t0 = time.time()
        while time.time() - t0 < 30 and not picked:
            a = await run.stack.autoscaler_state() or {}
            picked = sorted((set(a.get("draining") or []) | set(a.get("held_for_billing") or [])) - run.pre_drained)
            if not picked:
                await asyncio.sleep(0.5)
        out.append(CheckResult("scale-down:exactly-one-replica-drained", len(picked) == 1, f"drained/held={picked}"))
        if not picked:
            return out
        ref = picked[0]
        busiest = args.get("busiest")
        if busiest:
            out.append(CheckResult("scale-down:picked-least-loaded-not-busiest", ref != busiest,
                                   f"drained {ref}; busiest (holding in-flight requests) was {busiest}"))
    rep = next((r for r in await run.stack.replicas() if r["id"] == ref), None) or run.replica(ref)
    created = rep.get("created_at")
    if created is None:
        return [CheckResult("billing-hold", False, "replica state has no created_at; cannot compute boundary", "warn")]
    H = run.sim_hour_s
    st = run.stack
    # 1. wait for the replica to be idle and parked in held_for_billing (or already gone)
    held = False
    t0 = time.time()
    while time.time() - t0 < 30:
        a = await st.autoscaler_state() or {}
        if rep["id"] in (a.get("held_for_billing") or []):
            held = True
            break
        await asyncio.sleep(0.5)
    out.append(CheckResult("billing-hold:drained-replica-parked-in-held_for_billing", held,
                           f"autoscaler held_for_billing={((await st.autoscaler_state()) or {}).get('held_for_billing')}"))
    remaining = H - ((time.time() - created) % H)
    # 2. early window: replica must still exist
    if remaining > 20:
        await asyncio.sleep(min(10.0, remaining - 12))
        a = await st.autoscaler_state() or {}
        present = rep["id"] in (a.get("held_for_billing") or []) or rep["id"] in (a.get("draining") or [])
        alive = await _healthz(run, rep["url"])
        out.append(CheckResult("billing-hold:not-released-before-boundary", present and alive,
                               f"{rep['id']} in autoscaler lists={present}, process healthz={alive}, "
                               f"~{remaining - 10:.0f}s before the {H:.0f}s billed-hour boundary"))
    else:
        run.notes.append(f"billing-hold early check skipped: only {remaining:.0f}s to the billed-hour boundary")
    # 3. after the boundary the replica is deleted
    if run.fast:
        run.notes.append("billing-hold post-boundary deletion check skipped (--fast)")
        return out
    remaining = H - ((time.time() - created) % H)
    deadline = time.time() + remaining + 40
    gone = False
    while time.time() < deadline:
        a = await st.autoscaler_state() or {}
        listed = rep["id"] in (a.get("held_for_billing") or []) or rep["id"] in (a.get("draining") or [])
        if not listed and not await _healthz(run, rep["url"]):
            gone = True
            break
        await asyncio.sleep(1.0)
    out.append(CheckResult("billing-hold:released-after-boundary", gone,
                           f"{rep['id']} deleted after boundary: {gone} (waited up to {remaining + 40:.0f}s)"))
    return out


async def gateway_reset(run, args: dict) -> list[CheckResult]:
    """Clear gateway decisions and learned correction factors mid-scenario (phase 2 must not inherit phase 1's
    online-correction state), then let heartbeats/metadata repopulate the replica views."""
    for name in ("gateway", "gateway2"):
        if run.alive.get(name):
            await run.stack.post_json(getattr(run.stack.u, name) + "/admin/reset")
    await asyncio.sleep(float(args.get("settle_s", 1.5)))
    return []


HOOKS = {"billing_hold": billing_hold, "gateway_reset": gateway_reset}
