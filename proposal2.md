# Proposal 2: PackServe-style Cluster on Akamai GPU Linodes + Akamai Inference

Status: proposal only. Nothing has been built. Items marked **(hypothesis)** are my ideas, not from a paper or from Akamai documentation. Items marked **(unverified)** depend on facts I could not find on Akamai's pages.

## 1. Goal

Run an LLM serving cluster on Akamai GPU Linodes that uses a PackServe-style gateway scheduler to meet per-token latency targets (TPOT) with fewer GPU-hours. Use Akamai Inference (serverless) for two secondary jobs: overflow and asynchronous trace analysis.

## 2. Source facts

**PackServe (arXiv 2609.33224)**
- Gateway-level scheduler over unmodified engines. The paper's testbed is SGLang with homogeneous instance pools.
- Per-request decisions take about 1.9 ms on average.
- Latency comes from small numeric models: a prefill model fit from profiling, and a decode model linear in batch size and total context. An online smoothed correction factor adjusts them.
- Admission: filter instances that can't admit the request, keep those within a prefix-cache recompute budget (default 0.5), keep those whose predicted TPOT is under target, and pick the one with the most decoding requests (packing). If none pass, fall back to cache-aware load balancing. If no instance can admit, reject.
- Reported: 13.0–16.8% fewer GPU-hours at a 30 ms target and 21.1–24.6% at 50 ms (64 H20 GPUs, 230B model). Production: 34.7% smaller footprint on 1,000+ GPUs (~700B model).
- Limits: targets median TPOT and doesn't guarantee tails. Production mean TTFT rose 66.4%. It assumes colocated prefill/decode and instance-local caches. Each model and hardware pair needs its own profiling.

**Akamai GPU Linodes**
- RTX PRO 6000 Blackwell (96 GB): $3.00/hr for x1, $6.00 for x2, $12.00 for x4, $24.00 for x8. Access is by request, with limited availability.
- RTX 4000 Ada (20 GB): $0.52–$3.57/hr. Quadro RTX 6000 (24 GB): $1.50–$6.00/hr.
- A powered-off VM is still billed. Egress overage is $0.005/GB. No spot or preemptible tier found.

**Akamai Inference**
- Serverless: shared pools, autoscaling, OpenAI-compatible API, curated catalog (Llama, Qwen, DeepSeek families). In beta, free during beta, per-million-token billing later. No published rate limits, cold-start data, or prefix-cache behavior.
- Dedicated: per-GPU-hour billed by the minute including idle time, limited availability. This proposal uses GPU Linodes for the cluster, not this tier.

## 3. Architecture

```
clients ──> gateway(s) ──> replicas on GPU Linodes (SGLang or vLLM)
              |  |                ^
              |  |                | create/delete Linodes
              |  |            autoscaler
              |  +--> metadata service (shared per-instance state)
              |
              +--(overflow, hypothesis)--> Akamai Inference serverless
telemetry (aggregates) ──> trace store ──> scheduled analysis ──> Akamai Inference serverless ──> reports
```

### Components

