# PackServe gateway (core-B)

SIMULATED pool: replicas are simulated GPU processes. The scheduler is real.

```
uv run python -m packserve.gateway --port 8080 --id gw1      # second gateway: --port 8081 --id gw2
uv run python -m packserve.metadata                          # port 8090, snapshot data/metadata.json ('--snapshot ""' disables)
uv run pytest tests/unit/gateway tests/unit/metadata
```

## Request path
1. `Authorization: Bearer <key>`: tenant looked up at metadata `GET /v1/tenants/by-key` (cached 5 s; a stale entry is used if metadata is down). Missing or bad key: 401 `unauthorized`. Metadata down and no cache: 503 (route=rejected, reason=no_capacity).
2. Unknown model, or a model not in the tenant's `models`: 404 `unknown_model`.
3. Features: tokens (`common.tokens.count_tokens`), `max_tokens` (default 256), chained 64-token block hashes, median recent output length.
4. `scheduler.schedule()` (pure, `packserve/gateway/scheduler.py`) picks a replica, overflow, or a rejection.
5. The response is proxied unbuffered (httpx stream to `StreamingResponse`). TTFT is measured from arrival to the first content chunk, TPOT as the mean inter-chunk gap. Non-stream requests record TTFT only. A connect error or replica 5xx is retried once on another replica.
6. `RouteDecision` (after the upstream connects, so overflow failures show the final `overflow_rejected`) and `CompletionRecord` are POSTed fire-and-forget to telemetry. Telemetry being down is tolerated (5 s backoff).

## Scheduler (7 steps)
1. **Capacity guard**: `healthy && !draining && model match && running < max_running && kv_free >= prompt+max_tokens`.
2. **Recompute budget** (eta = 0.5): keep replicas with `(pf_c - pf_best) / max(pf_nocache - pf_best, eps) <= eta`, where `pf_c` is the predicted prefill given that replica's cache hit.
3. **TPOT filter**: `LatencyModel.predicted_tpot_ms(running, total_ctx, prompt, pf, median_output, correction) <= tenant.tpot_target_ms` (30 ms vs 50 ms tiers). Prefill is amortized only if the replica is busy.
4. **Packing**: max by (decoding count, cached tokens, -queued); lowest replica id breaks ties. Reason `packed`.
5. **Fallback** (step 3 empty): minimize `queued + running - 4 * cache_hit_ratio` among the budget set. Reason `fallback`.
6. **Overflow** (step 1 empty): `tenant.burst_enabled` and model burst-eligible -> serverless. Reason `overflow`. Serverless 429/5xx/unreachable -> 429 `overflow_rejected` with the serverless `Retry-After` propagated.
7. **Reject** (429 + `Retry-After`) when step 1 is empty and overflow is not possible:
   - `pool_only_model`: the model is not burst-eligible (checked first, regardless of the tenant's burst flag).
   - `burst_disabled`: model eligible but tenant has `burst_enabled=false`.
   - `no_capacity`: reserved for when overflow is unavailable (and for the 503 metadata-outage case). With default config an eligible, burst-enabled request overflows instead.

## State
- Per replica: running/queued/kv/context counters, a local prefix cache (LRU of chained block hashes, 8192 blocks), and a `CorrectionFactor` (EMA, updated from measured TPOT on completion).
- Dispatch and completion update the counters optimistically and accumulate a pending delta.
- Every 250 ms: flush deltas (`POST /v1/state/delta`), then `GET /v1/replicas` and merge: `view = snapshot + deltas not yet flushed`. Replica ground truth (heartbeats) wins over local guesses. The metadata service adds all gateways' recent deltas on top of the replica's last heartbeat, so gw2 sees gw1's load within about 250-500 ms. `POST /admin/refresh` forces an immediate sync.
- Draining or unhealthy (no heartbeat for 3 s) replicas are excluded once the next snapshot arrives (at most 250 ms).

## Response headers (every response, including rejections)
`X-PackServe-Route` (replica|overflow|rejected), `X-PackServe-Reason`, `X-PackServe-Request-Id`, `X-PackServe-Candidates: capacity=N,budget=N,tpot=N`, `X-PackServe-Simulated`; for route=replica also `X-PackServe-Replica` and `X-PackServe-Predicted-TPOT-Ms`. Rejections add `Retry-After` and an OpenAI-style error body; `unauthorized` is 401, `unknown_model` 404.

## Admin
- `GET /admin/decisions?limit=N`: newest first, ring buffer of 1000 `RouteDecision`s (includes `decision_us`).
- `GET /admin/state`: `{gateway_id, replicas, correction, decision_us:{count,mean,p50,p99,max}, ...}`.
- `POST /admin/reset`: clears decisions, stats, local prefix caches, correction factors and the tenant cache.
- `GET /v1/models`, `GET /healthz`, `POST /admin/refresh`.

## Overflow client
POSTs the original body to `{AKAMAI_BASE_URL}/chat/completions` (default: mock on 8099). If env var `AKAMAI_API_KEY` is set it is sent as a Bearer token. The key is never logged or echoed.
