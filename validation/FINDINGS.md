# Validation findings

Latest live run (SIMULATED stack, `deploy/run-local.sh up --two-gateways --replicas 3`, full suite without
`--fast`): 18/18 scenarios PASS, 0 FAIL, 0 ERROR, 0 SKIP. Reports: reports/validation/{junit.xml,report.md,results.json}.

## Implementation bugs

None confirmed. Failures seen during harness bring-up were all harness defects (below), not gateway,
autoscaler or replica bugs.

## Harness defects found and fixed (for the record)

| Symptom | Cause | Fix |
|---|---|---|
| S05 failed only when run after S04 (fallback went to the busier replica) | Prompt seeds were `run_id/group`, so S04 and S05 groups named `tight`/`loose` produced identical prompts; replica prefix cache from S04 produced a 64-token hit and the recompute budget legitimately pruned the other replica | seed now `run_id/scenario_id/group`, plus a per-process nonce |
| S10/S11 checks selected all groups | Unquoted `on:` key is parsed as boolean True by YAML 1.1 | loader maps True keys back to "on" |
| S09 got HTTP 400 from serverless | The mock returns 400 (not 429) when a single request costs more than tokens_per_minute | scenario uses tokens_per_minute=300 and drains the bucket first |
| S12/S13 scale never converged | Harness counted all pools, and the controller reconciles a manual target only when NOT paused | `ensure_scale` is per-model and resumes the autoscaler; manual target cleared at cleanup |

## Spec ambiguities (behaviour observed, not asserted as bugs)

1. `POST /admin/scale` while paused only records `manual_target`; no scaling happens until resume. The contract
   says only "pause/resume". Harness resumes before scaling.
2. `/admin/scale` without `model` targets the first configured pool; `AutoscalerState.target` is the sum
   across pools. Contract shows only `{"replicas": N}`.
3. Precedence for a burst-disabled tenant asking for a pool-only model with a full pool
   (`burst_disabled` vs `pool_only_model`) is unspecified; S07/S08 avoid that combination.
4. Tenant model entitlement (e.g. interactive tenant requesting packserve-large-230b) is not specified; S16
   records the result as a warn-only observation.
5. `no_capacity` rejection: no scenario can reach it distinct from overflow/pool_only_model/burst_disabled.
6. ReplicaView, correction dict and /admin/stats shapes are only partially specified; S18 asserts on
   predicted TPOT, with the correction state exposure as a warn-only check.
7. SSE chunk count vs token count is not specified (S10 truncation check is warn-only).
8. A serverless request larger than the per-minute token limit yields upstream 400, which the gateway
   relays as route=overflow with status 400 (not overflow_rejected). Whether that should map to a 429 or a
   4xx pass-through is unspecified.

## Repro

```
deploy/run-local.sh up --two-gateways --replicas 3
uv run python -m validation.run            # all (S13 waits for the billed-hour boundary; use --fast to skip)
uv run python -m validation.run -s S05 -s S12
deploy/run-local.sh down
```
