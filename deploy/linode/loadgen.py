#!/usr/bin/env python3
"""Stdlib-only streaming load generator (runs on a Nanode). Proposal 7 serverless scaling probe.

Prints one JSON line per ramp step with TTFT / per-token latency / throughput / 429 / 5xx counts.
Reads the key from the file named by --key-file (never argv, never printed).
"""
import argparse
import json
import statistics
import threading
import time
import urllib.error
import urllib.request

PROMPTS = ["Explain what a load balancer does in two sentences.",
           "List three uses of a message queue.",
           "Summarize why caching reduces latency."]


def one_request(base, key, model, max_tokens, prompt):
    body = json.dumps({"model": model, "stream": True, "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(f"{base}/chat/completions", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    t0 = time.perf_counter()
    first, n = None, 0
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            for line in r:
                if line.startswith(b"data: ") and b"[DONE]" not in line:
                    n += 1
                    first = first or time.perf_counter()
    except urllib.error.HTTPError as e:
        return {"status": e.code}
    except Exception:
        return {"status": 0}
    end = time.perf_counter()
    if first is None:
        return {"status": 200, "tokens": 0}
    return {"status": 200, "ttft_ms": (first - t0) * 1000, "tokens": n,
            "tpot_ms": (end - first) * 1000 / max(n - 1, 1)}


def run_step(base, key, model, conc, seconds, max_tokens):
    results, stop = [], time.time() + seconds

    def worker(i):
        while time.time() < stop:
            results.append(one_request(base, key, model, max_tokens, PROMPTS[i % len(PROMPTS)]))

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(conc)]
    t0 = time.time()
    [t.start() for t in ts]
    [t.join() for t in ts]
    wall = time.time() - t0
    ok = [r for r in results if r["status"] == 200 and r.get("ttft_ms") is not None]
    med = lambda k: round(statistics.median([r[k] for r in ok]), 1) if ok else None
    return {"concurrency": conc, "requests": len(results), "ok": len(ok),
            "http_429": sum(r["status"] == 429 for r in results),
            "http_5xx": sum(500 <= r["status"] < 600 for r in results),
            "errors": sum(r["status"] == 0 for r in results),
            "ttft_ms_p50": med("ttft_ms"), "tpot_ms_p50": med("tpot_ms"),
            "tokens_per_s": round(sum(r.get("tokens", 0) for r in ok) / wall, 1)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--key-file", required=True)
    ap.add_argument("--model", default="qwen3-8-27b")
    ap.add_argument("--steps", default="1,2,4")
    ap.add_argument("--step-seconds", type=float, default=20)
    ap.add_argument("--max-tokens", type=int, default=64)
    a = ap.parse_args()
    key = open(a.key_file).read().strip()
    for c in (int(x) for x in a.steps.split(",")):
        res = run_step(a.base_url, key, a.model, c, a.step_seconds, a.max_tokens)
        print(json.dumps(res), flush=True)
        if res["requests"] and res["http_429"] / res["requests"] > 0.2:   # shared budget: stop at sustained 429
            print(json.dumps({"stopped": "sustained 429"}), flush=True)
            break
