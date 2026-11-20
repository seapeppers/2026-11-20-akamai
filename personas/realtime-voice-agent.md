# Kai's voice agent (POOR FIT, shown for honesty)

**Profile.** CTO of a seed-stage voice-AI contact-center vendor (archetype) with customer SLAs written on P99 latency. Model `qwen3-8-27b`, tier 30 ms.

**Workload.** baseline 10 rps, peak 30 rps, prompt p50 1,200, output p50 80, shared prefix yes, `reserved_replicas` 2 (illustrative), burst off.

**Why it looks tempting.** Short prompts with a shared prefix pack well; business-hours load is predictable.

**Why it is a poor fit.**
- The scheduler targets **median** TPOT. The PackServe paper reports request-level P90 TPOT around 80 ms under a 50 ms target (~1.6x) and production mean TTFT +66.4% (docs/validity-review.md, arXiv 2609.33224; not re-verified by us). Packing fills replicas up to the target on purpose, which pushes tails out.
- Voice latency is dominated by time-to-first-token, which this design does not optimize and may worsen.
- Burst to serverless adds cold-start and unpublished rate-limit risk, not what a P99 SLA needs.

**Cost sketch.** **Cost assumptions (all ours, not Akamai data):** replica = one RTX PRO 6000 Blackwell Linode ($12/hr for x4 on the 230B-class model, $6/hr for x2 on 8-27B models, from proposal2.md); 720 h/month; per-replica capacity = concurrent streams C / (output tokens x TPOT), with C = 48 streams at 50 ms, 32 at 30 ms, 24 for 40k-token contexts (assumed, to be replaced by the profiler). Serverless is free during beta and its future price is unpublished, so burst cost uses a **hypothetical** $0.20/M input and $0.60/M output tokens. Hourly billing rounding and egress are ignored. PackServe savings (13-25% GPU-hours in the paper) are not stacked on top. Peak reservation 3 x $6 x 720 = $12,960/mo; the scheduler's savings come from running replicas hotter, which is the opposite of what this customer needs. The comparison is not meaningful.

**Recommendation.** Do not onboard on a P99 or TTFT SLA. Offer only if the customer accepts median-TPOT tiers and a non-guaranteed tail. No external source was found for specific voice-agent latency budgets; this persona rests on the paper's tail results.
