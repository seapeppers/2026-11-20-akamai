# Jo's NPC dialogue at launch (30 ms tier, burst on)

**Profile.** Technical director at a studio shipping a live-service game with LLM-driven NPC chatter (archetype). Model `gemma-4-26b-a4b-it` (burst-eligible), tier 30 ms.

**Day in the life.** Weeks of beta at ~20 rps, then launch day: friends-and-family traffic times fifteen as a global player base logs in over 12 hours. Content drops produce smaller repeats.

**Workload.** baseline 20 rps, peak 300 rps (~12 peak-hours per drop, assumed), prompt p50 1,500 (persona + world state), output p50 60 (one or two lines), shared prefix yes, tier 30 ms, `reserved_replicas` = 2.

**Real-world grounding.**
- Ubisoft NEO NPC (https://news.ubisoft.com/en-us/article/5qXdxhshJBXoanFZApdG3L/how-ubisofts-new-generative-ai-prototype-changes-the-narrative-for-npcs): prototype with Nvidia Audio2Face and Inworld's LLM; human-written backstories; guardrails; "only a prototype". No latency or scale numbers in the source.
- *Extrapolation:* launch spike size, rps, and the 30 ms tier are ours. We did not find a public source for launch-day LLM request volumes.

**Fit.** Pool: persona prefixes are reused across a player's conversation, short outputs favor low TPOT, steady live-ops base. Burst: strong for unplanned overage. Tier 30 ms: lines are short and players wait on the first words, so tighter per-token pacing matters; the cost is less packing (smaller savings in the paper at 30 ms: 13.0-16.8% vs 21.1-24.6% at 50 ms).

**Cost sketch.** **Cost assumptions (all ours, not Akamai data):** replica = one RTX PRO 6000 Blackwell Linode ($12/hr for x4 on the 230B-class model, $6/hr for x2 on 8-27B models, from proposal2.md); 720 h/month; per-replica capacity = concurrent streams C / (output tokens x TPOT), with C = 48 streams at 50 ms, 32 at 30 ms, 24 for 40k-token contexts (assumed, to be replaced by the profiler). Serverless is free during beta and its future price is unpublished, so burst cost uses a **hypothetical** $0.20/M input and $0.60/M output tokens. Hourly billing rounding and egress are ignored. PackServe savings (13-25% GPU-hours in the paper) are not stacked on top.
- Reserve-for-peak: 17 x $6 x 720 = **$73,440/mo**.
- Reserved 2 + burst (11.4M requests, 17.1B in / 0.7B out at hypothetical prices ~$3,800): **~$12,500 (-83%)**.
- If the launch date is known weeks ahead, pre-scaling for the 12 peak hours: $8,640 + 15 x $6 x 12 = **$9,720 (-87%)**, cheaper than burst; burst is for the unplanned part.

**Risks / not a fit if.** Voice-to-voice loops need under ~1 s end to end; serverless cannot hold 30 ms TPOT (unpublished); NPC safety filtering is the studio's job.
