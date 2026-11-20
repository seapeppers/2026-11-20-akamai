# End-to-end demo: Black Friday retail support

This walkthrough follows the **Black Friday retail-support persona** ([personas/retail-support-bfcm.md](personas/retail-support-bfcm.md)) from onboarding through a traffic burst and back to normal. Each step matches commands already run on this stack, and every number below comes from those runs.

> **Simulated GPUs.** GPU plans are blocked on the account, so the replicas are simulated (they follow the PackServe latency model) and overflow goes to a local mock of Akamai Inference. The measurements show that the mechanisms work. They are not real GPU performance. Dollar projections come from the PackServe paper or from labelled assumptions.

## Step 0: Start the stack and the website

```bash
deploy/run-local.sh up --two-gateways --replicas 3
uv run python onboarding-web/app.py        # http://127.0.0.1:3000
```

This starts:
- **Two gateways** (`:8080` and `:8081`) running the packing scheduler.
- **The shared state service** (`:8090`), holding the replica registry and tenant accounts.
- **Simulated Blackwell x4 replicas**: three for `qwen3-8-27b` and one for the 230B model, each with 64 request slots.
- **A mock Akamai Inference endpoint** (`:8099`), rate-limited to 20,000 tokens per minute.
- **The telemetry store** (`:8060`) and **the autoscaler** (`:8070`).

Stop everything with `deploy/run-local.sh down && pkill -f onboarding-web/app.py`.

## Step 1: Onboarding (website)

1. On **/use-cases**, the customer picks "Retail support, Black Friday/Cyber Monday". The persona file says their peak runs about 6x their baseline.
2. On **/plan**, the form fills in `qwen3-8-27b`, the 50 ms tier and burst on.
   - Burst is allowed because the same model is in the serverless catalog, so overflow gives the same answers.
   - The cost estimate compares reserving GPUs for peak with reserving for baseline plus burst.
3. On **/signup**, the site creates a tenant in the shared state service and shows the API key once.

**Value:** the customer gets an OpenAI-compatible endpoint with a latency target and burst policy attached to their key. Their code doesn't change.

## Step 2: Normal traffic is packed (S01)

The customer sends a chat from **/playground**. In our run it came back:

```
x-packserve-route: replica      x-packserve-reason: packed
x-packserve-replica: sim-9001   x-packserve-predicted-tpot-ms: 10.500
x-packserve-candidates: capacity=3,budget=3,tpot=3
```

For each request, the gateway:
1. Drops replicas that have no free slot or cache room.
2. Drops replicas that would have to recompute too much of the prompt.
3. Predicts the per-token latency (TPOT) on each remaining replica and drops any over the tenant's 50 ms target.
4. Picks the **busiest** replica that still meets the target.

**Value (the service kicks in here): packing.** S01 sends 9 requests to 4 idle replicas, and all 9 land on `sim-9001`. Predicted latency peaked at 15.7 ms against the 30 ms target. A normal load balancer would spread them across all 4, which keeps every GPU busy so none can be released. Each scheduling decision takes about **23 µs at the median** (p99 50 µs), well under the paper's 1.9 ms.

## Step 3: Latency headroom becomes fewer GPUs (S04)

S04 sends 50 long-running requests from two tenants:

| Tenant tier | Where the 50 requests went | Peak predicted latency |
|---|---|---|
| 30 ms | 45 on `sim-9001`, 5 on `sim-9002` | 29.99 ms |
| 50 ms | All 50 on `sim-9001` | 31.75 ms |

**Value: this is the PackServe trade.** A customer who accepts 50 ms instead of 30 ms lets the scheduler use that headroom to fit more requests on one GPU.

| | 50 ms tier | 30 ms tier |
|---|---|---|
| Replicas used | 1 of 4 | 2 of 4 |
| Replicas left idle for the autoscaler to delete | 3 | 2 |
| Saving at Blackwell x4 ($12/hr each), if deleted | $36/hr | $24/hr |

A least-loaded balancer would put about 12–13 requests on each of the 4 replicas and release nothing.

**What that number does and doesn't mean:** 75% is a best case at low load on simulated GPUs. The only real-hardware evidence is the paper's: **13.0–16.8% fewer GPU-hours at a 30 ms target and 21.1–24.6% at 50 ms**, on 64 H20 GPUs. Expect something in that range, not 75%.

## Step 4: Repeated system prompts hit the cache (S02, S03)

Support bots send the same long system prompt and policy text on every request. The gateway tracks which replica holds which prompt prefix and sends repeats there.

