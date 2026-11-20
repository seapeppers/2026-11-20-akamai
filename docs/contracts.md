# Contracts (binding)

The Python types live in `packserve/common/schemas.py`. Ports and env vars live in `packserve/common/config.py`. If this file and the code disagree, the code wins. Tell the lead when they do.

## Ports (localhost)

| Service | Port | Env override |
|---|---|---|
| gateway | 8080 | `PACKSERVE_GATEWAY_URL` |
| gateway #2 (multi-gateway scenarios) | 8081 | — |
| metadata | 8090 | `PACKSERVE_METADATA_URL` |
| telemetry | 8060 | `PACKSERVE_TELEMETRY_URL` |
| autoscaler | 8070 | `PACKSERVE_AUTOSCALER_URL` |
| serverless mock | 8099 | `AKAMAI_BASE_URL` (default `http://127.0.0.1:8099/v1`) |
| replicas | 9001–9099 | — |
| onboarding web | 3000 | — |

## Models

| Model id | Pool | Burst-eligible | Notes |
|---|---|---|---|
| `qwen3-8-27b` | yes | **yes** | Same id as the serverless catalog, so overflow gives the same answers |
| `gemma-4-26b-a4b-it` | yes | **yes** | |
| `packserve-large-230b` | yes | **no** | Pool-only. Stands in for a MiniMax-M2.5-class model. Never overflows |

## Routing response headers (gateway → client)

| Header | Values |
|---|---|
| `X-PackServe-Route` | `replica` \| `overflow` \| `rejected` |
| `X-PackServe-Reason` | `packed` \| `fallback` \| `overflow` \| `overflow_rejected` \| `no_capacity` \| `pool_only_model` \| `burst_disabled` \| `unauthorized` \| `unknown_model` |
| `X-PackServe-Replica` | replica id (when route=replica) |
| `X-PackServe-Request-Id` | uuid, matches `RouteDecision.request_id` |
| `X-PackServe-Predicted-TPOT-Ms` | float (when route=replica) |
| `X-PackServe-Candidates` | `capacity=N,budget=N,tpot=N` counts after each filter |
| `X-PackServe-Simulated` | `true` while replicas are simulated |

Rejections return HTTP 429 with `Retry-After` and an OpenAI-style error body. `unauthorized` returns 401. `unknown_model` returns 404.

## HTTP APIs

### Replica (`replica_sim`)
- `POST /v1/chat/completions` — OpenAI-compatible. Accepts the extra header `X-PackServe-Request-Id`.
- `GET /state` → `ReplicaState`
- `POST /admin/fault` body `FaultConfig` → `ReplicaState`
- `GET /healthz` → `{"ok": true}`

### Metadata
- `POST /v1/replicas/register` body `ReplicaRegistration` → `ReplicaState`
- `POST /v1/replicas/{id}/heartbeat` body `ReplicaState`
- `POST /v1/replicas/{id}/drain` → marks draining. `DELETE /v1/replicas/{id}` → deregisters
- `GET /v1/replicas` → `list[ReplicaState]`
- `POST /v1/state/delta` body `StateDelta` (from gateways)
- `POST /v1/tenants` body `TenantCreate` → `TenantCreated` (contains the plaintext `api_key` once)
- `GET /v1/tenants` → `list[Tenant]` (no keys). `GET /v1/tenants/by-key` header `Authorization: Bearer` → `Tenant`
- `PATCH /v1/tenants/{id}` body partial `TenantCreate`
- Seed tenants are created on startup (see `config.SEED_TENANTS`). Their keys are fixed so tests can use them.

### Gateway
- `POST /v1/chat/completions`, `GET /v1/models`
- `GET /admin/decisions?limit=100` → `list[RouteDecision]` (newest first)
- `GET /admin/state` → `{"replicas": [ReplicaView...], "correction": {...}}`
- `POST /admin/reset` clears the decision ring buffer and correction factors

### Telemetry
- `POST /v1/decisions` body `RouteDecision`. `POST /v1/completions` body `CompletionRecord`
- `GET /v1/rollup?window_s=30` → `Rollup`

### Autoscaler
- `GET /admin/state` → `AutoscalerState`. `POST /admin/scale` body `{"replicas": N}`. `POST /admin/pause` / `POST /admin/resume`

### Serverless mock
- `POST /v1/chat/completions`, `GET /v1/models`, `POST /admin/config` body `ServerlessMockConfig`, `POST /admin/reset`, `GET /admin/stats`

## personas/personas.json

```json
[{"id": "kebab-id", "name": "…", "company_archetype": "…", "industry": "…",
  "use_case": "…", "real_world_examples": [{"org": "…", "summary": "…", "source_url": "…"}],
  "traffic_shape": {"baseline_rps": 0, "peak_rps": 0, "peak_pattern": "…", "prompt_tokens_p50": 0, "output_tokens_p50": 0, "shared_prefix": true},
  "recommended": {"model": "qwen3-8-27b", "tpot_target_ms": 50, "reserved_replicas": 2, "burst_enabled": true},
  "why_pool": "…", "why_burst": "…", "not_a_fit_if": "…", "validation_scenario": "scenario-id or null"}]
```
