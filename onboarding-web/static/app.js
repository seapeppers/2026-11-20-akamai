(() => {
const $ = (s, r = document) => r.querySelector(s);
const page = document.body.dataset.page;
const GATEWAY = document.body.dataset.gateway;
const PLAN_KEY = "ps_plan", TENANT_KEY = "ps_tenant";
const DEFAULT_PLAN = {model: "qwen3-8-27b", tpot_target_ms: 50, reserved_replicas: 1, burst_enabled: true,
  baseline_rps: 2, peak_rps: 12, peak_hours_per_day: 1.5, tokens_per_request: 800, gpu_usd_hr: 12,
  serverless_usd_per_m_tokens: 0.3, rps_per_replica: null, persona_id: null, persona_name: null};
const getPlan = () => ({...DEFAULT_PLAN, ...JSON.parse(localStorage.getItem(PLAN_KEY) || "{}")});
const setPlan = p => localStorage.setItem(PLAN_KEY, JSON.stringify(p));
const usd = n => "$" + Number(n).toLocaleString(undefined, {maximumFractionDigits: 0});
const el = (tag, attrs = {}, ...kids) => { const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) k === "class" ? e.className = v : e.setAttribute(k, v);
  kids.forEach(c => e.append(c)); return e; };
const jpost = (url, body) => fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});

async function banner() {
  const b = $("#banner");
  try {
    const s = await (await fetch("/api/status")).json();
    window.__status = s;
    b.hidden = false;
    if (s.demo_mode) {
      b.className = "banner";
      b.textContent = "DEMO MODE: the metadata/gateway services are not reachable. Signups use a local JSON store and the playground returns SIMULATED responses.";
    } else {
      b.className = "banner ok";
      b.textContent = "Connected to the PackServe stack (metadata + gateway). GPU replicas are SIMULATED.";
    }
  } catch { b.hidden = false; b.textContent = "Cannot reach the onboarding backend."; }
}

async function usecases() {
  const d = await (await fetch("/api/personas")).json();
  $("#persona-source").textContent = d.source === "fallback" ? "Showing a small built-in list; the full persona set will appear when personas/personas.json exists." : "";
  const box = $("#personas"); box.replaceChildren();
  for (const p of d.personas) {
    const r = p.recommended || {}, t = p.traffic_shape || {};
    const card = el("article", {class: "card persona"},
      el("h3", {}, p.name), el("div", {class: "muted"}, `${p.company_archetype || ""} ${p.industry ? "· " + p.industry : ""}`),
      el("p", {}, p.use_case || ""),
      el("div", {}, el("span", {class: "tag"}, r.model), el("span", {class: "tag"}, `${r.tpot_target_ms} ms TPOT`),
        el("span", {class: "tag"}, `${r.reserved_replicas} reserved`), el("span", {class: "tag " + (r.burst_enabled ? "ok" : "bad")}, r.burst_enabled ? "burst on" : "burst off")),
      el("div", {class: "muted"}, `Traffic: ${t.baseline_rps} to ${t.peak_rps} req/s${t.peak_pattern ? " (" + t.peak_pattern + ")" : ""}`));
    const det = el("details", {}, el("summary", {}, "Why this fit"),
      el("p", {}, "Pool: " + (p.why_pool || "")), el("p", {}, "Burst: " + (p.why_burst || "")),
      el("p", {}, "Not a fit if: " + (p.not_a_fit_if || "")));
    (p.real_world_examples || []).forEach(x => det.append(el("p", {}, `${x.org}: ${x.summary} `, x.source_url ? el("a", {href: x.source_url, rel: "noopener", target: "_blank"}, "source") : "")));
    card.append(det);
    const b = el("button", {class: "btn primary", type: "button"}, "Use this plan");
    b.setAttribute("aria-label", "Use plan for " + p.name);
    b.onclick = () => {
      setPlan({...DEFAULT_PLAN, model: r.model, tpot_target_ms: r.tpot_target_ms, reserved_replicas: r.reserved_replicas,
        burst_enabled: r.burst_enabled, baseline_rps: t.baseline_rps, peak_rps: t.peak_rps, persona_id: p.id, persona_name: p.name,
        tokens_per_request: (t.prompt_tokens_p50 || 500) + (t.output_tokens_p50 || 300)});
      location.href = "/plan";
    };
    card.append(b); box.append(card);
  }
}

