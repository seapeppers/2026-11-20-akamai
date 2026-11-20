"""PackServe onboarding site. Run: uv run python onboarding-web/app.py  (serves http://127.0.0.1:3000)."""
import asyncio
import hashlib
import json
import os
import secrets
import sys
import time
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import estimator  # noqa: E402

try:
    from packserve.common import config as C
    GATEWAY_URL, METADATA_URL, TELEMETRY_URL = C.GATEWAY_URL, C.METADATA_URL, C.TELEMETRY_URL
    MODELS = dict(C.MODELS)
except Exception:  # pragma: no cover
    GATEWAY_URL = os.environ.get("PACKSERVE_GATEWAY_URL", "http://127.0.0.1:8080")
    METADATA_URL = os.environ.get("PACKSERVE_METADATA_URL", "http://127.0.0.1:8090")
    TELEMETRY_URL = os.environ.get("PACKSERVE_TELEMETRY_URL", "http://127.0.0.1:8060")
    MODELS = {"qwen3-8-27b": True, "gemma-4-26b-a4b-it": True, "packserve-large-230b": False}

PERSONAS_PATH = Path(os.environ.get("ONBOARDING_PERSONAS", HERE.parent / "personas" / "personas.json"))
DATA_DIR = Path(os.environ.get("ONBOARDING_DATA_DIR", HERE / "data"))
PROBE_TIMEOUT = 1.0

MODEL_INFO = {
    "qwen3-8-27b": "Qwen3 class general model. In the serverless catalog, so it can burst.",
    "gemma-4-26b-a4b-it": "Gemma class instruction model. In the serverless catalog, so it can burst.",
    "packserve-large-230b": "230B-class model (stands in for a MiniMax-M2.5-class model). Pool only: no serverless equivalent, never overflows.",
}

FALLBACK_PERSONAS = [
    {"id": "support-chat", "name": "Customer support copilot", "company_archetype": "SaaS company with a help-desk assistant",
     "industry": "SaaS", "use_case": "Interactive chat with a long shared system prompt.",
     "traffic_shape": {"baseline_rps": 2, "peak_rps": 12, "peak_pattern": "Business-hours spikes", "prompt_tokens_p50": 1500, "output_tokens_p50": 250, "shared_prefix": True},
     "recommended": {"model": "qwen3-8-27b", "tpot_target_ms": 30, "reserved_replicas": 1, "burst_enabled": True},
     "why_pool": "Shared prefixes pack and cache well on a few reserved replicas.", "why_burst": "Spikes are short, so serverless covers them.",
     "not_a_fit_if": "You need guaranteed tail latency.", "real_world_examples": [], "validation_scenario": None},
    {"id": "doc-batch", "name": "Document processing pipeline", "company_archetype": "Back-office automation team",
     "industry": "Finance", "use_case": "Steady batch extraction and summarization.",
     "traffic_shape": {"baseline_rps": 6, "peak_rps": 8, "peak_pattern": "Nearly flat", "prompt_tokens_p50": 3000, "output_tokens_p50": 400, "shared_prefix": False},
     "recommended": {"model": "qwen3-8-27b", "tpot_target_ms": 50, "reserved_replicas": 1, "burst_enabled": False},
     "why_pool": "Flat load keeps reserved GPUs busy.", "why_burst": "Little burst; not needed.",
     "not_a_fit_if": "Load is mostly idle.", "real_world_examples": [], "validation_scenario": None},
    {"id": "large-model-agent", "name": "Large-model reasoning agent", "company_archetype": "Enterprise analytics team",
     "industry": "Enterprise", "use_case": "Agent workflows that need a 230B-class model.",
     "traffic_shape": {"baseline_rps": 1, "peak_rps": 4, "peak_pattern": "Daytime", "prompt_tokens_p50": 4000, "output_tokens_p50": 600, "shared_prefix": True},
     "recommended": {"model": "packserve-large-230b", "tpot_target_ms": 50, "reserved_replicas": 2, "burst_enabled": False},
     "why_pool": "Only the reserved pool can serve this model.", "why_burst": "No serverless equivalent, so burst is unavailable.",
     "not_a_fit_if": "You need elastic capacity for this model.", "real_world_examples": [], "validation_scenario": None},
]

app = FastAPI(title="PackServe onboarding")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


# ---------- helpers ----------
def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _store_path() -> Path:
    return DATA_DIR / "tenants.json"


def _load_store() -> dict:
    try:
        return json.loads(_store_path().read_text())
    except (OSError, ValueError):
        return {}


