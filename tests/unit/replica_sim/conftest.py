import socket
import threading
import time

import pytest
import uvicorn

from packserve.replica_sim.app import create_app


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Running:
    def __init__(self, app, port):
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        while not self.server.started:
            time.sleep(0.02)
        return self

    def __exit__(self, *a):
        self.server.should_exit = True
        self.thread.join(5)


@pytest.fixture
def make_replica():
    """Factory: real uvicorn server (ASGI transports don't stream in real time)."""
    started = []

    def make(**kw):
        kw.setdefault("register", False)
        kw.setdefault("seed", 1)
        port = free_port()
        r = Running(create_app(port=port, **kw), port).__enter__()
        started.append(r)
        return r

    yield make
    for r in started:
        r.__exit__()