1. **Replicas.** Engine on GPU Linodes, tensor-parallel within one Linode. Use x4 or x8 Blackwell plans for large models. The 8-card plans expose PCIe topology (vNUMA). Homogeneous pools, per PackServe.
2. **Gateway.** Stateless and horizontally scaled. Implements the packing algorithm: capacity guard, cache filter, TPOT admission, packing selection, fallback. Keeps a local prefix tree for cache estimates. Builds per-instance state from request and response paths, like the paper.
3. **Metadata service.** Aggregates per-instance state so gateways share a view.
4. **Profiler.** One-time per (model, Linode plan): prefill grid over new and cached tokens, decode sweep over concurrency. Online correction at runtime.
5. **Scaling.** Two options; the first is preferred.
   - **LKE node pool autoscaling (preferred).** Run replicas as Pods requesting `nvidia.com/gpu` on an LKE cluster. LKE supports GPU nodes for RTX PRO 6000 Blackwell and RTX 4000 Ada (not Quadro RTX 6000), installs NVIDIA drivers automatically on standard clusters, and has built-in node pool autoscaling (standard minimum 1, Enterprise minimum 0, maximum 100). It adds nodes for unschedulable Pods and drains surplus nodes. **(unverified)** The docs do not say whether the autoscaler works on GPU pools, and give no scale-up timings. The Blackwell known-limitations page returned 404, so there may be restrictions I have not seen.
   - **Custom controller on plain Linodes.** Create and delete Linodes through Akamai's API. Plain Linodes have no built-in autoscaling.
   - **Either way, a small controller is needed.** The built-in autoscaler is Pod-driven and picks nodes to drain without knowing load, and draining a busy replica kills in-flight requests. The controller picks the least-loaded replica, stops routing to it, waits for it to finish, and lets the node go.
   - **Delete, don't power off**, since powered-off VMs are still billed. Removed LKE nodes are deleted Linodes, so billing stops. PackServe's savings only appear as dollars if freed instances are deleted.
6. **Trace store and analysis.** Aggregate telemetry only (counts, timings, hit rates). A scheduled job calls the serverless endpoint to explain SLO misses, cluster traffic into labeled workload classes, and draft periodic tuning notes. Numbers come from deterministic statistics first; the LLM narrates.

### Serverless roles

- **Trace analysis (recommended).** Batch, latency-insensitive, bursty, small open-weight model. A good fit for per-token billing with no idle cost. Send aggregates, never prompts or code.
- **Overflow (hypothesis).** When no replica admits a request, spill it to serverless instead of rejecting. This only makes sense if the serverless catalog offers the same model (unverified), because a different model breaks PackServe's latency predictions and changes answer quality. Track overflow requests separately from the packing statistics.

## 4. Key risks and unknowns

1. **Instance start-up time (unverified).** Packing frees instances, but scale-up needs boot and weight load. I could not find Linode GPU boot or image-pull times. Mitigations: pre-baked images, weights on local or nearby storage, and a small warm buffer. Overflow could cover the gap, if it works.
2. **Blackwell availability.** Access is by request and limited. The 20–24 GB cards can't hold large models.
3. **Scale mismatch.** The paper's results are for a 230B model on 64 H20s and a ~700B model on 1,000+ GPUs. A hackathon-size cluster is far smaller, and consolidation gains shrink with fewer instances.
4. **Engine port.** The paper used SGLang. Verify that the chosen engine exposes forward-pass metrics or equivalent timing so the latency model can be corrected online.
5. **Tail and TTFT.** PackServe targets median TPOT and its production TTFT worsened. Fine for throughput-oriented traffic, risky for interactive use.
6. **Overflow viability (hypothesis).** Unknown rate limits, burst handling, catalog match, and beta stability.
7. **Value of LLM trace analysis.** It may add little over dashboards and alerts. Test before investing.

## 5. Cost framing (illustrative arithmetic, not a forecast)

- One x8 Blackwell Linode: $24/hr, about $17,520/month at 730 hours.
- If PackServe-style consolidation frees 13–25% of GPU-hours, that applies to the compute bill only if instances are deleted. The paper's percentages come from different hardware and scale and are not a prediction for this cluster.
- Serverless costs are unknown until Akamai publishes per-token pricing. Analysis and overflow volumes should be small relative to the GPU bill.

## 6. Suggested milestones

1. Stand up one x4 or x8 Linode with the engine; collect profiling grids.
2. Implement the gateway with TPOT admission and packing; replay a trace (the TraceLab trace is a candidate workload) against a baseline load balancer.
3. Add the autoscaler with delete-on-release; measure scale-up delay.
4. Add the trace store and the scheduled serverless analysis job.
5. Test overflow once the catalog and rate limits are confirmed.