**Value:** less prompt processing. On the simulated replica a cache hit cut time to first token by more than half. The recompute budget (S03) stops the gateway from following the cache onto a replica that would miss the latency target.

## Step 5: The Black Friday burst

From **/playground**, the customer runs a burst test of 300 concurrent requests. Our run:

```json
{"tally": {"replica": 192, "overflow": 75, "rejected": 33}}
```

| What happened | Requests | Why |
|---|---|---|
| Packed on replicas | 192 | 3 replicas × 64 slots, all full |
| Overflowed to serverless | 75 | No replica had a free slot, the tenant has burst on, and the model is burst-eligible (S06) |
| Rejected with 429 and `Retry-After` | 33 | The mock serverless hit its 20,000 tokens/min limit (S09) |

**Value: burst support.** Without burst, everything over the pool's capacity is rejected: 108 of 300, or **36%**. With burst, rejections fell to **11%**, and the 75 overflow requests ran on the same model. The 33 rejections come from the mock's made-up rate limit, so real results depend on Akamai's limits, which are unpublished.

Two scenarios show the guardrails:
- **S07 (healthcare scribe, burst turned off):** the request is rejected with `burst_disabled`, and nothing goes to serverless.
- **S08 (legal, 230B model):** the request is rejected with `pool_only_model`. The service never silently swaps in a different model.

## Step 6: The autoscaler adds capacity (S12)

Within its 2-second loop, the autoscaler sees that overflow plus rejections are above 5% of traffic. A 400-request smoke run logged "scale_up (pressure 64% > 5%)". It first revives any drained replica of that model, and only then creates a new one.

**Value:** serverless carries the burst while the new GPU starts. That gap is the main risk in the proposal, because GPU boot time on Linode is unknown. Overflow is what covers it.

## Step 7: After the peak, scale down without waste (S10, S13)

When pool utilization stays under 40% for 60 seconds:
1. The autoscaler picks the **least-loaded** replica and marks it draining.
2. The gateway stops sending it new requests within about 250 ms. Requests already running finish (S10).
3. Once the replica is idle, the autoscaler **holds it until the end of its billed hour** (compressed to 120 s in simulation) and then deletes it. In one run the delete happened 5 s before the boundary.

**Value:** billed GPU-hours come down without killing anyone's requests. This is where packing turns into dollars. Linode bills hourly, rounded up, so deleting earlier saves nothing, and a powered-off Linode is still billed. If load returns during the hold, the replica is put back into service.

## Step 8: Check it and get the report

```bash
uv run python -m validation.run                               # all 18 scenarios
uv run python -m validation.run --scenario S06-overflow-pool-full
uv run python -m packserve.telemetry.analyze --window 3600
```

The rollup from our run showed median per-token latency of 11.5 ms (p90 25.6 ms) and median time to first token of 94 ms. The p90 time to first token was **1.8 s**, which reflects the higher first-token latency the paper warns about.

## Monthly value for this persona (projection, not measured)

These figures are from [personas/retail-support-bfcm.md](personas/retail-support-bfcm.md):

| Option | Cost per month |
|---|---|
| Reserve GPUs for peak (24 replicas) | $103,680 |
| Reserve for baseline (4 replicas) plus serverless burst | about $21,400 (**−79%**) |

That projection rests on three assumptions:
- Per-replica capacity is the persona's estimate, not profiler output.
- The serverless price is a placeholder, because Akamai hasn't published one.
- It assumes $6/hr Blackwell x2 replicas, while the simulation models x4.

PackServe's own 13–25% saving would apply on top of the reserved part. The overflow volume this needs has to fit within Akamai's real serverless rate limits, which are unknown.

## Summary of where the value comes from

| Step | Service mechanism | Measured in simulation | Real-world evidence |
|---|---|---|---|
| 2–3 | Packing to the latency target | 50 requests on 1 of 4 replicas at 50 ms; 2 of 4 at 30 ms | 13–25% fewer GPU-hours (paper) |
| 4 | Prefix-cache routing | Time to first token more than halved on a cache hit | Paper's recompute-budget results |
| 5 | Same-model serverless burst | Rejections cut from 36% to 11% in a 300-request burst | None; Akamai's limits unpublished |
| 6–7 | Drain, then billing-aware delete | No requests lost; deleted 5 s before the billed hour ended | Linode's hourly billing rules |

The step that would turn the simulated numbers into real ones is profiling an actual Blackwell Linode. That is blocked until the GPU account limit is lifted (see [docs/validity-review.md](docs/validity-review.md)).
