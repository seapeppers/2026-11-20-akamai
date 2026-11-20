"""Replica providers: SimProvider (subprocesses) and LinodeProvider (API v4, dry-run by default)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import signal
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from packserve.common import config

log = logging.getLogger("packserve.autoscaler.providers")

PROBE_TAG = "claude-probe"
PROBE_LABEL_PREFIX = "claude-probe-"
LINODE_API = "https://api.linode.com/v4"
LIVE_ENV = "PACKSERVE_LINODE_LIVE"


@dataclass
class Provisioned:
    id: str
    url: str


class Provider(Protocol):
    name: str
    hour_s: float | None   # real billing hour, or None to use the configured sim hour

    async def create(self, model: str, profile: str) -> Provisioned: ...
    async def delete(self, replica_id: str) -> None: ...
    async def list(self) -> list[str]: ...
    async def cleanup(self) -> list[str]: ...


# --------------------------------------------------------------------------- sim

def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


class SimProvider:
    """Spawns `python -m packserve.replica_sim` on the next free port in 9001-9099."""
    name = "sim"
    hour_s = None

    def __init__(self, run_dir: str | None = None, log_dir: str | None = None,
                 cmd_prefix: list[str] | None = None, cwd: str | None = None):
        self.run_dir = Path(run_dir or os.environ.get("PACKSERVE_RUN_DIR", ".run"))
        self.log_dir = Path(log_dir or os.environ.get("PACKSERVE_LOG_DIR", "logs"))
        self.cmd_prefix = cmd_prefix or [sys.executable]
        self.cwd = cwd
        self.procs: dict[str, subprocess.Popen] = {}
        self.ports: dict[str, int] = {}

    def _next_port(self) -> int:
        used = set(self.ports.values())
        for port in config.REPLICA_PORT_RANGE:
            if port not in used and _port_free(port):
                return port
        raise RuntimeError("no free replica port in 9001-9099")

    async def create(self, model: str, profile: str) -> Provisioned:
        port = self._next_port()
        rid = f"sim-{port}"
        cmd = [*self.cmd_prefix, "-m", "packserve.replica_sim", "--port", str(port),
               "--id", rid, "--model", model, "--profile", profile]
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        logf = open(self.log_dir / f"replica-{rid}.log", "ab")
        proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, cwd=self.cwd,
                                start_new_session=True)
        logf.close()
        self.procs[rid], self.ports[rid] = proc, port
        (self.run_dir / f"replica-{rid}.pid").write_text(str(proc.pid))
        log.info("SIMULATED replica spawned: %s", " ".join(cmd))
        return Provisioned(rid, f"http://127.0.0.1:{port}")

    async def delete(self, replica_id: str) -> None:
        pidfile = self.run_dir / f"replica-{replica_id}.pid"
        proc = self.procs.pop(replica_id, None)
        self.ports.pop(replica_id, None)
        if proc is not None:
            await asyncio.to_thread(self._stop_proc, proc)
        elif pidfile.exists():  # replica started by run-local.sh; adopt it via its pid file
            try:
                pid = int(pidfile.read_text().strip())
                if _is_replica_process(pid):
                    os.kill(pid, signal.SIGTERM)
            except (ValueError, ProcessLookupError, PermissionError):
                pass
        pidfile.unlink(missing_ok=True)
        log.info("SIMULATED replica deleted: %s", replica_id)

    @staticmethod
    def _stop_proc(proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)

    async def list(self) -> list[str]:
        return [rid for rid, p in self.procs.items() if p.poll() is None]

    async def cleanup(self) -> list[str]:
        """Stop every replica this provider spawned. Returns ids stopped."""
        ids = list(self.procs)
        for rid in ids:
            await self.delete(rid)
        return ids


def _is_replica_process(pid: int) -> bool:
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True,
                             text=True, timeout=3).stdout
    except Exception:
        return False
    return "replica_sim" in out


# ------------------------------------------------------------------------ linode

class LinodeProvider:
    """Linode API v4. DRY-RUN unless PACKSERVE_LINODE_LIVE=1: requests are logged and recorded in
    `self.requests`, and no network call is ever made. Everything created is labeled
    claude-probe-* and tagged claude-probe; delete/cleanup refuse anything else."""
    name = "linode"
    hour_s = 3600.0

    def __init__(self, region: str | None = None, plan: str | None = None, image: str | None = None,
                 live: bool | None = None, token: str | None = None, base_url: str = LINODE_API):
        self.region = region or os.environ.get("PACKSERVE_LINODE_REGION", "us-sea")
        self.plan = plan or os.environ.get("PACKSERVE_LINODE_TYPE", "g6-nanode-1")
        self.image = image or os.environ.get("PACKSERVE_LINODE_IMAGE", "linode/ubuntu24.04")
        self.live = (os.environ.get(LIVE_ENV) == "1") if live is None else live
        self._token = token  # falls back to LINODE_TOKEN at call time; never logged
        self.base_url = base_url
        self.requests: list[dict] = []           # every request built, sent or not
        self._ids: dict[str, int] = {}           # replica id -> linode id (live) or fake id (dry-run)
        self._fake_seq = 0
        self._dry_instances: dict[int, dict] = {}

    # -- plumbing
    def _auth(self) -> dict[str, str]:
        tok = self._token or os.environ.get("LINODE_TOKEN")
        if not tok:
            raise RuntimeError("LINODE_TOKEN not set (live mode)")
        return {"Authorization": f"Bearer {tok}"}

    async def _request(self, method: str, path: str, payload: dict | None = None,
                       headers: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        req = {"method": method, "url": url, "json": payload, "headers": headers or {}}
        self.requests.append(req)
        if not self.live:
            log.warning("DRY-RUN (set %s=1 to send): %s %s %s", LIVE_ENV, method, url,
                        json.dumps(payload) if payload is not None else "")
            return self._dry_response(method, path, payload)
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.request(method, url, json=payload, headers={**self._auth(), **(headers or {})})
            r.raise_for_status()
            return r.json() if r.content else {}

    def _dry_response(self, method: str, path: str, payload: dict | None) -> dict:
        if method == "POST" and path == "/linode/instances":
            self._fake_seq += 1
            inst = {"id": 900000 + self._fake_seq, "label": payload["label"], "tags": payload["tags"],
                    "ipv4": ["203.0.113.%d" % self._fake_seq], "status": "dry-run"}
            self._dry_instances[inst["id"]] = inst
            return inst
        if method == "GET" and path.startswith("/linode/instances"):
            return {"data": list(self._dry_instances.values()), "page": 1, "pages": 1}
        if method == "DELETE":
            self._dry_instances.pop(int(path.rsplit("/", 1)[-1]), None)
        return {}

    # -- builders (pure; used by tests)
    def create_payload(self, replica_id: str) -> dict:
        payload = {
            "label": f"{PROBE_LABEL_PREFIX}{replica_id}",
            "region": self.region,
            "type": self.plan,
            "image": self.image,
            "tags": [PROBE_TAG],
            "root_pass": secrets.token_urlsafe(24) if self.live else "<generated at send time>",
            "booted": True,
        }
        pub = os.environ.get("PACKSERVE_LINODE_AUTHORIZED_KEY")  # a PUBLIC key, for ssh access
        if pub:
            payload["authorized_keys"] = [pub.strip()]
        return payload

    # -- Provider API
    async def create(self, model: str, profile: str) -> Provisioned:
        rid = f"lin-{secrets.token_hex(3)}"
        inst = await self._request("POST", "/linode/instances", self.create_payload(rid))
        self._ids[rid] = inst["id"]
        ip = (inst.get("ipv4") or ["127.0.0.1"])[0]
        return Provisioned(rid, f"http://{ip}:9001")

    @staticmethod
    def _is_probe(inst: dict) -> bool:
        return PROBE_TAG in inst.get("tags", []) and str(inst.get("label", "")).startswith(PROBE_LABEL_PREFIX)

    async def _list_instances(self) -> list[dict]:
        resp = await self._request("GET", "/linode/instances?page_size=500",
                                   headers={"X-Filter": json.dumps({"tags": PROBE_TAG})})
        return [i for i in resp.get("data", []) if self._is_probe(i)]

    async def delete(self, replica_id: str) -> None:
        lid = self._ids.get(replica_id)
        if lid is None:
            label = f"{PROBE_LABEL_PREFIX}{replica_id}"
            lid = next((i["id"] for i in await self._list_instances() if i["label"] == label), None)
        if lid is None:
            return
        await self._request("DELETE", f"/linode/instances/{lid}")
        self._ids.pop(replica_id, None)

    async def list(self) -> list[str]:
        return [i["label"][len(PROBE_LABEL_PREFIX):] for i in await self._list_instances()]

    async def cleanup(self) -> list[str]:
        """Delete everything tagged claude-probe (and labeled claude-probe-*). Returns leftovers
        after verification (empty list = clean). Safe to call repeatedly; call it from `finally`."""
        for inst in await self._list_instances():
            try:
                await self._request("DELETE", f"/linode/instances/{inst['id']}")
            except Exception:
                log.exception("cleanup: failed to delete %s", inst.get("label"))
        self._ids.clear()
        return [i["label"] for i in await self._list_instances()]


def make_provider(name: str) -> Provider:
    if name == "sim":
        return SimProvider()
    if name == "linode":
        return LinodeProvider()
    raise ValueError(f"unknown provider {name!r}")
