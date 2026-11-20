import json

import httpx
import pytest
import respx

from packserve.autoscaler.providers import LinodeProvider, SimProvider


@pytest.fixture(autouse=True)
def _no_live(monkeypatch):
    monkeypatch.delenv("PACKSERVE_LINODE_LIVE", raising=False)
    monkeypatch.delenv("LINODE_TOKEN", raising=False)


@respx.mock
async def test_dry_run_makes_no_network_calls_and_logs_payload(caplog):
    route = respx.route().mock(return_value=httpx.Response(500))  # would fail the test if hit
    p = LinodeProvider()
    assert p.live is False
    with caplog.at_level("WARNING"):
        prov = await p.create("qwen3-8-27b", "virtual-blackwell-x4")
    assert route.call_count == 0
    req = p.requests[0]
    assert req["method"] == "POST" and req["url"] == "https://api.linode.com/v4/linode/instances"
    body = req["json"]
    assert body["label"].startswith("claude-probe-")
    assert body["tags"] == ["claude-probe"] and body["region"] == "us-sea"
    assert body["root_pass"] == "<generated at send time>"
    assert "DRY-RUN" in caplog.text and "claude-probe" in caplog.text
    assert prov.id.startswith("lin-")


@respx.mock
async def test_dry_run_cleanup_deletes_everything_and_never_hits_network():
    route = respx.route().mock(return_value=httpx.Response(500))
    p = LinodeProvider()
    a = await p.create("m", "p")
    await p.create("m", "p")
    assert len(await p.list()) == 2
    left = await p.cleanup()
    assert left == [] and route.call_count == 0
    deletes = [r for r in p.requests if r["method"] == "DELETE"]
    assert len(deletes) == 2
    assert a.id  # silence unused


@respx.mock
async def test_live_requires_flag_env(monkeypatch):
    monkeypatch.setenv("PACKSERVE_LINODE_LIVE", "1")
    monkeypatch.setenv("LINODE_TOKEN", "test-token")
    p = LinodeProvider()
    assert p.live
    route = respx.post("https://api.linode.com/v4/linode/instances").mock(
        return_value=httpx.Response(200, json={"id": 7, "label": "x", "ipv4": ["1.2.3.4"], "tags": ["claude-probe"]}))
    prov = await p.create("m", "p")
    assert route.call_count == 1
    sent = json.loads(route.calls[0].request.content)
    assert sent["root_pass"] != "<generated at send time>"
    assert route.calls[0].request.headers["authorization"] == "Bearer test-token"
    assert prov.url.startswith("http://1.2.3.4")


@respx.mock
async def test_live_cleanup_only_deletes_tagged_and_labeled(monkeypatch):
    monkeypatch.setenv("PACKSERVE_LINODE_LIVE", "1")
    monkeypatch.setenv("LINODE_TOKEN", "t")
    listing = {"data": [
        {"id": 1, "label": "claude-probe-a", "tags": ["claude-probe"]},
        {"id": 2, "label": "someone-elses", "tags": ["claude-probe"]},      # tag but wrong label
        {"id": 3, "label": "claude-probe-b", "tags": []},                   # label but no tag
    ]}
    respx.get(url__startswith="https://api.linode.com/v4/linode/instances").mock(
        side_effect=[httpx.Response(200, json=listing),
                     httpx.Response(200, json={"data": []})])
    d1 = respx.delete("https://api.linode.com/v4/linode/instances/1").mock(return_value=httpx.Response(200, json={}))
    d2 = respx.delete("https://api.linode.com/v4/linode/instances/2").mock(return_value=httpx.Response(200, json={}))
    d3 = respx.delete("https://api.linode.com/v4/linode/instances/3").mock(return_value=httpx.Response(200, json={}))
    assert await LinodeProvider().cleanup() == []
    assert d1.call_count == 1 and d2.call_count == 0 and d3.call_count == 0


async def test_sim_provider_spawns_and_kills(tmp_path):
    prov = SimProvider(run_dir=str(tmp_path / "run"), log_dir=str(tmp_path / "logs"),
                       cmd_prefix=["python3", "-c", "import time,sys; time.sleep(60)", "--"])
    # cmd_prefix + "-m ..." is passed as argv to the sleeper; it ignores them
    p = await prov.create("qwen3-8-27b", "virtual-blackwell-x4")
    assert p.id.startswith("sim-") and 9001 <= int(p.url.rsplit(":", 1)[1]) <= 9099
    proc = prov.procs[p.id]
    assert proc.poll() is None and (tmp_path / "run" / f"replica-{p.id}.pid").exists()
    p2 = await prov.create("qwen3-8-27b", "virtual-blackwell-x4")
    assert p2.url != p.url                                  # next free port
    await prov.delete(p.id)
    assert proc.poll() is not None
    assert await prov.cleanup() == [p2.id]
