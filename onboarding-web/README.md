# PackServe onboarding site

Customer onboarding demo on http://127.0.0.1:3000. No build step, no CDN.

```
uv run python onboarding-web/app.py        # ONBOARDING_PORT=3000 by default
uv run pytest tests/unit/onboarding_web
```

Pages: `/` landing (flow diagram, guarantees and non-guarantees), `/use-cases` (from `personas/personas.json`, with a built-in fallback), `/plan` (model, TPOT tier, replicas, burst toggle, cost estimator), `/signup` (POST metadata `/v1/tenants`, key shown once), `/playground` (streamed chat through the gateway, `X-PackServe-*` routing badge, burst test, telemetry rollup).

Config: gateway, metadata and telemetry URLs come from `packserve.common.config` (`PACKSERVE_*_URL` env vars). `ONBOARDING_PERSONAS` and `ONBOARDING_DATA_DIR` override the personas file and local tenant store.

Demo mode: if metadata or the gateway is unreachable, a banner appears, signups go to `onboarding-web/data/tenants.json` (key hashes only) and the playground and burst test return responses labelled SIMULATED. Keys starting `ps_local_` always use the simulated path.

Honesty notes: tiers are median TPOT only; replicas are simulated; burst applies only to burst-eligible models; the serverless price in the estimator is a user-editable assumption (Akamai has not published it); GPU billing is hourly, rounded up.
