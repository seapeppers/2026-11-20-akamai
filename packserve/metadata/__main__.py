import argparse

import uvicorn

from packserve.common import config

from .app import DEFAULT_SNAPSHOT, create_app


def main() -> None:
    ap = argparse.ArgumentParser(description="PackServe metadata service")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=config.METADATA_PORT)
    ap.add_argument("--snapshot", default=DEFAULT_SNAPSHOT, help="JSON snapshot path ('' disables)")
    a = ap.parse_args()
    uvicorn.run(create_app(a.snapshot or None), host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
