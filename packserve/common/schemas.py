"""Shared wire types. See docs/contracts.md."""
from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, Field

Route = Literal["replica", "overflow", "rejected"]
Reason = Literal[
    "packed", "fallback", "overflow", "overflow_rejected", "no_capacity",
    "pool_only_model", "burst_disabled", "unauthorized", "unknown_model",
]


class LatencyProfile(BaseModel):
    """Coefficients for the PackServe latency model, per (model, hardware)."""
    name: str
    model: str
    # prefill: a0 + a1*N + a2*N*C + a3*N^2  (ms; N new tokens, C cached tokens)
    alpha: tuple[float, float, float, float]
    # decode iteration, piecewise by batch segment: b0 + b1*B + b2*S (ms; S total context)
    # segments: list of (max_batch_inclusive, b0, b1, b2), ascending
    beta_segments: list[tuple[int, float, float, float]]
    max_running: int = 64
    kv_capacity_tokens: int = 400_000
    prefix_block_tokens: int = 64
    noise_sigma: float = 0.08  # lognormal sigma used by the simulator only


class ReplicaRegistration(BaseModel):
    id: str
    url: str
    model: str
    profile: str
    provider: str = "sim"


class ReplicaState(BaseModel):
    id: str
    url: str
    model: str
    profile: str
    healthy: bool = True
    draining: bool = False
    running: int = 0          # occupied slots (prefilling + decoding)
    queued: int = 0
    kv_used_tokens: int = 0
    kv_capacity_tokens: int = 0
    max_running: int = 64
    total_context_tokens: int = 0
    cached_block_hashes: list[str] = Field(default_factory=list)  # may be truncated
    measured_tpot_ms_ema: float | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class FaultConfig(BaseModel):
    slowdown: float = 1.0
    fail: bool = False
    drop_rate: float = 0.0


class StateDelta(BaseModel):
    gateway_id: str
    replica_id: str
    running_delta: int = 0
    queued_delta: int = 0
    kv_delta_tokens: int = 0
    context_delta_tokens: int = 0
    added_block_hashes: list[str] = Field(default_factory=list)
    ts: float = Field(default_factory=time.time)


class TenantCreate(BaseModel):
    name: str
    contact_email: str | None = None
    persona_id: str | None = None
    tpot_target_ms: float = 50
    burst_enabled: bool = True
    reserved_replicas: int = 1
    models: list[str] = Field(default_factory=lambda: ["qwen3-8-27b"])


class Tenant(TenantCreate):
    id: str
    created_at: float = Field(default_factory=time.time)


class TenantCreated(Tenant):
    api_key: str  # returned once


class Candidates(BaseModel):
    capacity: int = 0
    budget: int = 0
    tpot: int = 0


class RouteDecision(BaseModel):
    request_id: str
    ts: float = Field(default_factory=time.time)
    gateway_id: str
    tenant_id: str | None
    model: str
    route: Route
    reason: Reason
    replica_id: str | None = None
    predicted_tpot_ms: float | None = None
    predicted_prefill_ms: float | None = None
    cache_hit_tokens: int = 0
    prompt_tokens: int = 0
    max_tokens: int = 0
    candidates: Candidates = Field(default_factory=Candidates)
    decision_us: float = 0.0
    simulated: bool = True


class CompletionRecord(BaseModel):
    request_id: str
    ts: float = Field(default_factory=time.time)
    tenant_id: str | None
    model: str
    route: Route
    replica_id: str | None = None
    ttft_ms: float | None = None
    tpot_ms: float | None = None
    output_tokens: int = 0
    status: int = 200
    simulated: bool = True


class Rollup(BaseModel):
    window_s: float
    requests: int
    by_reason: dict[str, int]
    overflow_share: float
    reject_share: float
    tpot_p50_ms: float | None
    tpot_p90_ms: float | None
    ttft_p50_ms: float | None
    ttft_p90_ms: float | None
    pool_utilization: float | None  # mean running/max_running over healthy replicas


class ServerlessMockConfig(BaseModel):
    tokens_per_minute: int = 20_000
    cold_start_ms: float = 1500
    idle_s: float = 60
    tpot_ms: float = 25
    fail_rate: float = 0.0


class AutoscalerState(BaseModel):
    paused: bool
    provider: str
    replicas: list[str]
    draining: list[str]
    held_for_billing: list[str]
    target: int
    min_replicas: int
    max_replicas: int
    last_action: str | None = None
    last_action_ts: float | None = None
