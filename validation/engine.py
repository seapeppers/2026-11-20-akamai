"""Scenario executor: drives the stack through admin APIs, collects records, evaluates checks."""
from __future__ import annotations

import asyncio
import time
import traceback
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import hooks, load
from .assertions import CheckResult, check_invariants, compare, evaluate_check
from .model import build_calc
from .paths import extract, render
from .scenario import Scenario
from .stack import TENANT_KEYS, TENANT_TPOT, Stack, Urls


@dataclass
class ScenarioResult:
    id: str
    title: str
    status: str = "PASS"            # PASS | FAIL | SKIP | ERROR
    duration_s: float = 0.0
    checks: list[CheckResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str | None = None
    groups: dict[str, dict] = field(default_factory=dict)
    intent: str = ""

    def finalize(self) -> None:
        if self.status in ("SKIP", "ERROR"):
            return
        self.status = "FAIL" if any(c.status == "FAIL" for c in self.checks) else "PASS"


class Fatal(Exception):
    """Abort the scenario (precondition could not be established)."""


class Run:
    """Mutable state of one scenario execution; also the object hooks receive."""

    def __init__(self, stack: Stack, client: httpx.AsyncClient, scn: Scenario, *, run_id: str,
                 alive: dict[str, bool], sim_hour_s: float, fast: bool):
        self.stack, self.client, self.scn = stack, client, scn
        self.run_id, self.alive, self.sim_hour_s, self.fast = run_id, alive, sim_hour_s, fast
        self.groups: dict[str, load.Group] = {}
        self.facts: dict[str, Any] = {}
        self.bound: dict[str, dict] = {}
        self.checks: list[CheckResult] = []
        self.notes: list[str] = []
        self.touched_replicas: set[str] = set()
        self.scaled = False
        self.pre_drained: set[str] = set()
        self.calc = build_calc()
        self.facts["calc"] = self.calc
        self.baseline_target: int | None = None
        self.baseline_manual: int | None = None
        self.baseline_paused: bool | None = None

    # ------------------------------------------------------------ templating context
    def ctx(self) -> dict:
        n = Counter(r["model"] for r in self.bound.values())
        return {**self.bound, **self.aliases(), "key": TENANT_KEYS, "tpot": TENANT_TPOT, "url": self.stack.u.as_ctx(),
                "calc": self.calc, "fact": self.facts, "g": {k: v.view() for k, v in self.groups.items()},
                "n_qwen": n["qwen3-8-27b"], "n_large": n["packserve-large-230b"], "n_gemma": n["gemma-4-26b-a4b-it"],
                "sim_hour_s": self.sim_hour_s, "now": time.time()}

    def aliases(self) -> dict[str, dict]:
        qs = [k for k in self.bound if k[0] == "q" and k[1:].isdigit()]
        return {"qlast": self.bound[max(qs, key=lambda k: int(k[1:]))]} if qs else {}

    def r(self, x: Any) -> Any:
        return render(x, self.ctx())

    def replica(self, ref: str) -> dict:
        al = {**self.bound, **self.aliases()}
        if ref in al:
            return al[ref]
        for r in self.bound.values():
            if r["id"] == ref:
                return r
        for rid, reg in self.stack.registrations.items():
            if rid == ref:
                return reg
        raise Fatal(f"unknown replica reference {ref!r}")

    def add(self, name: str, ok: bool, detail: str = "", severity: str = "fail") -> None:
        self.checks.append(CheckResult(name, ok, detail, severity))

    def seed(self, s: Any) -> str:
        return f"{self.run_id}/{self.scn.id}/{s}"

    def url_for(self, step: dict) -> str:
        if "url" in step:
            return self.r(step["url"])
        svc = step.get("service", "gateway")
        return getattr(self.stack.u, svc) + self.r(step.get("url_path", ""))

    # ------------------------------------------------------------ step handlers
    async def step_traffic(self, st: dict) -> None:
        st = self.r({k: v for k, v in st.items() if k != "prompt"}) | {"prompt": st.get("prompt")}
        name, count = st["name"], int(st.get("count", 1))
        tenant = st.get("tenant")
        raw_auth = st.get("raw_auth")
        key = TENANT_KEYS.get(tenant) if tenant else None
        if raw_auth is None and tenant and key is None:
            raise Fatal(f"unknown tenant alias {tenant!r}")
        base = self.stack.u.gateway2 if int(st.get("gateway", 1)) == 2 else self.stack.u.gateway
        spec = dict(self.r(st.get("prompt")) or {})
        spec["seed"] = self.seed(spec.get("seed", name))
        calls = [dict(base_url=base, model=st["model"], content=load.build_prompt(spec, i),
                      max_tokens=int(st.get("max_tokens", 8)), key=key, raw_auth=raw_auth,
                      stream=st.get("stream", True), tenant=tenant, gw=int(st.get("gateway", 1)),
                      timeout_s=float(st.get("timeout_s", 120)), tolerate_errors=bool(st.get("tolerate_errors")))
                 for i in range(count)]
        bg = bool(st.get("background"))
        g = load.launch(self.client, name, calls, concurrency=count if bg else int(st.get("concurrency", count)),
                        pace_ms=float(st.get("pace_ms", 0)))
        self.groups[name] = g
        if bg:
            ok = await g.started(float(st.get("start_timeout_s", 60)))
            if not ok:
                self.add(f"started:{name}", False, "not all background requests got response headers in time")
        else:
            ok = await g.finished(float(st.get("timeout_s", 120)) + 10)
            if not ok:
                self.add(f"finished:{name}", False, "requests did not finish in time")

    async def step_direct_load(self, st: dict) -> None:
        st = self.r({k: v for k, v in st.items() if k != "prompt"}) | {"prompt": st.get("prompt")}
        name = st["name"]
        loads = st.get("loads")
        if loads is None:
            target = st.get("target", "qwen")
            prefix = {"qwen": "q", "gemma": "m", "large": "l"}[target]
            keys = sorted((k for k in self.bound if k[0] == prefix and k[1:].isdigit()), key=lambda k: int(k[1:]))
            counts = st.get("counts") or []
            loads = [{"replica": k, "count": counts[i] if i < len(counts) else st.get("count", "max")}
                     for i, k in enumerate(keys)]
        calls = []
        for ld in loads:
            rep = self.replica(ld["replica"])
            cnt = ld.get("count", "max")
            cnt = int(rep.get("max_running", 64)) if cnt == "max" else int(cnt)
            for _ in range(cnt):
                i = len(calls)
                spec = {"kind": "unique", "chars": 400, "seed": self.seed(f"{name}/{rep['id']}")}
                calls.append(dict(base_url=rep["url"], model=rep["model"], content=load.build_prompt(spec, i),
                                  max_tokens=int(st.get("max_tokens", 500)), tenant=None, gw="direct",
                                  timeout_s=float(st.get("timeout_s", 180)), tolerate_errors=True))
        g = load.launch(self.client, name, calls, direct=True)
        self.groups[name] = g
        if not await g.started(60):
            self.add(f"started:{name}", False, "direct load: not all requests started")
        # let replica heartbeat + gateway merge (1 s heartbeat, 250 ms merge) observe the load
        await asyncio.sleep(float(st.get("settle_s", 2.5)))

    async def step_direct_prime(self, st: dict) -> None:
        rep = self.replica(self.r(st["replica"]))
        spec = dict(self.r(st["prompt"]))
        spec["seed"] = self.seed(spec.get("seed", "prime"))
        rec = await load.chat(self.client, rep["url"], model=rep["model"], content=load.build_prompt(spec, 0),
                              max_tokens=int(st.get("max_tokens", 1)), group="prime", direct=True, gw="direct")
        if rec["status"] != 200:
            self.add(f"prime:{rep['id']}", False, f"direct prime failed: {rec['status']} {rec['exception']}")

    async def step_await(self, st: dict) -> None:
        names = st.get("groups") or list(self.groups)
        for n in names:
            ok = await self.groups[n].finished(float(st.get("timeout_s", 120)))
            if not ok:
                self.add(f"await:{n}", False, "timed out waiting for group to finish")

    async def step_cancel(self, st: dict) -> None:
        for n in st.get("groups") or list(self.groups):
            self.groups[n].cancel()
        await asyncio.sleep(0.3)

    async def step_wait(self, st: dict) -> None:
        if "seconds" in st:
            await asyncio.sleep(float(st["seconds"]))
            return
        if st.get("idle"):
            ok, busy = await self.stack.wait_idle(float(st.get("timeout_s", 90)))
            if not ok:
                self.add("wait:idle", False, f"replicas still busy: {busy}")
            return
        u = self.r({k: v for k, v in st["until"].items()})
        url = self.url_for(u)
        timeout, poll = float(u.get("timeout_s", 30)), float(u.get("poll_s", 0.5))
        op, want, tol = u.get("op", "=="), u.get("value"), u.get("tol", 0)
        t0, last = time.time(), None
        while time.time() - t0 < timeout:
            try:
                data = (await self.client.get(url, timeout=5)).json()
                last = extract(data, u["path"])
                if compare(last, op, want, tol):
                    if st.get("name"):
                        self.facts[st["name"]] = {"elapsed_s": time.time() - t0, "value": last}
                    return
            except (httpx.HTTPError, ValueError):
                pass
            await asyncio.sleep(poll)
        msg = f"timeout after {timeout}s waiting for {u['path']} {op} {want} (last={last})"
        if st.get("fatal"):
            raise Fatal(msg)
        self.add(f"wait:{st.get('name') or u['path']}", False, msg)

    async def step_fault(self, st: dict) -> None:
        st = self.r(st)
        rep = self.replica(st["replica"])
        body = {"slowdown": float(st.get("slowdown", 1.0)), "fail": bool(st.get("fail", False)),
                "drop_rate": float(st.get("drop_rate", 0.0))}
        resp = await self.stack.post_json(rep["url"] + "/admin/fault", body)
        if resp.status_code >= 300:
            raise Fatal(f"fault on {rep['id']} -> {resp.status_code}")

    async def step_metadata(self, st: dict) -> None:
        st = self.r(st)
        rep = self.replica(st["replica"])
        base = self.stack.u.metadata
        op = st["op"]
        if op == "drain":
            resp = await self.stack.post_json(f"{base}/v1/replicas/{rep['id']}/drain")
        elif op == "delete":
            resp = await self.c_delete(f"{base}/v1/replicas/{rep['id']}")
        elif op == "register":
            resp = await self.stack.post_json(f"{base}/v1/replicas/register", self.stack.registrations[rep["id"]])
        else:
            raise Fatal(f"bad metadata op {op}")
        self.touched_replicas.add(rep["id"])
        if resp.status_code >= 300:
            raise Fatal(f"metadata {op} {rep['id']} -> {resp.status_code} {resp.text[:100]}")

    async def c_delete(self, url: str) -> httpx.Response:
        return await self.client.delete(url, timeout=10)

    async def step_autoscaler(self, st: dict) -> None:
        st = self.r(st)
        op, base = st["op"], self.stack.u.autoscaler
        if not self.alive.get("autoscaler"):
            raise Fatal("autoscaler not reachable")
        if op in ("pause", "resume"):
            await self.stack.post_json(f"{base}/admin/{op}")
        elif op == "scale":
            self.scaled = True
            a0 = await self.stack.autoscaler_state() or {}
            self.pre_drained = set(a0.get("draining") or []) | set(a0.get("held_for_billing") or [])
            ok, note = await self.stack.ensure_scale(int(st["replicas"]), float(st.get("wait_s", 90)))
            if note:
                self.notes.append(note)
            if not ok:
                self.add(f"scale:{st['replicas']}", False, note or "replica count did not converge")
        else:
            raise Fatal(f"bad autoscaler op {op}")
        if st.get("rebind"):
            if op == "scale":
                # converged: freeze the autoscaler so pressure rules on other pools cannot add replicas mid-check
                await self.stack.post_json(f"{base}/admin/pause")
            old = {r["id"] for r in self.bound.values()}
            model = "qwen3-8-27b"
            self.bound = await self.stack.bind()
            new = [r["id"] for r in self.bound.values() if r["id"] not in old and r.get("model") == model]
            self.facts["scale"] = {"new_ids": new, "old_ids": sorted(old)}

    async def step_serverless(self, st: dict) -> None:
        st = self.r(st)
        base = self.stack.u.serverless
        if st.get("reset"):
            await self.stack.post_json(base + "/admin/reset")
        if "config" in st:
            from packserve.common.schemas import ServerlessMockConfig
            body = ServerlessMockConfig().model_dump() | st["config"]
            resp = await self.stack.post_json(base + "/admin/config", body)
            if resp.status_code >= 300:
                raise Fatal(f"serverless config -> {resp.status_code} {resp.text[:100]}")
        if st.get("snapshot"):
            r = await self.client.get(base + "/admin/stats", timeout=5)
            self.facts[st["snapshot"]] = {"status": r.status_code, "json": _json(r)}

    async def step_observe(self, st: dict) -> None:
        st = self.r({k: v for k, v in st.items() if k != "name"}) | {"name": st["name"]}
        headers = dict(st.get("headers") or {})
        if st.get("tenant"):
            headers["Authorization"] = f"Bearer {TENANT_KEYS[st['tenant']]}"
        r = await self.client.request(st.get("method", "GET"), self.url_for(st), headers=headers,
                                      json=st.get("json"), timeout=10)
        self.facts[st["name"]] = {"status": r.status_code, "json": _json(r), "headers": dict(r.headers)}

    async def step_custom(self, st: dict) -> None:
        fn = hooks.HOOKS[st["hook"]]
        for res in await fn(self, self.r(st.get("args") or {})):
            self.checks.append(res)

    async def step_check(self, st: dict) -> None:
        await self.join_decisions()
        groups = {n: g.records for n, g in self.groups.items()}
        for c in st["checks"]:
            c = self.r(c)
            self.checks.append(evaluate_check(c, groups, self.facts))

    # ------------------------------------------------------------ decisions join
    async def join_decisions(self) -> None:
        recs = [r for g in self.groups.values() if not g.direct for r in g.records if r.get("request_id")]
        if not recs:
            return
        urls = [self.stack.u.gateway] + ([self.stack.u.gateway2] if self.alive.get("gateway2") else [])
        for attempt in range(3):
            by_id: dict[str, dict] = {}
            for u in urls:
                try:
                    r = await self.client.get(f"{u}/admin/decisions", params={"limit": 5000}, timeout=10)
                    by_id.update({d["request_id"]: d for d in r.json()})
                except (httpx.HTTPError, ValueError, KeyError):
                    pass
            missing = 0
            for rec in recs:
                d = by_id.get(rec["request_id"])
                if d is None:
                    missing += 1
                    continue
                c = d.get("candidates") or {}
                rec.update(d_route=d.get("route"), d_reason=d.get("reason"), d_replica=d.get("replica_id"),
                           decision_us=d.get("decision_us"), cache_hit_tokens=d.get("cache_hit_tokens"),
                           prompt_tokens=d.get("prompt_tokens"), d_tenant=d.get("tenant_id"),
                           predicted_prefill_ms=d.get("predicted_prefill_ms"), gateway_id=d.get("gateway_id"),
                           d_cand_capacity=c.get("capacity"), d_cand_budget=c.get("budget"), d_cand_tpot=c.get("tpot"),
                           d_simulated=d.get("simulated"))
            if not missing:
                return
            await asyncio.sleep(0.4)
        self.notes.append(f"{missing} request(s) not found in /admin/decisions (ring buffer or telemetry lag)")


def _json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return {"_text": r.text[:300]}


def summarize_groups(groups: dict[str, load.Group]) -> dict[str, dict]:
    out = {}
    for n, g in groups.items():
        rs = g.records
        out[n] = {
            "n": len(rs), "direct": g.direct,
            "status": dict(Counter(r.get("status") for r in rs)),
            "route": dict(Counter(r.get("route") for r in rs)),
            "reason": dict(Counter(r.get("reason") for r in rs)),
            "replica": dict(Counter(r.get("replica") for r in rs if r.get("replica"))),
            "complete": sum(1 for r in rs if r.get("complete")),
            "errors": dict(Counter(r["exception"] for r in rs if r.get("exception"))),
        }
    return out


STEPS = {"traffic": "step_traffic", "direct_load": "step_direct_load", "direct_prime": "step_direct_prime",
         "await": "step_await", "cancel": "step_cancel", "wait": "step_wait", "fault": "step_fault",
         "metadata": "step_metadata", "autoscaler": "step_autoscaler", "serverless": "step_serverless",
         "observe": "step_observe", "custom": "step_custom", "check": "step_check"}


async def run_scenario(scn: Scenario, stack: Stack, client: httpx.AsyncClient, alive: dict[str, bool], *,
                       run_id: str, sim_hour_s: float = 120, fast: bool = False) -> ScenarioResult:
    res = ScenarioResult(scn.id, scn.title, intent=scn.intent)
    t0 = time.time()
    run = Run(stack, client, scn, run_id=run_id, alive=alive, sim_hour_s=sim_hour_s, fast=fast)
    try:
        # ---- requirements
        req = scn.requires
        if req.get("gateway2") and not alive.get("gateway2"):
            return _skip(res, t0, "gateway #2 (:8081) not running")
        if req.get("autoscaler") and not alive.get("autoscaler"):
            return _skip(res, t0, "autoscaler not running")
        if req.get("serverless", True) and not alive.get("serverless"):
            return _skip(res, t0, "serverless mock not running")
        # ---- baseline
        res.notes += await stack.reset_all(alive)
        run.bound = await stack.bind()
        n = Counter(r["model"] for r in run.bound.values())
        for key, model in (("qwen_replicas", "qwen3-8-27b"), ("gemma_replicas", "gemma-4-26b-a4b-it"),
                           ("large_replicas", "packserve-large-230b")):
            if n[model] < req.get(key, 0):
                return _skip(res, t0, f"needs >= {req[key]} healthy {model} replicas, found {n[model]}")
        if alive.get("autoscaler"):
            st = await stack.autoscaler_state() or {}
            pool = (st.get("pools") or {}).get("qwen3-8-27b") or {}
            run.baseline_target, run.baseline_paused = pool.get("active"), st.get("paused")
            run.baseline_manual = pool.get("manual_target")
            await stack.post_json(stack.u.autoscaler + "/admin/pause")  # keep the control loop out of the way
        idle, busy = await stack.wait_idle(30)
        if not idle:
            res.notes.append(f"baseline not idle before scenario ({busy}); results may be affected")
        # ---- steps
        async def go():
            for i, st in enumerate(scn.steps):
                await getattr(run, STEPS[st["action"]])(st)
        await asyncio.wait_for(go(), scn.timeout_s)
        await run.join_decisions()
        if scn.invariants:
            recs = [r for g in run.groups.values() if not g.direct for r in g.records]
            models = {r["id"]: r["model"] for r in run.bound.values()}
            run.checks += check_invariants(recs, TENANT_TPOT_BY_ID(), models)
    except Fatal as e:
        res.status, res.error = "ERROR", f"precondition/step failed: {e}"
    except asyncio.TimeoutError:
        res.status, res.error = "ERROR", f"scenario exceeded {scn.timeout_s}s"
    except Exception as e:
        res.status, res.error = "ERROR", f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=4)}"
    finally:
        res.checks += run.checks
        res.notes += run.notes
        res.groups = summarize_groups(run.groups)
        try:
            res.notes += await restore(run)
        except Exception as e:  # restoration problems must be visible, never silent
            res.notes.append(f"RESTORE FAILED: {type(e).__name__}: {e}")
    res.duration_s = time.time() - t0
    res.finalize()
    return res


