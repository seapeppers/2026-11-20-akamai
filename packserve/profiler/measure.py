"""Timed streaming requests against any OpenAI-compatible endpoint."""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field

import httpx

CHARS_PER_TOKEN = 4  # matches packserve.common.tokens; real endpoints use usage.prompt_tokens instead


@dataclass
class StreamResult:
    t_start: float
    token_times: list[float] = field(default_factory=list)  # perf_counter at each content chunk
    prompt_tokens: int | None = None
    cached_tokens: int | None = None
    completion_tokens: int | None = None

    @property
    def ttft_ms(self) -> float:
        return (self.token_times[0] - self.t_start) * 1000


def make_text(tokens: int, seed: str | None = None) -> str:
    """~`tokens` tokens of text; `seed` makes it unique (defeats prefix caching) or shared."""
    seed = seed or uuid.uuid4().hex
    chars = tokens * CHARS_PER_TOKEN
    unit = f"{seed} "
    return (unit * (chars // len(unit) + 1))[:chars]


async def stream_once(client: httpx.AsyncClient, model: str, prompt: str, max_tokens: int,
                      headers: dict | None = None) -> StreamResult:
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens,
            "stream": True, "stream_options": {"include_usage": True}}
    res = StreamResult(t_start=time.perf_counter())
    async with client.stream("POST", "/chat/completions", json=body, headers=headers or {}) as r:
        r.raise_for_status()
        async for line in r.aiter_lines():
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                break
            ev = json.loads(data)
            if ev.get("usage"):
                u = ev["usage"]
                res.prompt_tokens = u.get("prompt_tokens")
                res.completion_tokens = u.get("completion_tokens")
                res.cached_tokens = (u.get("prompt_tokens_details") or {}).get("cached_tokens")
            for ch in ev.get("choices") or []:
                if (ch.get("delta") or {}).get("content"):
                    res.token_times.append(time.perf_counter())
    return res


async def prefill_point(client, model: str, new: int, cached: int, probe_tokens: int = 1) -> tuple[float, int, int]:
    """TTFT (ms) for a prompt with `cached` block-aligned shared-prefix tokens + `new` unique tokens.
    Returns (ttft_ms, new_tokens, cached_tokens) using server-reported cached tokens when available."""
    suffix = make_text(new)
    if cached:
        prefix = make_text(cached, seed=f"shared-{uuid.uuid4().hex[:8]}")
        await stream_once(client, model, prefix, 1)  # warm the prefix cache
        prompt = prefix + suffix
    else:
        prompt = suffix
    r = await stream_once(client, model, prompt, probe_tokens)
    c = r.cached_tokens if r.cached_tokens is not None else cached
    total = r.prompt_tokens if r.prompt_tokens is not None else (cached + new)
    return r.ttft_ms, total - c, c


async def decode_point(client, model: str, batch: int, prompt_tokens: int, max_tokens: int) -> tuple[float, float] | None:
    """Run `batch` concurrent streams; measure the mean inter-token time while all are decoding.
    Returns (iter_ms, mean_total_context) or None if the steady window was too short."""
    results = await asyncio.gather(*[stream_once(client, model, make_text(prompt_tokens), max_tokens) for _ in range(batch)])
    t0 = max(r.token_times[0] for r in results)
    t1 = min(r.token_times[-1] for r in results)
    if t1 <= t0:
        return None
    gaps, ctx_mid = [], 0.0
    tmid = (t0 + t1) / 2
    for r in results:
        in_win = [t for t in r.token_times if t0 <= t <= t1]
        if len(in_win) < 8:
            return None
        gaps.append((in_win[-1] - in_win[0]) / (len(in_win) - 1))
        produced_mid = sum(1 for t in r.token_times if t <= tmid)
        ctx_mid += (r.prompt_tokens or prompt_tokens) + produced_mid
    return sum(gaps) / len(gaps) * 1000, ctx_mid