Success measure: GPU-hours (billed Linode-hours) versus a baseline balancer at equal median TPOT attainment, plus tail TPOT and TTFT reported alongside.

## 7. Demo variant for the hackathon (what we will actually build first)

### Why a variant

Tested on 2026-11-20 with the account's Linode API token:
- Linodes, firewalls and tags: full create/read/update/delete works. A Nanode in `us-sea` reached "running" in about 60 seconds.
- **All 13 GPU plans fail** to create (`g1-gpu-rtx6000-*`, `g2-gpu-rtx4000a*`) with "A limit on your account is preventing the deployment of the selected Linode plan." RTX PRO 6000 Blackwell plans do not appear in the plan list. LKE Enterprise returned 404 (standard tier only).
- The inference host `api.akamai-inference.com` does not resolve through this laptop's corporate DNS (the `100.64.0.x` resolvers time out on it; public resolvers resolve it). This is probably a corporate policy, so it is not bypassed. Inference calls are therefore made from Linodes, as the workshop labs do.
- The inference key in the workshop `.env` works only with `qwen3-8-27b`, `gemma-4-26b-a4b-it` and `qwen3-embedding-8b`. Its per-minute token budget is shared with everyone on the same account; a 429 means the budget is used up.

The demo goal is to show how Akamai's serverless inference behaves as load grows. GPU performance is explicitly out of scope for the demo.

### Design: serverless scaling probe

1. **Load ramp.** A script steps concurrency up (for example 1, 2, 4, 8, 16, 32) against the serverless endpoint with a fixed prompt mix and streaming requests.
2. **Load generators on Nanodes.** A controller on the laptop creates Nanodes in `us-sea` through the Linode API. Each runs the load generator and calls the inference endpoint (Akamai's network resolves the host). The ramp comes from several machines. The controller deletes every Nanode when the run ends and verifies nothing is left.
3. **Metrics per step.** Time to first token, per-token latency, tokens per second, success rate, and 429/5xx counts. Scaling shows as aggregate throughput rising with concurrency while latency stays roughly flat. The limit shows as rising latency or sustained 429s.
4. **Output.** A report or page with the curves and a clear statement of what is measured: client-side latency and errors only. Replica counts and server-side cache behavior are not visible.
5. **Optional contrast (not in the first build).** Nanodes acting as simulated replicas (latency from PackServe's model, not real inference) behind the packing gateway, as a fixed-capacity comparison. Any result from it is a simulation and must be labeled as such.

### Safeguards

- **Shared budget.** Keep `max_tokens` around 64, start at low concurrency, ramp slowly, and stop at the first sustained 429, so other teams on the same account are not starved. How many people share the key is unknown and should be confirmed.
- **Key handling.** The key is copied to each Nanode over SSH (as in the workshop labs) and disappears with the instance. It is never printed or committed. Do not put it in cloud-init user data.
- **Billing.** Nanodes cost about $0.0075/hr each. Everything is labeled `claude-probe-*` and tagged `claude-probe`, deleted in a cleanup step that runs even on failure.
- **Instance cap.** The account's maximum Linode count is unknown. Start with 2–4 Nanodes.

### Out of scope for the demo

GPU latency, real vLLM or SGLang, PackServe's measured savings, LKE GPU pools, and overflow routing. These remain in sections 1–6 as the longer-term design and need GPU access from Akamai.

## 8. Open questions for Akamai

- Linode GPU boot and image-pull time; any spot or preemptible pricing.
- Whether LKE node pool autoscaling works on GPU pools, and its scale-up timing; limitations of the RTX PRO 6000 Blackwell Linodes.
- Lifting the account limit on GPU plans (ask Support with the plan name, e.g. `g2-gpu-rtx4000a1-m`).
- The inference key's shared token budget: how many people share it, and the per-minute limit.
- Serverless catalog (exact models), rate limits, burst behavior, prefix caching, and data-retention guarantees in writing.
- Blackwell access timeline.
