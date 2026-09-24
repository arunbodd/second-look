/* Second Look: dashboard front end. No build step, no external dependencies. */
(() => {
  "use strict";

  const LANES = ["suspicious", "review", "likely_fp"];
  const LANE_LABEL = { suspicious: "Suspicious", review: "Needs review", likely_fp: "Likely false positive" };
  const LANE_ACTION = { suspicious: "verify before contact", review: "check the records", likely_fp: "confirm and close" };
  const LANE_VAR = { suspicious: "var(--lane-suspicious)", review: "var(--lane-review)", likely_fp: "var(--lane-likely_fp)" };
  const VERDICT_LABEL = { pass: "pass", revise: "flagged", fail: "failed" };
  const SIGNAL_LABEL = {
    duplicate_service_billed: "Duplicate claim", service_overlap_other_provider: "Provider overlap", weekly_visit_frequency: "Visits / week",
    member_provider_distance_miles: "Distance", weekend_billing_ratio: "Weekend share", shared_contact_with_provider: "Shared contact",
    amount_vs_peer_avg_pct: "Amount vs norm", round_dollar_billing_ratio: "Round-dollar", recent_policy_change_flag: "Policy change",
    prior_claims_last_12mo: "Prior claims",
  };
  const FAMILY_LABEL = { double_billing: "Double billing", not_rendered: "Care not delivered", relationship: "Relationship", billing_anomaly: "Billing anomalies", timing_history: "Timing & history" };
  const FAMILY_ORDER = ["double_billing", "not_rendered", "relationship", "billing_anomaly", "timing_history"];
  const EMPTY_FILTERS = () => ({ lane: null, care: null, state: null, status: "all", search: "", bin: null, signal: null, picks: false });
  const STORE = { get(k, d) { try { const v = localStorage.getItem("jai." + k); return v == null ? d : JSON.parse(v); } catch (_) { return d; } },
    set(k, v) { try { localStorage.setItem("jai." + k, JSON.stringify(v)); } catch (_) { /* private mode */ } } };

  const THEMES = ["auto", "light", "dark"], THEME_LABEL = { auto: "Theme: auto", light: "Theme: light", dark: "Theme: dark" };
  function applyTheme(t) {
    if (t === "auto") document.documentElement.removeAttribute("data-theme"); else document.documentElement.setAttribute("data-theme", t);
    const b = document.getElementById("theme-btn"); if (b) b.textContent = THEME_LABEL[t];
  }
  applyTheme(STORE.get("theme", "auto"));

  const state = {
    meta: null, rows: [], values: {}, metrics: null, models: null, review: null,
    filters: EMPTY_FILTERS(), sort: { key: "risk_score", dir: -1 }, selected: null, view: null, poll: null, qpoll: null, rpoll: null,
    overrideOpen: false, mode: "offline", askOpen: false, page: "queue", analysis: null,
    capacity: 8, rejectOpen: false,
  };
  state.capacity = STORE.get("capacity", 8);

  const M = () => `mode=${state.mode}`;

  const $ = (s, r = document) => r.querySelector(s);
  const el = (tag, attrs = {}, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") n.className = v; else if (k === "html") n.innerHTML = v;
      else if (k === "style") n.style.cssText = v; // CSSOM, allowed by the strict CSP (no inline style attributes)
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v); else if (v != null) n.setAttribute(k, v);
    }
    for (const c of kids.flat(Infinity)) if (c != null) n.append(c.nodeType ? c : document.createTextNode(String(c)));
    return n;
  };
  const svgEl = (tag, attrs = {}, ...kids) => {
    const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs)) { if (k.startsWith("on")) n.addEventListener(k.slice(2), v); else if (k === "style") n.style.cssText = v; else n.setAttribute(k, v); }
    for (const c of kids.flat(Infinity)) if (c != null) n.append(c.nodeType ? c : document.createTextNode(String(c)));
    return n;
  };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const money = (n) => "$" + Math.round(n).toLocaleString("en-US");
  const kmoney = (n) => n >= 1e6 ? `$${(n / 1e6).toFixed(1)}M` : n >= 1000 ? `$${Math.round(n / 1000)}k` : `$${Math.round(n)}`;
  const ordinal = (n) => { n = Math.round(n); const s = (n % 100 >= 11 && n % 100 <= 13) ? "th" : ({ 1: "st", 2: "nd", 3: "rd" }[n % 10] || "th"); return n + s; };
  const actor = () => ($("#actor").value || "investigator").trim();
  const cites = (t) => esc(t).replace(/\[(E\d+)\]/g, '<span class="cite">[$1]</span>');
  const eyebrow = (...kids) => el("span", { class: "eyebrow" }, ...kids);

  async function api(path, opts = {}) {
    const res = await fetch(path, { headers: { "Content-Type": opts.raw ? "text/csv" : "application/json", "X-Investigator": actor() }, method: opts.method || "GET", body: opts.raw ?? (opts.body ? JSON.stringify(opts.body) : undefined) });
    if (!res.ok) { let m = res.statusText; try { m = (await res.json()).detail || m; } catch (_) { /* ignore */ } throw new Error(m); }
    return res.headers.get("content-type")?.includes("json") ? res.json() : res.text();
  }
  let toastT = null;
  const toast = (t) => { const n = $("#toast"); n.textContent = t; n.hidden = false; clearTimeout(toastT); toastT = setTimeout(() => (n.hidden = true), 2800); };

  /* tooltip for anything with data-tip */
  const tip = $("#tip");
  document.addEventListener("mousemove", (e) => {
    const t = e.target.closest?.("[data-tip]");
    if (!t) { tip.hidden = true; return; }
    tip.textContent = t.dataset.tip; tip.hidden = false;
    tip.style.left = Math.min(e.clientX + 12, window.innerWidth - 290) + "px"; tip.style.top = (e.clientY + 14) + "px";
  });

  /* ------------------------------------------------------------------ boot + data */
  async function boot() {
    state.meta = await api("/api/meta");
    state.mode = state.meta.ai_available ? "ai" : "offline";
    try { state.values = await api("/api/queue/values"); } catch (_) { /* fine */ }
    await refresh();
    $("#csv-file").onchange = async (e) => { const f = e.target.files[0]; e.target.value = ""; if (!f) return; toast(`Triaging ${f.name}…`); await switchDataset(`/api/dataset?filename=${encodeURIComponent(f.name)}`, await f.text()); };
    $("#data-btn").onclick = () => toggleData();
    $("#models-btn").onclick = () => toggleModels();
    const rerender = () => { renderAll(); if (state.page === "drivers" && state.analysis) renderDrivers(); if (state.selected) renderDrawer(); };
    applyTheme(STORE.get("theme", "auto"));
    $("#theme-btn").onclick = () => { const cur = STORE.get("theme", "auto"); const next = THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length]; STORE.set("theme", next); applyTheme(next); rerender(); };
    try { window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (STORE.get("theme", "auto") === "auto") rerender(); }); } catch (_) { /* old browsers */ }
    $("#scrim").onclick = closeDrawer;
    document.addEventListener("click", (e) => {
      if (!$("#models-menu").hidden && !e.target.closest("#models-menu, #models-btn")) toggleModels(false);
      if (!$("#data-menu").hidden && !e.target.closest("#data-menu, #data-btn")) toggleData(false);
    });
    document.addEventListener("keydown", (e) => {
      if (e.target.matches("input, select, textarea")) return;
      if (e.key === "Escape") { closeDrawer(); toggleModels(false); toggleData(false); }
      if (!state.selected) return;
      const list = visible().map((r) => r.case_id), i = list.indexOf(state.selected);
      if (e.key === "j" && i < list.length - 1) openCase(list[i + 1]);
      if (e.key === "k" && i > 0) openCase(list[i - 1]);
      if (e.key === "a" && state.view?.state.decision === "pending") decide("accept");
    });
  }

  async function refresh() {
    const [q, m, meta] = await Promise.all([api(`/api/queue?${M()}`), api("/api/metrics"), api("/api/meta")]);
    state.rows = q.cases; state.metrics = m; state.briefing = q.briefing; state.meta = meta; state.review = meta.review;
    renderMeta(); renderRunBar(); renderAll();
    if (state.page === "drivers") loadDrivers();
    clearTimeout(state.qpoll);
    if (state.review?.running || state.rows.some((r) => r.assessment_status === "running")) state.qpoll = setTimeout(refresh, 3000);
  }

  async function switchDataset(path, body) {
    try {
      const ds = await api(path, { method: "POST", raw: body ?? "" });
      try { state.values = await api("/api/queue/values"); } catch (_) { /* ignore */ }
      state.filters = EMPTY_FILTERS();
      closeDrawer();
      await refresh();
      toast(`${ds.name}: ${ds.rows} cases triaged` + (state.mode === "ai" ? ". Run the AI review when ready." : ""));
    } catch (e) { toast("Upload failed: " + e.message); }
  }

  function setMode(mode) {
    const wasDrivers = state.page === "drivers";
    if (mode === state.mode && !wasDrivers) return;
    state.page = "queue"; showPage();
    if (mode === state.mode) { renderMeta(); renderRunBar(); return; }
    state.mode = mode;
    refresh();
    if (state.selected) loadCase(state.selected);
  }

  function setPage(page) {
    if (state.page === page) return;
    state.page = page; showPage(); renderMeta(); renderRunBar();
    if (page === "drivers") loadDrivers();
  }

  function showPage() {
    const q = state.page === "queue";
    for (const s of ["#brief", "#charts", ".table-wrap"]) $(s).hidden = !q;
    $("#drivers").hidden = q;
    window.scrollTo(0, 0);
  }

  function modelLabel(id, fallback) {
    const e = state.models?.catalog.find((x) => x.id === id);
    return e ? e.label : (fallback && fallback !== "offline" ? fallback.split(":").pop() : "none");
  }

  function renderMeta() {
    const m = state.meta;
    const tabs = $("#mode-tabs"); tabs.innerHTML = "";
    const aiReady = m.modes.ai.ready;
    const q = state.page === "queue";
    tabs.append(
      el("button", { class: q && state.mode === "offline" ? "on" : "", "data-tip": "Engine score, template narrative, rule-based checks. No model calls.", onclick: () => setMode("offline") }, "Rules review"),
      el("button", { class: q && state.mode === "ai" ? "on" : "", "data-tip": m.ai_available ? `Narratives written by ${m.generator}, reviewed by ${m.judge}.` : "Pick a writer in the Models menu, then run the review.", onclick: () => setMode("ai") }, "AI review",
        el("span", { class: "cnt" }, `${aiReady}/${m.cases}`)),
      el("span", { class: "tab-sep", "aria-hidden": "true" }),
      el("button", { class: q ? "" : "on", "data-tip": "How each column drives the risk score, computed for the loaded file.", onclick: () => setPage("drivers") }, "Score drivers"));
    $("#data-btn").textContent = `${m.dataset.name} · ${m.dataset.rows} ▾`;
    const w = modelLabel(m.models.writer, m.generator), j = modelLabel(m.models.judge, m.judge);
    $("#models-btn").textContent = m.replay ? "Recorded run ▾" : m.ai_available ? `${w} · judge ${j} ▾` : "Models ▾";
  }

  /* ------------------------------------------------------------------ data menu */
  function placeMenu(menu, btn) {
    const r = btn.getBoundingClientRect(), w = Math.min(menu.offsetWidth || 420, window.innerWidth - 16);
    menu.style.cssText = `top:${r.bottom + 6}px;left:${Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8))}px;right:auto`;
  }

  function toggleData(open) {
    const menu = $("#data-menu"), btn = $("#data-btn");
    const want = open ?? menu.hidden;
    if (!want) { menu.hidden = true; btn.setAttribute("aria-expanded", "false"); return; }
    toggleModels(false);
    const m = state.meta; menu.innerHTML = "";
    const item = (label, sub, on, cls = "") => el("button", { class: "m-item " + cls, onclick: () => { toggleData(false); on(); } }, el("span", {}, label), sub ? el("span", { class: "eyebrow" }, sub) : null);
    menu.append(
      el("div", { class: "m-head" }, el("span", { class: "label" }, "Data"), eyebrow(`${m.dataset.name} · ${m.dataset.rows} cases`)),
      el("div", { class: "m-list" },
        item("Upload a case sheet…", "CSV with the same 16 columns as the sample", () => $("#csv-file").click()),
        m.dataset.uploaded ? item("Use the sample data", "50 synthetic referrals", () => switchDataset("/api/dataset/sample")) : null,
        item("Export to Excel", "summary with live formulas, queue, assessments, activity", () => { window.location.href = `/api/export.xlsx?${M()}`; }),
        el("div", { class: "m-rule" }),
        item("Reset decisions and notes", "model results stay cached", async () => {
          if (!confirm("Clear every decision, note, and question for this file?")) return;
          await api("/api/reset", { method: "POST" }); toast("Decisions cleared"); await refresh(); if (state.selected) loadCase(state.selected);
        }, "danger")),
      el("details", { class: "m-note" }, el("summary", {}, `Required columns (${m.required_columns.length})`), el("div", {}, m.required_columns.join(", "))));
    menu.hidden = false; placeMenu(menu, btn); btn.setAttribute("aria-expanded", "true");
  }

  /* ------------------------------------------------------------------ models menu */
  async function toggleModels(open) {
    const menu = $("#models-menu"), btn = $("#models-btn");
    const want = open ?? menu.hidden;
    if (!want) { menu.hidden = true; btn.setAttribute("aria-expanded", "false"); return; }
    if (!$("#data-menu").hidden) toggleData(false);
    try { state.models = await api("/api/models"); } catch (e) { toast(e.message); return; }
    renderModels(); menu.hidden = false; placeMenu(menu, btn); btn.setAttribute("aria-expanded", "true");
  }

  function renderModels() {
    const menu = $("#models-menu"), mm = state.models; menu.innerHTML = "";
    const fams = [...new Set(mm.catalog.map((e) => e.family))];
    const price = (e) => e.in == null ? "" : ` · $${e.in} / $${e.out}`;
    const select = (id, role, current) => {
      const s = el("select", { id });
      s.append(el("option", { value: "offline" }, role === "writer" ? "None (rules review only)" : "None (rule checks only)"));
      if (mm.recorded?.available) s.append(el("option", { value: "recorded", selected: current === "recorded" ? "" : null }, role === "writer" ? `${mm.recorded.label} (no key needed)` : `${mm.recorded.judge} (recorded with the run)`));
      for (const f of fams) {
        const g = el("optgroup", { label: mm.catalog.find((e) => e.family === f).family_label + (mm.keys[f] ? "" : " · no key") });
        for (const e of mm.catalog.filter((e) => e.family === f)) g.append(el("option", { value: e.id, disabled: e.key_present ? null : "", selected: e.id === current ? "" : null }, e.label + price(e) + (e.roles.includes(role) ? "" : " · not recommended")));
        s.append(g);
      }
      if (!current) s.value = "offline";
      if (role === "judge" && current !== "recorded") s.querySelector('option[value="recorded"]')?.remove();
      return s;
    };
    const wSel = select("m-writer", "writer", mm.current.writer), jSel = select("m-judge", "judge", mm.current.judge);
    const note = el("div", { class: "m-note" });
    const famOf = (id) => { const e = mm.catalog.find((x) => x.id === id); return e ? (e.vendor || e.family) : undefined; };
    const updNote = () => {
      note.innerHTML = "";
      const rec = wSel.value === "recorded";
      if (rec && !jSel.querySelector('option[value="recorded"]')) jSel.append(el("option", { value: "recorded" }, `${mm.recorded.judge} (recorded with the run)`));
      if (rec) jSel.value = "recorded";
      else { jSel.querySelector('option[value="recorded"]')?.remove(); if (!jSel.value) jSel.value = "offline"; }
      jSel.disabled = rec;
      if (rec) { note.append(el("div", {}, `A run recorded on ${mm.recorded.recorded_at.slice(0, 10)}: ${mm.recorded.writer} wrote every assessment and ${mm.recorded.judge} judged it; the suspicious cases also have answers to the starter questions. Nothing is sent to a model. A case whose evidence has changed since the recording shows "not run".`)); return; }
      note.append(el("div", {}, ...Object.entries(mm.keys).map(([f, ok]) => el("span", { style: "margin-right:12px" }, el("i", { class: "key" + (ok ? " on" : "") }), `${mm.catalog.find((e) => e.family === f)?.family_label || f} key ${ok ? "set" : "not set"}`))));
      if (wSel.value !== "offline" && famOf(wSel.value) && famOf(wSel.value) === famOf(jSel.value)) note.append(el("div", { class: "warn" }, "Writer and judge come from the same model maker. A model tends to rate its own output favourably; a judge from another family is a more independent review."));
      else if (wSel.value !== "offline" && jSel.value === "offline") note.append(el("div", {}, "Without a judge, drafts get the rule checks only."));
      else note.append(el("div", {}, "Keys are read from .env (OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY, or PERPLEXITY_API_KEY, which reaches all three makers). Prices are $ per million tokens in / out."));
    };
    wSel.onchange = jSel.onchange = updNote; updNote();
    menu.append(
      el("div", { class: "m-head" }, el("span", { class: "label" }, "Models"), eyebrow("applies to the next run · switching keeps each pair's cache")),
      el("div", { class: "m-body" }, el("label", { for: "m-writer" }, "Writer", wSel), el("label", { for: "m-judge" }, "Judge", jSel)),
      note,
      el("div", { class: "m-foot" }, el("button", { class: "btn quiet", onclick: () => toggleModels(false) }, "Cancel"),
        el("button", { class: "btn primary", onclick: async (e) => {
          e.target.disabled = true;
          try { state.models = await api("/api/models", { method: "POST", body: { writer: wSel.value, judge: jSel.value } }); toggleModels(false); toast("Models updated"); if (state.models.current.writer && state.mode !== "ai") state.mode = "ai"; await refresh(); if (state.selected) loadCase(state.selected); }
          catch (err) { toast(err.message); e.target.disabled = false; }
        } }, "Use these models")));
  }

  /* ------------------------------------------------------------------ run bar */
  function estimate(n) {
    const mm = state.models, m = state.meta;
    if (!mm) return `${n} cases`;
    const w = mm.catalog.find((e) => e.id === mm.current.writer), j = mm.catalog.find((e) => e.id === mm.current.judge);
    const pc = mm.estimate.per_case;
    const per = (e, role) => e && e.in != null ? (pc[role].in * e.in + pc[role].out * e.out) / 1e6 : null;
    const cost = (per(w, "writer") ?? 0) + (per(j, "judge") ?? 0);
    const calls = Math.round(n * (j ? mm.estimate.calls_per_case : 1));
    const mins = Math.max(1, Math.round(n * 9 / 60 / 3));
    const toks = n * (pc.writer.in + pc.writer.out + (j ? pc.judge.in + pc.judge.out : 0));
    return `≈ ${calls} calls · ≈ ${ktok(toks)} tokens · ${per(w, "writer") == null ? "cost unknown" : "≈ " + usd(n * cost)}${mm.estimate.measured ? ` (from ${mm.estimate.measured} reviews)` : ""} · ~${mins} min`;
  }
  const ktok = (n) => n >= 1e6 ? `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(n >= 1e4 ? 0 : 1)}k` : String(Math.round(n || 0));
  const usd = (x) => x == null ? "–" : x > 0 && x < 0.01 ? `$${x.toFixed(4)}` : `$${x.toFixed(2)}`;
  function usageText(u, lead) {
    if (!u || !u.calls) return null;
    const cachedPct = u.input ? Math.round((u.cached / u.input) * 100) : 0;
    const src = u.cost_source === "provider" ? "as billed by the provider" : u.cost_source === "mixed" ? "billed and estimated" : "estimated from list prices";
    return `${lead}${ktok(u.input)} in${u.cached ? ` (${cachedPct}% cached)` : ""} · ${ktok(u.output)} out${u.reasoning ? ` (${ktok(u.reasoning)} reasoning)` : ""} · ${u.calls} calls · ${usd(u.cost_usd)} ${src}`;
  }
  function usageLine(m) {
    const us = m.usage; if (!us) return null;
    const rv = us.reviews, parts = [];
    const r = usageText(rv, us.recorded ? `The recording used, for ${rv.cases} reviews: ` : `Tokens for the ${rv.cases} AI reviews loaded: `);
    if (r) parts.push(r);
    const a = usageText(us.recorded ? us.recorded_answers : us.answers, us.recorded ? "Its recorded answers: " : "Questions answered: ");
    if (a) parts.push(a);
    return parts.length ? el("div", { class: "r-usage", "data-tip": "Token counts come from each provider's response. Reviews loaded from the cache or the recording were paid for once, when they were written." }, parts.join("  ·  ")) : null;
  }
  function renderRunBar() {
    const bar = $("#runbar"), m = state.meta, rv = state.review;
    bar.hidden = state.mode !== "ai" || state.page !== "queue";
    if (bar.hidden) return;
    bar.innerHTML = "";
    if (!state.models) api("/api/models").then((mm) => { state.models = mm; renderRunBar(); }).catch(() => {});
    const main = el("div", { class: "r-main" });
    const side = el("div", { class: "r-side" });
    if (!m.ai_available) {
      main.append(el("div", { class: "r-title" }, el("span", { class: "label" }, "Run AI review"), el("span", { class: "muted" }, "No writer configured. Pick one in the Models menu; the queue and the rules review work without it.")));
      side.append(el("button", { class: "btn tall", onclick: () => toggleModels(true) }, "Choose models"));
      bar.append(main, side); return;
    }
    if (m.replay) {
      const rp = m.replay, shown = visible(), have = shown.filter((r) => r.assessment_status === "ready").length;
      main.append(el("div", { class: "r-title" }, el("span", { class: "label" }, "Recorded AI review"),
        el("span", { class: "muted" }, `Recorded on ${rp.recorded_at.slice(0, 10)} with ${rp.writer} writing and ${rp.judge} judging. ${rp.matched} of ${m.cases} cases match today's evidence${rp.questions ? "; the suspicious cases also have answers to the starter questions" : ""}. Nothing is sent to a model. Add an API key to .env to run new reviews.`)));
      main.append(el("div", { class: "r-scope" }, el("span", { class: "lbl" }, `Shown: ${shown.length} case${shown.length === 1 ? "" : "s"} · ${have} with a recorded review`),
        el("span", { class: "r-est" }, rp.same_maker ? "Both models come from one maker. With your own keys, a judge from another maker is the more independent check." : "The writer and the judge come from different model makers, so the judge is not grading its own family's writing.")));
      const ul = usageLine(m); if (ul) main.append(ul);
      side.append(el("button", { class: "btn tall", onclick: () => toggleModels(true) }, "Choose models"));
      bar.append(main, side); return;
    }
    const todo = (rows) => rows.filter((r) => r.assessment_status !== "ready").length;
    const target = visible(), n = todo(target);
    main.append(el("div", { class: "r-title" }, el("span", { class: "label" }, "Run AI review"),
      el("span", { class: "muted" }, rv?.running ? "Rows fill in as each case finishes." : `${m.modes.ai.ready} of ${m.cases} cases reviewed. The run covers the cases the table shows below, so choose Today's picks, a risk rating, or any filter to change it. Results are cached, and nothing goes to a model until you run it.`)));
    if (rv?.running) {
      const pct = rv.total ? Math.round((rv.done / rv.total) * 100) : 0;
      main.append(el("div", { class: "r-prog" }, el("div", { class: "bar" }, el("i", { style: `width:${pct}%` })),
        el("span", {}, `${rv.done} / ${rv.total} reviewed · ${rv.revised} revised`, rv.failed ? el("span", { class: "fail" }, ` · ${rv.failed} failed`) : null, rv.stopped ? " · stopping" : "")));
      side.append(el("button", { class: "btn tall", disabled: rv.stopped ? "" : null, onclick: async () => { await api("/api/review/stop", { method: "POST" }); refresh(); } }, "Stop"));
    } else {
      main.append(el("div", { class: "r-scope" }, el("span", { class: "lbl" }, `Shown: ${target.length} case${target.length === 1 ? "" : "s"} · ${n} not reviewed yet`),
        el("span", { class: "r-est" }, `${modelLabel(m.models.writer, m.generator)} · judge ${modelLabel(m.models.judge, m.judge)} · ${estimate(n)}`)));
      if (rv && rv.done) main.append(el("div", { class: "r-prog" }, el("span", {}, `Last run: ${rv.done} reviewed · ${rv.revised} revised`, rv.failed ? el("span", { class: "fail" }, ` · ${rv.failed} failed`) : null)));
      const ul = usageLine(m); if (ul) main.append(ul);
      side.append(el("button", { class: "btn tall accent", disabled: n ? null : "", onclick: () => runReview(target.filter((r) => r.assessment_status !== "ready").map((r) => r.case_id)) }, n ? `Run ${n} case${n === 1 ? "" : "s"}` : "Nothing to run"));
    }
    bar.append(main, side);
  }
  async function runReview(ids) {
    try { state.review = await api("/api/review/run", { method: "POST", body: { case_ids: ids } }); toast(`Reviewing ${ids.length} case${ids.length === 1 ? "" : "s"}…`); await refresh(); if (state.selected) loadCase(state.selected); }
    catch (e) { toast(e.message); }
  }

  /* ------------------------------------------------------------------ filters */
  const F = () => state.filters;
  function matches(r, skip = null) {
    const f = F();
    if (skip !== "lane" && f.lane && r.lane !== f.lane) return false;
    if (skip !== "care" && f.care && r.care_type !== f.care) return false;
    if (skip !== "state" && f.state && r.state !== f.state) return false;
    if (f.status === "todo" && !undecided(r)) return false;
    if (f.status === "decided" && !["accept", "reject"].includes(r.decision)) return false;
    if (f.status === "closed" && !r.closed) return false;
    if (f.picks && !pickSet().has(r.case_id)) return false;
    if (skip !== "bin" && f.bin !== null && Math.min(9, Math.floor(r.risk_score / 10)) !== f.bin) return false;
    if (skip !== "signal" && f.signal && !["elevated", "high"].includes(r.signals[f.signal])) return false;
    if (f.search) {
      const hay = `${r.case_id} ${r.claim_number} ${r.state} ${r.care_type} ${r.status_label} ${r.decision_label} ${r.drivers.join(" ")}`.toLowerCase();
      if (!hay.includes(f.search)) return false;
    }
    return true;
  }
  const visible = () => sortRows(state.rows.filter((r) => matches(r)));
  function setFilter(k, v) { F()[k] = F()[k] === v ? null : v; renderAll(); }

  function renderAll() { renderBrief(); renderCharts(); renderFilters(); renderTable(); if (state.mode === "ai" && !state.review?.running) renderRunBar(); }

  /* capacity: open cases in order of dollars at risk; the first N are today's picks */
  const undecided = (r) => ["pending", "needs_evidence"].includes(r.decision) && !r.closed;
  function openQueue() { return state.rows.filter(undecided).sort((a, b) => b.dollars_at_risk - a.dollars_at_risk || a.case_id.localeCompare(b.case_id)); }
  function capacityN() { const n = openQueue().length; return Math.max(0, Math.min(state.capacity, n)); }
  function pickSet() { return new Set(openQueue().slice(0, capacityN()).map((r) => r.case_id)); }
  function pickIndex() { const m = new Map(); openQueue().slice(0, capacityN()).forEach((r, i) => m.set(r.case_id, i + 1)); return m; }
  function coverage(n) {
    const q = openQueue(), total = q.reduce((s, r) => s + r.dollars_at_risk, 0);
    const covered = q.slice(0, n).reduce((s, r) => s + r.dollars_at_risk, 0);
    return { covered, total, share: total ? covered / total : 0 };
  }
  function setCapacity(n) {
    const max = openQueue().length;
    state.capacity = Math.max(1, Math.min(max || 1, Math.round(n))); STORE.set("capacity", state.capacity);
    renderBrief(); renderCharts(); renderTable(); if (F().picks) renderFilters();
  }

  function renderBrief() {
    const root = $("#brief"); root.innerHTML = "";
    const rows = state.rows, total = rows.reduce((s, r) => s + r.claim_amount_usd, 0);
    const byLane = Object.fromEntries(LANES.map((l) => { const rs = rows.filter((r) => r.lane === l); return [l, { n: rs.length, amt: rs.reduce((s, r) => s + r.claim_amount_usd, 0) }]; }));
    const sus = byLane.suspicious, share = total ? sus.amt / total : 0;
    const open = openQueue().length, n = capacityN(), cov = coverage(n);
    const decided = rows.filter((r) => ["accept", "reject"].includes(r.decision)).length;
    const m = state.metrics;

    const stepper = el("span", { class: "stepper" },
      el("button", { "aria-label": "One fewer review", onclick: () => setCapacity(state.capacity - 1), disabled: n <= 1 ? "" : null }, "−"),
      el("input", { type: "number", min: 1, max: Math.max(1, open), value: n, "aria-label": "Reviews planned today", onchange: (e) => setCapacity(+e.target.value || 1) }),
      el("button", { "aria-label": "One more review", onclick: () => setCapacity(state.capacity + 1), disabled: n >= open ? "" : null }, "+"));
    root.append(
      el("div", { class: "brief-head" }, eyebrow(`Morning brief · ${state.meta.dataset.name} · triaged ${new Date(state.meta.triaged_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`),
        el("span", { class: "eyebrow" }, decided ? `${decided} decided · ${Math.round((m.agreement_rate || 0) * 100)}% agree with the triage` : "no decisions yet")),
      el("p", { class: "brief-lead" }, el("b", { class: "num-sus" }, String(sus.n)), ` of ${rows.length} referrals look suspicious and hold `, el("b", {}, `${Math.round(share * 100)}%`), " of claim dollars."),
      open ? el("p", { class: "brief-sub" }, "Reviewing the top ", stepper, ` case${n === 1 ? "" : "s"} by dollars at risk covers `, el("b", {}, `${Math.round(cov.share * 100)}%`),
        ` of the open dollars at risk (${kmoney(cov.covered)} of ${kmoney(cov.total)}).`)
        : el("p", { class: "brief-sub" }, "Every case has a decision. Nice work."),
      ...(state.briefing?.alerts || []).map((al) => el("p", { class: "brief-alert" }, el("b", {}, "Queue alert · "), al.text, " ",
        (al.case_ids || []).map((id, i) => [i ? " " : "", el("a", { href: "#", onclick: (e) => { e.preventDefault(); openCase(id); } }, `Open ${id}`)]))),
      dollarStrip(byLane, total));
  }

  function dollarStrip(byLane, total) {
    const wrap = el("div", { class: "strip-wrap" });
    const bar = el("div", { class: "dstrip", role: "img", "aria-label": "Each case as a segment sized by its claim amount, coloured by risk rating" });
    const picks = pickSet(), f = F();
    const order = { suspicious: 0, review: 1, likely_fp: 2 };
    const rows = state.rows.slice().sort((a, b) => order[a.lane] - order[b.lane] || b.claim_amount_usd - a.claim_amount_usd);
    for (const r of rows) {
      const dim = (f.lane && f.lane !== r.lane) || !matches(r, "lane");
      bar.append(el("button", { class: `seg lane-${r.lane}` + (picks.has(r.case_id) ? " pick" : "") + (dim ? " dim" : "") + (r.closed ? " closed" : ""),
        style: `flex-grow:${Math.max(r.claim_amount_usd, total * 0.002)}`, "aria-label": `${r.case_id}, ${money(r.claim_amount_usd)}, ${LANE_LABEL[r.lane]}`,
        "data-tip": `${r.case_id} · ${money(r.claim_amount_usd)} · score ${r.risk_score}${picks.has(r.case_id) ? " · today's pick" : ""}${r.closed ? " · closed" : ""}`, onclick: () => openCase(r.case_id) }));
    }
    const legend = el("div", { class: "strip-legend" }, LANES.map((l) => el("button", { class: "lg" + (f.lane === l ? " on" : ""), "aria-pressed": String(f.lane === l), onclick: () => { f.lane = f.lane === l ? null : l; renderAll(); } },
      el("span", { class: "sw", style: `background:${LANE_VAR[l]}` }), el("b", {}, LANE_LABEL[l]), ` ${byLane[l].n} cases · ${kmoney(byLane[l].amt)} · ${total ? Math.round(byLane[l].amt / total * 100) : 0}%`)),
      el("span", { class: "eyebrow strip-note" }, el("i", { class: "pick-key" }), "today's picks · width = claim amount"));
    wrap.append(bar, legend);
    return wrap;
  }

  function renderFilters() {
    const f = F(), root = $("#filters"); root.innerHTML = "";
    const seg = (opts, cur, on) => { const s = el("div", { class: "seg" }); for (const [v, l] of opts) s.append(el("button", { class: cur === v ? "on" : "", "aria-pressed": String(cur === v), onclick: () => on(v) }, l)); return s; };
    const cares = [...new Set(state.rows.map((r) => r.care_type))].sort();
    const states = [...new Set(state.rows.map((r) => r.state))].sort();
    const sel = (label, key, opts) => el("select", { "aria-label": label, onchange: (e) => { f[key] = e.target.value || null; renderAll(); } }, el("option", { value: "" }, label), opts.map((o) => el("option", { value: o, selected: f[key] === o ? "" : null }, o)));
    const shown = visible().length;
    root.append(
      el("div", { class: "tools-left" },
        el("input", { type: "search", class: "search", "aria-label": "Search", placeholder: "Search cases, claims, drivers…", value: f.search, oninput: (e) => { f.search = e.target.value.toLowerCase(); renderCharts(); renderTable(); $("#count").textContent = countText(); if (state.mode === "ai" && !state.review?.running) renderRunBar(); } }),
        seg([["all", "All"], ["todo", "To decide"], ["decided", "Decided"], ["closed", "Closed"]], f.status, (v) => { f.status = v; renderAll(); }),
        seg([["all", "All"], ["picks", `Today's picks · ${capacityN()}`], ["suspicious", "Suspicious"], ["review", "Needs review"], ["likely_fp", "Likely false positive"]],
          f.picks ? "picks" : (f.lane || "all"), (v) => { f.picks = v === "picks"; f.lane = ["suspicious", "review", "likely_fp"].includes(v) ? v : null; renderAll(); }),
        sel("Care type", "care", cares), sel("State", "state", states)),
      el("div", { class: "tools-right" },
        (f.lane || f.care || f.state || f.status !== "all" || f.search || f.picks) ? el("button", { class: "linkish", onclick: () => { state.filters = EMPTY_FILTERS(); renderAll(); } }, "Clear") : null,
        el("span", { id: "count", class: "eyebrow" }, countText(shown))));
  }
  const countText = (n = visible().length) => `${n} of ${state.rows.length} cases`;

  function kpi(label, value, sub, o = {}) {
    return el("div", { class: "kpi" + (o.click ? " click" : "") + (o.on ? " on" : ""), onclick: o.click },
      el("div", { class: "l" }, o.color ? el("span", { class: "sw", style: `background:${o.color}` }) : null, label),
      el("div", { class: "v" }, String(value)), el("div", { class: "s" }, sub));
  }

  /* ------------------------------------------------------------------ charts */
  function renderCharts() {
    const root = $("#charts"); root.innerHTML = "";
    root.append(chartCapacity(), chartHistogram(), chartCare());
  }
  const W = 320, H = 150;
  const chartBox = (num, title, sub, ...kids) => el("div", { class: "chart" }, el("h3", {}, el("span", {}, `Fig. ${num} · `, el("b", {}, title)), sub ? el("span", {}, sub) : null), ...kids);

  function chartCapacity() {
    const q = openQueue(), n = capacityN();
    const CW = 420, CH = 170, L = 40, R = 14, T = 14, B = 28;
    const maxN = Math.max(1, Math.min(q.length, Math.max(n + 4, q.filter((r) => r.dollars_at_risk >= 1).length + 2)));
    const pts = [{ k: 0, share: 0 }];
    for (let k = 1; k <= maxN; k++) pts.push({ k, share: coverage(k).share });
    const x = (k) => L + (k / maxN) * (CW - L - R), y = (v) => T + (1 - v) * (CH - T - B);
    const svg = svgEl("svg", { viewBox: `0 0 ${CW} ${CH}`, class: "capsvg", role: "img", "aria-label": `Share of open dollars at risk covered by the first N reviews; ${n} planned covers ${Math.round(coverage(n).share * 100)}%` });
    for (const v of [0, 0.5, 1]) { svg.append(svgEl("line", { x1: L, x2: CW - R, y1: y(v), y2: y(v), class: v ? "grid" : "axis" })); svg.append(svgEl("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end" }, `${v * 100}%`)); }
    const step = maxN > 20 ? 5 : maxN > 10 ? 2 : 1;
    for (let k = 0; k <= maxN; k += step) svg.append(svgEl("text", { x: x(k), y: CH - 10, "text-anchor": "middle" }, String(k)));
    const line = pts.map((p) => `${x(p.k).toFixed(1)},${y(p.share).toFixed(1)}`).join(" ");
    const inside = pts.filter((p) => p.k <= n);
    svg.append(svgEl("polygon", { class: "cap-area", points: `${x(0)},${y(0)} ${inside.map((p) => `${x(p.k).toFixed(1)},${y(p.share).toFixed(1)}`).join(" ")} ${x(n)},${y(0)}` }));
    svg.append(svgEl("polyline", { class: "cap-line", points: line }));
    const cov = coverage(n);
    svg.append(svgEl("line", { class: "cap-mark", x1: x(n), x2: x(n), y1: y(0), y2: y(cov.share) }));
    svg.append(svgEl("circle", { class: "cap-dot", cx: x(n), cy: y(cov.share), r: 5 }));
    svg.append(svgEl("text", { class: "cap-label", x: L + 10, y: T + 14 }, `${n} review${n === 1 ? "" : "s"} → ${Math.round(cov.share * 100)}% · ${kmoney(cov.covered)}`));
    // invisible hit columns: click or drag along the curve to set today's capacity
    for (let k = 1; k <= maxN; k++) svg.append(svgEl("rect", { class: "cap-hit", x: x(k - 0.5), y: T, width: (CW - L - R) / maxN, height: CH - T - B, "data-tip": `${k} review${k === 1 ? "" : "s"}: ${Math.round(coverage(k).share * 100)}% of open dollars at risk (${kmoney(coverage(k).covered)})`, onclick: () => setCapacity(k) }));
    return chartBox(1, "What a day of reviews covers", "click to set capacity", svg,
      el("div", { class: "legend wrapok" }, el("span", {}, "Reviews in order of dollars at risk → share of open dollars at risk covered")));
  }

  function chartHistogram() {
    const rows = state.rows.filter((r) => matches(r, "bin"));
    const bins = Array.from({ length: 10 }, (_, i) => ({ i, suspicious: 0, review: 0, likely_fp: 0 }));
    for (const r of rows) bins[Math.min(9, Math.floor(r.risk_score / 10))][r.lane]++;
    const max = Math.max(1, ...bins.map((b) => b.suspicious + b.review + b.likely_fp));
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}` });
    const left = 8, bottom = 18, top = 16, bw = (W - left - 8) / 10;
    const y = (v) => top + (H - top - bottom) * (1 - v / max);
    svg.append(svgEl("line", { class: "axis", x1: left, x2: W - 8, y1: H - bottom, y2: H - bottom }));
    bins.forEach((b) => {
      let acc = 0;
      const x = left + b.i * bw + 3, w = bw - 6, total = b.suspicious + b.review + b.likely_fp;
      for (const l of [...LANES].reverse()) {
        if (!b[l]) continue;
        const y1 = y(acc + b[l]), y0 = y(acc);
        svg.append(svgEl("rect", { class: `bar hit lane-${l}` + (F().lane && F().lane !== l ? " dim" : ""), x, y: y1, width: w, height: Math.max(0, y0 - y1 - 1),
          "data-tip": `Risk ${b.i * 10}–${b.i === 9 ? 100 : b.i * 10 + 9}: ${total} cases (${b[l]} ${LANE_LABEL[l].toLowerCase()})`, onclick: () => setFilter("lane", l) }));
        acc += b[l];
      }
      if (total) svg.append(svgEl("text", { class: "val", x: x + w / 2, y: y(total) - 3, "text-anchor": "middle" }, String(total)));
      svg.append(svgEl("text", { x: x + w / 2, y: H - 5, "text-anchor": "middle" }, b.i === 9 ? "90+" : String(b.i * 10)));
    });
    const legend = el("div", { class: "legend" }, LANES.map((l) => el("span", {}, el("span", { class: "sw", style: `background:${LANE_VAR[l]}` }), LANE_LABEL[l])));
    return chartBox(2, "Risk score", "ratings split at 30 and 60", svg, legend);
  }

  function hbars(items, { key, tipFn, selected, stacked = false, fmt = String }) {
    const n = items.length, rowH = Math.min(22, (H - 4) / Math.max(1, n)), labelW = 104;
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${Math.max(H, n * rowH + 4)}` });
    const max = Math.max(1, ...items.map((i) => i.value));
    const x0 = labelW + 4, xw = W - x0 - 40;
    items.forEach((it, idx) => {
      const y = 2 + idx * rowH, h = rowH - 5;
      const dim = selected && selected !== it.id;
      svg.append(svgEl("text", { x: labelW, y: y + h / 2 + 4, "text-anchor": "end", "data-tip": it.label }, it.label.length > 18 ? it.label.slice(0, 17) + "…" : it.label));
      if (stacked) {
        let acc = 0;
        for (const l of LANES) {
          const v = it.parts[l] || 0; if (!v) continue;
          svg.append(svgEl("rect", { class: `bar hit lane-${l}` + (dim ? " dim" : ""), x: x0 + (acc / max) * xw, y, width: Math.max(1, (v / max) * xw - 1), height: h, "data-tip": tipFn(it, l, v), onclick: () => setFilter(key, it.id) }));
          acc += v;
        }
      } else {
        svg.append(svgEl("rect", { class: "bar hit" + (dim ? " dim" : ""), x: x0, y, width: Math.max(1, (it.value / max) * xw), height: h, "data-tip": tipFn(it), onclick: () => setFilter(key, it.id) }));
      }
      svg.append(svgEl("text", { class: "val", x: x0 + (it.value / max) * xw + 4, y: y + h / 2 + 4 }, fmt(it.value)));
    });
    return svg;
  }

  function chartCare() {
    const rows = state.rows.filter((r) => matches(r, "care"));
    const agg = {};
    for (const r of rows) { agg[r.care_type] ??= { id: r.care_type, label: r.care_type, value: 0, n: 0 }; agg[r.care_type].value += r.dollars_at_risk; agg[r.care_type].n++; }
    const items = Object.values(agg).sort((a, b) => b.value - a.value);
    return chartBox(3, "Dollars at risk", "by care type", hbars(items, { key: "care", selected: F().care, fmt: kmoney, tipFn: (it) => `${it.label}: ${money(it.value)} at risk across ${it.n} cases` }));
  }

  /* ------------------------------------------------------------------ table */
  const COLS = () => [
    ["pick", "#", "num narrow"], ["case_id", "Case"], ["care_type", "Care type"], ["state", "State", "opt"], ["claim_amount_usd", "Amount", "num"],
    ["risk_score", "Risk", "num"], ["lane", "Risk rating"], ["drivers", "Why", "whycol"],
    ...(state.mode === "ai" ? [["quality_verdict", "AI review"]] : []), ["decision", "Decision · status"],
  ];
  function sortRows(rows) {
    const { key, dir } = state.sort;
    const laneRank = { suspicious: 0, review: 1, likely_fp: 2 };
    return rows.slice().sort((a, b) => {
      let va = a[key], vb = b[key];
      if (key === "lane") { va = laneRank[a.lane]; vb = laneRank[b.lane]; }
      if (key === "pick") { const p = pickIndex(); va = p.get(a.case_id) ?? 1e6; vb = p.get(b.case_id) ?? 1e6; return (va - vb) * dir || b.dollars_at_risk - a.dollars_at_risk; }
      if (key === "drivers") { va = a.drivers.join(", "); vb = b.drivers.join(", "); }
      if (key === "decision") { const r = { pending: 0, needs_evidence: 1, accept: 2, reject: 3 }; va = r[a.decision] * 10 + (a.closed ? 5 : 0); vb = r[b.decision] * 10 + (b.closed ? 5 : 0); }
      if (key === "confidence") { const r = { High: 2, Medium: 1, Low: 0 }; va = r[va]; vb = r[vb]; }
      if (key === "quality_verdict") { va = va || ""; vb = vb || ""; }
      if (typeof va === "string") return va.localeCompare(vb) * dir || b.risk_score - a.risk_score;
      return (va - vb) * dir || b.dollars_at_risk - a.dollars_at_risk;
    });
  }
  function aiCell(r) {
    if (r.assessment_status === "running") return el("span", { class: "st busy pulse" }, "writing…");
    if (r.assessment_status === "failed") return el("span", { class: "mini crit link", "data-tip": "The model call failed; open the case for the error. Click to retry.", onclick: (e) => { e.stopPropagation(); runReview([r.case_id]); } }, "failed · retry");
    if (r.assessment_status === "not_recorded") return el("span", { class: "st faint", "data-tip": "Not in the recorded run, or its evidence changed after the recording" }, "not run");
    if (r.assessment_status !== "ready") return el("span", { class: "mini link", "data-tip": "Send this case to the model now", onclick: (e) => { e.stopPropagation(); runReview([r.case_id]); } }, "review");
    const v = r.quality_verdict, src = r.source;
    if (src === "template") return el("span", { class: "st faint", "data-tip": "No model has reviewed this case; the rules template is shown" }, "not run");
    if (src === "template_unavailable") return el("span", { class: "mini crit link", "data-tip": "The model was unavailable. Click to retry.", onclick: (e) => { e.stopPropagation(); runReview([r.case_id]); } }, "unavailable · retry");
    return el("span", { class: "st" + (v === "pass" ? " done" : ""), style: v === "pass" ? "" : "color:var(--accent-text)" }, v === "pass" ? "pass" : src === "template_fallback" ? "fallback" : "flagged");
  }
  function decisionCell(r) {
    if (r.decision === "pending" && r.status === "new") return el("span", { class: "st faint" }, "to decide");
    const d = { pending: "Pending", accept: "Accepted", reject: "Rejected", needs_evidence: "Needs evidence" }[r.decision];
    return el("span", { class: "st" + (r.closed ? " done" : ""), "data-tip": `${r.decision_label} · ${r.status_label}` + (r.final_lane && r.decision === "reject" ? ` · moved to ${LANE_LABEL[r.final_lane]}` : "") },
      el("b", {}, d), ` · ${r.status_label}`);
  }
  function renderTable() {
    const rows = visible(), cols = COLS(), picks = pickIndex();
    const thead = $("#cases thead"); thead.innerHTML = "";
    thead.append(el("tr", {}, cols.map(([k, l, cls]) => el("th", { class: (cls || "") + (state.sort.key === k ? " on" : ""), scope: "col", "aria-sort": state.sort.key === k ? (state.sort.dir > 0 ? "ascending" : "descending") : null,
      onclick: () => { state.sort = { key: k, dir: state.sort.key === k ? -state.sort.dir : (["case_id", "care_type", "state", "pick", "drivers", "decision"].includes(k) ? 1 : -1) }; renderTable(); } }, l, state.sort.key === k ? (state.sort.dir > 0 ? " ↑" : " ↓") : ""))));
    const tbody = $("#cases tbody"); tbody.innerHTML = "";
    if (!rows.length) tbody.append(el("tr", {}, el("td", { colspan: cols.length, class: "empty" }, "No cases match these filters. ", el("button", { class: "linkish", onclick: () => { state.filters = EMPTY_FILTERS(); renderAll(); } }, "Clear filters"))));
    for (const r of rows.slice(0, 500)) {
      const tags = [];
      if (r.guardrails.includes("linked_to_suspicious")) tags.push(el("span", { class: "mini crit", "data-tip": "Shares a claim number with a suspicious case" }, "linked"));
      else if (r.linked.length) tags.push(el("span", { class: "mini" }, "linked"));
      if (r.qa_sample) tags.push(el("span", { class: "mini warn", "data-tip": "Random QA sample: full review before closing" }, "QA"));
      if (r.guardrails.includes("anomaly_disagrees")) tags.push(el("span", { class: "mini warn", "data-tip": "Statistical outlier check disagrees with the rules" }, "outlier"));
      const p = picks.get(r.case_id);
      tbody.append(el("tr", { class: "row" + (r.case_id === state.selected ? " sel" : "") + (p ? " picked" : "") + (r.closed ? " closed" : ""), tabindex: "0",
        onclick: () => openCase(r.case_id), onkeydown: (e) => { if (e.key === "Enter") openCase(r.case_id); } },
        el("td", { class: "num narrow" }, p ? el("span", { class: "pickno", "data-tip": `Today's pick ${p} of ${picks.size}` }, String(p)) : ""),
        el("td", {}, el("b", {}, r.case_id), ...tags),
        el("td", {}, r.care_type), el("td", { class: "opt" }, r.state),
        el("td", { class: "num" }, money(r.claim_amount_usd)),
        el("td", { class: "num" }, el("span", { class: "score" }, el("i", {}, el("b", { style: `width:${r.risk_score}%;background:${LANE_VAR[r.lane]}` })), String(r.risk_score))),
        el("td", {}, el("span", { class: "dot", style: `background:${LANE_VAR[r.lane]}` }), LANE_LABEL[r.lane]),
        whyCell(r),
        state.mode === "ai" ? el("td", {}, aiCell(r)) : null,
        el("td", {}, decisionCell(r)),
      ));
    }
    if (rows.length > 500) tbody.append(el("tr", {}, el("td", { colspan: cols.length, class: "muted" }, `Showing 500 of ${rows.length}. Narrow the filters to see the rest.`)));
  }

  /* ------------------------------------------------------------------ score drivers */
  const MATRIX_LABEL = { ...SIGNAL_LABEL, risk_score: "Risk score", claim_amount_usd: "Claim amount" };
  const pct = (x) => x == null ? "–" : `${Math.round(x * 100)}%`;
  const fix = (x, d = 2) => x == null ? "–" : (Math.abs(x) < 0.5 * 10 ** -d ? (0).toFixed(d) : x.toFixed(d));

  async function loadDrivers() {
    const root = $("#drivers");
    if (!state.analysis) root.innerHTML = '<div class="drv-loading pulse">Computing score drivers…</div>';
    try { state.analysis = await api(`/api/analysis?${M()}`); } catch (e) { root.innerHTML = `<div class="drv-loading">Could not compute: ${esc(e.message)}</div>`; return; }
    if (state.page === "drivers") renderDrivers();
  }

  function renderDrivers() {
    const a = state.analysis, root = $("#drivers"); root.innerHTML = "";
    const n = a.cases, sigs = a.signals;

    root.append(el("div", { class: "drv-head" },
      eyebrow(`Score drivers · ${a.dataset.name} · ${n} cases`),
      el("h2", {}, "How the columns drive the risk score"),
      el("p", { class: "muted" }, "Each signal is re-scored away one at a time with the engine, so the figures show what the score and the key indicators actually depend on in this file. Everything is recomputed when you upload a new case sheet.")));


    root.append(driversTable(a));
    root.append(el("div", { class: "drv-grid" }, heatmap(a), el("div", { class: "drv-col" }, amountScatter(a), contextTable(a))));
    if (a.crosslogic) root.append(crossLogic(a));
    root.append(el("p", { class: "drv-foot" },
      "Method. Points added when flagged: the case's score minus its score with that one signal set to normal, averaged over the cases where the signal was flagged. Ratings that change without it: cases whose risk rating (split at 30 and 60) would differ without the signal. Not explained by the other nine: the share of the signal's rank variation left after a linear fit on the other nine signals. Correlations are Spearman rank correlations across cases. ",
      el("b", {}, "Reading it: "), "in this synthetic file the ten signals rise and fall together, so each one on its own changes few decisions. In real claims these schemes separate (a duplicate bill and an 84-mile commute need different proof), so all ten stay in the evidence pack and in the key indicators."));
  }

  function whyCell(r) {
    const d = r.driver_detail || [];
    if (!d.length) return el("td", { class: "whycol", "data-tip": "No evidence family is above its expected range" }, el("span", { class: "faint" }, "none"));
    const tip = "Share of the risk score by evidence family (adds up to 100%):\n" + d.map((x) => `${x.short}: ${Math.round(x.share * 100)}% · ${x.points} of ${r.risk_score} points · ${x.evidence_ids.join(", ")}`).join("\n");
    const top = d.slice(0, 2), more = d.length - top.length;
    return el("td", { class: "whycol", "data-tip": tip },
      el("div", { class: "why-main" }, top.map((x, i) => [i ? " · " : "", x.short, " ", el("b", { class: "mono" }, `${Math.round(x.share * 100)}%`)])),
      el("div", { class: "why-sub" }, top.map((x) => x.evidence_ids.join(", ")).join("  ·  ") + (more ? `  ·  +${more} more` : "")));
  }

  function barCell(value, max, text, tipText, cls = "") {
    const w = value == null || max <= 0 ? 0 : Math.max(0, Math.min(1, value / max)) * 100;
    return el("td", { class: "bar-td", "data-tip": tipText }, el("span", { class: "bar-wrap" }, el("span", { class: "bar-val" }, text), el("span", { class: "bar-track" }, el("i", { class: cls, style: `width:${w}%` }))));
  }

  function driversTable(a) {
    const n = a.cases, ki = a.key_indicators;
    const maxPts = Math.max(1, ...a.signals.map((x) => x.avg_points_when_fired ?? 0));
    const fams = a.families.slice().sort((x, y) => (y.avg_points_flagged ?? 0) - (x.avg_points_flagged ?? 0));
    const modeLabel = a.mode === "ai" ? "AI review" : "Rules review";
    const thead = el("thead", {}, el("tr", {},
      el("th", {}, "Signal · flagged when"), el("th", {}, "Flagged in"),
      el("th", { "data-tip": "Average drop in a case's score if this signal alone were normal, over the cases where it was flagged" }, "Points added when flagged"),
      el("th", { "data-tip": "Cases whose risk rating would change without this signal" }, "Ratings that change without it"),
      el("th", { "data-tip": "Share of this signal's variation that the other nine signals do not already explain" }, "Not explained by the other nine"),
      el("th", { "data-tip": `How many ${modeLabel} assessments cite this signal as a risk key indicator` }, `Key indicator in ${modeLabel}`)));
    const tbody = el("tbody");
    for (const f of fams) {
      tbody.append(el("tr", { class: "fam-row" }, el("td", { colspan: 6 },
        el("b", {}, FAMILY_LABEL[f.key] || f.label),
        el("span", { class: "eyebrow" }, ` · weight ${f.weight.toFixed(2)} · active in ${f.active} of ${n} cases · adds ${fix(f.avg_points_flagged, 1)} points on average to flagged cases`))));
      const rows = a.signals.filter((x) => x.family === f.key).sort((x, y) => (y.avg_points_when_fired ?? 0) - (x.avg_points_when_fired ?? 0));
      for (const x of rows) {
        const label = SIGNAL_LABEL[x.key] || x.label, kc = ki.counts[x.key] || 0;
        tbody.append(el("tr", {},
          el("td", { "data-tip": x.label }, label, el("div", { class: "cut" }, x.cutoff)),
          el("td", { class: "mono" }, `${x.fired} / ${x.known}`),
          barCell(x.avg_points_when_fired, maxPts, x.avg_points_when_fired == null ? "never flagged" : `+${fix(x.avg_points_when_fired, 1)}`, `Largest single drop: ${fix(x.max_points, 1)} points`, "ink"),
          el("td", { class: "mono" + (x.lane_changes ? " strong" : " faint"), "data-tip": x.lane_change_cases.length ? `Would change: ${x.lane_change_cases.join(", ")}` : "No rating changes" }, String(x.lane_changes)),
          barCell(x.unique_share, 1, pct(x.unique_share), `${pct(x.unique_share)} of ${label} is not predicted by the other nine signals`, "grey"),
          ki.assessments ? barCell(kc, ki.assessments, `${kc} of ${ki.assessments}`, `${label} is a key indicator in ${kc} of ${ki.assessments} ${modeLabel} assessments`, "ink")
            : el("td", { class: "mono faint" }, "not run")));
      }
    }
    return el("figure", { class: "drv-fig" },
      el("figcaption", {}, el("span", {}, "Fig. 4 · ", el("b", {}, "What each signal does to the risk score")), el("span", {}, "grouped by evidence family · largest effect first")),
      el("div", { class: "drv-scroll" }, el("table", { class: "drv-table" }, thead, tbody)));
  }

  function mix(c1, c2, t) {
    const h = (c) => [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16));
    const a = h(c1), b = h(c2);
    return "#" + a.map((v, i) => Math.round(v + (b[i] - v) * t).toString(16).padStart(2, "0")).join("");
  }
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  function corrColor(r) {
    const zero = cssVar("--heat-zero"), mid = cssVar("--heat-mid"), hi = cssVar("--heat-hi"), neg = cssVar("--heat-neg");
    if (r == null) return cssVar("--surface");
    if (r >= 0) return r < 0.5 ? mix(zero, mid, r / 0.5) : mix(mid, hi, (r - 0.5) / 0.5);
    return mix(zero, neg, Math.min(1, -r));
  }

  function luminance(hex) {
    const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((v) => v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  }

  function heatmap(a) {
    const cols = a.matrix.columns, V = a.matrix.values, k = cols.length;
    const cell = 38, left = 118, top = 10, bottom = 92;
    const W = left + (k - 1) * cell + 8, H = top + (k - 1) * cell + bottom;
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "heat", role: "img", "aria-label": "Correlation matrix of the ten signals" });
    for (let i = 1; i < k; i++) {
      const y = top + (i - 1) * cell;
      const strong = cols[i] === "risk_score";
      svg.append(svgEl("text", { x: left - 8, y: y + cell / 2 + 4, "text-anchor": "end", class: strong ? "lbl strong" : "lbl" }, MATRIX_LABEL[cols[i]]));
      for (let j = 0; j < i; j++) {
        const r = V[i][j], x = left + j * cell;
        const fill = corrColor(r), dark = luminance(fill) < 0.22;
        svg.append(svgEl("rect", { x: x + 1, y: y + 1, width: cell - 2, height: cell - 2, fill, "data-tip": `${MATRIX_LABEL[cols[i]]} × ${MATRIX_LABEL[cols[j]]}: r = ${fix(r)}` }));
        svg.append(svgEl("text", { x: x + cell / 2, y: y + cell / 2 + 4, "text-anchor": "middle", class: "cellv", fill: dark ? "#ffffff" : "#17171a", "pointer-events": "none" }, r == null ? "–" : fix(r)));
      }
    }
    for (let j = 0; j < k - 1; j++) {
      const x = left + j * cell + cell / 2, y = top + (k - 1) * cell + 8;
      svg.append(svgEl("text", { x, y, transform: `rotate(-45 ${x} ${y})`, "text-anchor": "end", class: cols[j] === "risk_score" ? "lbl strong" : "lbl" }, MATRIX_LABEL[cols[j]]));
    }
    // the score row and column are the ones that matter for key indicators: outline them
    const si = cols.indexOf("risk_score");
    if (si > 0) svg.append(svgEl("rect", { x: left, y: top + (si - 1) * cell, width: si * cell, height: cell, fill: "none", stroke: cssVar("--ink"), "stroke-width": 1.5 }));
    const legend = el("div", { class: "heat-legend" },
      el("span", { class: "mono" }, "−1"), el("span", { class: "grad" }), el("span", { class: "mono" }, "+1"),
      el("span", { class: "faint" }, "Spearman r · blue = move in opposite directions, gold = move together"));
    const rng = a.signal_corr_range;
    return el("figure", { class: "drv-fig" },
      el("figcaption", {}, el("span", {}, "Fig. 5 · ", el("b", {}, "How the ten signals move together")), el("span", {}, rng ? `signal pairs r ${fix(rng[0])} to ${fix(rng[1])}` : "")),
      svg, legend);
  }

  function amountScatter(a) {
    const pts = a.scatter.filter((p) => p.amount > 0);
    const W = 520, H = 250, L = 44, R = 12, T = 12, B = 34;
    const lo = Math.log10(Math.min(...pts.map((p) => p.amount))), hi = Math.log10(Math.max(...pts.map((p) => p.amount)));
    const x = (v) => L + ((Math.log10(v) - lo) / Math.max(1e-9, hi - lo)) * (W - L - R);
    const y = (v) => T + (1 - v / 100) * (H - T - B);
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "scat", role: "img", "aria-label": "Claim amount against risk score, one dot per case" });
    for (const v of [0, 30, 60, 100]) {
      svg.append(svgEl("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), class: v === 30 || v === 60 ? "rule" : "grid" }));
      svg.append(svgEl("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end" }, String(v)));
    }
    svg.append(svgEl("text", { x: W - R, y: y(60) - 4, "text-anchor": "end", class: "note" }, "suspicious ≥ 60"));
    svg.append(svgEl("text", { x: W - R, y: y(30) - 4, "text-anchor": "end", class: "note" }, "needs review ≥ 30"));
    for (const t of [1e3, 3e3, 1e4, 3e4, 1e5, 3e5]) {
      if (Math.log10(t) < lo - 0.01 || Math.log10(t) > hi + 0.01) continue;
      svg.append(svgEl("line", { x1: x(t), x2: x(t), y1: H - B, y2: H - B + 4, class: "grid" }));
      svg.append(svgEl("text", { x: x(t), y: H - B + 16, "text-anchor": "middle" }, kmoney(t)));
    }
    svg.append(svgEl("text", { x: (L + W - R) / 2, y: H - 2, "text-anchor": "middle", class: "note" }, "Claim amount (log scale)"));
    const order = { likely_fp: 0, review: 1, suspicious: 2 };
    for (const p of pts.slice().sort((m, q) => order[m.lane] - order[q.lane]))
      svg.append(svgEl("circle", { cx: x(p.amount), cy: y(p.risk), r: 5, class: `dot lane-${p.lane}`, "data-tip": `${p.case_id} · ${money(p.amount)} · score ${p.risk} · ${LANE_LABEL[p.lane]}`, onclick: () => openCase(p.case_id) }));
    const legend = el("div", { class: "legend" }, LANES.map((l) => el("span", {}, el("span", { class: "sw", style: `background:${LANE_VAR[l]}` }), LANE_LABEL[l])));
    return el("figure", { class: "drv-fig" },
      el("figcaption", {}, el("span", {}, "Fig. 6 · ", el("b", {}, "Claim amount against the risk score")), el("span", {}, "click a dot to open the case")),
      svg, legend);
  }

  function crossLogic(a) {
    const groups = [["together", "Should rise together"], ["tension", "Should cut against each other"], ["support", "Can support each other"], ["independent", "No logical link"]];
    const rows = a.crosslogic;
    const count = (rel, ok) => rows.filter((x) => x.relation === rel && (x.verdict === "Matches the logic.") === ok).length;
    const total = (rel) => rows.filter((x) => x.relation === rel).length;
    const all = rows.filter((x) => x.r != null).map((x) => x.r);
    const lo = Math.min(...all), hi = Math.max(...all);
    const off = total("support") + total("independent") - count("support", true) - count("independent", true);
    const summary = el("p", { class: "cl-sum" },
      `Fig. 5 shows what the data does: every pair of signals moves together here (r ${fix(lo)} to ${fix(hi)}). This figure shows which of those pairs have a business reason to. `,
      el("b", {}, `${count("together", true)} of ${total("together")}`), " pairs that should rise together do. ",
      el("b", {}, `${off} of ${total("support") + total("independent")}`), " pairs with a weak or no business link move together just as strongly, which points to one hidden driver behind every column in this synthetic file. ",
      el("b", {}, `${total("tension") - count("tension", true)} of ${total("tension")}`), " pairs that should cut against each other in legitimate claims also rise together, so here a suspicious case tends to carry the contradictory combination. Both figures are right: Fig. 5 is the observation, this one is the expectation it is checked against.");
    const body = el("tbody", {});
    for (const [rel, label] of groups) {
      const g = rows.filter((x) => x.relation === rel);
      if (!g.length) continue;
      body.append(el("tr", { class: "cl-group" }, el("td", { colspan: 6 }, el("b", {}, label), el("span", { class: "faint" }, ` · expected ${g[0].expect} · ${g.length} pairs`))));
      for (const x of g.slice().sort((p, q) => (q.r ?? 0) - (p.r ?? 0)))
        body.append(el("tr", {},
          el("td", { class: "cl-pair" }, el("span", { class: "mono" }, `${x.a_id} × ${x.b_id}`), el("div", {}, `${SIGNAL_LABEL[x.a] || x.a_label} × ${SIGNAL_LABEL[x.b] || x.b_label}`)),
          el("td", {}, x.why),
          el("td", {}, x.both),
          el("td", {}, x.one),
          el("td", { class: "mono" }, x.r == null ? "–" : fix(x.r), x.both_fired != null ? el("div", { class: "faint" }, `${x.both_fired} cases with both`) : null),
          el("td", { class: x.verdict === "Matches the logic." ? "" : "strong" }, x.verdict)));
    }
    return el("figure", { class: "drv-fig" },
      el("figcaption", {}, el("span", {}, "Fig. 8 · ", el("b", {}, "Which of the Fig. 5 correlations make business sense")), el("span", {}, "expected relation for every pair against the observed Spearman r")),
      summary,
      el("div", { class: "drv-scroll" }, el("table", { class: "drv-table cl" },
        el("thead", {}, el("tr", {}, el("th", {}, "Pair"), el("th", {}, "Why they should or should not move together"), el("th", {}, "Both high in one case means"), el("th", {}, "Only one high means"), el("th", {}, "Observed r"), el("th", {}, "Data vs logic"))),
        body)));
  }

  function contextTable(a) {
    const read = (c) => {
      const v = c.value;
      if (c.column === "claim_amount_usd") return v == null ? "–" : `${Math.abs(v) >= 0.5 ? "Tracks the score closely" : Math.abs(v) >= 0.3 ? "Moves with the score" : "Little relation"} (r = ${fix(v)}); median ${money(c.detail.likely_fp ?? 0)} / ${money(c.detail.review ?? 0)} / ${money(c.detail.suspicious ?? 0)} by risk rating. Kept out on purpose so the score follows patterns, not claim size.`;
      if (c.column === "care_type") return v == null ? "Too few cases per type to test." : `${v < 0.05 ? "Risk differs by care type" : "No clear difference by care type"} (p = ${fix(v)}).`;
      if (c.column === "state") return v == null ? "Too few cases per state to test." : `${v < 0.05 ? "Risk differs by state" : "No relation to risk"} (p = ${fix(v)}, ${c.detail.states} states).`;
      if (c.column === "claim_date") return v == null ? "–" : `${Math.abs(v) >= 0.3 ? "Risk changes over time" : "No trend over time"} (r = ${fix(v)}, ${c.detail.from} to ${c.detail.to}).`;
      if (c.column === "claim_number") return v ? `${v} number${v === 1 ? "" : "s"} used on more than one case; that link is evidence (E14+).` : "Every number is unique.";
      return "";
    };
    return el("figure", { class: "drv-fig" },
      el("figcaption", {}, el("span", {}, "Fig. 7 · ", el("b", {}, "Columns that are not signals")), el("span", {}, "tested against the risk score")),
      el("table", { class: "drv-table ctx" },
        el("thead", {}, el("tr", {}, el("th", {}, "Column"), el("th", {}, "Used in the score"), el("th", {}, "What the data shows"))),
        el("tbody", {}, a.context.map((c) => el("tr", {}, el("td", { class: "mono" }, c.column), el("td", {}, c.in_score), el("td", {}, read(c)))))));
  }

  /* ------------------------------------------------------------------ drawer (Option A) */
  function closeDrawer() { if (!state.selected) return; state.selected = null; $("#drawer").hidden = true; $("#scrim").hidden = true; clearTimeout(state.poll); renderTable(); refresh(); }
  async function openCase(cid) {
    state.selected = cid; state.rejectOpen = false;
    $("#drawer").hidden = false; $("#scrim").hidden = false;
    renderTable();
    await loadCase(cid);
  }
  async function loadCase(cid) {
    clearTimeout(state.poll);
    let v;
    try { v = await api(`/api/cases/${cid}?${M()}`); } catch (e) { toast(e.message); return; }
    if (cid !== state.selected) return;
    state.view = v; renderDrawer();
    if (v.assessment_status === "running") state.poll = setTimeout(() => loadCase(cid), 2500);
  }

  function renderDrawer() {
    const v = state.view, c = v.case, a = v.assessment, st = v.state, f = c.facts;
    const d = $("#drawer"); const prev = $(".d-scroll", d); const scrollTop = prev ? prev.scrollTop : 0; d.innerHTML = "";
    const body = el("div", { class: "d-scroll" });
    const list = visible().map((r) => r.case_id), i = list.indexOf(c.case_id);
    const conf = a ? a.confidence : c.confidence;

    /* header: identity, score, decision */
    const facts = el("div", { class: "facts" });
    const push = (...k) => { if (facts.childNodes.length) facts.append(" · "); facts.append(...k); };
    if (st.closed) push(el("b", {}, st.status_label));
    push(el("span", { "data-tip": conf.reasons.map((r) => (r.positive ? "+ " : "− ") + r.text).join("\n") }, `confidence ${conf.level.toLowerCase()}`));
    push(el("span", { "data-tip": c.anomaly.text }, `outlier ${ordinal(c.anomaly.percentile)} pct ${c.anomaly.agrees ? "✓" : "✗"}`));
    push(el("span", { "data-tip": "Claim amount × risk score" }, `${money(c.dollars_at_risk)} at risk`));
    for (const l of c.linked) push("linked to ", el("a", { href: "#", onclick: (e) => { e.preventDefault(); openCase(l.case_id); } }, l.case_id));
    if (c.base_lane !== c.lane) push(el("span", { "data-tip": "A safety rule raised this case above the rating its score alone gives" }, "raised by rule"));
    const head = el("div", { class: "d-head" },
      el("div", { class: "d-top" }, eyebrow(`${f.case_id} · ${LANE_LABEL[c.lane]} · ${f.care_type} · ${f.state} · ${f.claim_date} · claim ${f.claim_number} · ${money(f.claim_amount_usd)}`),
        el("div", { class: "d-nav" },
          el("button", { class: "btn", "aria-label": "Previous case", disabled: i <= 0 ? "" : null, onclick: () => openCase(list[i - 1]) }, "‹"),
          el("button", { class: "btn", "aria-label": "Next case", disabled: i >= list.length - 1 ? "" : null, onclick: () => openCase(list[i + 1]) }, "›"),
          el("button", { class: "btn wide", onclick: () => window.open(`/api/cases/${f.case_id}/export.md?${M()}`, "_blank"), "data-tip": "Export the case file (Markdown)" }, "Export"),
          el("button", { class: "btn", "aria-label": "Close", onclick: closeDrawer }, "×"))),
      el("div", { class: "d-score" }, el("div", { class: "n", style: `color:${LANE_VAR[c.lane]}` }, String(c.risk_score)),
        el("div", { class: "t" }, el("div", { class: "lane" }, `${LANE_LABEL[c.lane]} · ${LANE_ACTION[c.lane]}`), facts)));
    head.append(decisionBlock(c, st));
    for (const g of c.guardrails) head.append(el("div", { class: "flag " + (g.code === "linked_to_suspicious" ? "err" : g.code === "qa_sample" ? "info" : "") }, g.text));
    body.append(head);

    /* why */
    const why = el("div", { class: "d-sec" });
    if (!a) {
      const running = v.assessment_status === "running";
      if (state.meta.replay) {
        why.append(el("div", { class: "sec-head" }, el("span", { class: "label" }, `Why it scored ${c.risk_score}`), eyebrow("Recorded AI review · not run")),
          el("div", { class: "why-note" }, "This case is not in the recorded run, or its evidence changed after the recording. Add an API key to review it with a model."),
          el("div", {}, el("button", { class: "btn small quiet", onclick: () => setMode("offline") }, "See the rules review")));
      } else why.append(el("div", { class: "sec-head" }, el("span", { class: "label" }, `Why it scored ${c.risk_score}`), eyebrow(running ? "AI review · writing…" : "AI review · not run")),
        el("div", { class: "why-note" + (running ? " pulse" : "") }, running ? "The writer is drafting the assessment and the judge will review it." : "This case has not been sent to the model yet."),
        running ? null : el("div", {}, el("button", { class: "btn small", onclick: () => runReview([c.case_id]) }, "Review this case"), " ", el("button", { class: "btn small quiet", onclick: () => setMode("offline") }, "See the rules review")));
    } else {
      const n = a.narrative, q = a.quality;
      const srcLabel = { llm: `${state.meta.replay ? "Recorded AI review" : "AI review"} · ${q.judge_status === "ok" ? `judge ${Math.round(Object.values(q.scores).filter((s) => s != null).reduce((x, y) => x + y, 0) / Math.max(1, Object.values(q.scores).filter((s) => s != null).length))}/5` : "rules " + q.rules.verdict}${a.revisions ? ` · ${a.revisions} revision` : ""}`, template: "Rules review · template", template_fallback: "Rules review · model draft failed review", template_unavailable: "Rules review · model unavailable" }[a.source] || a.source;
      why.append(el("div", { class: "sec-head" }, el("span", { class: "label" }, `Why it scored ${c.risk_score}`), eyebrow(srcLabel, q.verdict !== "pass" ? el("span", { style: "color:var(--accent-text)" }, ` · quality ${VERDICT_LABEL[q.verdict]}`) : null)));
      if (n.summary) why.append(el("p", { class: "summary", html: cites(n.summary) }));
      for (const fl of a.flags) why.append(el("div", { class: "flag " + (fl.level === "error" ? "err" : "") }, fl.text));
      if (a.source === "template_unavailable") why.append(el("div", {}, el("button", { class: "btn small", onclick: () => runReview([c.case_id]) }, "Retry with the model")));
      const risk = (n.key_indicators || []).filter((k) => k.direction !== "mitigating").slice(0, 4);
      const mit = (n.key_indicators || []).filter((k) => k.direction === "mitigating");
      const fbButtons = (id) => { const fb = st.feedback[id]?.verdict; return el("span", { class: "fb" },
        el("button", { class: fb === "agree" ? "a" : "", "aria-label": "Agree", "data-tip": "Agree with this indicator", onclick: () => feedback(id, fb === "agree" ? "clear" : "agree") }, "✓"),
        el("button", { class: fb === "disagree" ? "d" : "", "aria-label": "Disagree", "data-tip": "Disagree with this indicator", onclick: () => feedback(id, fb === "disagree" ? "clear" : "disagree") }, "✗")); };
      const clean = c.families.filter((fm) => fm.level === "normal").map((fm) => `No sign of ${fm.short.toLowerCase()} in this file.`);
      const counter = [...mit.map((k) => ({ html: cites(k.statement) + ` <span class="cite">[${k.evidence_id}]</span>` })),
        ...(n.innocent_explanations || []).slice(0, 3).map((t) => ({ text: t })), ...clean.slice(0, Math.max(0, 3 - mit.length)).map((t) => ({ text: t }))];
      why.append(el("div", { class: "why2" },
        el("div", { class: "col" }, el("div", { class: "col-h" }, "Points to risk"),
          risk.length ? el("ol", { class: "why" }, risk.map((ind) => el("li", {}, el("span", { html: cites(ind.statement) }), " ",
            el("span", { class: "tail" }, el("span", { class: "cite" }, `[${ind.evidence_id}]`), fbButtons(ind.evidence_id))))) : el("p", { class: "why-note" }, "No signal is above its expected range.")),
        el("div", { class: "col" }, el("div", { class: "col-h" }, "Could explain it"),
          counter.length ? el("ul", { class: "why counter" }, counter.map((x) => x.html ? el("li", { html: x.html }) : el("li", {}, x.text))) : el("p", { class: "why-note" }, "Nothing in the file points the other way."))));
      const gaps = c.data_gaps || [];
      if (gaps.length) why.append(el("details", { class: "gaps" }, el("summary", {}, el("span", { class: "col-h" }, "Can't tell from this file"), el("span", { class: "eyebrow" }, `${gaps.length} item${gaps.length === 1 ? "" : "s"}`)),
        el("ul", {}, gaps.map((g) => el("li", {}, g)))));
      const neg = conf.reasons.filter((r) => !r.positive).map((r) => r.text);
      if (neg.length) why.append(el("div", { class: "why-note" }, `${conf.level} confidence: ${neg.join("; ").replace(/\.$/, "")}.`));
      why.append(el("div", { class: "next", html: "<b>Next</b> · " + cites(n.recommended_action) }));
    }
    body.append(why);

    /* signals */
    const fired = c.signals.filter((s) => ["elevated", "high"].includes(s.level)).length;
    const sg = el("div", { class: "d-sec" },
      el("div", { class: "sec-head" }, el("span", { class: "label" }, `Signals · ${fired} of ${c.signals.length} flagged`),
        el("div", { class: "sig-legend" }, el("span", {}, el("i", { class: "sw", style: "background:var(--accent)" }), "strong"), el("span", {}, el("i", { class: "sw", style: "background:var(--ochre)" }), "moderate"), el("span", {}, el("i", { class: "sw", style: "background:var(--rule)" }), "clear"))),
      el("div", { class: "sig-grid" }, c.signals.map((s) => el("div", { class: `sig-cell ${s.level}`, "data-tip": `${s.text}\n${s.meaning}` + (s.percentile != null ? `\n${ordinal(s.percentile)} percentile of the queue.` : "") }, el("div", { class: "k" }, SIGNAL_LABEL[s.key] || s.label), el("div", { class: "v" }, s.level === "missing" ? "n/a" : s.display)))),
      el("div", { class: "chips" }, c.families.map((fm) => el("span", { class: "chip" + (fm.level === "elevated" || fm.level === "high" ? "" : " off"), "data-tip": fm.label }, fm.level === "missing" ? `${fm.short} n/a` : fm.level === "normal" ? `${fm.short} clear` : `${fm.short} −${Math.round(fm.drop_if_explained)}`)), el("span", { class: "faint" }, "points if the family were explained")));
    body.append(sg);

    /* collapsed rows */
    const rows = el("div", { class: "rows" });
    const row = (title, right, ...kids) => el("details", {}, el("summary", {}, el("span", {}, title), eyebrow(right)), el("div", { class: "body" }, ...kids));
    rows.append(row(`Evidence pack · E1–E${v.evidence.length}`, "show", el("div", { class: "ev" }, v.evidence.map((e) => [
      el("span", { class: "id" }, e.id),
      el("div", { class: "ev-item" + (e.reading === "red flag" ? " flag" : ""), "data-ev": e.id },
        el("div", {}, el("b", { style: "font-weight:600" }, e.label), e.reading ? el("span", { class: "ev-tag" }, e.reading) : null, ": ", e.text),
        e.source ? el("div", { class: "ev-why" }, el("span", { class: "ev-k" }, "Source"), e.source) : null,
        e.context ? el("div", { class: "ev-why ev-ctx" }, el("span", { class: "ev-k" }, "Read together"), e.context) : null,
        e.points_to ? el("div", { class: "ev-why" }, el("span", { class: "ev-k" }, "Points to"), e.points_to) : null,
        e.assumption ? el("div", { class: "ev-why" }, el("span", { class: "ev-k" }, "Assumption"), e.assumption) : null,
        e.counter_arguments?.length ? el("div", { class: "ev-why" }, el("span", { class: "ev-k" }, "Counter-arguments"), el("ul", {}, e.counter_arguments.map((t) => el("li", {}, t)))) : null,
        e.verify ? el("div", { class: "ev-why" }, el("span", { class: "ev-k" }, "Settled by"), e.verify) : null)]))));
    if (v.column_logic?.length) rows.append(row(`How the red flags combine · ${v.column_logic.length} pairs`, "show", el("div", { class: "ev" }, v.column_logic.map((p) => [
      el("span", { class: "id" }, p.evidence_ids.join("×")),
      el("div", { class: "ev-item" + (p.relation.includes("cut against") ? " flag" : "") },
        el("div", {}, el("b", { style: "font-weight:600" }, p.relation), ": ", p.both_high_means),
        el("div", { class: "ev-why" }, el("span", { class: "ev-k" }, "Why"), p.why))]))));
    if (a) {
      const n = a.narrative, q = a.quality;
      rows.append(row(`Verification steps · ${(n.next_steps || []).length}`, "show",
        el("ol", {}, (n.next_steps || []).map((s) => el("li", {}, s))),
        n.uncertainties?.length ? el("div", { class: "label", style: "font-size:11px;margin-top:4px" }, "Uncertain") : null, n.uncertainties?.length ? el("ul", {}, n.uncertainties.map((s) => el("li", {}, s))) : null));
      const qk = { groundedness: "grounded", completeness: "complete", calibration: "calibrated", actionability: "actionable", neutrality: "fair" };
      const judged = q.judge_status === "ok";
      const JUDGE_STATUS = { off: "not run", error: "unavailable", skipped: "not run" };
      const checkList = ["Every citation points to an item in the evidence pack", "Every figure matches the evidence pack", "The top driver is mentioned", "The wording fits the risk rating", "No accusatory or protected-class wording"];
      rows.append(row(`Quality review · automated checks ${VERDICT_LABEL[q.rules.verdict] || q.rules.verdict}` + (judged ? ` · AI judge ${VERDICT_LABEL[q.judge.verdict] || q.judge.verdict}` : ` · AI judge ${JUDGE_STATUS[q.judge_status] || q.judge_status}`), "show",
        judged
          ? el("div", { class: "qrow" }, Object.entries(q.scores).map(([k, s]) => el("div", {}, el("div", { class: "n" + (s != null && s <= 3 ? " low" : "") }, s ?? "–"), el("div", { class: "k" }, qk[k] || k))))
          : el("div", {}, el("ul", { class: "checks" }, checkList.map((t) => el("li", { class: q.issues.length ? "" : "ok" }, t))),
              el("div", { class: "muted", style: "font-size:12px" }, "No AI judge scored this narrative, so there are no 1-to-5 scores. Choose a judge model under Models to add them.")),
        q.issues.map((is) => el("div", { class: "issue " + is.severity }, el("b", {}, is.severity), is.problem, el("span", { class: "muted" }, ` (${is.source === "rules" ? "automated check" : "AI judge"})`))),
        a.rejected ? el("div", { class: "muted", style: "font-size:12px" }, `The first draft was sent back: ${a.rejected.quality?.issues?.[0]?.problem || "see issues"}.`) : null,
        judged ? el("div", { class: "muted", style: "font-size:12px" }, `Written by ${a.generator || state.meta.generator}; judged by ${a.judge}.`) : null,
        a.usage && a.usage.calls ? el("div", { class: "muted mono", style: "font-size:11.5px" },
          `Tokens · writer ${ktok(a.usage.writer.input)} in, ${ktok(a.usage.writer.output)} out · judge ${ktok(a.usage.judge.input)} in, ${ktok(a.usage.judge.output)} out · ${a.usage.calls} calls · ${usd(a.usage.cost_usd)}`) : null));
    }
    if (v.similar.length) rows.append(row(`Similar cases · ${v.similar.map((s) => s.case_id).join(", ")}`, "show",
      el("ul", {}, v.similar.map((s) => el("li", {}, el("a", { href: "#", onclick: (e) => { e.preventDefault(); openCase(s.case_id); } }, s.case_id), ` · ${s.text || s.reason || ""}`, el("span", { class: "muted" }, ` · ${s.status_label}`))))));
    body.append(rows);

    /* notes */
    const noteIn = el("input", { type: "text", placeholder: "Add a note to the case file…", "aria-label": "Note" });
    const notes = el("div", { class: "notes" }, el("span", { class: "label" }, `Notes & activity · ${st.timeline.length}`));
    if (st.timeline.length) notes.append(el("ul", { class: "tl" }, st.timeline.slice().reverse().slice(0, 4).map((e) => el("li", {}, eyebrow(`${new Date(e.ts).toLocaleString([], { dateStyle: "short", timeStyle: "short" })} · ${e.actor}`), e.summary))));
    notes.append(el("form", { onsubmit: async (e) => { e.preventDefault(); if (!noteIn.value.trim()) return; try { await api(`/api/cases/${c.case_id}/notes`, { method: "POST", body: { text: noteIn.value } }); loadCase(c.case_id); } catch (err) { toast(err.message); } } }, noteIn, el("button", { class: "btn" }, "Save")));
    body.append(notes);
    d.append(body);

    /* ask: docked panel with the model named */
    const aiMode = state.mode === "ai";
    const answeredBy = aiMode ? (state.meta.generator && state.meta.generator !== "off" ? state.meta.generator : "no model selected (rules fallback)") : "the rules engine · no AI model";
    const msgs = st.chat.filter((m) => (m.mode || "offline") === state.mode).slice(-8);
    const ask = el("div", { class: "ask" + (state.askOpen ? " open" : "") + (msgs.length ? " has-msgs" : "") });
    ask.append(el("div", { class: "a-head" },
      el("div", { class: "a-title" }, el("span", { class: "label" }, "Ask about this case"),
        el("span", { class: "model-pill", "data-tip": aiMode && state.meta.replay ? "Recorded answers to the starter questions (suspicious cases). Other questions are answered by the rules engine until you add an API key." : aiMode ? "Answers come from the writer model chosen under Models, grounded in this case's evidence pack" : "Rules review answers from the case data with fixed reasoning; switch to AI review for a language model" }, "Answered by ", el("b", {}, answeredBy))),
      el("button", { class: "a-toggle", onclick: () => { state.askOpen = !state.askOpen; renderDrawer(); } }, state.askOpen ? "Collapse ▾" : `Expand ▴${msgs.length ? ` · ${msgs.length / 2 | 0} asked` : ""}`)));
    if (state.askOpen) {
      const qa = el("div", { class: "qa" });
      for (const m of msgs) {
        if (m.role === "user") qa.append(el("div", { class: "q" }, m.content));
        else {
          const node = el("div", { class: "an", html: mdLite(m.content) });
          const badges = el("div", { class: "badges" });
          badges.append(el("span", {}, m.model && m.model !== "offline" ? `by ${m.model}` : "by the rules engine"));
          if (m.usage && m.usage.calls) badges.append(el("span", { "data-tip": usageText(m.usage, "") }, `${ktok(m.usage.input + m.usage.output)} tokens · ${usd(m.usage.cost_usd)}`));
          if (m.checks) badges.append(el("span", { class: m.checks.verdict === "pass" ? "" : "bad", "data-tip": (m.checks.issues || []).map((i) => i.problem).join(" ") || "Citations and figures verified" }, `checks ${VERDICT_LABEL[m.checks.verdict]}`));
          if (m.judge) badges.append(el("span", { class: m.judge.verdict === "pass" ? "" : "bad", "data-tip": m.judge.summary || "" }, `judge ${VERDICT_LABEL[m.judge.verdict]}`));
          else if (state.meta.judge_chat && aiMode) badges.append(el("button", { onclick: async (e) => { e.target.disabled = true; try { await api(`/api/cases/${c.case_id}/chat/${m.id}/judge`, { method: "POST" }); await loadCase(c.case_id); } catch (err) { toast(err.message); } } }, "Judge this answer"));
          node.append(badges); qa.append(node);
        }
      }
      if (!msgs.length) qa.append(el("div", { class: "a-empty" }, "Pick a question or type your own. Every answer cites the evidence pack."));
      ask.append(qa);
      ask.append(el("div", { class: "sugg" }, ASK_SUGGESTIONS.map((sg) => el("button", { type: "button", onclick: () => askQ(sg) }, sg))));
    }
    const input = el("input", { type: "text", id: "ask-input", "aria-label": "Question", placeholder: "Ask a follow-up about this case…", onfocus: () => { if (!state.askOpen) { state.askOpen = true; renderDrawer(); setTimeout(() => $("#ask-input")?.focus(), 0); } } });
    ask.append(el("form", { onsubmit: (e) => { e.preventDefault(); if (input.value.trim()) askQ(input.value.trim()); } }, input, el("button", { class: "btn primary" }, "Ask")));
    d.append(ask);
    $(".d-scroll", d).scrollTop = scrollTop;
    const qa = $(".qa", d); if (qa) qa.scrollTop = qa.scrollHeight;
  }

  function decisionBlock(c, st) {
    const box = el("div", { class: "decide" });
    const statusSel = el("select", { class: "status-sel", "aria-label": "Case status", onchange: (e) => setStatus(e.target.value) },
      Object.entries(state.meta.status_labels).map(([k, l]) => el("option", { value: k, selected: st.status === k ? "" : null,
        disabled: (["closed_benign", "closed_confirmed"].includes(k) && st.decision === "pending") ? "" : null }, l)));
    const statusWrap = el("label", { class: "status-wrap" }, el("span", { class: "eyebrow" }, "Status"), statusSel);
    if (st.decision === "pending" || st.decision === "needs_evidence") {
      box.append(el("div", { class: "decide-row" },
        el("span", { class: "eyebrow decide-q" }, st.decision === "needs_evidence" ? `Waiting on evidence · your call on the ${state.mode === "ai" ? "AI" : "triage"} finding` : `Your call on the ${state.mode === "ai" ? "AI" : "triage"} finding`),
        statusWrap));
      box.append(el("div", { class: "d-actions" },
        el("button", { class: "btn primary", "data-tip": `Agree: ${LANE_LABEL[c.lane].toLowerCase()} (key: a)`, onclick: () => decide("accept") }, "Accept"),
        el("button", { class: "btn" + (state.rejectOpen ? " on" : ""), "aria-expanded": String(state.rejectOpen), onclick: () => { state.rejectOpen = !state.rejectOpen; renderDrawer(); } }, "Reject…"),
        st.decision === "pending" ? el("button", { class: "btn quiet", onclick: () => decide("needs_evidence") }, "Needs more evidence") : null));
      if (state.rejectOpen) {
        const lane = el("select", { "aria-label": "Risk rating you would give it" }, LANES.filter((l) => l !== c.lane).map((l) => el("option", { value: l }, LANE_LABEL[l])));
        const reason = el("select", { "aria-label": "Reason" }, state.meta.override_reasons.map((r) => el("option", { value: r }, r)));
        const note = el("input", { type: "text", placeholder: "What did you find? (optional)", "aria-label": "Note" });
        box.append(el("div", { class: "ovr" }, lane, reason, note, el("div", { class: "row" },
          el("button", { class: "btn primary small", onclick: () => decide("reject", { lane: lane.value, reason_code: reason.value, note: note.value }) }, "Save rejection"),
          el("button", { class: "btn quiet small", onclick: () => { state.rejectOpen = false; renderDrawer(); } }, "Cancel"))));
      }
    } else {
      const last = st.decisions.filter((d) => d.type === "decision").slice(-1)[0];
      box.append(el("div", { class: "decide-row" },
        el("div", { class: "verdict " + st.decision }, el("b", {}, st.decision_label),
          st.decision === "reject" && st.final_lane ? el("span", {}, ` → ${LANE_LABEL[st.final_lane]} · ${st.reason_code}`) : null,
          last ? el("span", { class: "eyebrow" }, ` · ${last.actor}, ${new Date(last.ts).toLocaleString([], { dateStyle: "short", timeStyle: "short" })}`) : null),
        statusWrap));
      box.append(el("div", { class: "d-actions" }, el("button", { class: "btn quiet small", onclick: () => decide("reset") }, "Reset decision")));
    }
    return box;
  }

  function mdLite(text) {
    const lines = String(text || "").split("\n"); let html = "", inList = false;
    const inline = (s) => cites(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
    for (const raw of lines) {
      const li = raw.match(/^\s*[-*]\s+(.*)/);
      if (li) { if (!inList) { html += "<ul>"; inList = true; } html += `<li>${inline(li[1])}</li>`; }
      else { if (inList) { html += "</ul>"; inList = false; } if (raw.trim()) html += `<div>${inline(raw)}</div>`; }
    }
    return html + (inList ? "</ul>" : "");
  }

  /* ------------------------------------------------------------------ actions */
  async function decide(decision, extra = {}) {
    try { await api(`/api/cases/${state.selected}/decision`, { method: "POST", body: { decision, ...extra } }); toast({ accept: "Accepted", reject: "Rejection saved", needs_evidence: "Marked as needing evidence", reset: "Decision reset" }[decision] || "Saved"); state.rejectOpen = false; await loadCase(state.selected); await refresh(); }
    catch (e) { toast(e.message); }
  }
  async function setStatus(status) {
    try { await api(`/api/cases/${state.selected}/status`, { method: "POST", body: { status } }); toast(`Status: ${state.meta.status_labels[status]}`); await loadCase(state.selected); await refresh(); }
    catch (e) { toast(e.message); loadCase(state.selected); }
  }
  async function feedback(id, verdict) {
    try { await api(`/api/cases/${state.selected}/feedback`, { method: "POST", body: { evidence_id: id, verdict } }); await loadCase(state.selected); } catch (e) { toast(e.message); }
  }
  const ASK_SUGGESTIONS = [
    "Why is this case rated this way?",
    "Which innocent explanations should I rule out first?",
    "How do the red flags combine?",
    "What should I do next?",
    "What would change this assessment?",
    "What does the distance tell us?",
    "How does this compare with the rest of the queue?",
    "Are there linked or similar cases?",
    "How confident is the assessment?",
    "How much money is at stake?",
  ];
  async function askQ(q) {
    if (!state.askOpen) { state.askOpen = true; renderDrawer(); }
    const cid = state.selected;
    const qa = $("#drawer .qa"); if (qa) { qa.append(el("div", { class: "q" }, q), el("div", { class: "an muted pulse" }, "Thinking…")); qa.scrollTop = qa.scrollHeight; }
    try { await api(`/api/cases/${cid}/chat?${M()}`, { method: "POST", body: { question: q } }); await loadCase(cid); } catch (e) { toast(e.message); loadCase(cid); }
  }

  document.addEventListener("click", (e) => {
    const c = e.target.closest("#drawer .cite"); if (!c) return;
    const id = (c.textContent.match(/E\d+/) || [])[0]; if (!id) return;
    const item = document.querySelector(`#drawer .ev-item[data-ev="${id}"]`); if (!item) return;
    const det = item.closest("details"); if (det) det.open = true;
    item.scrollIntoView({ block: "center", behavior: "smooth" });
    item.classList.remove("hl"); void item.offsetWidth; item.classList.add("hl");
  });
  boot().catch((e) => { $("#brief").innerHTML = `<p class="brief-sub">Could not load the queue: ${esc(e.message)}</p>`; });
})();
