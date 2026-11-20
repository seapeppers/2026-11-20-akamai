# Dana's agentic coding assistant (pool-only, no burst)

**Profile.** Platform lead at a Series B coding-agent startup (~40k weekly developers, archetype not a real company). Serves a 230B-class coding model (stand-in: `packserve-large-230b`). Burst: **off** (pool-only model).

**Day in the life.** 8 am Europe starts the ramp, US East joins at 9, the pool hits ~12 rps around 1 pm ET; after 8 pm and on weekends it drops to ~3 rps from CI bots and night owls. Dana's on-call pain: GPU bill climbs with users but prompts are 95% identical turn to turn.

**Workload.** baseline 3 rps, peak 12 rps (weekday hours), prompt p50 40,000 tokens, output p50 400 (ratio ~100:1), shared prefix yes, TPOT tier 50 ms, `reserved_replicas` = 3.

**Real-world grounding.**
- Cursor (Pragmatic Engineer, https://newsletter.pragmaticengineer.com/p/cursor): "billions of code completions, daily", 100x growth in 12 months, Tab suggestions "ideally less than a second". The visible text does not discuss caching.
- Manus via a secondary summary (https://agentic-ai.readthedocs.io/en/latest/ContextEngineering/manus/): KV-cache hit rate is "the single most important metric for a production agent"; ~100:1 input:output over ~50 tool calls. Original post could not be fetched (connection reset).
- SGLang RadixAttention (https://www.lmsys.org/blog/2024-01-17-sglang/): KV cache retained in a radix tree so multi-turn history and shared prompts are reused; "up to 5 times higher throughput" on the authors' benchmarks.
- *Extrapolation:* these show agent loops are prefix-heavy; they do not say how much Cursor or Manus would save on PackServe.

**Fit.** Pool: strong. Long shared prefixes make cache-aware packing (recompute budget 0.5) valuable, and a coding agent's steady daytime base justifies reserved GPUs. Burst: **none**, the model is pool-only; growth is handled by scheduled pre-scaling. Tier: 50 ms (an agent turn streams a diff; 20 tok/s is above reading speed and the 30 ms tier would reduce packing density).

**Cost sketch.** **Cost assumptions (all ours, not Akamai data):** replica = one RTX PRO 6000 Blackwell Linode ($12/hr for x4 on the 230B-class model, $6/hr for x2 on 8-27B models, from proposal2.md); 720 h/month; per-replica capacity = concurrent streams C / (output tokens x TPOT), with C = 48 streams at 50 ms, 32 at 30 ms, 24 for 40k-token contexts (assumed, to be replaced by the profiler). Serverless is free during beta and its future price is unpublished, so burst cost uses a **hypothetical** $0.20/M input and $0.60/M output tokens. Hourly billing rounding and egress are ignored. PackServe savings (13-25% GPU-hours in the paper) are not stacked on top.
- Reserve-for-peak: 10 replicas x $12 x 720 = **$86,400/mo**.
- Reserved 3 + pre-scale to 10 for ~220 weekday-peak hours: $25,920 + 7 x $12 x 220 = **$44,400/mo (-49%)**.
- Overflow is not an option here; sending 266B input tokens/month to serverless is moot (model absent).

**Risks / not a fit if.** Inline completions needing sub-second TTFT (TTFT can rise under packing); P99 TPOT guarantees; frontier closed models; scale-up gap (GPU boot + weight load unknown, no overflow to cover it, so pre-scale ahead of 8 am).
