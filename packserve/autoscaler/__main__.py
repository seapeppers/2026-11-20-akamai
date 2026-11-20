"""uv run python -m packserve.autoscaler --provider sim"""
import argparse
import logging
import os

import uvicorn
import yaml

from packserve.autoscaler.app import create_app
from packserve.autoscaler.controller import Controller
from packserve.autoscaler.policy import DEFAULT_POOLS, PoolConfig, ScalingParams
from packserve.autoscaler.providers import make_provider
from packserve.common import config


def load_pools(path: str | None, specs: list[str]) -> list[PoolConfig]:
    if path:
        with open(path) as f:
            raw = yaml.safe_load(f)["pools"]
        return [PoolConfig(model=m, min=v["min"], max=v["max"], profile=v.get("profile", config.DEFAULT_PROFILE))
                for m, v in raw.items()]
    if specs:  # model:min:max[:profile]
        out = []
        for s in specs:
            parts = s.split(":")
            out.append(PoolConfig(parts[0], int(parts[1]), int(parts[2]),
                                  parts[3] if len(parts) > 3 else config.DEFAULT_PROFILE))
        return out
    return list(DEFAULT_POOLS)


def main() -> None:
    p = argparse.ArgumentParser(description="PackServe autoscaler")
    p.add_argument("--provider", choices=["sim", "linode"], default="sim")
    p.add_argument("--port", type=int, default=config.AUTOSCALER_PORT)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--config", help="YAML: pools: {model: {min, max, profile}}")
    p.add_argument("--pool", action="append", default=[], help="model:min:max[:profile] (repeatable)")
    p.add_argument("--sim-hour-s", type=float,
                   default=float(os.environ.get("PACKSERVE_SIM_HOUR_S", 120)),
                   help="length of one billed hour in seconds (sim provider)")
    p.add_argument("--no-billing-hold", action="store_true",
                   default=os.environ.get("PACKSERVE_BILLING_HOLD", "1") == "0",
                   help="delete drained replicas immediately instead of at the billed-hour boundary")
    p.add_argument("--no-cleanup-on-exit", action="store_true",
                   help="leave spawned replicas running when the autoscaler exits")
    a = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    provider = make_provider(a.provider)
    controller = Controller(provider, load_pools(a.config, a.pool),
                            ScalingParams(sim_hour_s=a.sim_hour_s, billing_hold=not a.no_billing_hold),
                            interval_s=a.interval)
    controller.cleanup_on_exit = not a.no_cleanup_on_exit
    uvicorn.run(create_app(controller), host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
