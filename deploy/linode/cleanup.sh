#!/usr/bin/env bash
# Delete every Linode tagged claude-probe (and labeled claude-probe-*). DRY-RUN unless PACKSERVE_LINODE_LIVE=1.
# Needs LINODE_TOKEN only in live mode. Safe to run repeatedly.
set -euo pipefail
cd "$(dirname "$0")/../.."
exec uv run python - <<'PY'
import asyncio, logging
from packserve.autoscaler.providers import LinodeProvider
logging.basicConfig(level=logging.WARNING, format="%(message)s")
async def main():
    p = LinodeProvider()
    print("mode:", "LIVE" if p.live else "DRY-RUN (set PACKSERVE_LINODE_LIVE=1 to delete for real)")
    left = await p.cleanup()
    print("leftover:", left or "none")
asyncio.run(main())
PY
