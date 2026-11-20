"""Shared ports, URLs, model catalog and seed tenants. See docs/contracts.md."""
import os

GATEWAY_PORT = 8080
GATEWAY2_PORT = 8081
METADATA_PORT = 8090
TELEMETRY_PORT = 8060
AUTOSCALER_PORT = 8070
SERVERLESS_MOCK_PORT = 8099
REPLICA_PORT_RANGE = range(9001, 9100)
ONBOARDING_WEB_PORT = 3000

GATEWAY_URL = os.environ.get("PACKSERVE_GATEWAY_URL", f"http://127.0.0.1:{GATEWAY_PORT}")
METADATA_URL = os.environ.get("PACKSERVE_METADATA_URL", f"http://127.0.0.1:{METADATA_PORT}")
TELEMETRY_URL = os.environ.get("PACKSERVE_TELEMETRY_URL", f"http://127.0.0.1:{TELEMETRY_PORT}")
AUTOSCALER_URL = os.environ.get("PACKSERVE_AUTOSCALER_URL", f"http://127.0.0.1:{AUTOSCALER_PORT}")
# Real Akamai Inference when set (from a host that resolves it); mock otherwise.
SERVERLESS_BASE_URL = os.environ.get("AKAMAI_BASE_URL", f"http://127.0.0.1:{SERVERLESS_MOCK_PORT}/v1")
SERVERLESS_API_KEY_ENV = "AKAMAI_API_KEY"  # read at call time; never log it

SIMULATED = os.environ.get("PACKSERVE_SIMULATED", "1") == "1"

# model id -> burst eligible (same model exists in the serverless catalog)
MODELS: dict[str, bool] = {
    "qwen3-8-27b": True,
    "gemma-4-26b-a4b-it": True,
    "packserve-large-230b": False,
}
SERVERLESS_CATALOG = [m for m, eligible in MODELS.items() if eligible]

DEFAULT_PROFILE = "virtual-blackwell-x4"

# Recompute budget (paper default) and correction-factor parameters.
RECOMPUTE_BUDGET_ETA = 0.5
CORRECTION_WEIGHT = 0.02
CORRECTION_RATIO_CLIP = (0.5, 2.0)
CORRECTION_FACTOR_CLIP = (1.0, 2.0)

# Fixed demo keys so tests and scenarios can authenticate. Not secrets.
SEED_TENANTS = [
    {"id": "t-interactive", "name": "Seed interactive", "api_key": "ps_demo_interactive",
     "tpot_target_ms": 30, "burst_enabled": True, "models": ["qwen3-8-27b", "gemma-4-26b-a4b-it"]},
    {"id": "t-batch", "name": "Seed batch", "api_key": "ps_demo_batch",
     "tpot_target_ms": 50, "burst_enabled": False, "models": ["qwen3-8-27b", "packserve-large-230b"]},
    {"id": "t-large", "name": "Seed large-model", "api_key": "ps_demo_large",
     "tpot_target_ms": 50, "burst_enabled": True, "models": ["packserve-large-230b"]},
]
