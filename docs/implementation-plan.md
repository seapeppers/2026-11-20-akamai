# Implementation plan: PackServe-style GPU pool with serverless burst

Read [validity-review.md](validity-review.md) first. This plan builds the architecture from proposal2.md §3, plus the corrections in that review, so it runs **locally** on one laptop. GPU replicas are simulated. Overflow goes to a mock serverless endpoint by default, or to real Akamai Inference when `AKAMAI_BASE_URL`/`AKAMAI_API_KEY` are set on a host that can resolve it.

The interfaces in [contracts.md](contracts.md) are binding. Code in `packserve/common/` is shared and owned by the lead. Ask for changes; don't edit it in place.

## Repository layout and ownership

```
pyproject.toml                 lead (shared deps; ask lead to add)
packserve/common/              lead — schemas, config, latency model, tokens/prefix hashing
packserve/replica_sim/         core-A  simulated GPU replica (OpenAI-compatible, streaming)
packserve/serverless_mock/     core-A  mock Akamai Inference (rate limit, cold start)
packserve/profiler/            core-A  profiling sweep + least-squares fit -> profiles/*.json
packserve/gateway/             core-B  scheduler + OpenAI-compatible front door + overflow
packserve/metadata/            core-B  shared per-instance state + tenant registry
packserve/autoscaler/          core-C  controller, providers (sim, linode dry-run)
packserve/telemetry/           core-C  trace store (SQLite) + analysis job
deploy/                        core-C  run-local.sh, Procfile-style launcher, docker-compose.yml, linode/
tests/unit/<component>/        each owner
validation/                    validation agent — scenarios + routing verification scripts
personas/                      persona agent — customer personas mapped to use cases
onboarding-web/                web agent — localhost onboarding site
```

## Components

### Simulated replica (`replica_sim`, core-A)
- FastAPI app; one process = one replica = one "virtual GPU Linode" with a named profile (default `virtual-blackwell-x4`).
- `POST /v1/chat/completions` (stream and non-stream). It does not generate real text: it emits lorem-style tokens, timed as follows:
  - Waits in a queue if `running >= max_running`.
  - Prefill delay = `LatencyModel.prefill_ms(new_tokens, cached_tokens)`, where cached tokens come from the replica's own prefix-block LRU (`common.tokens`).
  - Each decode step's delay = `LatencyModel.decode_iter_ms(batch, total_context)` × lognormal noise × `slowdown` fault factor. The batch is shared across all running requests (one asyncio loop ticks the batch).
- `GET /state` → `ReplicaState` (ground truth). `POST /admin/fault` → `{slowdown: float, fail: bool, drop_rate: float}` for scenarios.
- Registers itself with the metadata service on startup and sends a heartbeat every 1 s.

### Mock serverless (`serverless_mock`, core-A)
- OpenAI-compatible `/v1/chat/completions` and `/v1/models` on port 8099.
- Token-bucket rate limit (`tokens_per_minute`, default 20000) → 429 with `Retry-After`. Cold start: the first request after `idle_s` (default 60) adds `cold_start_ms` (default 1500). Per-token latency is about 25 ms ± noise. Catalog: `qwen3-8-27b`, `gemma-4-26b-a4b-it`.
- `POST /admin/config` changes these at runtime, and `POST /admin/reset` resets them.

### Profiler (`profiler`, core-A)
- Runs a prefill grid (new × cached tokens) and a decode sweep (concurrency 1..64) against any OpenAI-compatible endpoint, timing streamed tokens. It fits α/β by least squares and writes `profiles/<name>.json` in the `LatencyProfile` schema. Against a simulated replica it should recover the coefficients the replica was configured with (this is the self-test).

### Gateway (`gateway`, core-B)
- `POST /v1/chat/completions`: OpenAI-compatible, `Authorization: Bearer <tenant key>`. It looks up the tenant from metadata (cached for 5 s), routes the request, and proxies the stream.
- Scheduler (pure function, unit-testable; paper §Admission):
  1. **Capacity guard**: candidates are `healthy && !draining && model match && running < max_running && kv_free >= prompt+max_tokens`.
  2. **Recompute budget**: for each candidate, predicted prefill with its cache hit. Keep those whose `(pf_c − pf_best) / max(pf_nocache − pf_best, ε) ≤ η` (η = 0.5).
  3. **TPOT filter**: predicted TPOT (`decode_iter_ms(A+1, S+ctx) × correction` plus prefill amortized over median output length, omitted if the instance is idle) `≤ tenant.tpot_target_ms`.
  4. **Packing**: pick max by (decoding count, cached tokens, −queued). Route reason `packed`.
  5. **Fallback**: if step 3 is empty but step 2 is not, pick cache-aware least-loaded (LMetric-style: minimize `queued + running − α·cache_hit_ratio`). Reason `fallback`.
  6. **Overflow**: if step 1 is empty and `tenant.burst_enabled && model.burst_eligible`, send the request to serverless. Reason `overflow`. If serverless returns 429/5xx, reject. Reason `overflow_rejected`.
  7. Otherwise **reject** with 429 + `Retry-After`. Reason `no_capacity`, or `pool_only_model` when the model can't overflow.
