// AI-checked papers: every paper the LLM has read, its latest verdict and locations, with
// Save / Remove per location so a curator can file the correct ones into Saved records.
(() => {
  const list = document.getElementById("chk-list");
  const filter = document.getElementById("chk-filter");
  const verdictSel = document.getElementById("chk-verdict");
  const savedSel = document.getElementById("chk-saved");
  const journalSel = document.getElementById("chk-journal");
  const count = document.getElementById("chk-count");
  let checks = [];
  let records = [];

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const safeUrl = (u) => /^(https?|ftp):\/\//i.test(u || "") ? u : "#";
  const fmtTok = (n) => (n = Number(n) || 0) >= 1e6 ? `${(n / 1e6).toFixed(2)}M`
    : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : String(n);
  const CONTENT = {
    qtl_summary_statistics: "QTL summary statistics", qtl_results_browser: "QTL results browser",
    supplementary_table: "supplementary table", raw_or_individual_data: "raw / individual data", other: "other",
  };
  const STATUS = { open: "open download", controlled: "controlled access", on_request: "on request", not_found: "no download found" };
  const LABEL = { yes: "New QTL data", no: "No new QTL data", unsure: "Unclear" };

  // Same identity rules as records.paper_key / url_key / title_key on the server.
  const urlKey = (u) => String(u || "").trim().toLowerCase().replace(/^[a-z]+:\/\//, "").replace(/^www\./, "").replace(/\/+$/, "");
  const titleKey = (t) => String(t || "").toLowerCase().replace(/<[^>]+>/g, "").replace(/[^a-z0-9]+/g, "");
  const recordUrls = (r) => String(r.download_url || "").split("; ").filter(Boolean);

  // The saved record of a paper, matched like records.find_paper: DOI, PMID or title.
  function recordFor(p) {
    const dois = [p.doi, p.published_doi].filter(Boolean).map((d) => d.toLowerCase());
    const t = titleKey(p.title);
    return records.find((r) => (r.doi && dois.includes(r.doi.toLowerCase()))
      || (p.pmid && r.pmid === p.pmid) || (t.length > 20 && titleKey(r.publication_title) === t));
  }
  const isSaved = (rec, s) => rec && recordUrls(rec).some((u) => urlKey(u) === urlKey(s.url));

  function toRecord(c, s) {
    const p = c.paper, ai = c.ok ? c.verdict : null;
    return {
      publication_title: p.title, publication_url: p.url, doi: p.doi, pmid: p.pmid, pmcid: p.pmcid,
      authors: p.authors, year: p.year, journal: p.journal, preprint_server: p.preprint_server,
      qtl_type: (ai && (ai.qtl_types || []).length ? ai.qtl_types : p.qtl_types || []).join("; "),
      qtl_context: ai ? ai.tissues_or_cells : "", species: ai ? ai.species : "",
      download_url: s.url, repository: s.repository, access_route: s.access,
      content: s.ai_pick || s.kind, file_urls: (s.files || []).map((f) => f.url).join("; "),
      extraction_note: [s.ai_note, s.context].filter(Boolean).join(" — "),
      ai_verdict: ai ? ai.new_qtl_data : "",
      ai_confidence: ai && ai.confidence != null ? Number(ai.confidence).toFixed(2) : "",
      ai_reason: ai ? ai.reason : "",
      evidence_source: (p.evidence || []).join("; "), search_terms: c.search_terms || "",
    };
  }

  function sourceRow(c, ci, s, si, rec) {
    const saved = isSaved(rec, s);
    const files = s.files || [];
    return `
      <tr class="${s.ai_pick ? "ai" : ""}${saved ? (rec.saved_by === "auto" ? " is-auto" : " is-saved") : ""}">
        <td>
          ${s.ai_pick ? `<span class="dq-ai-pick" title="${esc(s.ai_note)}">AI: ${esc(CONTENT[s.ai_pick] || s.ai_pick)}</span>` : ""}
          <a class="dq-src-link" href="${esc(safeUrl(s.url))}" target="_blank" rel="noopener">${esc(s.label || s.url)}</a>
          ${s.label && s.label !== s.url ? `<span class="dq-src-url">${esc(s.url)}</span>` : ""}
          ${files.length ? `<div class="muted dq-files-more">${files.length} file${files.length > 1 ? "s" : ""}</div>` : ""}
        </td>
        <td>${esc(s.repository)}</td>
        <td><span class="badge badge-${esc(s.access)}">${esc(s.access || "unknown")}</span></td>
        <td class="dq-context">${s.ai_note ? `<div class="dq-ai-note">${esc(s.ai_note)}</div>` : ""}${esc(s.context || "")}</td>
        <td class="dq-save-cell">
          <button type="button" class="dq-save${saved ? " saved" : ""}" data-c="${ci}" data-s="${si}"
                  title="${saved ? "In Saved records — click to remove this location" : "This location is correct: save it to Saved records"}">${saved
                    ? (rec.saved_by === "auto" ? "🤖 Auto-saved ✓" : "Saved ✓") : "Save"}</button>
        </td>
      </tr>`;
  }

  function card(c, ci) {
    const p = c.paper, v = c.verdict || {}, rec = recordFor(p);
    const sources = (p.sources || []).slice().sort((a, b) => !a.ai_pick - !b.ai_pick);
    const verdict = c.ok ? `
      <div class="dq-verdict dq-verdict-${esc(v.new_qtl_data)}">
        <span class="dq-verdict-label">${esc(LABEL[v.new_qtl_data] || v.new_qtl_data || "Unclear")}</span>
        ${v.new_qtl_data === "yes" && v.download_status ? `<span class="badge badge-${v.download_status === "open" ? "open" : v.download_status === "controlled" ? "controlled" : v.download_status === "on_request" ? "request" : "unknown"}">${esc(STATUS[v.download_status] || v.download_status)}</span>` : ""}
        <span class="dq-verdict-reason">${esc(v.reason || "")}</span>
        ${[(v.qtl_types || []).join(", "), v.species, v.tissues_or_cells].filter(Boolean).length
          ? `<span class="dq-verdict-model">${esc([(v.qtl_types || []).join(", "), v.species, v.tissues_or_cells].filter(Boolean).join(" · "))}</span>` : ""}
        <span class="dq-verdict-model">${esc(c.model)} · confidence ${Math.round((v.confidence || 0) * 100)}% · ${esc(fmtTok(c.total_tokens))} tokens · checked ${esc(String(c.checked_at).slice(0, 10))}${c.search_terms ? ` · search “${esc(c.search_terms)}”` : ""}</span>
      </div>` : `<div class="dq-verdict dq-verdict-error">AI check failed: ${esc(v.error || "unknown error")} · ${esc(fmtTok(c.total_tokens))} tokens</div>`;
    const ids = [p.doi && `doi:${p.doi}`, p.pmid && `PMID ${p.pmid}`, p.pmcid, p.preprint_server && `${p.preprint_server} preprint`]
      .filter(Boolean).map(esc).join(" · ");
    const pubUrl = p.doi ? `https://doi.org/${p.doi}` : p.url;
    return `
      <article class="dq-paper${rec ? " has-saved" : ""}">
        <div class="dq-paper-head">
          <h3 class="dq-paper-title"><a href="${esc(safeUrl(pubUrl))}" target="_blank" rel="noopener">${esc(p.title || p.doi)}</a></h3>
          <div class="dq-paper-meta">${esc([p.authors && p.authors.split(",").slice(0, 3).join(","), p.journal, p.year].filter(Boolean).join(" · "))}</div>
          <div class="dq-paper-ids">${ids}${rec ? ` · <a href="/records?q=${encodeURIComponent(p.doi || p.pmid || p.title)}">in Saved records (#${rec.record_id})</a>` : ""}</div>
        </div>${verdict}
        ${sources.length ? `
        <div class="table-wrap"><table class="dq-sources">
          <colgroup><col style="width:34%"><col style="width:14%"><col style="width:9%"><col style="width:33%"><col style="width:10%"></colgroup>
          <thead><tr><th>Download / data location</th><th>Repository</th><th>Access</th><th>Where the paper says it</th><th></th></tr></thead>
          <tbody>${sources.map((s) => sourceRow(c, ci, s, (p.sources || []).indexOf(s), rec)).join("")}</tbody>
        </table></div>` : `<div class="dq-note">No data location was found for this paper.</div>`}
      </article>`;
  }

  function render() {
    DQJournals.update(journalSel, checks.map((c) => c.paper));
    const q = filter.value.trim().toLowerCase();
    const shown = checks.map((c, i) => [c, i]).filter(([c]) => {
      if (journalSel.value && DQJournals.key(c.paper) !== journalSel.value) return false;
      const v = c.ok ? (c.verdict.new_qtl_data || "unsure") : "error";
      if (verdictSel.value && v !== verdictSel.value) return false;
      const rec = recordFor(c.paper);
      if (savedSel.value === "saved" && !rec) return false;
      if (savedSel.value === "unsaved" && rec) return false;
      return !q || JSON.stringify([c.paper.title, c.paper.journal, c.paper.doi, c.paper.pmid, c.verdict, (c.paper.sources || []).map((s) => [s.url, s.repository])]).toLowerCase().includes(q);
    });
    const unsaved = checks.filter((c) => c.ok && c.verdict.new_qtl_data === "yes" && !recordFor(c.paper)).length;
    count.textContent = `${shown.length === checks.length ? checks.length : `${shown.length} of ${checks.length}`} paper${checks.length === 1 ? "" : "s"}`
      + (unsaved ? ` · ${unsaved} with new QTL data not saved yet` : "");
    list.innerHTML = shown.length ? shown.map(([c, i]) => card(c, i)).join("")
      : `<p class="muted">${checks.length ? "No paper matches the filter." : 'No paper has been checked by the AI yet. Run a search with <strong>AI check</strong> on <a href="/">Find QTL data</a>.'}</p>`;
  }

  async function load() {
    try {
      [checks, records] = await Promise.all([
        fetch("/api/checks").then((r) => r.json()),
        fetch("/api/records").then((r) => r.json()),
      ]);
    } catch {
      list.innerHTML = '<p class="muted">Could not load the checked papers.</p>';
      return;
    }
    render();
  }

  list.addEventListener("click", async (e) => {
    const btn = e.target.closest(".dq-save");
    if (!btn) return;
    const c = checks[+btn.dataset.c], s = c && (c.paper.sources || [])[+btn.dataset.s];
    if (!s) return;
    const rec = recordFor(c.paper);
    btn.disabled = true;
    try {
      let r;
      if (isSaved(rec, s)) {
        if (!confirm(`Remove this location from Saved records?\n${s.url}`)) { btn.disabled = false; return; }
        r = await fetch(`/api/records/${rec.record_id}?url=${encodeURIComponent(s.url)}`, { method: "DELETE" });
      } else {
        r = await fetch("/api/records", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(toRecord(c, s)),
        });
      }
      if (!r.ok && r.status !== 404) throw new Error(`HTTP ${r.status}`);
      records = await fetch("/api/records").then((x) => x.json());
      render();
    } catch {
      btn.disabled = false;
      btn.textContent = "Failed — retry";
    }
  });

  const params = new URLSearchParams(location.search);
  if (params.get("q")) filter.value = params.get("q");
  [filter, verdictSel, savedSel, journalSel].forEach((el) => el.addEventListener("input", render));
  load();
  document.addEventListener("visibilitychange", () => { if (!document.hidden) load(); });
})();
