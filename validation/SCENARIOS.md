# PackServe routing validation scenarios

All traffic runs against a SIMULATED stack (simulated replicas using the PackServe latency model, a local
serverless mock). Expectations are derived from the spec (PackServe scheduler, contracts), not from the
implementation. Run with `uv run python -m validation.run` (see README.md).

## Catalogue

| ID | Intent | Key assertions |
|---|---|---|
| S01-pack-light-load | Light load packs onto the busiest admissible replica | >=75% of requests on one replica; reason=packed |
| S02-prefix-cache-affinity | Shared long prefix goes to the replica that caches it | cache holder chosen despite not being busiest |
| S03-recompute-budget-eta | eta=0.5 budget prunes weak-cache replica; overloaded cache holder loses | candidates capacity=N, budget=2, tpot=1; ratio_b 0.33 < 0.5 < ratio_c 0.74 |
| S04-tpot-30-vs-50 | 30 ms tenant stops packing at cap30 (41-45) and spills; 50 ms keeps packing to 64 | per-replica max differs by tenant |
| S05-fallback-all-exceed-tpot | Every replica would exceed TPOT: LMetric fallback | reason=fallback, tpot candidates 0, budget >= 1 |
| S06-overflow-pool-full | Pool full, burst tenant, burst-eligible model | route=overflow, capacity candidates 0 |
| S07-reject-burst-disabled | Pool full, burst-disabled tenant | 429 + Retry-After, reason=burst_disabled, serverless untouched |
| S08-reject-pool-only-model | packserve-large-230b never overflows | 429 reason=pool_only_model |
| S09-overflow-rejected | Serverless 429/5xx during overflow | 429 + Retry-After reason=overflow_rejected; recovers |
| S10-drain-no-new-requests | Draining replica gets no new requests; in-flight finish | none routed to R; in-flight complete |
| S11-unhealthy-excluded | Failed replica excluded after health detection | none routed to R |
| S12-scaleup-routable | Scale to N+1; new replica becomes routable | post-scale requests packed on new replica |
| S13-scaledown-drain-billing-hold | Scale-down drains least-loaded, holds until billed-hour boundary | one drained, not busiest, held then deleted |
| S14-two-gateways-no-double-booking | Two gateways share state | per-replica max <= cap30 (+4); both gateways seen |
| S15-auth-401 | Missing/invalid credentials | 401 unauthorized, no replica header |
| S16-unknown-model-404 | Unknown model | 404 unknown_model; /v1/models lists qwen |
| S17-decision-latency | Decision latency | decision_us p50 < 2000 |
| S18-correction-slowdown | Online correction learns a 2x-slow replica | predicted TPOT ratio >= 1.3 |

Global invariants are appended to every scenario (headers present, legal route/reason pairs, rejections are
429 with Retry-After, 401/404 mapping, replica header iff route=replica, candidate counts monotone
capacity >= budget >= tpot, packed implies tpot >= 1, fallback implies tpot = 0 and budget >= 1, overflow
implies capacity = 0, packed predicted TPOT within tenant target, routed replica serves requested model,
Simulated header true, headers match /admin/decisions).

## Reference values (validation/model.py, spec latency model for virtual-blackwell-x4)

cap30 in [41,45]; cap50 = 64 (limit never binds); over30 = 60; ratio_b = 0.332 (21 of 32 blocks cached);
ratio_c = 0.740 (8 blocks cached); eta = 0.5.

## Scenario file schema (validation/scenarios/*.yaml)

```yaml
id: S99-name            # must equal the file stem; unique
title: one line
intent: paragraph
tags: [core]
requires: {qwen_replicas: 2, gemma_replicas: 0, large_replicas: 0, gateway2: true, autoscaler: true, serverless: true}
timeout_s: 240
invariants: true        # set false to skip global invariants
steps: [ ... ]
```

Strings of the form `{{ expr }}` are templates (whole-string templates keep native types). Context: `calc.*`,
`g.<group>.top_replica`, `fact.<name>...`, `n_qwen`, `n_large`, `n_gemma`, `sim_hour_s`, `key`, `tpot`.
Path language: `a.b[0].c`, `[*]`, `[k=v]`, pipes `len count sum min max any all first last`.

Actions: `traffic` (name, model, tenant|raw_auth, count, concurrency, pace_ms, max_tokens, background,
gateway: 2), `direct_load` (loads directly on replicas to build state), `direct_prime` (prefix cache priming),
`await`, `cancel`, `wait` (seconds | idle | until), `fault` (replica; fail, slowdown), `metadata` (drain,
delete, register), `autoscaler` (pause, resume, scale; rebind), `serverless` (config, reset, snapshot),
`observe`, `custom` (python hook from validation/hooks.py), `check`.

Check types (all accept `on`, `where`, `slice`, `severity: warn`, `tol`): `count`, `none`, `all` (expect),
`stat` (field, agg count/min/max/mean/sum/pN), `distinct`, `share`, `max_per_value`, `in_set` (values,
negate), `fact`, `compare_stat` (a, b, ratio|diff). Ops: `== != < <= > >= between in not_in contains
not_contains exists truthy`; `tol` only loosens.

## Persona mapping (personas/personas.json `validation_scenario`)

| Persona | validation_scenario | Why / also relevant |
|---|---|---|
| agentic-coding-assistant | S02-prefix-cache-affinity | long shared prefixes; also S03 |
| retail-support-bfcm | S06-overflow-pool-full | BFCM spike overflows to serverless; also S12 |
| live-event-recaps | S06-overflow-pool-full | bursty spikes; also S09 |
| legal-contract-extraction | S08-reject-pool-only-model | large model is pool-only |
| healthcare-scribe | S07-reject-burst-disabled | burst-disabled tenant is rejected, not overflowed |
| game-npc-launch | S04-tpot-30-vs-50 | 30 ms tier; also S12 scale-up |
| realtime-voice-agent (poor fit) | S05-fallback-all-exceed-tpot | tight TPOT cannot be met under load |
| homework-helper-startup (poor fit) | S06-overflow-pool-full | burst-enabled; mostly served by serverless, which is why a reserved pool is a poor fit |