async function planPage() {
  const models = await (await fetch("/api/models")).json();
  const byId = Object.fromEntries(models.map(m => [m.id, m]));
  const mSel = $("#model");
  models.forEach(m => mSel.append(el("option", {value: m.id}, `${m.id} (${m.burst_eligible ? "burst-eligible" : "pool-only"})`)));
  const plan = getPlan();
  if (plan.persona_name) $("#plan-persona").textContent = "Pre-filled from use case: " + plan.persona_name;
  const f = {model: "#model", tpot_target_ms: "#tpot", reserved_replicas: "#replicas", baseline_rps: "#base", peak_rps: "#peak",
    peak_hours_per_day: "#peakh", tokens_per_request: "#tokens", rps_per_replica: "#rpr", gpu_usd_hr: "#gpu", serverless_usd_per_m_tokens: "#sp"};
  for (const [k, s] of Object.entries(f)) $(s).value = plan[k] ?? "";
  $("#burst").checked = !!plan.burst_enabled;
  let timer;
  const read = () => {
    const p = {...plan};
    for (const [k, s] of Object.entries(f)) { const v = $(s).value; p[k] = k === "model" ? v : (v === "" ? null : Number(v)); }
    p.burst_enabled = $("#burst").checked;
    const eligible = byId[p.model]?.burst_eligible;
    $("#model-note").textContent = byId[p.model]?.description || "";
    $("#burst").disabled = !eligible;
    if (!eligible) { p.burst_enabled = false; $("#burst").checked = false;
      $("#burst-note").textContent = "Burst is unavailable for this model: it has no equivalent in the serverless catalog, so overflow could not give the same answers. Excess requests are rejected with Retry-After. Add reserved replicas instead."; }
    else $("#burst-note").textContent = "Overflow goes to Akamai Inference serverless. Capacity and rate limits there are unpublished (beta), so treat it as best effort.";
    return p;
  };
  const render = async () => {
    const p = read(); Object.assign(plan, p); setPlan(plan);
    const r = await (await jpost("/api/estimate", p)).json();
    const b = r.burst, box = $("#cost"); box.replaceChildren();
    const win = b.usd_month <= r.peak.usd_month;
    box.append(
      el("div", {class: "cost" + (win ? "" : " win")}, el("small", {}, "Reserve for peak"), el("b", {}, usd(r.peak.usd_month)), el("small", {}, `${r.peak.replicas} replicas x $${r.assumptions.gpu_usd_hr}/hr x ${r.assumptions.hours_per_month} h`)),
      el("div", {class: "cost" + (win ? " win" : "")}, el("small", {}, p.burst_enabled ? "Reserve baseline + serverless burst" : "Reserve chosen replicas (no burst)"), el("b", {}, usd(b.usd_month)),
        el("small", {}, `${b.reserved_replicas} replicas ${usd(b.reserved_usd_month)} + serverless ${usd(b.serverless_usd_month)}`)));
    if (r.peak.usd_month) box.append(el("p", {class: "saving"}, r.saving_usd_month >= 0 ? `Estimated saving: ${usd(r.saving_usd_month)}/month (${r.saving_pct}%)` : `This choice costs ${usd(-r.saving_usd_month)}/month more than reserving for peak.`));
    const notes = $("#cost-notes"); notes.replaceChildren();
    const add = t => notes.append(el("li", {}, t));
    if (b.unserved_rps_at_peak > 0) add(`Warning: at peak about ${b.unserved_rps_at_peak} req/s exceed the pool and would be rejected (burst is off). Add replicas.`);
    add(`Assumes ${r.rps_per_replica} req/s per replica at the ${r.tier_ms} ms tier (editable), ${r.assumptions.peak_hours_per_day} peak hours/day, ${r.assumptions.tokens_per_request} tokens/request.`);
    add(`Serverless price $${r.assumptions.serverless_usd_per_m_tokens}/M tokens is YOUR assumption; Akamai has not published it.`);
    add("GPU Linodes bill hourly, rounded up, and powered-off instances are still billed. Reserved cost is therefore a full-month figure; scaling down mid-hour saves nothing for that hour.");
    add(`GPU plans include no bundled transfer; egress is $0.005/GB (about ${usd(r.egress_usd_month_both)}/month here, negligible).`);
  };
  const debounced = () => { clearTimeout(timer); timer = setTimeout(render, 150); };
  document.querySelectorAll("#plan-form input,#plan-form select").forEach(e => e.addEventListener("input", debounced));
  render();
}

