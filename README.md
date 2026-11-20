# PackServe on Akamai: simulated GPU pool with serverless burst

A runnable demo of a PackServe-style LLM serving architecture (proposal2.md): a gateway that **packs** requests onto as few GPU replicas as the TPOT target allows, sends overflow to serverless inference, and an autoscaler that adds replicas under pressure and releases them only at the billed-hour boundary.

**Everything GPU-related here is SIMULATED.** The account cannot create GPU Linodes (see [docs/validity-review.md](docs/validity-review.md)), so replicas are processes that emit lorem tokens with latencies from the PackServe latency model. Numbers from this stack show scheduler behavior, not GPU performance or savings.

## Quickstart

```bash
uv sync
deploy/run-local.sh up                 # telemetry, metadata, mock serverless, 2x qwen3-8-27b + 1x large replica, gateway :8080, autoscaler
deploy/run-local.sh up --two-gateways  # adds gateway gw2 on :8081
deploy/run-local.sh status
curl -si localhost:8080/v1/chat/completions \
  -H 'Authorization: Bearer ps_demo_interactive' -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-8-27b","messages":[{"role":"user","content":"hi"}],"max_tokens":16}' | rg -i '^x-packserve'
deploy/run-local.sh logs gateway-gw1
deploy/run-local.sh down               # stops everything, verifies ports are free
```

Other entry points:

```bash
uv run pytest                                              # all unit tests
uv run python -m validation.run                            # scenario validation (needs the stack up)
uv run python onboarding-web/app.py                        # onboarding site on :3000
uv run python -m packserve.telemetry.analyze --window 3600 # stats report + LLM narrative -> reports/
uv run python -m packserve.telemetry.analyze --dry-run     # stats only, prints the LLM request instead of sending it
curl -s localhost:8070/admin/state | jq                    # autoscaler
curl -XPOST localhost:8070/admin/scale -H 'content-type: application/json' -d '{"replicas":3}'
```

Seed tenants (fixed demo keys, not secrets): `ps_demo_interactive` (30 ms TPOT, burst on), `ps_demo_batch` (50 ms, burst off), `ps_demo_large` (large model, burst on but the model is pool-only).

## Architecture

```
                         clients / onboarding-web (:3000) / validation runner
                                          |
                       Authorization: Bearer <tenant key>
                                          v
   +----------------------------- gateway (:8080, :8081) ------------------------------+
   |  capacity guard -> recompute budget -> TPOT filter -> packing -> fallback           |
   |  -> overflow (burst-eligible models, burst-enabled tenants) -> reject 429           |
   +--------+-----------------------+----------------------------+-----------+---------+
            | route=replica         | route=overflow             |           | decisions + completions
            v                       v                            |           v
   replicas (:9001-9099)     serverless (:8099 mock,             |      telemetry (:8060)
   SIMULATED GPU, one        or real Akamai Inference            |      SQLite data/traces.db
   process each              via AKAMAI_BASE_URL)                |      GET /v1/rollup
            ^                                                    |           ^
            | heartbeat / register                               v           | rollups
            +--------------------------------------->  metadata (:8090)  <----+--- autoscaler (:8070)
                                                       replica registry,          2 s loop, drain ->
                                                       tenant registry            billing hold -> delete
```

Routing response headers (`X-PackServe-Route`, `-Reason`, `-Replica`, `-Candidates`, ...) are listed in [docs/contracts.md](docs/contracts.md).

## Simulated vs real

| Piece | Status |
|---|---|
| Gateway scheduler, metadata, telemetry, autoscaler logic, SQLite traces | Real code |
| GPU replicas (`packserve/replica_sim`) | **SIMULATED**: no model runs; latency from the fitted latency model plus noise |
| Serverless overflow | **Mock** by default (`packserve/serverless_mock`, with a token-bucket rate limit and cold start). Real Akamai Inference only if `AKAMAI_BASE_URL` and `AKAMAI_API_KEY` are set, from a host that can resolve it |
| Autoscaler `SimProvider` | Spawns replica subprocesses locally |
| Autoscaler `LinodeProvider` | **Dry-run unless `PACKSERVE_LINODE_LIVE=1`**; logs the exact API request instead of sending it. Labels `claude-probe-*`, tag `claude-probe`, region `us-sea`; cleanup runs in `finally` |
| `deploy/linode/` Nanode load-generator probe | Dry-run by default; never run live without approval |
| LLM trace analysis | One LLM call that only narrates numbers already computed; the stats tables are authoritative |
| Billed hour | Compressed to `sim_hour_s` (default 120 s) in the sim. Real Linode billing rounds up to the hour, so a drained replica is held until near the hour boundary before it is deleted |

Tail latency is not promised: tiers are median-TPOT only, as in the paper.

## Autoscaler rules

- **Scale up** (every 2 s) when, over 30 s, overflow + reject share is above 5% (with at least 5 requests) or mean pool utilization is above 80%. Capped by the pool's `max`.
- **Scale down** when utilization stays below 40% for 60 s: drain the least-loaded replica (the gateway stops routing to it), wait for `running == 0` on consecutive ticks, hold to the billed-hour boundary, then `provider.delete()` and deregister. It never powers off, and never deletes a replica with running or queued requests.
- Pools: `qwen3-8-27b` 2..6, `gemma-4-26b-a4b-it` 0..2, `packserve-large-230b` 1..2 (profile `virtual-blackwell-x8-large`). Override with `--pool model:min:max[:profile]` or `--config pools.yaml`.
- `--sim-hour-s N`, `--no-billing-hold` (or `POST /admin/billing_hold {"enabled": false}`) for fast scenarios. `POST /admin/scale {"replicas": N, "model": "..."}` sets a manual target; `{"replicas": null}` clears it. `POST /admin/pause` and `/admin/resume`.

## Layout

| Path | What |
|---|---|
| [docs/](docs/) | [implementation-plan.md](docs/implementation-plan.md), [contracts.md](docs/contracts.md), [validity-review.md](docs/validity-review.md) |
| [proposal2.md](proposal2.md) | Original proposal, including the Nanode demo variant (section 7) |
| [validation/](validation/) | Scenario runner that asserts on routing headers and decisions |
| [personas/](personas/) | Customer personas mapped to tiers; `personas.json` feeds the website |
| [onboarding-web/](onboarding-web/) | Onboarding site: plan builder, signup, playground |
| `packserve/` | `common`, `replica_sim`, `serverless_mock`, `profiler`, `gateway`, `metadata`, `telemetry`, `autoscaler` |
| `deploy/` | `run-local.sh`, optional `docker-compose.yml` (experimental, untested), `linode/` probe scripts |

PIDs live in `.run/`, logs in `logs/`, traces in `data/traces.db`, analysis reports in `reports/`.

## Safety

No real Linode or Akamai calls happen by default. `LinodeProvider` and `deploy/linode/*` only send requests when `PACKSERVE_LINODE_LIVE=1` (and `LINODE_TOKEN` is set); the Nanode probe additionally needs `--live` and copies the inference key to the Nanode over ssh, never through user data, argv or logs.
