"""Reusable async load-generator helpers (httpx, streaming) that return flat request records.

Prompt sizing follows packserve.common.tokens: ~4 chars/token, prefix blocks of 64 tokens (=256 chars)
over `messages_text` = "<user>" + content for a single user message.
"""
from __future__ import annotations

import asyncio
import uuid
import json
import random
import string
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from packserve.common.tokens import CHARS_PER_TOKEN

ROLE_TAG = len("<user>")
BLOCK_CHARS = 64 * CHARS_PER_TOKEN
_WORDS = ["".join(random.Random(i).choices(string.ascii_lowercase, k=3 + i % 6)) for i in range(512)]


def text_of(seed: str, chars: int) -> str:
    """Deterministic pseudo-prose of exactly `chars` characters."""
    rng = random.Random(seed)
    out, n = [], 0
    while n < chars:
        w = rng.choice(_WORDS)
        out.append(w)
        n += len(w) + 1
    return " ".join(out)[:chars]


# Replica prefix caches survive between scenarios and runs, so every seed is salted with a per-process nonce:
# texts are identical within one run (what shared-prefix scenarios need) but never collide with earlier runs.
_NONCE = uuid.uuid4().hex[:8]


def build_prompt(spec: dict | None, idx: int = 0) -> str:
    """Prompt content from a spec:
      {kind: unique, chars: 400, seed: s}                -> distinct text per request (no cache sharing)
      {kind: shared_prefix, prefix_blocks: 32, suffix_chars: 80, seed: s}
            -> first `prefix_blocks` 64-token blocks of messages_text are identical across requests;
               the suffix is unique per request idx.
      {kind: prefix_only, prefix_blocks: 21, seed: s}    -> exactly the first N blocks (primes a cache)
    """
    spec = spec or {}
    kind, seed = spec.get("kind", "unique"), f"{_NONCE}-{spec.get('seed', 'u')}"
    if kind == "unique":
        return text_of(f"{seed}/{idx}", int(spec.get("chars", 400)))
    pre_chars = int(spec["prefix_blocks"]) * BLOCK_CHARS - ROLE_TAG
    prefix = text_of(f"{seed}/prefix", pre_chars)
    if kind == "prefix_only":
        return prefix
    if kind == "shared_prefix":
        return prefix + " " + text_of(f"{seed}/suffix/{idx}", int(spec.get("suffix_chars", 80)))
    raise ValueError(f"unknown prompt kind {kind!r}")


def new_record(group: str, idx: int, **kw: Any) -> dict:
    rec = {"group": group, "idx": idx, "status": None, "route": None, "reason": None, "replica": None,
           "predicted_tpot_ms": None, "cand_capacity": None, "cand_budget": None, "cand_tpot": None,
           "request_id": None, "simulated": None, "retry_after": None, "ttft_ms": None, "tpot_ms": None,
           "chunks": 0, "complete": False, "finish_reason": None, "error_type": None, "error_message": None,
           "headers_ms": None, "duration_ms": None, "cancelled": False, "exception": None,
           "t_start": None, "model_returned": None}
    rec.update(kw)
    return rec


def parse_candidates(v: str | None) -> tuple[int | None, int | None, int | None]:
    if not v:
        return None, None, None
    d = dict(p.split("=", 1) for p in v.split(",") if "=" in p)
    try:
        return int(d["capacity"]), int(d["budget"]), int(d["tpot"])
    except (KeyError, ValueError):
        return None, None, None


def _f(v: str | None) -> float | None:
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


def apply_headers(rec: dict, h: httpx.Headers) -> None:
    rec["route"] = h.get("x-packserve-route")
    rec["reason"] = h.get("x-packserve-reason")
    rec["replica"] = h.get("x-packserve-replica")
    rec["request_id"] = h.get("x-packserve-request-id")
    rec["predicted_tpot_ms"] = _f(h.get("x-packserve-predicted-tpot-ms"))
    rec["cand_capacity"], rec["cand_budget"], rec["cand_tpot"] = parse_candidates(h.get("x-packserve-candidates"))
    sim = h.get("x-packserve-simulated")
    rec["simulated"] = None if sim is None else sim.lower() == "true"
    rec["retry_after"] = _f(h.get("retry-after"))