function snippets(key, model, base) {
  return {
    curl: `curl ${base}/chat/completions \\\n  -H "Authorization: Bearer ${key}" \\\n  -H "Content-Type: application/json" \\\n  -i \\\n  -d '{"model": "${model}", "messages": [{"role": "user", "content": "Hello"}]}'`,
    python: `from openai import OpenAI\n\nclient = OpenAI(base_url="${base}", api_key="${key}")\nresp = client.chat.completions.with_raw_response.create(\n    model="${model}",\n    messages=[{"role": "user", "content": "Hello"}],\n)\nprint(resp.headers.get("X-PackServe-Route"), resp.headers.get("X-PackServe-Reason"))\nprint(resp.parse().choices[0].message.content)`};
}

function signupPage() {
  const plan = getPlan();
  $("#plan-summary").textContent = `Plan: ${plan.model}, ${plan.tpot_target_ms} ms TPOT, ${plan.reserved_replicas} reserved replica(s), burst ${plan.burst_enabled ? "on" : "off"}.` + (plan.persona_name ? ` Use case: ${plan.persona_name}.` : "");
  let cur = "curl", data;
  const show = () => { if (data) $("#snippet").textContent = snippets(data.tenant.api_key, plan.model, data.gateway_base_url)[cur]; };
  document.querySelectorAll("[data-snip]").forEach(b => b.onclick = () => { cur = b.dataset.snip; show(); });
  $("#copy-key").onclick = async e => { await navigator.clipboard?.writeText($("#api-key").textContent); e.target.textContent = "Copied"; };
  $("#signup-form").onsubmit = async ev => {
    ev.preventDefault();
    const err = $("#signup-error"); err.hidden = true;
    const body = {name: $("#name").value, email: $("#email").value, company: $("#company").value, model: plan.model,
      tpot_target_ms: plan.tpot_target_ms, burst_enabled: plan.burst_enabled, reserved_replicas: plan.reserved_replicas, persona_id: plan.persona_id};
    const r = await jpost("/api/signup", body), j = await r.json();
    if (!r.ok) { err.textContent = j.error || "Signup failed"; err.hidden = false; return; }
    data = j; $("#signup-form").hidden = true; $("#result").hidden = false;
    $("#api-key").textContent = j.tenant.api_key;
    $("#result-mode").textContent = j.mode === "live" ? "Tenant registered in the metadata service." : "DEMO MODE: the metadata service was unreachable, so this tenant is stored in a local JSON file. The key works only against the simulated playground.";
    sessionStorage.setItem(TENANT_KEY, JSON.stringify({key: j.tenant.api_key, model: plan.model, burst: plan.burst_enabled}));
    show(); $("#result").querySelector("h2").focus?.();
  };
}

