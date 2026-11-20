# replica_sim (SIMULATED GPU replica)

One process = one simulated GPU replica. No model runs and no real text is produced; timing comes from `packserve.common.latency.LatencyModel` with the chosen profile.

```
uv run python -m packserve.replica_sim --port 9001 --id r1 --model qwen3-8-27b --profile virtual-blackwell-x4 [--no-register]
  [--metadata-url URL] [--seed N] [--noise-sigma X]
```

## Behavior
- **Shared continuous batch**: one asyncio ticker advances every decoding request one token per iteration. Iteration time = `decode_iter_ms(batch, total_context)` x lognormal noise (mean 1, sigma from the profile) x `slowdown`. Every stream in the batch therefore sees the same inter-token gap.
- **Admission**: a request holds a slot (`running`) from prefill start until it finishes. If `running >= max_running`, or its `prompt+max_tokens` KV reservation does not fit, it waits (`queued`).
- **Prefill**: `prefill_ms(new, cached)` with `cached` from the replica's own LRU of chained prefix-block hashes (`common.tokens`, capacity `kv_capacity_tokens / block`). Prefills are serialized (one at a time) but do not stall decoding (simplification).
- **Output**: exactly `max_tokens` (or `max_completion_tokens`, default 128) lorem tokens, `finish_reason: "length"`. Usage includes `prompt_tokens_details.cached_tokens`.
- **API**: `POST /v1/chat/completions` (SSE with `stream_options.include_usage`, or JSON), `GET /v1/models`, `GET /state` (`ReplicaState`), `POST /admin/fault` (`slowdown`, `fail` -> 503 + unhealthy, `drop_rate` -> stream aborted mid-way / 500), `GET /healthz`. Every response carries `X-PackServe-Simulated: true` and `X-PackServe-Replica`; `X-PackServe-Request-Id` is echoed.
- **Registration**: POST `/v1/replicas/register`, then a 1 s heartbeat with the full state; re-registers if metadata forgets it (404) or was down. Failures are silent. DELETEs itself on shutdown.
- Client disconnects free the slot and KV reservation.

## Known simplifications
- `running` counts prefilling + decoding requests; `kv_used_tokens` equals actual context (not reservations).
- Model name in the request is not validated.
- Tests use a real uvicorn thread (httpx ASGI transport does not stream incrementally): `uv run pytest tests/unit/replica_sim`.
