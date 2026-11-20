# Validity review of proposal2.md

Reviewed 2026-11-20 against primary sources: arXiv 2609.33224 (abstract and HTML full text), akamai.com/cloud/pricing/gpu, akamai.com/cloud/pricing (FAQ), TechDocs "GPU compute instances", "GPUs on LKE", and "Manage nodes and node pools". Workshop repo `../akamai-workshop-seattle-hackathon` was read for the inference setup (the `.env` was not read).

**Verdict:** The facts in the proposal are accurate. The architecture is sound and matches the paper's design. Three gaps matter: hourly billing granularity, a likely explanation for the GPU account limit, and the model-matching problem for overflow. The full GPU version cannot be built today because GPU plans are blocked on the account. We build it with simulated GPU replicas and real (or mock) serverless overflow, and label it as a simulation.

## 1. Claims checked

| Proposal claim | Source says | Status |
|---|---|---|
| PackServe is a gateway-level scheduler over unmodified engines, SGLang testbed, homogeneous pools | "without changing engine scheduling or migrating ongoing requests"; 16 × SGLang (4×H20, TP=4); policy integrated into production gateway | ✅ Verified |
| ~1.9 ms per decision | Mean arrival-to-decision 1.9 ms (vs 132.9 ms for llm-d's XGBoost predictor) | ✅ Verified |
| Prefill model from profiling, decode linear in batch and context, online smoothed correction | T_pf ≈ α0+α1N+α2NC+α3N²; T_iter ≈ β0+β1B+β2S, piecewise at CUDA-graph sizes; multiplicative EMA, weight 0.02, ratio clip [0.5,2], factor clip [1,2] | ✅ Verified (proposal omits the details; the plan uses them) |
| Admission: capacity → recompute budget (0.5) → TPOT → most-decoding packing → cache-aware fallback → reject | Same, with the packing tie-break D–C–Q (most decoding, then most cached tokens, then fewest queued). Fallback is LMetric within the cache-admissible set | ✅ Verified (tie-break added in plan) |
| 13.0–16.8% at 30 ms, 21.1–24.6% at 50 ms, 64 H20, 230B | Abstract: "up to 16.8%" / "up to 24.6%"; 64 H20; MiniMax-M2.5 230B | ✅ Verified (lower bounds not re-checked) |
| 34.7% production, 1,000+ GPUs, ~700B | Same; matched non-concurrent six-day windows, normalized metrics | ✅ Verified (comparison is weaker than a controlled test) |
| Median TPOT only, no tail guarantee; TTFT +66.4% in production | Request-level P90 TPOT ≈ 80 ms under a 50 ms SLO; production mean TTFT +66.4% | ✅ Verified. The tail gap is bigger than the proposal says |
| Colocated P/D, instance-local cache, per-config profiling | Same, plus "not a latency guarantee for heterogeneous, time-varying traffic" | ✅ Verified |
| Blackwell 96 GB at $3/$6/$12/$24 per hour; Ada $0.52–$3.57; Quadro $1.50–$6.00 | Exact match. Prices may vary by region | ✅ Verified |
| Powered-off VM still billed; egress $0.005/GB; no spot tier | All confirmed. GPU plans include **no bundled transfer**, so every GB of egress is billed | ✅ Verified, with an addition |
| LKE GPU: Blackwell and Ada, not Quadro; drivers auto-installed on standard | Same | ✅ Verified |
| Autoscaler min 1 (standard) / 0 (Enterprise), max 100; Pod-driven; drains surplus | Same. Out-of-range pools are not forced back in range | ✅ Verified |
| GPU-pool autoscaling and scale-up timing unknown | Still undocumented on both pages | ⚠️ Still unverified |
| 8-card Blackwell plans expose PCIe topology (vNUMA) | Confirmed | ✅ Verified |

## 2. Gaps and corrections

1. **Hourly billing granularity (missing from the proposal).** The pricing FAQ says hourly billing is rounded up to the nearest hour. Deleting a GPU Linode 5 minutes into a billed hour saves nothing for that hour, and fast scale-down/scale-up churn costs more. **Change:** the autoscaler releases a drained instance near the end of its billed hour (configurable), not right away. This is our own idea and is labeled as one.
2. **GPU "account limit" probably means the $100 deposit.** TechDocs says accounts active for under 90 days or with under $100 of spend may need a $100 deposit before deploying GPU Linodes. That fits the "A limit on your account is preventing the deployment" error better than a capacity problem. Ask event staff or Support about the deposit first.
3. **Model capacity.** Blackwell x8 has 768 GB of VRAM. A ~230B model at FP8 (~230 GB of weights) fits on x4 (384 GB) with KV headroom. The paper's ~700B production model would barely fit on x8 at FP8 with almost no room for KV cache, so it is not a realistic target for one Linode. Ada x4 tops out at 80 GB.
4. **Overflow model matching is the main design risk.** The serverless catalog the event key can use (`qwen3-8-27b`, `gemma-4-26b-a4b-it`) does not include a 230B-class model. **Change:** each model in the pool is either *burst-eligible* (the same model is in the serverless catalog, so overflow gives the same answers) or *pool-only* (overflow is never used and the request is rejected with Retry-After). The gateway enforces this per model and per tenant.
5. **Overflow's real job is covering scale-up delay.** Risk #1 (unknown GPU boot and weight-load time) and the overflow hypothesis address the same problem. Serverless absorbs bursts while the autoscaler adds a replica. That is the "large GPU pool with burst support" pitch. It holds only if serverless rate limits allow it, and those are unpublished.
6. **Egress cost.** With no bundled transfer on GPU plans, streamed tokens to clients are billed egress. Text is small (about 1 KB per 250 tokens), so this is minor. It should still appear in the cost model.
7. **Tail SLOs.** Request-level P90 TPOT was about 1.6× the target in the paper. Personas and onboarding copy should not promise tail latency. Offer median-TPOT tiers only.

## 3. What we can and cannot build today

| Piece | Today | How |
|---|---|---|
| Packing gateway, metadata service, profiler, autoscaler logic | ✅ | Real code |
| GPU replicas | ❌ GPU plans blocked | **Simulated replicas** that produce PackServe's latency model plus noise, behind an OpenAI-compatible streaming API. Labeled SIMULATED everywhere |
| Serverless overflow | ⚠️ Host doesn't resolve on corporate DNS | Real client pointed at `AKAMAI_BASE_URL` when it runs on a Linode; a **mock serverless** with rate limiting and cold starts for local runs |
| Linode autoscaling | ⚠️ Nanodes work, GPUs don't | Provider interface: `sim` (local processes) by default; `linode` (Nanodes as simulated replicas) dry-run by default, real only with explicit opt-in |
| Trace analysis | ✅ | Deterministic stats first, then the LLM narrates (serverless or mock) |
