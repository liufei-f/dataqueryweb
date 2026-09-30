// Find QTL data page: two modes share one form.
//   search → GET /api/search (NDJSON stream, one line per inspected paper)
//   ref    → GET /api/inspect (one JSON paper)
(() => {
  const $ = (id) => document.getElementById(id);
  const form = $("dq-form"), input = $("dq-input"), submit = $("dq-submit");
  const status = $("dq-status"), statusText = $("dq-status-text"), bar = $("dq-progress-bar");
  const results = $("dq-results"), head = $("dq-results-head");
  const title = $("dq-results-title"), sub = $("dq-results-sub");
  const hideEmpty = $("dq-hide-empty"), hideLow = $("dq-hide-low");
  const journalSel = $("dq-journal");
  const aiBox = $("dq-ai"), autoBox = $("dq-autosave");
  let reviewFilter = "all";
  const CONTENT = {
    qtl_summary_statistics: "QTL summary statistics", qtl_results_browser: "QTL results browser",
    supplementary_table: "supplementary table", raw_or_individual_data: "raw / individual data", other: "other",
  };
  const STATUS = { open: "open download", controlled: "controlled access", on_request: "on request", not_found: "no download found" };
  // ── LLM settings: model, (masked) API key, and a notice when the key changes ──
  // The server re-reads .env on every use, so a swapped key applies without a restart;
  // this polls it so the page shows the change too.
  const KEY_STORE = "dq-llm-key";
  function storedKey() {
    try { return JSON.parse(localStorage.getItem(KEY_STORE) || "null"); } catch { return null; }
  }
  function rememberKey(k) {
    try { localStorage.setItem(KEY_STORE, JSON.stringify(k)); } catch { /* private mode */ }
  }
  async function checkKey() {
    const out = $("dq-key-status");
    out.className = "dq-key-status"; out.textContent = "checking…";
    try {
      const c = await (await fetch("/api/llm/check")).json();
      out.classList.add(c.ok ? "ok" : "bad");
      out.textContent = `${c.ok ? "✓" : "✗"} ${c.detail}`;
    } catch { out.classList.add("bad"); out.textContent = "✗ could not check"; }
  }
  async function refreshLLM() {
    let m;
    try { m = await (await fetch("/api/llm")).json(); } catch { return; }
    $("dq-ai-model").textContent = `${m.provider} · ${m.model}${m.fallback ? ` (fallback: ${m.fallback.provider} · ${m.fallback.model})` : ""}`;
    $("dq-ai-key").textContent = `key ${m.key}`;
    if (m.autosave) $("dq-autosave-note").textContent = `confidence ≥ ${Math.round(m.autosave.min_confidence * 100)}%${m.autosave.allow_fallback ? "" : ", main model only"}`;
    if (m.max_limit) { maxLimit = m.max_limit; limitBox.max = maxLimit; }
    const now = { id: m.key_id, key: m.key, provider: m.provider };
    const before = storedKey();
    if (before && (before.id !== now.id || before.provider !== now.provider)) {
      $("dq-key-old").textContent = `${before.provider} · ${before.key}`;
      $("dq-key-new").textContent = `${now.provider} · ${now.key}`;
      $("dq-key-old").parentElement.hidden = false;
      $("dq-key-notice").hidden = false;
      checkKey();
    }
    rememberKey(now);
  }
  document.addEventListener("click", (e) => {
    if (e.target.closest("#dq-key-dismiss")) $("dq-key-notice").hidden = true;
    if (e.target.closest("#dq-key-check")) { $("dq-key-notice").hidden = false; $("dq-key-old").parentElement.hidden = true; checkKey(); }
  });
  let mode = "search";
  let controller = null;
  let papers = [];
  let lastQuery = "";
  const stopBtn = $("dq-stop");
  const progress = { done: 0, total: 0 }; // papers finished / expected in the current search
  // Saved records, indexed two ways: by record key (row badges, Save buttons) and by
  // publication (the "already saved" banner on a paper card).
  let savedByKey = new Map(), savedByPaper = new Map();
  let savedById = new Map(); // other ways to recognise the same paper: DOI, PMID, title

  // Same identity rules as records.paper_key / url_key / record_key on the server.
  const paperKey = (p) => String(p.doi || p.pmid || p.title || p.publication_title || "").trim().toLowerCase();
  const urlKey = (u) => String(u || "").trim().toLowerCase().replace(/^[a-z]+:\/\//, "").replace(/^www\./, "").replace(/\/+$/, "");
  // A record is one paper; each of its saved locations is indexed as paper|url.
  const recordKey = (p, url) => `${paperKey(p)}|${urlKey(url)}`;
  const recordUrls = (r) => String(r.download_url || "").split("; ").filter(Boolean);

  // Same as records.title_key on the server.
  const titleKey = (t) => String(t || "").toLowerCase().replace(/<[^>]+>/g, "").replace(/[^a-z0-9]+/g, "");
  const push = (map, k, r) => { if (!k) return; if (!map.has(k)) map.set(k, []); map.get(k).push(r); };

  function indexSaved(rows) {
    savedByKey = new Map(); savedByPaper = new Map(); savedById = new Map();
    for (const r of rows) {
      recordUrls(r).forEach((u) => savedByKey.set(`${r.record_key}|${urlKey(u)}`, r));
      push(savedByPaper, r.record_key.split("|")[0], r);
      push(savedById, r.doi && `doi:${r.doi.toLowerCase()}`, r);
      push(savedById, r.pmid && `pmid:${r.pmid}`, r);
      const t = titleKey(r.publication_title);
      push(savedById, t.length > 20 && `title:${t}`, r);
    }
  }

  // Saved records of a paper, matched like records.find_paper: DOI (or a preprint's
  // published DOI), PMID or title.
  function savedFor(p) {
    const out = new Map();
    const add = (list) => (list || []).forEach((r) => out.set(r.record_id, r));
    add(savedByPaper.get(paperKey(p)));
    [p.doi, p.published_doi].filter(Boolean).forEach((d) => add(savedById.get(`doi:${d.toLowerCase()}`)));
    if (p.pmid) add(savedById.get(`pmid:${p.pmid}`));
    const t = titleKey(p.title);
    if (t.length > 20) add(savedById.get(`title:${t}`));
    return [...out.values()];
  }
  // The saved record for one row: same paper, same download location.
  const savedRow = (p, s) => savedByKey.get(recordKey(p, s.url))
    || savedFor(p).find((r) => recordUrls(r).some((u) => urlKey(u) === urlKey(s.url)));
  async function loadSaved() {
    try {
      indexSaved(await (await fetch("/api/records")).json());
      renderAll();
    } catch { /* the page still works without saved state */ }
  }
  loadSaved();

  // ── token usage ────────────────────────────────────────────────────────
  const fmtTok = (n) => (n = Number(n) || 0) >= 1e6 ? `${(n / 1e6).toFixed(2)}M`
    : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : String(n);
  async function loadUsage() {
    let u;
    try { u = await (await fetch("/api/usage?recent=0")).json(); } catch { return; }
    const box = $("dq-usage");
    if (!u) return;
    box.hidden = false;
    if (!u.checks) {
      $("dq-usage-line").textContent = "AI tokens used: 0 so far — counted from the next AI check";
      $("dq-usage-detail").innerHTML = "<div>Every paper the AI reads is recorded with the tokens it used; a paper read before is not sent again.</div>";
      return;
    }
    $("dq-usage-line").textContent = `AI tokens used: ${fmtTok(u.total_tokens)} in total · `
      + `${u.checks} paper check${u.checks > 1 ? "s" : ""} (${u.papers} distinct papers) · `
      + `average ${fmtTok(u.avg_tokens_per_check)} per paper`;
    $("dq-usage-detail").innerHTML = `
      <div>Prompt ${esc(fmtTok(u.prompt_tokens))} · completion ${esc(fmtTok(u.completion_tokens))} · ${u.calls} model calls${u.failed ? ` · ${u.failed} failed check${u.failed > 1 ? "s" : ""} (tokens counted, verdict not reused)` : ""}</div>
      ${(u.by_model || []).map((m) => `<div>${esc(m.model || "unknown model")}: ${esc(fmtTok(m.total))} over ${m.checks} check${m.checks > 1 ? "s" : ""}</div>`).join("")}`;
  }
  loadUsage();
  // Records may be saved or deleted in another tab (e.g. on Saved records).
  document.addEventListener("visibilitychange", () => { if (!document.hidden) loadSaved(); });

  const PLACEHOLDER = {
    search: "Keywords — e.g. single-cell eQTL immune cells",
    ref: "DOI, article URL, PMID or PMCID — e.g. 10.1038/s41588-021-00913-z",
  };

  function setMode(next) {
    mode = next;
    document.querySelectorAll(".dq-toggle-btn").forEach((b) => {
      const on = b.dataset.mode === mode;
      b.classList.toggle("active", on);
      b.setAttribute("aria-selected", on);
    });
    input.placeholder = PLACEHOLDER[mode];
    submit.textContent = mode === "search" ? "Search" : "Find data";
    $("dq-search-options").hidden = mode !== "search";
    if (mode !== "search") $("dq-limit-warn").hidden = true;
    $("dq-qtl-wrap").hidden = mode !== "search";
    document.querySelector(".dq-ex-search").hidden = mode !== "search";
    document.querySelector(".dq-ex-ref").hidden = mode !== "ref";
    const url = new URL(location.href);
    url.searchParams.set("mode", mode);
    history.replaceState(null, "", url);
  }

  document.querySelectorAll(".dq-toggle-btn").forEach((b) =>
    b.addEventListener("click", () => { setMode(b.dataset.mode); input.focus(); }));
  document.querySelectorAll(".dq-chip").forEach((c) =>
    c.addEventListener("click", () => { input.value = c.textContent.trim(); form.requestSubmit(); }));
  [hideEmpty, hideLow, journalSel].forEach((el) => el.addEventListener("change", renderAll));
  aiBox.addEventListener("change", () => { autoBox.disabled = !aiBox.checked; });
  document.querySelectorAll(".dq-review-btn").forEach((b) => b.addEventListener("click", () => {
    reviewFilter = b.dataset.f;
    renderAll();
  }));

  // Review status of a paper: saved by a person, auto-saved by the AI, judged "no new
  // data", or waiting for a person (new/unclear/unjudged and not saved).
  function category(p) {
    const recs = savedFor(p);
    if (p.ai && p.ai.skipped && !recs.length) return p.ai.saved_by.includes("human") ? "human" : "auto";
    if (recs.some((r) => r.saved_by !== "auto")) return "human";
    if (recs.length) return "auto";
    if (p.ai && !p.ai.error && p.ai.new_qtl_data === "no") return "no";
    return "check";
  }

  // Paste a DOI or URL into search mode → switch to the ref mode automatically.
  input.addEventListener("paste", () => setTimeout(() => {
    if (mode === "search" && /^(https?:\/\/|www\.|10\.\d{4,9}\/|PMC\d+$)/i.test(input.value.trim())) setMode("ref");
  }, 0));

  // ── helpers ────────────────────────────────────────────────────────────
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const safeUrl = (u) => /^(https?|ftp):\/\//i.test(u || "") ? u : "#";
  const size = (n) => {
    if (n == null) return "";
    const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(n < 10 && i ? 1 : 0)} ${u[i]}`;
  };
  const highlight = (text) => esc(text).replace(
    /(\b[a-z]{0,4}QTLs?\b|summary[ -]statistics?|data availability|accession)/gi, "<mark>$1</mark>");
  const KIND = { repository: "repository", accession: "accession", supplement: "supplementary file", file: "direct file", link: "link", web: "found by AI web search" };

  function setStatus(text, { busy = true, error = false, progress = null } = {}) {
    status.hidden = false;
    status.classList.toggle("idle", !busy);
    status.classList.toggle("error", error);
    statusText.textContent = text;
    if (progress != null) bar.style.width = `${Math.round(progress * 100)}%`;
  }

  function sourceRow(s, pi, si, p) {
    const saved = savedRow(p, s);
    const savedId = saved && saved.record_id;
    const files = s.files || [];
    const shown = files.slice(0, 8);
    const filesHtml = files.length ? `
      <ul class="dq-files">${shown.map((f) => `
        <li><a href="${esc(safeUrl(f.url))}" target="_blank" rel="noopener">${esc(f.name)}</a>
            <span class="size">${esc(size(f.size))}</span></li>`).join("")}
      </ul>${files.length > shown.length ? `<div class="muted dq-files-more">+ ${files.length - shown.length} more files in the record</div>` : ""}` : "";
    return `
      <tr class="${esc(s.relevance)}${s.ai_pick ? " ai" : ""}${saved ? (saved.saved_by === "auto" ? " is-auto" : " is-saved") : ""}">
        <td>
          ${saved ? (saved.saved_by === "auto"
            ? `<span class="dq-saved-tag dq-auto-tag" title="Record #${saved.record_id}, saved automatically by the AI">🤖 Auto-saved · ${esc(saved.saved_at.slice(0, 10))}</span>`
            : `<span class="dq-saved-tag" title="Record #${saved.record_id} in Saved records">✓ Already saved · ${esc(saved.saved_at.slice(0, 10))}</span>`) : ""}
          ${s.ai_pick ? `<span class="dq-ai-pick" title="${esc(s.ai_note)}">AI: ${esc(CONTENT[s.ai_pick] || s.ai_pick)}</span>` : ""}
          <a class="dq-src-link" href="${esc(safeUrl(s.url))}" target="_blank" rel="noopener">${esc(s.label)}</a>
          ${s.label !== s.url ? `<span class="dq-src-url">${esc(s.url)}</span>` : ""}
          ${filesHtml}
        </td>
        <td>${esc(s.repository)}<span class="dq-kind">${esc(KIND[s.kind] || s.kind)}${s.in_availability ? " · data availability" : ""}</span></td>
        <td><span class="badge badge-${esc(s.access)}">${esc(s.access)}</span></td>
        <td><span class="dq-rel dq-rel-${esc(s.relevance)}">${esc(s.relevance)}</span></td>
        <td class="dq-context">${s.ai_note ? `<div class="dq-ai-note">${esc(s.ai_note)}</div>` : ""}${highlight(s.context)}</td>
        <td class="dq-save-cell">
          ${saved && saved.saved_by === "auto" ? `
          <button type="button" class="dq-save dq-confirm" data-p="${pi}" data-s="${si}" title="The AI got this right: mark it as checked by you">Confirm</button>
          <button type="button" class="dq-save dq-remove" data-p="${pi}" data-s="${si}" title="The AI got this wrong: remove it from Saved records">Remove</button>` : `
          <button type="button" class="dq-save${savedId ? " saved" : ""}" data-p="${pi}" data-s="${si}"
                  title="${savedId ? "Saved to Saved records — click to remove" : "This row is correct: save it to Saved records"}">${savedId ? "Saved ✓" : "Save"}</button>`}
        </td>
      </tr>`;
  }

  // The rubric checklist behind the confidence (rubric.py): which criteria were met, with
  // their weights, and the LLM's three answers with the quotes that support them.
  const ANSWER_FOR = { mapped_qtls: "mapped_qtls", not_reuse_only: "reuse_only", results_released: "results_released" };
  const QUESTION = {
    mapped_qtls: "1. Did the authors themselves perform QTL association mapping?",
    reuse_only: "2. Does the paper only reuse QTL results published by others?",
    results_released: "3. Does the paper say where its own QTL results can be obtained?",
  };
  function criteriaHtml(ai) {
    if (!ai.criteria) return "";
    const chips = ai.criteria.map((c) => {
      const ans = ANSWER_FOR[c.id] && ai.answers && ai.answers[ANSWER_FOR[c.id]];
      const tip = ans ? `LLM answer: ${ans.answer}${ans.quote ? ` — “${ans.quote}”` : ""}` : "checked by the program";
      return `<span class="dq-crit${c.met ? " met" : ""}" title="${esc(tip)}">${c.met ? "✓" : "✗"} ${esc(c.label)} <b>${Math.round(c.weight * 100)}</b></span>`;
    }).join("");
    const why = ai.answers ? Object.entries(ai.answers).map(([k, v]) => `
        <li><span class="dq-ans dq-ans-${esc(v.answer)}">${esc(v.answer)}</span> ${esc(QUESTION[k] || k)}
          ${v.quote ? `<q>${esc(v.quote)}</q>` : ""}</li>`).join("") : "";
    return `<div class="dq-criteria">${chips}</div>${why ? `
        <details class="dq-why"><summary>Why this score</summary><ol>${why}</ol>
          <div class="muted">Score = sum of the weights of the criteria met (for "New QTL data"). See <a href="/about#rubric">How it works → Scoring rubric</a>.</div></details>` : ""}`;
  }

  function paperCard(p, pi) {
    const all = p.sources || [];
    const visible = hideLow.checked
      ? all.filter((s) => s.relevance !== "low" || s.ai_pick || savedByKey.has(recordKey(p, s.url))) : all;
    const nFiles = all.reduce((n, s) => n + (s.files || []).length, 0);
    const ids = [
      p.doi && `<a href="https://doi.org/${esc(p.doi)}" target="_blank" rel="noopener">doi:${esc(p.doi)}</a>`,
      p.pmid && `<a href="https://pubmed.ncbi.nlm.nih.gov/${esc(p.pmid)}/" target="_blank" rel="noopener">PMID ${esc(p.pmid)}</a>`,
      p.pmcid && `<a href="https://europepmc.org/article/PMC/${esc(p.pmcid)}" target="_blank" rel="noopener">${esc(p.pmcid)}</a>`,
      p.preprint_server && `<span class="badge badge-preprint">${esc(p.preprint_server)} preprint</span>`,
      p.published_doi && `<a href="https://doi.org/${esc(p.published_doi)}" target="_blank" rel="noopener">published: ${esc(p.published_journal || p.published_doi)}</a>`,
      p.open_access && `<span class="badge badge-open">open access</span>`,
      ...(p.qtl_types || []).slice(0, 8).map((t) => `<span class="badge badge-blue">${esc(t)}</span>`),
    ].filter(Boolean).join("");
    const strong = all.filter((s) => s.relevance === "high").length;
    const count = all.length
      ? `<span class="dq-count">${all.length} source${all.length > 1 ? "s" : ""}${strong ? ` · ${strong} high` : ""}${nFiles ? ` · ${nFiles} files` : ""}</span>`
      : `<span class="dq-count none">no data source found</span>`;
    const hiddenLow = all.length - visible.length;
    const table = visible.length ? `
      <div class="table-wrap"><table class="dq-sources">
        <colgroup><col style="width:28%"><col style="width:13%"><col style="width:9%"><col style="width:9%"><col style="width:32%"><col style="width:9%"></colgroup>
        <thead><tr><th>Download / data location</th><th>Repository</th><th>Access</th><th>Relevance</th><th>Where the paper says it</th><th></th></tr></thead>
        <tbody>${visible.map((s) => sourceRow(s, pi, all.indexOf(s), p)).join("")}</tbody>
      </table></div>` : "";
    const avail = (p.availability || []).length ? `
      <details class="dq-avail"><summary>Data availability statement</summary>
        ${p.availability.map((a) => `<blockquote>${highlight(a)}</blockquote>`).join("")}
      </details>` : "";
    const note = [p.note, hiddenLow ? `${hiddenLow} low-relevance link${hiddenLow > 1 ? "s" : ""} hidden.` : ""]
      .filter(Boolean).map((t) => `<div class="dq-note">${esc(t)}</div>`).join("");
    const meta = [p.authors && p.authors.split(",").slice(0, 3).join(",") + (p.authors.split(",").length > 3 ? " et al." : ""), p.journal, p.year]
      .filter(Boolean).map(esc).join(" · ");
    const ai = p.ai;
    const recheckRef = p.doi || p.pmcid || p.pmid || p.url;
    const verdict = !ai ? "" : ai.skipped ? `
      <div class="dq-verdict dq-verdict-skipped">
        <span class="dq-verdict-label">⏭ AI check skipped</span>
        <span class="dq-verdict-reason">Already in Saved records (${ai.saved_records.length} record${ai.saved_records.length > 1 ? "s" : ""},
          ${ai.saved_by.map((b) => (b === "auto" ? "auto-saved by the AI" : "saved by you")).join(" and ")}) — not sent to the AI again, to save tokens.</span>
        ${recheckRef ? `<button type="button" class="dq-recheck" data-p="${pi}" title="Run the AI check on this paper anyway (uses tokens)">Re-check with AI</button>` : ""}
      </div>` : ai.error ? `
      <div class="dq-verdict dq-verdict-error">AI check failed: ${esc(ai.error)}</div>` : `
      <div class="dq-verdict dq-verdict-${esc(ai.new_qtl_data)}">
        <span class="dq-verdict-label">${ai.new_qtl_data === "yes" ? "New QTL data" : ai.new_qtl_data === "no" ? "No new QTL data" : "Unclear"}</span>
        ${ai.new_qtl_data === "yes" ? `<span class="badge badge-${ai.download_status === "open" ? "open" : ai.download_status === "controlled" ? "controlled" : ai.download_status === "on_request" ? "request" : "unknown"}">${esc(STATUS[ai.download_status] || ai.download_status)}</span>` : ""}
        ${[ai.species, (ai.qtl_types || []).join(", "), ai.tissues_or_cells].filter(Boolean).map((t) => `<span class="dq-verdict-tag">${esc(t)}</span>`).join("")}
        <span class="dq-verdict-reason">${esc(ai.reason)}</span>
        ${criteriaHtml(ai)}
        ${ai.autosave ? `<span class="dq-autosave-line ${ai.autosave.saved.length ? "done" : ""}">${ai.autosave.saved.length
          ? `🤖 Auto-saved ${(n => `${n} location${n > 1 ? "s" : ""}`)(ai.autosave.locations || ai.autosave.saved.length)} to Saved records (${esc(ai.autosave.reason)})`
          : ai.new_qtl_data === "no" ? "" : `Needs human check: ${esc(ai.autosave.reason)}`}</span>` : ""}
        <span class="dq-verdict-model">${esc(ai.model)} · confidence ${Math.round((ai.confidence || 0) * 100)}%${ai.rubric ? ` (rubric ${esc(ai.rubric)})` : ""}
          · ${ai.cached ? `reused the check of ${esc(String(ai.cached.checked_at || "").slice(0, 10))} — no tokens now (it used ${esc(fmtTok(ai.cached.tokens_then))})`
            : `${esc(fmtTok((ai.usage || {}).total_tokens))} tokens`}
          ${recheckRef ? `<button type="button" class="dq-recheck" data-p="${pi}" title="Ask the AI again instead of reusing the earlier check (uses tokens)">Re-check with AI</button>` : ""}
          · ${p.full_text ? `read full text (${Math.round((p.text_chars || 0) / 1000)}k chars)` : "no full text"}</span>
        ${ai.web ? `
        <details class="dq-web">
          <summary>AI web search: ${ai.web.queries.length} searches, ${ai.web.pages.length} pages${ai.web.full_text_found ? ", full text found" : ", full text not reachable"}${ai.web.dropped.length ? `, ${ai.web.dropped.length} dead link${ai.web.dropped.length > 1 ? "s" : ""} removed` : ""}</summary>
          ${ai.web.queries.length ? `<div><strong>Searched:</strong> ${ai.web.queries.map(esc).join(" · ")}</div>` : ""}
          ${ai.web.pages.length ? `<div><strong>Opened:</strong><ul>${ai.web.pages.map((u) => `<li><a href="${esc(safeUrl(u))}" target="_blank" rel="noopener">${esc(u)}</a></li>`).join("")}</ul></div>` : ""}
          ${ai.web.dropped.length ? `<div><strong>Removed (URL does not exist):</strong> ${ai.web.dropped.map(esc).join(" · ")}</div>` : ""}
        </details>` : ""}
      </div>`;
    const recs = savedFor(p);
    const shownUrls = new Set(all.map((s) => urlKey(s.url)));
    const elsewhere = recs.flatMap((r) => recordUrls(r)).filter((u) => !shownUrls.has(urlKey(u)))
      .map((u) => ({ download_url: u }));
    const last = recs.map((r) => r.saved_at).sort().pop();
    const allAuto = recs.length && recs.every((r) => r.saved_by === "auto");
    const banner = recs.length ? `
      <div class="dq-saved-banner${allAuto ? " dq-auto-banner" : ""}">
        ${allAuto
          ? `<strong>🤖 Auto-saved by the AI</strong> — ${recs.length} record${recs.length > 1 ? "s" : ""}, saved ${esc((last || "").slice(0, 10))}. Spot-check and <em>Confirm</em> or <em>Remove</em> below.`
          : `<strong>✓ Already in Saved records</strong> — ${recs.length} record${recs.length > 1 ? "s" : ""} for this paper, last saved ${esc((last || "").slice(0, 10))}.`}
        <a href="/records?q=${encodeURIComponent(p.doi || p.pmid || p.title)}">View</a>
        ${elsewhere.length ? `<div class="dq-saved-other">Saved location${elsewhere.length > 1 ? "s" : ""} not in this result:
          ${elsewhere.map((r) => `<a href="${esc(safeUrl(r.download_url))}" target="_blank" rel="noopener">${esc(r.download_url)}</a>`).join(" · ")}</div>` : ""}
      </div>` : "";
    return `
      <article class="dq-paper${recs.length ? " has-saved" : ""}">
        <div class="dq-paper-head">
          <h3 class="dq-paper-title"><a href="${esc(safeUrl(p.url))}" target="_blank" rel="noopener">${esc(p.title || p.doi || p.url)}</a></h3>
          <div class="dq-paper-meta">${meta}</div>
          <div class="dq-paper-ids">${ids}${count}</div>
        </div>${banner}${verdict}
        <div class="dq-paper-body">${table}${note}${avail}
          ${(p.evidence || []).length ? `<div class="dq-evidence">Read: ${esc(p.evidence.join(" · "))}</div>` : ""}
        </div>
      </article>`;
  }

  // While a search streams, papers arrive one by one. Rebuilding every card for each
  // arrival is quadratic — a 1000-paper search rebuilt ~500,000 cards and froze the page —
  // so arrivals only schedule a redraw, at most one per RENDER_EVERY_MS.
  const RENDER_EVERY_MS = 1000;
  let renderTimer = null;
  function scheduleRender() {
    if (renderTimer) return;
    // A redraw of hundreds of cards is itself heavy: give big batches more room.
    const wait = papers.length > 300 ? 3 * RENDER_EVERY_MS : RENDER_EVERY_MS;
    renderTimer = setTimeout(() => { renderTimer = null; renderAll(); }, wait);
  }
  // Same for re-reading Saved records after auto-saves: one fetch per interval, not per paper.
  let savedTimer = null;
  function scheduleSavedReload() {
    if (savedTimer) return;
    savedTimer = setTimeout(() => { savedTimer = null; loadSaved(); }, RENDER_EVERY_MS);
  }

  function renderAll() {
    if (renderTimer) { clearTimeout(renderTimer); renderTimer = null; }
    DQJournals.update(journalSel, papers.filter(Boolean));
    const list = papers.map((p, pi) => p && Object.assign(p, { _pi: pi })).filter(Boolean)
      .filter((p) => !journalSel.value || DQJournals.key(p) === journalSel.value)
      .filter((p) => !hideEmpty.checked || (p.sources || []).length || savedFor(p).length)
      .filter((p) => reviewFilter === "all" || category(p) === reviewFilter);
    const hidden = papers.filter(Boolean).length - list.length;
    $("dq-filter-count").textContent = `${list.length} of ${papers.filter(Boolean).length} loaded papers`;
    const counts = { all: 0, check: 0, auto: 0, human: 0, no: 0 };
    papers.filter((p) => p && (!journalSel.value || DQJournals.key(p) === journalSel.value)).forEach((p) => { counts.all++; counts[category(p)]++; });
    document.querySelectorAll(".dq-review-btn").forEach((b) => {
      b.classList.toggle("active", b.dataset.f === reviewFilter);
      b.querySelector("span").textContent = counts[b.dataset.f];
    });
    $("dq-review").hidden = !counts.all;
    results.innerHTML = list.map((p) => paperCard(p, p._pi)).join("") + (hidden
      ? `<p class="muted dq-hidden-note">${hidden} paper${hidden > 1 ? "s" : ""} hidden by the filters above.</p>` : "");
  }

  // ── saving confirmed rows ─────────────────────────────────────────────
  function toRecord(p, s) {
    const ai = p.ai && !p.ai.error ? p.ai : null;
    return {
      publication_title: p.title, publication_url: p.url, doi: p.doi, pmid: p.pmid, pmcid: p.pmcid,
      authors: p.authors, year: p.year, journal: p.journal, preprint_server: p.preprint_server,
      qtl_type: (ai && ai.qtl_types.length ? ai.qtl_types : p.qtl_types || []).join("; "),
      qtl_context: ai ? ai.tissues_or_cells : "", species: ai ? ai.species : "",
      download_url: s.url, repository: s.repository, access_route: s.access,
      content: s.ai_pick || s.kind, file_urls: (s.files || []).map((f) => f.url).join("; "),
      extraction_note: [s.ai_note, s.context].filter(Boolean).join(" — "),
      ai_verdict: ai ? ai.new_qtl_data : "", ai_reason: ai ? ai.reason : "",
      evidence_source: (p.evidence || []).join("; "), search_terms: lastQuery,
    };
  }

  // Re-check an already-saved paper with the AI (the server skips it otherwise).
  results.addEventListener("click", async (e) => {
    const btn = e.target.closest(".dq-recheck");
    if (!btn) return;
    const pi = +btn.dataset.p, p = papers[pi];
    if (!p) return;
    btn.disabled = true;
    btn.textContent = "Re-checking… (up to ~2 min)";
    try {
      const params = new URLSearchParams({
        ref: p.doi || p.pmcid || p.pmid || p.url, ai: true, autosave: autoBox.checked, recheck: true,
      });
      const resp = await fetch(`/api/inspect?${params}`);
      const body = await resp.json();
      if (!resp.ok) throw new Error(body.detail || `HTTP ${resp.status}`);
      papers[pi] = body;
      await loadSaved();
      loadUsage();
      renderAll();
    } catch (err) {
      btn.disabled = false;
      btn.textContent = `Failed (${err.message}) — retry`;
    }
  });

  results.addEventListener("click", async (e) => {
    const btn = e.target.closest(".dq-save");
    if (!btn) return;
    const p = papers[+btn.dataset.p], s = p && p.sources[+btn.dataset.s];
    if (!s) return;
    const saved = savedRow(p, s);
    btn.disabled = true;
    try {
      if (btn.classList.contains("dq-confirm")) {
        // Re-saving as a person turns the auto record into a human one (notes are kept).
        const r = await fetch("/api/records", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(toRecord(p, s)),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
      } else if (saved) {
        if (!confirm(`Remove this location from Saved records?\n${s.url}`)) { btn.disabled = false; return; }
        const r = await fetch(`/api/records/${saved.record_id}?url=${encodeURIComponent(s.url)}`, { method: "DELETE" });
        if (!r.ok && r.status !== 404) throw new Error(`HTTP ${r.status}`);
      } else {
        const r = await fetch("/api/records", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(toRecord(p, s)),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
      }
      await loadSaved();
    } catch (err) {
      btn.disabled = false;
      btn.textContent = "Failed — retry";
    }
  });

  // ── requests ───────────────────────────────────────────────────────────
  const CHECKPOINT = "dq-search-checkpoint-v1";
  const resumeBtn = $("dq-resume"), nextBtn = $("dq-next");
  let continuing = null, checkpoint = null;
  try { checkpoint = JSON.parse(localStorage.getItem(CHECKPOINT) || "null"); } catch { /* unavailable */ }
  function saveCheckpoint() {
    try {
      if (checkpoint) localStorage.setItem(CHECKPOINT, JSON.stringify(checkpoint));
      else localStorage.removeItem(CHECKPOINT);
    } catch { /* private mode: in-memory resume still works */ }
    resumeBtn.hidden = !checkpoint;
    resumeBtn.disabled = !!controller && submit.disabled;
  }
  saveCheckpoint();
  resumeBtn.addEventListener("click", () => {
    if (!checkpoint || submit.disabled) return;
    continuing = structuredClone(checkpoint);
    input.value = checkpoint.q;
    setMode("search");
    form.requestSubmit();
  });

  async function runSearch(q, cont = null) {
    const options = cont ? cont.options : {
      q, limit: Math.min(50, maxLimit), qtl_only: $("dq-qtl-only").checked,
      source: $("dq-source").value, sort: $("dq-sort").value, ...yearRange(),
      ai: aiBox.checked, autosave: aiBox.checked && autoBox.checked,
    };
    checkpoint = cont || { q, options, cursor: "*", offset: 0, completed: 0, hits: 0 };
    saveCheckpoint();
    nextBtn.hidden = true;
    setStatus("Searching Europe PMC…", { progress: 0 });
    for (;;) {
      const current = checkpoint;
      const params = new URLSearchParams({ ...options, cursor: current.cursor, offset: current.offset });
      const resp = await fetch(`/api/search?${params}`, { signal: controller.signal });
      if (!resp.ok) throw new Error(`Search failed (HTTP ${resp.status})`);
      const reader = resp.body.getReader(), dec = new TextDecoder();
      let buf = "", nextCursor = "", finished = false;
      for (;;) {
        const { value, done: end } = await reader.read();
        if (end) break;
        buf += dec.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1);
          if (!line) continue;
          const msg = JSON.parse(line);
          if (msg.type === "error") throw new Error(msg.message);
          if (msg.type === "meta") {
            current.hits = msg.hits;
            nextCursor = msg.next_cursor || "";
            progress.total = msg.hits; progress.done = current.completed;
            head.hidden = false;
            title.textContent = `Results for “${q}”`;
            sub.textContent = `${msg.hits.toLocaleString()} matching papers · checking all results`;
            saveCheckpoint();
          } else if (msg.type === "paper") {
            papers.push(msg.paper);
            current.offset = msg.rank + 1;
            current.completed++;
            progress.done = current.completed;
            saveCheckpoint();
            if (msg.paper.ai?.autosave?.saved.length) scheduleSavedReload();
            scheduleRender();
            setStatus(`Processed ${current.completed.toLocaleString()} of ${current.hits.toLocaleString()} papers · previously checked papers reuse their verdict.`,
              { progress: current.hits ? current.completed / current.hits : 0 });
          } else if (msg.type === "paused") {
            current.offset = msg.offset;
            saveCheckpoint();
            renderAll(); loadUsage();
            setStatus(`Paused after ${current.completed.toLocaleString()} papers: ${msg.message}. Resume when tokens are available or the problem is resolved.`,
              { busy: false, progress: current.hits ? current.completed / current.hits : 0 });
            return;
          } else if (msg.type === "done") {
            finished = true;
          }
        }
      }
      if (!finished) throw new Error("Connection interrupted. Press Resume to continue.");
      await loadSaved(); loadUsage();
      if (!nextCursor) {
        const completed = current.completed;
        checkpoint = null; saveCheckpoint();
        setStatus(`Completed all ${completed.toLocaleString()} papers. AI checks are saved in AI-checked papers.`, { busy: false, progress: 1 });
        return;
      }
      current.cursor = nextCursor; current.offset = 0;
      saveCheckpoint();
    }
  }

  async function runRef(ref) {
    setStatus(aiBox.checked ? "Reading the paper and asking the AI… (up to ~2 min if it has to search the web for the full text)" : "Resolving the paper and reading its full text…", { progress: 0.3 });
    const resp = await fetch(`/api/inspect?${new URLSearchParams({ ref, ai: aiBox.checked, autosave: aiBox.checked && autoBox.checked })}`, { signal: controller.signal });
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(body.detail || `Lookup failed (HTTP ${resp.status})`);
    papers = [body];
    loadUsage();
    if (body.ai && body.ai.autosave && body.ai.autosave.saved.length) await loadSaved();
    head.hidden = false;
    title.textContent = "Data sources";
    sub.textContent = ref;
    renderAll();
    const n = body.sources.length;
    setStatus(n ? `Found ${n} data source${n > 1 ? "s" : ""}.` : "No data source found.", { busy: false, progress: 1 });
  }

  // ── search options: paper count and year range ────────────────────────
  let maxLimit = 1000;
  const limitBox = $("dq-limit"), yearFrom = $("dq-year-from"), yearTo = $("dq-year-to");
  const thisYear = new Date().getFullYear();
  function limitValue() {
    const n = Math.round(Number(limitBox.value));
    const v = Number.isFinite(n) && n >= 1 ? Math.min(n, maxLimit) : 10;
    limitBox.value = v;
    return v;
  }
  function yearRange() {
    const clean = (box) => {
      const y = Math.round(Number(box.value));
      if (!box.value || !Number.isFinite(y) || y < 1800 || y > 3000) { box.value = ""; return null; }
      return y;
    };
    let from = clean(yearFrom), to = clean(yearTo);
    if (from && to && from > to) { [from, to] = [to, from]; yearFrom.value = from; yearTo.value = to; }
    const out = {};
    if (from) out.year_from = from;
    if (to) out.year_to = to;
    return out;
  }
  function warnLimit() { $("dq-limit-warn").hidden = true; }
  document.querySelectorAll(".dq-year-chip").forEach((b) => b.addEventListener("click", () => {
    const span = +b.dataset.years;
    yearFrom.value = span ? thisYear - span + 1 : "";
    yearTo.value = span ? thisYear : "";
  }));
  refreshLLM();
  setInterval(() => { if (!document.hidden) refreshLLM(); }, 30000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshLLM(); });

  // Stop: abort the request. The server notices the closed connection and cancels the
  // papers still being read (so they stop using LLM quota and auto-saving).
  stopBtn.addEventListener("click", () => {
    if (!controller) return;
    controller.abort();
    stopBtn.hidden = true;
    submit.disabled = false;
    const shown = papers.filter(Boolean).length;
    setStatus(mode === "search" && progress.total
      ? `Stopped · ${progress.done} of ${progress.total} papers read — results so far are kept below.`
      : "Stopped.", { busy: false, progress: progress.total ? progress.done / progress.total : 0 });
    if (shown) renderAll();
    saveCheckpoint();
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const cont = continuing;
    continuing = null;
    const q = cont ? cont.q : input.value.trim();
    if (!q) { input.focus(); return; }
    const previousQuery = lastQuery;
    lastQuery = q;
    if (controller) controller.abort();
    controller = new AbortController();
    if (!cont || previousQuery !== q) { journalSel.value = ""; papers = []; results.innerHTML = ""; head.hidden = true; }
    submit.disabled = true;
    stopBtn.hidden = false;
    const url = new URL(location.href);
    url.searchParams.set("q", q);
    // Keep the search options in the URL, so a refresh or a shared link repeats the search.
    const opts = { limit: limitValue(), sort: $("dq-sort").value, source: $("dq-source").value, ...yearRange() };
    for (const k of ["limit", "sort", "source", "year_from", "year_to"]) {
      if (mode === "search" && opts[k] !== undefined) url.searchParams.set(k, opts[k]);
      else url.searchParams.delete(k);
    }
    history.replaceState(null, "", url);
    try {
      await (mode === "search" ? runSearch(q, cont) : runRef(q));
    } catch (err) {
      if (err.name !== "AbortError") setStatus(err.message, { busy: false, error: true });
    } finally {
      submit.disabled = false;
      stopBtn.hidden = true;
      saveCheckpoint();
    }
  });

  // Restore mode/query from the URL, so results can be shared as links.
  const params = new URLSearchParams(location.search);
  setMode(params.get("mode") === "ref" ? "ref" : "search");
  if (params.get("limit")) limitBox.value = params.get("limit");
  if (params.get("year_from")) yearFrom.value = params.get("year_from");
  if (params.get("year_to")) yearTo.value = params.get("year_to");
  if (["relevance", "newest"].includes(params.get("sort"))) $("dq-sort").value = params.get("sort");
  if (["all", "journal", "preprint"].includes(params.get("source"))) $("dq-source").value = params.get("source");
  warnLimit();
  // Restore the search but do not start it: a refresh used to re-run the whole search —
  // up to 1000 papers — every time the page was opened.
  if (params.get("q")) {
    input.value = params.get("q");
    setStatus("Press Search to run this search again (papers checked before reuse their AI verdict).",
      { busy: false });
  }
})();
