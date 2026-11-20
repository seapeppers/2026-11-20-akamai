import argparse

import uvicorn

from packserve.common import config
from packserve.telemetry.app import create_app


def main() -> None:
    p = argparse.ArgumentParser(description="PackServe telemetry service")
    p.add_argument("--port", type=int, default=config.TELEMETRY_PORT)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--db", default=None, help="SQLite path (default data/traces.db)")
    a = p.parse_args()
    uvicorn.run(create_app(a.db), host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
