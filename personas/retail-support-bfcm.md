# Marcus's retail support chat (Black Friday / Cyber Monday)

**Profile.** Head of CX engineering at an omnichannel retailer (~$1.5B GMV, archetype). Deflects order-status, returns, and size questions. Model `qwen3-8-27b`, burst **on**, 50 ms tier.

**Day in the life.** Normal days ~15 rps. Today is 2026-11-20: the BFCM code freeze is on, the pool is pre-scaled to 4 replicas, and Marcus is watching the `X-PackServe-Route` mix for `overflow`. Cyber Monday evening is when everything arrives at once.

**Workload.** baseline 15 rps, peak 90 rps (6x, ~30 peak-hours/season, evening heavy), prompt p50 1,800 (policy system prompt + order JSON + history), output p50 250, shared prefix yes, tier 50 ms, `reserved_replicas` = 4.

**Real-world grounding.**
- Klarna (https://www.klarna.com/international/press/klarna-ai-assistant-handles-two-thirds-of-customer-service-chats-in-its-first-month/, OpenAI mirror https://openai.com/index/klarna/): first month 2.3 million conversations, two-thirds of chats, <2 min vs 11 min resolution, equal to 700 full-time agents (company-reported).
- Adobe Analytics, Cyber Monday 2025 (https://news.adobe.com/news/2025/12/adobe-cyber-monday-hits-record): $14.25B US online spend, +7.1% YoY; $16M per minute from 8 to 10 pm; AI traffic to US retail sites +670% on the day.
- *Extrapolation:* the 6x ratio, rps and token sizes are ours. Adobe gives sales, not chat volume.

**Fit.** Pool: baseline is always on, the system prompt is shared, retailer wants order data in infrastructure it controls. Burst: strong. The peak is predictable (the date is known), but the gap between "traffic arrives" and "new GPU Linodes are loaded" is unknown, and overflow bridges it. Same model id in the serverless catalog means no answer drift. Tier 50 ms: chat text streaming at 20 tok/s reads fine.

**Cost sketch.** **Cost assumptions (all ours, not Akamai data):** replica = one RTX PRO 6000 Blackwell Linode ($12/hr for x4 on the 230B-class model, $6/hr for x2 on 8-27B models, from proposal2.md); 720 h/month; per-replica capacity = concurrent streams C / (output tokens x TPOT), with C = 48 streams at 50 ms, 32 at 30 ms, 24 for 40k-token contexts (assumed, to be replaced by the profiler). Serverless is free during beta and its future price is unpublished, so burst cost uses a **hypothetical** $0.20/M input and $0.60/M output tokens. Hourly billing rounding and egress are ignored. PackServe savings (13-25% GPU-hours in the paper) are not stacked on top.
- Reserve-for-peak: 24 x $6 x 720 = **$103,680/mo**.
- Reserve-for-baseline + burst: 4 x $6 x 720 = $17,280, plus overflow ~8.1M requests (14.5B in / 2.0B out) at the hypothetical rate = ~$4,100, total **~$21,400 (-79%)**.
- Pre-scale peak hours instead: $17,280 + 20 x $6 x 30 = $20,880, similar, but needs the scale-up to work; burst is the safety net.

**Risks / not a fit if.** Peak lasts weeks (reserve it); serverless rate limits are unpublished and may reject 60+ rps overflow; P99 SLAs; overflow responses will carry serverless latency, not the 50 ms target.
