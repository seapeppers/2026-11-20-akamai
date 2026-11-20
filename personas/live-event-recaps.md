# Sam's live-event personalized recaps (40x spikes, burst on)

**Profile.** Head of product at a streaming company (archetype). After each major live event the app generates personalized text recaps per viewer. Model `qwen3-8-27b`, burst **on**, 50 ms.

**Day in the life.** Between events: ~2 rps for evergreen content. When the final whistle blows, millions of viewers open the app within minutes.

**Workload.** baseline 2 rps, peak 80 rps (~2 h per event, ~8 events a month), prompt p50 4,000 (event context + viewer profile), output p50 400, shared prefix yes (event context shared by many viewers), tier 50 ms, `reserved_replicas` = 2.

**Real-world grounding.**
- NBCUniversal / Peacock Paris 2024 (https://www.nbcuniversal.com/article/peacock-unveils-personalized-olympic-recaps-featuring-voice-legendary-sports-announcer-al-michaels): "nearly 7 million personalized variants", built from "hundreds of NBC Sports-produced clips each day" into ~10-minute playlists; editors reviewed all content. The source does not describe the inference stack or the traffic curve.
- *Extrapolation:* 80 rps and the 40x ratio are ours.

**Fit.** Pool: shared event context across viewers rewards prefix caching; the baseline is small. Burst: strongest case. Spike is short, huge, and tied to a public schedule, but GPU billing is hourly rounded up and scale-up time is unknown. Tier 50 ms.

**Cost sketch.** **Cost assumptions (all ours, not Akamai data):** replica = one RTX PRO 6000 Blackwell Linode ($12/hr for x4 on the 230B-class model, $6/hr for x2 on 8-27B models, from proposal2.md); 720 h/month; per-replica capacity = concurrent streams C / (output tokens x TPOT), with C = 48 streams at 50 ms, 32 at 30 ms, 24 for 40k-token contexts (assumed, to be replaced by the profiler). Serverless is free during beta and its future price is unpublished, so burst cost uses a **hypothetical** $0.20/M input and $0.60/M output tokens. Hourly billing rounding and egress are ignored. PackServe savings (13-25% GPU-hours in the paper) are not stacked on top.
- Reserve-for-peak: 34 x $6 x 720 = **$146,880/mo**.
- Reserved 2 + burst (4.5M requests, 17.9B in / 1.8B out, ~$4,650): $8,640 + $4,650 = **~$13,300 (-91%)**.
- Scheduled pre-scale 32 extra replicas for 16 h: $8,640 + $3,072 = **$11,700 (-92%)**, again slightly cheaper when the schedule is known, if GPUs are obtainable (access is by request, limited availability).

**Risks / not a fit if.** Serverless rate limits unpublished; recaps due within seconds at 80 rps; quality needs a model not in the catalog.
