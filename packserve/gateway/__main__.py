import argparse

import uvicorn

from packserve.common import config

from .app import create_app


def main() -> None:
    ap = argparse.ArgumentParser(description="PackServe gateway")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=config.GATEWAY_PORT)
    ap.add_argument("--id", default="gw1")
    a = ap.parse_args()
    uvicorn.run(create_app(a.id), host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