- Local prefix tree (block hashes per replica) used to estimate cache hits. Instance state is built from the gateway's own dispatch and completion events, merged with metadata snapshots every 250 ms.
- Online correction factor per (replica, batch segment): multiplicative EMA, w = 0.02, ratio clip [0.5,2], factor clip [1,2], updated from measured TPOT on completion.
- Every response carries the routing headers from contracts.md. Every decision is POSTed (fire-and-forget) to telemetry as a `RouteDecision`.
- `GET /admin/decisions?limit=N` (ring buffer, used by validation) and `GET /admin/state`.

### Metadata service (`metadata`, core-B)
- In-memory with optional JSON snapshot to disk. Holds the replica registry (register, heartbeat, mark draining, deregister), merged instance state (gateways POST deltas), and the **tenant registry** (used by the onboarding site), with API key hashing.

### Autoscaler (`autoscaler`, core-C)
- Control loop every 2 s. It reads metadata and telemetry rollups.
- **Scale up** when, over a 30 s window, the overflow+reject rate > 5% or mean pool utilization > 80%. Provider `create()` makes a new replica, which registers itself. Max replicas comes from config.
- **Scale down** when utilization < 40% for 60 s. Pick the **least-loaded** replica and mark it `draining` (the gateway stops routing to it). Wait until `running == 0`. Then **hold until the billed-hour boundary** (`billing_hold=true`; the sim compresses an hour to `sim_hour_s`, default 120 s), and finally call provider `delete()`. Never power off.
- Providers: `SimProvider` (spawns `replica_sim` subprocesses on the next free port 9001–9099) and `LinodeProvider` (Linode API v4, tag `claude-probe`, label `claude-probe-*`, **dry-run unless `PACKSERVE_LINODE_LIVE=1`**, plus cleanup that runs even on failure).
- `GET /admin/state`, `POST /admin/scale {replicas:N}` (manual override for scenarios), and `POST /admin/pause`.

### Telemetry (`telemetry`, core-C)
- `POST /v1/decisions` and `POST /v1/completions` (aggregates only, never prompt text) → SQLite `data/traces.db`. `GET /v1/rollup?window_s=30` → counts by reason, utilization, TPOT p50/p90, TTFT p50/p90, and overflow share.
- `python -m packserve.telemetry.analyze --window 3600`: computes deterministic stats first, then makes one LLM call (serverless or mock) to narrate SLO misses and tuning notes. Writes `reports/analysis-<ts>.md`. Numbers in the report come from the stats, not from the model.

### Deploy (core-C)
- `deploy/run-local.sh up|down|status` starts telemetry, metadata, the mock serverless, N replicas, the gateway and the autoscaler with the contracts.md ports. It writes PIDs to `.run/` and logs to `logs/`. `deploy/docker-compose.yml` is optional.
- `deploy/linode/`: scripts for proposal §7 (Nanode load generators and simulated replicas), dry-run by default.

## Side tracks
- **Validation agent**: `validation/scenarios/*.yaml` (well-defined given/when/then) plus a runner `python -m validation.run [--scenario X]`. The runner brings the stack to a known state through the admin APIs, drives traffic, and asserts on `X-PackServe-*` headers and `/admin/decisions`. Exit code ≠ 0 on failure. It also writes a JUnit XML and a Markdown report.
- **Persona agent**: `personas/` with real-world, cited use cases mapped to tiers. Includes `personas/personas.json` (schema in contracts.md) for the website to consume.
- **Web agent**: `onboarding-web/` on port 3000. Pages: landing, use-case picker, plan builder (model, TPOT tier, reserved pool size, burst toggle) with a cost estimate, signup (→ metadata `POST /v1/tenants`, key shown once), and a playground (calls the gateway and shows the route headers). Works in "demo mode" with a local store if the core isn't running.

## Milestones
1. common/ + replica_sim + serverless_mock + metadata running; one request routes end to end.
2. Full scheduler + overflow + headers; unit tests on the scheduler.
3. Autoscaler (sim provider) + telemetry; `run-local.sh up` brings up everything.
4. Validation scenarios pass; website signs up a tenant that gets routed; personas are linked from the site.
5. (Optional, needs approval) Run with real Akamai Inference from a Nanode and real Linode provider.

## Ground rules for all agents
- Python ≥ 3.11, run through `uv run`. FastAPI + uvicorn + httpx + pydantic v2. Tests use pytest (`uv run pytest tests/unit/<you>`).
- Don't edit files outside your directories. Ask the lead (`SendMessage to: main`) for changes to `packserve/common/`, `pyproject.toml` or `contracts.md`.
- **Never** read `.env` files or print keys. Make **no** real cloud calls (Linode create, real Akamai Inference) without lead approval. Dry-run and mock are the defaults.
- Don't run `git commit`. The lead integrates.
- Label anything simulated as SIMULATED in output, UI and reports.
