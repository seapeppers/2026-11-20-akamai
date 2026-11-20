"""SIMULATED continuous-batching engine. One asyncio ticker advances every decoding request
by one token per iteration; iteration time comes from LatencyModel.decode_iter_ms."""
from __future__ import annotations

import asyncio
import math
import random
import time
from collections import OrderedDict, deque

from packserve.common.latency import LatencyModel
from packserve.common.schemas import FaultConfig, LatencyProfile
from packserve.common.tokens import cached_prefix_tokens

STATE_HASH_LIMIT = 512  # most recent block hashes reported in /state
TPOT_EMA_W = 0.1


class Req:
    def __init__(self, prompt_tokens: int, max_tokens: int, hashes: list[str]):
        self.prompt_tokens = prompt_tokens
        self.max_tokens = max_tokens
        self.hashes = hashes
        self.cached_tokens = 0
        self.produced = 0
        self.q: asyncio.Queue[int | None] = asyncio.Queue()  # token index, None = done
        self.state = "queued"
        self.cancelled = False
        self.t_arrive = time.monotonic()
        self.t_first: float | None = None
        self.t_done: float | None = None

    @property
    def need(self) -> int:
        return self.prompt_tokens + self.max_tokens


class Engine:
    def __init__(self, profile: LatencyProfile, seed: int | None = None):
        self.profile = profile
        self.lm = LatencyModel(profile)
        self.rng = random.Random(seed)
        self.fault = FaultConfig()
        self.waiting: deque[Req] = deque()
        self.prefilling: set[Req] = set()
        self.decoding: list[Req] = []
        self.kv_reserved = 0
        self.cache: OrderedDict[str, None] = OrderedDict()
        self.cache_cap_blocks = max(1, profile.kv_capacity_tokens // profile.prefix_block_tokens)
        self.tpot_ema: float | None = None
        self._wake = asyncio.Event()
        self._prefill_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()

    # lifecycle
    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        for t in [self._task, *self._tasks]:
            if t:
                t.cancel()

    # noise: lognormal with mean 1
    def _noise(self) -> float:
        s = self.profile.noise_sigma
        return self.rng.lognormvariate(-s * s / 2, s) if s > 0 else 1.0

    def submit(self, req: Req) -> None:
        self.waiting.append(req)
        self._wake.set()

    def cancel(self, req: Req) -> None:
        """Client went away: free everything the request holds."""
        req.cancelled = True
        if req.state == "queued":
            try:
                self.waiting.remove(req)
            except ValueError:
                pass
        elif req.state == "prefill":
            self.prefilling.discard(req)
            self.kv_reserved -= req.need
        elif req.state == "decode":
            if req in self.decoding:
                self.decoding.remove(req)
                self.kv_reserved -= req.need
        req.state = "done"

    # cache
    def cached_hashes(self) -> list[str]:
        return list(self.cache)[-STATE_HASH_LIMIT:]

    def lookup_cached(self, hashes: list[str]) -> int:
        return cached_prefix_tokens(hashes, set(self.cache), self.profile.prefix_block_tokens)

    def _insert(self, hashes: list[str]) -> None:
        for h in hashes:
            self.cache.pop(h, None)
            self.cache[h] = None
        while len(self.cache) > self.cache_cap_blocks:
            self.cache.popitem(last=False)

    # ticker
    def _admit(self) -> None:
        cap = self.profile.kv_capacity_tokens
        while self.waiting:
            req = self.waiting[0]
            if len(self.prefilling) + len(self.decoding) >= self.profile.max_running:
                break
            if self.kv_reserved + req.need > cap and (self.prefilling or self.decoding):
                break
            self.waiting.popleft()
            req.state = "prefill"
            self.prefilling.add(req)
            self.kv_reserved += req.need
            t = asyncio.create_task(self._prefill(req))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)

    async def _prefill(self, req: Req) -> None:
        async with self._prefill_lock:
            if req.cancelled:
                return
            req.cached_tokens = min(self.lookup_cached(req.hashes), req.prompt_tokens)
            new = req.prompt_tokens - req.cached_tokens
            ms = self.lm.prefill_ms(new, req.cached_tokens) * self._noise() * self.fault.slowdown
            await asyncio.sleep(ms / 1000)
        if req.cancelled:
            return
        self._insert(req.hashes)
        self.prefilling.discard(req)
        req.state = "decode"
        self.decoding.append(req)
        self._wake.set()

    async def _run(self) -> None:
        deadline = time.monotonic()
        while True:
            self._admit()
            if not self.decoding:
                self._wake.clear()
                await self._wake.wait()
                deadline = time.monotonic()
                continue
            batch = list(self.decoding)
            ctx = sum(r.prompt_tokens + r.produced for r in batch)
            ms = self.lm.decode_iter_ms(len(batch), ctx) * self._noise() * self.fault.slowdown
            deadline += ms / 1000
            delay = deadline - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                deadline = time.monotonic()  # fell behind (event loop busy); don't burst
                await asyncio.sleep(0)
            now = time.monotonic()
            for r in batch:
                if r.state != "decode":
                    continue
                r.produced += 1
                if r.t_first is None:
                    r.t_first = now
                r.q.put_nowait(r.produced)
                if r.produced >= r.max_tokens:
                    self._finish(r, now)

    def _finish(self, r: Req, now: float) -> None:
        r.t_done = now
        r.state = "done"
        self.decoding.remove(r)
        self.kv_reserved -= r.need
        r.q.put_nowait(None)
        if r.produced > 1 and r.t_first is not None:
            tpot = (now - r.t_first) * 1000 / (r.produced - 1)
            self.tpot_ema = tpot if self.tpot_ema is None else (1 - TPOT_EMA_W) * self.tpot_ema + TPOT_EMA_W * tpot

    # introspection
    @property
    def running(self) -> int:
        return len(self.prefilling) + len(self.decoding)

    def total_context(self) -> int:
        return sum(r.prompt_tokens for r in self.prefilling) + sum(r.prompt_tokens + r.produced for r in self.decoding)
