# Priya's contract extraction (pool-only, queue-tolerant)

**Profile.** Legal-engineering director at a law firm/legal-tech vendor (archetype). Extracts parties, terms, change-of-control and indemnity clauses from long contracts into JSON. Model: 230B-class (`packserve-large-230b`), burst **off**.

**Day in the life.** A diligence data room of ~90 contracts lands at 4 pm; associates need first-pass summaries by morning. Priya's pipeline retries on 429 with `Retry-After` and works through the night.

**Workload.** baseline 2 rps, peak 5 rps (deal crunch), prompt p50 30,000, output p50 800, shared prefix yes (extraction schema and instructions), tier 50 ms, `reserved_replicas` = 4.

**Real-world grounding.**
- Thomson Reuters CoCounsel / Century Communities (https://legal.thomsonreuters.com/en/insights/case-studies/turbocharging-legal-tasks-with-cocounsel-generative-ai, Mar 17 2025): tools used for "reviewing contracts, summarizing documents, extracting data from large volumes of files"; 87 land contracts in one M&A deal; avoided reading 10,000 emails. Vendor-reported, anecdotal, no methodology.
- *Extrapolation:* rps, token counts and the choice of a 230B model are ours; the source names no model or traffic.

**Fit.** Pool: confidential documents, large-model quality, shared schema prefix, and a steady work queue make reserved GPUs reasonable. Burst: none; model is pool-only and overflow to a smaller model would silently change extraction results, so the gateway returns 429 and the batch client waits. Tier 50 ms: output is a JSON blob consumed by software.

**Cost sketch.** **Cost assumptions (all ours, not Akamai data):** replica = one RTX PRO 6000 Blackwell Linode ($12/hr for x4 on the 230B-class model, $6/hr for x2 on 8-27B models, from proposal2.md); 720 h/month; per-replica capacity = concurrent streams C / (output tokens x TPOT), with C = 48 streams at 50 ms, 32 at 30 ms, 24 for 40k-token contexts (assumed, to be replaced by the profiler). Serverless is free during beta and its future price is unpublished, so burst cost uses a **hypothetical** $0.20/M input and $0.60/M output tokens. Hourly billing rounding and egress are ignored. PackServe savings (13-25% GPU-hours in the paper) are not stacked on top.
- Reserve-for-peak: 9 x $12 x 720 = **$77,760/mo**.
- Reserved 4 + pre-scale to 9 for ~220 peak hours: $34,560 + 5 x $12 x 220 = **$47,760 (-39%)**. Alternatively accept queuing at 4 replicas: $34,560 (-56%), peak work finishes later.

**Risks / not a fit if.** Needs >230B or a closed frontier model; hard interactive latency at peak; contractual confidentiality or residency terms (we have not verified anything about Akamai's); extraction accuracy is the product, so the model must be evaluated by the customer.