def _save_store(s: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _store_path().write_text(json.dumps(s, indent=2))


async def _probe(url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as c:
            r = await c.get(url)
            return r.status_code < 500
    except httpx.HTTPError:
        return False


def _bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _sim_headers(route: str, reason: str, replica: str | None = None, tpot: float | None = None) -> dict:
    h = {"X-PackServe-Route": route, "X-PackServe-Reason": reason, "X-PackServe-Simulated": "true",
         "X-Onboarding-Demo": "true", "X-PackServe-Request-Id": secrets.token_hex(8)}
    if replica:
        h["X-PackServe-Replica"] = replica
    if tpot is not None:
        h["X-PackServe-Predicted-TPOT-Ms"] = f"{tpot:.1f}"
    return h


def _local_tenant(key: str) -> dict | None:
    return _load_store().get(_hash(key))


def _sim_route(model: str, burst: bool, index: int = 0, pool_slots: int = 4) -> tuple[str, str, int]:
    """Simulated decision: first `pool_slots` requests get a replica, then overflow/reject."""
    if model not in MODELS:
        return "rejected", "unknown_model", 404
    if index < pool_slots:
        return "replica", "packed", 200
    if not MODELS[model]:
        return "rejected", "pool_only_model", 429
    if burst:
        return "overflow", "overflow", 200
    return "rejected", "burst_disabled", 429


# ---------- pages ----------
def _page(name: str):
    async def view(request: Request):
        return templates.TemplateResponse(request, f"{name}.html", {"page": name, "gateway_url": GATEWAY_URL})
    return view


for _route, _name in [("/", "landing"), ("/use-cases", "use_cases"), ("/plan", "plan"),
                      ("/signup", "signup"), ("/playground", "playground")]:
    app.add_api_route(_route, _page(_name), methods=["GET"], include_in_schema=False)


# ---------- API ----------
@app.get("/api/status")
async def status():
    meta, gw, tel = await asyncio.gather(_probe(f"{METADATA_URL}/v1/tenants"), _probe(f"{GATEWAY_URL}/v1/models"),
                                         _probe(f"{TELEMETRY_URL}/v1/rollup?window_s=1"))
    return {"metadata": meta, "gateway": gw, "telemetry": tel, "demo_mode": not (meta and gw),
            "gateway_url": GATEWAY_URL}


@app.get("/api/models")
async def models():
    return [{"id": m, "burst_eligible": e, "description": MODEL_INFO.get(m, "")} for m, e in MODELS.items()]


@app.get("/api/personas")
async def personas():
    try:
        data = json.loads(PERSONAS_PATH.read_text())
        if isinstance(data, list) and data:
            return {"source": "personas.json", "personas": data}
    except (OSError, ValueError):
        pass
    return {"source": "fallback", "personas": FALLBACK_PERSONAS}


@app.post("/api/estimate")
async def estimate(request: Request):
    body = await request.json()
    model = body.get("model")
    body["burst_eligible"] = MODELS.get(model, True)
    return estimator.estimate(body)


@app.post("/api/signup")
async def signup(request: Request):
    b = await request.json()
    name, email, company = (b.get("name") or "").strip(), (b.get("email") or "").strip(), (b.get("company") or "").strip()
    if not name or "@" not in email or not company:
        return JSONResponse({"error": "Name, a valid email and company are required."}, 422)
    model = b.get("model") or "qwen3-8-27b"
    if model not in MODELS:
        return JSONResponse({"error": f"Unknown model {model}"}, 422)
    plan = {
        "tpot_target_ms": 30 if float(b.get("tpot_target_ms", 50)) <= 30 else 50,
        "burst_enabled": bool(b.get("burst_enabled", True)) and MODELS[model],
        "reserved_replicas": max(1, int(b.get("reserved_replicas") or 1)),
        "models": [model], "persona_id": b.get("persona_id"),
    }
    payload = {"name": f"{company} ({name})", "contact_email": email, **plan}
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.post(f"{METADATA_URL}/v1/tenants", json=payload)
        if r.status_code < 300:
            return {"mode": "live", "tenant": r.json(), "simulated": True,
                    "gateway_base_url": f"{GATEWAY_URL}/v1"}
        return JSONResponse({"error": "Metadata service rejected signup", "detail": r.text[:300]}, 502)
    except httpx.HTTPError:
        pass
    key = "ps_local_" + secrets.token_urlsafe(18)
    tenant = {**payload, "id": "t-local-" + secrets.token_hex(4), "created_at": time.time()}
    store = _load_store()
    store[_hash(key)] = tenant
    _save_store(store)
    return {"mode": "demo", "tenant": {**tenant, "api_key": key}, "simulated": True,
            "gateway_base_url": f"{GATEWAY_URL}/v1"}


async def _gateway_request(key: str, body: dict):
    """Open a streaming connection to the gateway. Returns (client, response) or raises httpx.HTTPError."""
    client = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=1.5))
    try:
        req = client.build_request("POST", f"{GATEWAY_URL}/v1/chat/completions", json=body, headers=_bearer(key))
        return client, await client.send(req, stream=True)
    except Exception:
        await client.aclose()
        raise


def _passthrough_headers(h) -> dict:
    out = {k: v for k, v in h.items() if k.lower().startswith("x-packserve-") or k.lower() == "retry-after"}
    return out


async def _sim_stream(text: str):
    for tok in text.split(" "):
        yield "data: " + json.dumps({"choices": [{"delta": {"content": tok + " "}}]}) + "\n\n"
        await asyncio.sleep(0.03)
    yield "data: [DONE]\n\n"


