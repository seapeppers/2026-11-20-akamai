# Dr. Amara's ambient note drafting (burst eligible but disabled)

**Profile.** CMIO at a regional health system (~3,000 clinicians, archetype). Drafts SOAP notes from visit transcripts for clinician sign-off. Model `qwen3-8-27b`; burst **off by policy** even though the model is burst-eligible.

**Day in the life.** Notes spike at ~11 am and after 4 pm when clinicians close charts; nights are almost silent. She wants the transcripts to stay on dedicated infrastructure.

**Workload.** baseline 1.5 rps, peak 5 rps, prompt p50 6,000 (transcript + template), output p50 700, shared prefix yes (template), tier 50 ms, `reserved_replicas` = 2.

**Real-world grounding.**
- Abridge (https://www.abridge.com/): "300+ health systems", "53M+ conversations powered each year"; UW Health "800+ providers, 30 min/day documentation time saved"; Inova "9,400+ clinicians".
- *Extrapolation:* our rps derive from 3,000 clinicians x ~20 visits/day. Abridge's site does not state its model or serving stack.

**Fit.** Pool: diurnal predictable load, PHI data-control preference, template prefix reused. Burst: the pipeline is async and clinicians review notes later, so overflow is less needed; whether sending PHI to a shared serverless pool is acceptable depends on terms (e.g. a HIPAA BAA) **we could not verify for either Akamai product**. Tier 50 ms.

**Cost sketch.** **Cost assumptions (all ours, not Akamai data):** replica = one RTX PRO 6000 Blackwell Linode ($12/hr for x4 on the 230B-class model, $6/hr for x2 on 8-27B models, from proposal2.md); 720 h/month; per-replica capacity = concurrent streams C / (output tokens x TPOT), with C = 48 streams at 50 ms, 32 at 30 ms, 24 for 40k-token contexts (assumed, to be replaced by the profiler). Serverless is free during beta and its future price is unpublished, so burst cost uses a **hypothetical** $0.20/M input and $0.60/M output tokens. Hourly billing rounding and egress are ignored. PackServe savings (13-25% GPU-hours in the paper) are not stacked on top.
- Reserve-for-peak: 4 x $6 x 720 = **$17,280/mo**.
- Reserved 2 + scheduled scale to 4 for ~220 clinic peak hours: $8,640 + 2 x $6 x 220 = **$11,280 (-35%)**.
- If burst were allowed: 2 reserved + ~$2,900 overflow = ~$11,500 (-33%), no better, so we keep PHI off serverless.

**Risks / not a fit if.** Compliance terms are missing; live in-visit prompts with strict latency; hourly billing means short scale-ups still pay a full hour.