async function playgroundPage() {
  const models = await (await fetch("/api/models")).json();
  const sel = $("#pg-model"); models.forEach(m => sel.append(el("option", {value: m.id}, m.id)));
  const t = JSON.parse(sessionStorage.getItem(TENANT_KEY) || "null");
  if (t) { $("#pg-key").value = t.key; sel.value = t.model; }
  document.querySelectorAll("[data-key]").forEach(b => b.onclick = () => $("#pg-key").value = b.dataset.key);
  const badge = h => {
    const route = h.get("x-packserve-route") || "none";
    const b = $("#route-badge"); b.className = "badge " + (["replica", "overflow", "rejected"].includes(route) ? route : "none");
    b.textContent = route === "none" ? "No routing headers" : `Route: ${route}`;
    if (h.get("x-packserve-simulated") === "true") b.append(" ", el("span", {class: "chip-sim"}, "SIMULATED"));
    const dl = $("#route-details"); dl.replaceChildren();
    [["Reason", "x-packserve-reason"], ["Replica", "x-packserve-replica"], ["Predicted TPOT (ms)", "x-packserve-predicted-tpot-ms"],
     ["Candidates", "x-packserve-candidates"], ["Request id", "x-packserve-request-id"]].forEach(([l, k]) => { if (h.get(k)) dl.append(el("dt", {}, l), el("dd", {}, h.get(k))); });
    if (h.get("x-onboarding-demo") === "true") dl.append(el("dt", {}, "Source"), el("dd", {}, "local demo simulation"));
  };
  $("#pg-send").onclick = async () => {
    const out = $("#pg-out"); out.textContent = "";
    const r = await jpost("/api/chat", {api_key: $("#pg-key").value, model: sel.value, prompt: $("#pg-prompt").value});
    badge(r.headers);
    if (!r.ok) { const j = await r.json().catch(() => ({})); out.textContent = `HTTP ${r.status}: ` + (j.error?.message || j.error || "request failed") + (r.headers.get("retry-after") ? ` (retry after ${r.headers.get("retry-after")}s)` : ""); return; }
    const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "";
    for (;;) { const {done, value} = await rd.read(); if (done) break;
      buf += dec.decode(value, {stream: true}); const lines = buf.split("\n"); buf = lines.pop();
      for (const l of lines) { if (!l.startsWith("data:") || l.includes("[DONE]")) continue;
        try { out.textContent += JSON.parse(l.slice(5)).choices[0].delta?.content ?? ""; } catch {} } }
  };
  $("#burst-go").onclick = async () => {
    const tally = {replica: 0, overflow: 0, rejected: 0}; const show = t => Object.keys(tally).forEach(k => $("#t-" + k).textContent = t[k] || 0); show(tally);
    const r = await jpost("/api/burst", {api_key: $("#pg-key").value, model: sel.value, n: Number($("#burst-n").value), burst_enabled: t?.burst ?? true});
    if (!r.ok) { $("#burst-note-pg").textContent = "Error: " + ((await r.json()).error || r.status); return; }
    const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "";
    for (;;) { const {done, value} = await rd.read(); if (done) break;
      buf += dec.decode(value, {stream: true}); const lines = buf.split("\n"); buf = lines.pop();
      for (const l of lines) { if (!l.trim()) continue; const m = JSON.parse(l);
        if (m.start) $("#burst-note-pg").textContent = m.mode === "live" ? "Live run against the gateway (replicas SIMULATED)." : "DEMO MODE: SIMULATED tally (gateway unreachable).";
        if (m.tally) show(m.tally); } }
  };
  const roll = async () => { const j = await (await fetch("/api/rollup?window_s=60")).json();
    $("#rollup").textContent = j.available ? JSON.stringify(j.rollup, null, 2) : "Telemetry service not reachable."; };
  $("#rollup-go").onclick = roll; roll();
}

banner();
({use_cases: usecases, plan: planPage, signup: signupPage, playground: playgroundPage})[page]?.();
})();