@app.post("/api/chat")
async def chat(request: Request):
    b = await request.json()
    key = (b.pop("api_key", "") or "").strip()
    prompt = b.pop("prompt", "Hello")
    model = b.get("model") or "qwen3-8-27b"
    body = {"model": model, "stream": True, "max_tokens": int(b.get("max_tokens") or 128),
            "messages": [{"role": "user", "content": prompt}]}
    if not key:
        return JSONResponse({"error": "Missing API key"}, 401)
    if not key.startswith("ps_local_"):
        try:
            client, resp = await _gateway_request(key, body)
        except httpx.HTTPError:
            pass
        else:
            h = _passthrough_headers(resp.headers)
            h["X-Onboarding-Demo"] = "false"
            if resp.status_code != 200:
                data = await resp.aread()
                await client.aclose()
                return JSONResponse(_json_or_text(data), resp.status_code, headers=h)

            async def gen():
                try:
                    async for chunk in resp.aiter_raw():
                        yield chunk
                finally:
                    await resp.aclose()
                    await client.aclose()
            return StreamingResponse(gen(), media_type="text/event-stream", headers=h)
    # Demo mode: simulated reply
    t = _local_tenant(key)
    if key.startswith("ps_local_") and not t:
        return JSONResponse({"error": "Unknown demo key"}, 401)
    burst = bool(t["burst_enabled"]) if t else True
    route, reason, code = _sim_route(model, burst)
    h = _sim_headers(route, reason, "sim-replica-1" if route == "replica" else None,
                     28.4 if route == "replica" else None)
    if code != 200:
        return JSONResponse({"error": {"message": f"Rejected ({reason}) - SIMULATED demo response"}}, code, headers=h)
    text = (f"[SIMULATED reply - gateway not reachable] You asked: {prompt[:80]}. "
            "This text is generated locally by the onboarding site so you can see how streaming and routing badges look.")
    return StreamingResponse(_sim_stream(text), media_type="text/event-stream", headers=h)


def _json_or_text(data: bytes):
    try:
        return json.loads(data)
    except ValueError:
        return {"error": {"message": data.decode(errors="replace")[:300]}}


@app.post("/api/burst")
async def burst(request: Request):
    """Fire N concurrent requests; stream NDJSON tally lines as each completes."""
    b = await request.json()
    key = (b.get("api_key") or "").strip()
    n = max(1, min(int(b.get("n") or 20), 400))
    model = b.get("model") or "qwen3-8-27b"
    if not key:
        return JSONResponse({"error": "Missing API key"}, 401)
    body = {"model": model, "stream": False, "max_tokens": int(b.get("max_tokens") or 64),
            "messages": [{"role": "user", "content": b.get("prompt") or "Say hello in five words."}]}
    live = not key.startswith("ps_local_") and await _probe(f"{GATEWAY_URL}/v1/models")
    t = _local_tenant(key)
    burst_on = bool(t["burst_enabled"]) if t else bool(b.get("burst_enabled", True))

    async def one(i: int, q: asyncio.Queue):
        if live:
            try:
                async with httpx.AsyncClient(timeout=60) as c:
                    r = await c.post(f"{GATEWAY_URL}/v1/chat/completions", json=body, headers=_bearer(key))
                route = r.headers.get("x-packserve-route") or ("rejected" if r.status_code >= 400 else "replica")
                await q.put({"i": i, "route": route, "reason": r.headers.get("x-packserve-reason"),
                             "status": r.status_code, "simulated": r.headers.get("x-packserve-simulated") == "true"})
            except httpx.HTTPError as e:
                await q.put({"i": i, "route": "error", "reason": type(e).__name__, "status": 0, "simulated": False})
        else:
            await asyncio.sleep(0.05 + 0.02 * (i % 7))
            route, reason, code = _sim_route(model, burst_on, i)
            await q.put({"i": i, "route": route, "reason": reason, "status": code, "simulated": True})

    async def gen():
        q: asyncio.Queue = asyncio.Queue()
        tasks = [asyncio.create_task(one(i, q)) for i in range(n)]
        tally = {"replica": 0, "overflow": 0, "rejected": 0, "error": 0}
        yield json.dumps({"start": True, "n": n, "mode": "live" if live else "demo"}) + "\n"
        for _ in range(n):
            r = await q.get()
            tally[r["route"] if r["route"] in tally else "error"] += 1
            yield json.dumps({"result": r, "tally": tally}) + "\n"
        await asyncio.gather(*tasks)
        yield json.dumps({"done": True, "tally": tally}) + "\n"
    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.get("/api/rollup")
async def rollup(window_s: int = 60):
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT) as c:
            r = await c.get(f"{TELEMETRY_URL}/v1/rollup", params={"window_s": window_s})
        if r.status_code == 200:
            return {"available": True, "rollup": r.json()}
    except httpx.HTTPError:
        pass
    return {"available": False, "rollup": None}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("ONBOARDING_PORT", "3000")), log_level="info")
