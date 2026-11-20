# Customer personas

Eight archetypes (none is a real Akamai customer; no Akamai customer is named or quoted). Each is grounded in a public example of a similar workload, with the numbers we invented labeled as extrapolations. The machine-readable form is `personas.json` (schema in `docs/contracts.md`).

## Fit matrix

| Persona | Pool fit | Burst fit | Tier | Model | Reserved | Key reason |
|---|---|---|---|---|---|---|
| [agentic-coding-assistant](agentic-coding-assistant.md) | Strong | None (pool-only model) | 50 ms | packserve-large-230b | 3 | ~100:1 input:output agent loops with long shared prefixes; pre-scale on weekday schedule |
| [retail-support-bfcm](retail-support-bfcm.md) | Strong | Strong | 50 ms | qwen3-8-27b | 4 | 6x predictable BFCM peak; overflow bridges the scale-up gap (-79% vs reserve-for-peak) |
| [legal-contract-extraction](legal-contract-extraction.md) | Strong | None (pool-only model) | 50 ms | packserve-large-230b | 4 | Confidential long documents, large model, queue-tolerant batch (retry on 429) |
| [healthcare-scribe](healthcare-scribe.md) | Strong | Eligible, disabled by policy | 50 ms | qwen3-8-27b | 2 | Diurnal predictable load; PHI keeps data on dedicated GPUs; compliance unverified |
| [game-npc-launch](game-npc-launch.md) | Good | Strong | 30 ms | gemma-4-26b-a4b-it | 2 | 15x launch spike, short outputs, persona prefixes; scheduled pre-scale cheaper than burst |
| [live-event-recaps](live-event-recaps.md) | Moderate | Strong | 50 ms | qwen3-8-27b | 2 | 40x two-hour spikes after live events (-91%) |
| [realtime-voice-agent](realtime-voice-agent.md) | **Poor** | Poor | 30 ms | qwen3-8-27b | 2 | SLA on P99/TTFT; we target median TPOT only |
| [homework-helper-startup](homework-helper-startup.md) | **Poor** | n/a | 50 ms | qwen3-8-27b | 1 | 0.2 rps average; fully serverless is ~9x cheaper |

## Methodology

- **Sizing assumption (ours).** Replica capacity = concurrent streams C / (output tokens x TPOT): C = 48 at 50 ms, 32 at 30 ms, 24 for 40k-token contexts. Replace with profiler output when available.
- **Prices.** GPU: RTX PRO 6000 Blackwell $3/$6/$12/$24 per hour for x1/x2/x4/x8 (proposal2.md, verified in docs/validity-review.md). Replicas: x4 ($12) for the 230B-class model, x2 ($6) for 8-27B models. 720 h/month.
- **Serverless price is hypothetical.** Akamai Inference serverless is free in beta and has no published per-token price or rate limit; burst cost uses $0.20/M input and $0.60/M output as a placeholder. Treat burst savings as scenario arithmetic, not a quote.
- **Peak hours/month** per persona are assumptions stated in each file.
- **Not stacked:** PackServe's reported GPU-hour savings (13-25% in the paper, 34.7% in production on a ~700B model) are not applied on top of these comparisons.
- **Honest limits** carried from docs/validity-review.md: median TPOT not tails, TTFT can rise, overflow only for same-model (burst-eligible) models, serverless rate limits unpublished, GPU billing rounds up to the hour, GPU access is by request.
- **Finding.** When a peak is scheduled and the pool can scale in time, pre-scaling is often slightly cheaper than burst at our placeholder prices (game, media). Burst earns its place for unscheduled surges and for covering the unknown GPU scale-up delay.

## Sources actually fetched (11 URLs)

1. Klarna press release, Feb 27 2024: https://www.klarna.com/international/press/klarna-ai-assistant-handles-two-thirds-of-customer-service-chats-in-its-first-month/
2. OpenAI Klarna page (same figures): https://openai.com/index/klarna/
3. Adobe, Cyber Monday 2025: https://news.adobe.com/news/2025/12/adobe-cyber-monday-hits-record
4. Pragmatic Engineer on Cursor: https://newsletter.pragmaticengineer.com/p/cursor
5. Manus context-engineering summary (secondary): https://agentic-ai.readthedocs.io/en/latest/ContextEngineering/manus/
6. SGLang / RadixAttention (LMSYS): https://www.lmsys.org/blog/2024-01-17-sglang/
7. Thomson Reuters CoCounsel, Century Communities: https://legal.thomsonreuters.com/en/insights/case-studies/turbocharging-legal-tasks-with-cocounsel-generative-ai
8. Abridge homepage: https://www.abridge.com/
9. Ubisoft NEO NPC: https://news.ubisoft.com/en-us/article/5qXdxhshJBXoanFZApdG3L/how-ubisofts-new-generative-ai-prototype-changes-the-narrative-for-npcs
10. NBCUniversal / Peacock Olympic recaps: https://www.nbcuniversal.com/article/peacock-unveils-personalized-olympic-recaps-featuring-voice-legendary-sports-announcer-al-michaels
11. Google / Khan Academy back to school: https://blog.google/products-and-platforms/products/education/khan-academy-back-to-school/

Also cited, not re-verified here: PackServe paper https://arxiv.org/abs/2609.33224 (via docs/validity-review.md).

## Unverified or weakly sourced

- Manus' primary blog post could not be fetched; only a secondary summary was read.
- Cursor source's caching section is paywalled; it does not state prefix-cache usage.
- No public source found for launch-day game LLM volumes, exam-season LLM spikes, BFCM chatbot volumes, or voice-agent latency budgets; those numbers are our assumptions.
- Compliance (HIPAA BAA, confidentiality, residency) terms for Akamai GPU Linodes or Inference were not verified.
- Khan Academy source has no usage figures; Abridge and Klarna figures are vendor-reported.
