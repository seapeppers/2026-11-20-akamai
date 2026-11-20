# PackServe validation harness

Independent black-box checks of the gateway routing behaviour. Everything is SIMULATED (no real GPUs, no real
Akamai/Linode calls).

## Run

```
deploy/run-local.sh up --two-gateways --replicas 3   # S03 needs >=3 qwen replicas, S14 needs gateway2
uv run python -m validation.run              # all scenarios
uv run python -m validation.run --scenario S03 --scenario S06
uv run python -m validation.run --list
uv run python -m validation.run --validate   # static schema check, no stack needed
uv run python -m validation.run --fast       # skip the billed-hour wait in S13
deploy/run-local.sh down
```

Options: `--base-url`, `--gateway2-url`, `--metadata-url`, `--autoscaler-url`, `--serverless-url`,
`--telemetry-url`, `--report-dir` (default reports/validation), `--sim-hour-s` (default 120), `--tag`,
`--no-reports`. Exit codes: 0 pass, 1 FAIL/ERROR, 2 nothing selected or invalid, 3 stack not up.
Outputs: PASS/FAIL table on stdout, `junit.xml`, `report.md`, `results.json`.

Each scenario resets state first (gateway reset, faults, serverless mock, autoscaler paused) and restores
replicas and the autoscaler target afterwards.

## Unit tests of the assertion logic

```
uv run pytest tests/validation
```

## Add a scenario

1. Create `validation/scenarios/S19-my-scenario.yaml` (id must equal the file stem). Schema: SCENARIOS.md.
2. `uv run python -m validation.run --validate`
3. Run it: `uv run python -m validation.run -s S19`
4. Add it to the catalogue in SCENARIOS.md. For logic YAML cannot express, add a hook in `hooks.py`.
