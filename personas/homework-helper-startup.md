# Lee's homework helper (POOR FIT: use serverless only)

**Profile.** Solo founder of a student Q&A app (archetype). Model `qwen3-8-27b`. Burst: on, but the right answer is to skip the pool.

**Workload.** baseline 0.2 rps, peak 8 rps for ~20 hours in finals weeks, prompt p50 800, output p50 500, no shared prefix (every question is different), `reserved_replicas` 1 (minimum so it fits the schema).

**Real-world grounding.** Khan Academy and Google.org describe AI tutoring tools "timed for the 2026 school year" (https://blog.google/products-and-platforms/products/education/khan-academy-back-to-school/); the source has **no usage numbers**. The exam-season spike and all volumes here are our extrapolation.

**Why not a fit.** One x2 replica is $6 x 720 = **$4,320/mo** and would sit at ~10% utilization (0.2 rps vs ~1.9 rps capacity). At the hypothetical serverless rate, ~0.2 rps average plus finals peaks is roughly **$500/mo** (~1.1M requests/month: ~0.9B input + ~0.55B output tokens x $0.20/$0.60 per M). Pay-per-token wins by about 9x until sustained volume reaches roughly 20-30% of one replica's capacity. No prefix sharing means no packing benefit either.

**not_a_fit_if.** Average utilization of one replica below ~20-30%; spikes are unpredictable and tiny.

**Cost assumptions.** Same as other personas; serverless pricing is hypothetical (free in beta).
