"""Pure scaling decisions. No I/O, no clocks (callers pass `now`), so they unit-test directly."""
from __future__ import annotations

import math
from dataclasses import dataclass

from packserve.common import config
from packserve.common.schemas import ReplicaState


@dataclass(frozen=True)
class PoolConfig:
    model: str
    min: int
    max: int
    profile: str = config.DEFAULT_PROFILE


@dataclass
class ScalingParams:
    window_s: float = 30.0          # signal window for scale-up
    up_pressure: float = 0.05       # overflow+reject share that triggers scale-up
    up_util: float = 0.80
    min_requests: int = 5           # ignore the pressure share below this many requests
    up_cooldown_s: float = 10.0
    down_util: float = 0.40
    down_hold_s: float = 60.0       # utilization must stay low this long
    sim_hour_s: float = 120.0       # one "billed hour" (3600 for a real provider)
    billing_hold: bool = True
    release_margin_s: float = 5.0   # delete this long before the billed-hour boundary
    idle_ticks_required: int = 2    # consecutive ticks seen idle before deleting


DEFAULT_POOLS = [
    PoolConfig("qwen3-8-27b", 2, 6, config.DEFAULT_PROFILE),
    PoolConfig("gemma-4-26b-a4b-it", 0, 2, config.DEFAULT_PROFILE),
    PoolConfig("packserve-large-230b", 1, 2, "virtual-blackwell-x8-large"),
]


def should_scale_up(pool: PoolConfig, n_active: int, pressure: float, requests: int,
                    util: float | None, p: ScalingParams,
                    now: float, last_up_ts: float | None) -> str | None:
    """Reason string if a replica should be added, else None. `n_active` includes pending creates."""
    if n_active >= pool.max:
        return None
    if n_active < pool.min:
        return "below_min"
    if last_up_ts is not None and now - last_up_ts < p.up_cooldown_s:
        return None
    if requests >= p.min_requests and pressure > p.up_pressure:
        return f"pressure {pressure:.0%} > {p.up_pressure:.0%}"
    if util is not None and util > p.up_util:
        return f"utilization {util:.0%} > {p.up_util:.0%}"
    return None


def should_scale_down(pool: PoolConfig, n_active: int, util: float | None, low_since: float | None,
                      pressure: float, now: float, p: ScalingParams) -> bool:
    if n_active <= pool.min or util is None or util >= p.down_util or pressure > 0:
        return False
    return low_since is not None and now - low_since >= p.down_hold_s


def pick_drain_victim(active: list[ReplicaState]) -> ReplicaState | None:
    """Least-loaded replica; ties go to the newest (it has the least warm cache and billed time)."""
    if not active:
        return None
    return min(active, key=lambda r: (r.running + r.queued, -r.created_at, r.id))


def billed_hour_end(created_at: float, now: float, hour_s: float) -> float:
    """End of the billed hour that `now` falls in (hours are billed from creation, rounded up)."""
    hours = max(1, math.ceil((now - created_at) / hour_s))
    return created_at + hours * hour_s


def hold_elapsed(created_at: float, now: float, p: ScalingParams) -> bool:
    """True when a drained replica may be released: hold disabled, or within the margin of the
    billed-hour boundary (deleting earlier saves nothing for the hour already paid for)."""
    if not p.billing_hold:
        return True
    return billed_hour_end(created_at, now, p.sim_hour_s) - now <= p.release_margin_s


def can_delete(r: ReplicaState, now: float, idle_ticks: int, p: ScalingParams) -> bool:
    """Only a draining replica that has been idle for consecutive ticks and has passed its hold."""
    if not r.draining or r.running > 0 or r.queued > 0:
        return False
    if idle_ticks < p.idle_ticks_required:
        return False
    return hold_elapsed(r.created_at, now, p)


def is_idle_draining(r: ReplicaState) -> bool:
    return r.draining and r.running == 0 and r.queued == 0