def TENANT_TPOT_BY_ID() -> dict[str, float]:
    return dict(TENANT_TPOT)


def _skip(res: ScenarioResult, t0: float, why: str) -> ScenarioResult:
    res.status, res.error, res.duration_s = "SKIP", why, time.time() - t0
    return res


async def restore(run: Run) -> list[str]:
    """Undo everything the scenario changed. Returns notes."""
    notes: list[str] = []
    st = run.stack
    for g in run.groups.values():
        g.cancel()
    await asyncio.sleep(0.3)
    # faults + serverless + gateway state
    notes += await st.reset_all(run.alive)
    # replicas drained/deleted through metadata -> re-register
    if run.touched_replicas:
        current = {r["id"]: r for r in await st.replicas()}
        for rid in run.touched_replicas:
            reg = st.registrations.get(rid)
            cur = current.get(rid)
            if reg and (cur is None or cur.get("draining")):
                if cur is not None:
                    await run.c_delete(f"{st.u.metadata}/v1/replicas/{rid}")
                await st.post_json(f"{st.u.metadata}/v1/replicas/register", reg)
                notes.append(f"re-registered {rid}")
    if run.scaled and run.baseline_target is not None:
        ok, note = await st.ensure_scale(run.baseline_target, 90)
        notes.append(f"restored qwen pool to {run.baseline_target} replicas ok={ok} {note}".strip())
        await st.clear_manual_scale()
    if run.alive.get("autoscaler") and run.baseline_paused is not None:
        await st.post_json(st.u.autoscaler + ("/admin/pause" if run.baseline_paused else "/admin/resume"))
    idle, busy = await st.wait_idle(90)
    if not idle:
        notes.append(f"pool still busy after cleanup ({busy})")
    return notes