async def chat(client: httpx.AsyncClient, base_url: str, *, model: str, content: str, max_tokens: int,
               key: str | None = None, raw_auth: str | None = None, stream: bool = True,
               group: str = "g", idx: int = 0, direct: bool = False, tenant: str | None = None,
               gw: Any = 1, headers_event: asyncio.Event | None = None, timeout_s: float = 120,
               extra_headers: dict | None = None, rec: dict | None = None, tolerate_errors: bool = False) -> dict:
    """One streaming (or plain) chat completion; never raises (errors land in the record)."""
    rec = rec if rec is not None else new_record(group, idx)
    rec.update(group=group, idx=idx, direct=direct, tenant=tenant, model=model, gw=gw,
               max_tokens=max_tokens, tolerate_errors=tolerate_errors)
    headers = dict(extra_headers or {})
    if raw_auth is not None:
        if raw_auth != "":
            headers["Authorization"] = raw_auth
    elif key:
        headers["Authorization"] = f"Bearer {key}"
    body = {"model": model, "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens, "stream": stream}
    t0 = time.perf_counter()
    rec["t_start"] = time.time()
    try:
        async with client.stream("POST", f"{base_url}/v1/chat/completions", json=body, headers=headers,
                                 timeout=httpx.Timeout(timeout_s, connect=10)) as resp:
            rec["status"] = resp.status_code
            rec["headers_ms"] = (time.perf_counter() - t0) * 1000
            apply_headers(rec, resp.headers)
            if headers_event is not None:
                headers_event.set()
            if resp.status_code != 200:
                raw = (await resp.aread()).decode(errors="replace")
                _parse_error(rec, raw)
            elif stream:
                await _consume_sse(resp, rec, t0)
            else:
                raw = (await resp.aread()).decode(errors="replace")
                try:
                    j = json.loads(raw)
                    rec["complete"] = True
                    rec["chunks"] = int((j.get("usage") or {}).get("completion_tokens") or 1)
                    rec["model_returned"] = j.get("model")
                except json.JSONDecodeError:
                    rec["error_message"] = raw[:200]
    except asyncio.CancelledError:
        rec["cancelled"] = True
        raise
    except Exception as e:
        rec["exception"] = f"{type(e).__name__}: {e}"[:200]
    finally:
        rec["duration_ms"] = (time.perf_counter() - t0) * 1000
        if headers_event is not None:
            headers_event.set()
    return rec


def _parse_error(rec: dict, raw: str) -> None:
    try:
        err = json.loads(raw).get("error", {})
        rec["error_type"] = err.get("type") or err.get("code")
        rec["error_message"] = err.get("message")
    except (json.JSONDecodeError, AttributeError):
        rec["error_message"] = raw[:200]


async def _consume_sse(resp: httpx.Response, rec: dict, t0: float) -> None:
    first = last = None
    async for line in resp.aiter_lines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            rec["complete"] = True
            break
        try:
            ch = json.loads(data)
        except json.JSONDecodeError:
            continue
        rec["model_returned"] = ch.get("model", rec.get("model_returned"))
        for c in ch.get("choices", []):
            if (c.get("delta") or {}).get("content"):
                now = time.perf_counter()
                first = first or now
                last = now
                rec["chunks"] += 1
            if c.get("finish_reason"):
                rec["finish_reason"] = c["finish_reason"]
    if first is not None:
        rec["ttft_ms"] = (first - t0) * 1000
        if rec["chunks"] > 1:
            rec["tpot_ms"] = (last - first) * 1000 / (rec["chunks"] - 1)


@dataclass
class Group:
    """A set of in-flight or finished requests sharing a name."""
    name: str
    records: list[dict] = field(default_factory=list)
    tasks: list[asyncio.Task] = field(default_factory=list)
    started_events: list[asyncio.Event] = field(default_factory=list)
    direct: bool = False

    async def started(self, timeout_s: float = 60) -> bool:
        try:
            await asyncio.wait_for(asyncio.gather(*(e.wait() for e in self.started_events)), timeout_s)
            return True
        except asyncio.TimeoutError:
            return False

    async def finished(self, timeout_s: float = 120) -> bool:
        if not self.tasks:
            return True
        done, pending = await asyncio.wait(self.tasks, timeout=timeout_s)
        return not pending

    def cancel(self) -> None:
        for t in self.tasks:
            if not t.done():
                t.cancel()

    @property
    def replicas(self) -> list[str]:
        return [r["replica"] for r in self.records if r.get("replica")]

    @property
    def top_replica(self) -> str | None:
        from collections import Counter
        c = Counter(self.replicas)
        return c.most_common(1)[0][0] if c else None

    def view(self) -> dict:
        return {"records": self.records, "replicas": self.replicas, "top_replica": self.top_replica,
                "count": len(self.records)}


def launch(client: httpx.AsyncClient, name: str, calls: list[dict], *, concurrency: int | None = None,
           pace_ms: float = 0, direct: bool = False) -> Group:
    """Start one task per call dict (kwargs for `chat`). Returns immediately."""
    g = Group(name, direct=direct)
    sem = asyncio.Semaphore(concurrency or len(calls) or 1)

    async def one(i: int, kw: dict, ev: asyncio.Event, rec: dict):
        if pace_ms and i:
            await asyncio.sleep(pace_ms * i / 1000)
        async with sem:
            await chat(client, group=name, idx=i, direct=direct, headers_event=ev, rec=rec, **kw)

    for i, kw in enumerate(calls):
        ev = asyncio.Event()
        rec = new_record(name, i, direct=direct)
        g.records.append(rec)
        g.started_events.append(ev)
        g.tasks.append(asyncio.create_task(one(i, kw, ev, rec)))
    return g


def make_client(max_conns: int = 1000) -> httpx.AsyncClient:
    return httpx.AsyncClient(limits=httpx.Limits(max_connections=max_conns, max_keepalive_connections=20),
                             timeout=httpx.Timeout(30, connect=10))
