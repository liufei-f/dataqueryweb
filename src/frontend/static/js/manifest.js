// Manifest rows page: review candidate qtl_manifest rows and the evidence behind each field.
(() => {
  const body = document.getElementById("mf-body");
  const filter = document.getElementById("mf-filter");
  const byStatus = document.getElementById("mf-status");
  const byReview = document.getElementById("mf-review");
  const count = document.getElementById("mf-count");
  let data = { rows: [], locations: [], records: {}, issues: {}, columns: [], role_columns: [] };
  const open = new Set();
  let busy = false; // a run is in progress: no second one can start

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const safeUrl = (u) => /^(https?|ftp):\/\//i.test(u || "") ? u : "#";
  const size = (n) => n == null ? "" : n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1e3))} kB`;
  const STATUS = { ready: "Ready", needs_review: "Needs review", needs_processing: "Needs processing", not_loadable: "Not loadable" };
  const EDITABLE = ["dataset", "source_project_id", "qtl_type", "biocontext", "level_1_context", "level_2_context",
    "population", "sample_size", "file_path"];
  const FACTS = [["dataset", "Dataset"], ["qtl_type", "QTL type"], ["holds", "What the file holds"], ["context", "Context"],
    ["sample_size", "Sample size"], ["population", "Ancestry"], ["genome_build", "Genome build"], ["file_path", "File path"]];

  const mark = (e) => e.verified === true ? '<span class="mf-ok" title="Found in the text the AI was given, or read from the file">✓</span>'
    : e.verified === false ? '<span class="mf-bad" title="No supporting text found: check by hand">✗ unverified</span>'
    : '<span class="muted" title="Set by a fixed rule">rule</span>';

  function evidenceTable(r) {
    const rows = FACTS.filter(([k]) => r.evidence[k]).map(([k, label]) => {
      const e = r.evidence[k];
      return `<tr><th>${label}</th><td>${esc(e.value)}</td><td>${mark(e)}</td><td>${esc(e.source)}</td>
        <td>${e.quote ? `<q>${esc(e.quote)}</q>` : ""}${e.note ? `<div class="rec-sub">${esc(e.note)}</div>` : ""}</td></tr>`;
    }).join("");
    return `<table class="mf-ev"><thead><tr><th>Field</th><th>Value</th><th></th><th>Source</th><th>Quote / note</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  function columnTable(r) {
    // Why a column is empty: the importer reads chrom/pos/ref/alt as their own columns, so a
    // packed variant id (1:2900856:T:C) has to be split before loading.
    const t = r.evidence.table || {}, vid = r.variant_id_col;
    const sample = vid && (t.columns || []).includes(vid) ? ((t.rows || [])[0] || [])[t.columns.indexOf(vid)] : "";
    const PACKED = { chrom_col: 0, position_col: 1, ref_col: 2, alt_col: 3 };
    const PART = ["chromosome", "position", "ref allele", "alt allele"];
    const why = (col) => {
      if (col in PACKED) {
        const bit = sample ? String(sample).split(/[:_]/)[PACKED[col]] : "";
        return vid ? `Not a column in the file. Required: split it out of “${vid}”${bit ? ` (${sample} → ${PART[PACKED[col]]} ${bit})` : ""} before loading.`
          : "Not in the file and no variant id to split it from. Required.";
      }
      if (col === "pval_col") return "Not in the file. Required.";
      if (col === "rsid_col") return "The file has no rsID column. Optional: the data still loads, but no variant → rsID mapping is built (every existing manifest row has one).";
      if (col === "median_tpm") return "Not in the file. Optional (empty in 171 of 466 existing manifest rows).";
      return "Not in the file. Optional.";
    };
    const rows = data.role_columns.map((col) => {
      const e = r.evidence[col];
      const note = r[col] ? (e ? esc(e.note) : "") : `<span class="${["chrom_col", "position_col", "ref_col", "alt_col", "pval_col"].includes(col) ? "mf-bad" : "muted"}">${esc(why(col))}</span>`;
      return `<tr><th>${esc(col)}</th><td><code>${esc(r[col]) || "—"}</code></td><td>${note}</td></tr>`;
    }).join("");
    const extra = [["delimiter", r.delimiter], ["gene_id_mode", r.gene_id_mode || "(empty)"]]
      .map(([k, v]) => `<tr><th>${k}</th><td><code>${esc(v)}</code></td><td></td></tr>`).join("");
    const missing = r.evidence.missing_columns ? `<p class="mf-bad">Not found in the header: ${esc(r.evidence.missing_columns.value)}</p>` : "";
    return `${missing}<table class="mf-ev"><thead><tr><th>Manifest column</th><th>Header in the file</th><th>Why</th></tr></thead><tbody>${rows}${extra}</tbody></table>`;
  }

  function preview(r) {
    const t = r.evidence.table || {};
    if (!(t.columns || []).length) return `<p class="muted">Header not read${t.note ? `: ${esc(t.note)}` : ""}.</p>`;
    const notes = [t.header_from && `header taken from ${t.header_from}`, t.has_header === false && "the file itself has no header row",
      t.fmt && t.fmt !== "text" && t.fmt, t.n_rows && `${t.n_rows.toLocaleString()} rows`, t.note].filter(Boolean).map(esc).join(" · ");
    return `${t.preamble ? `<p class="rec-sub">Title lines: ${esc(t.preamble)}</p>` : ""}${notes ? `<p class="rec-sub">${notes}</p>` : ""}
      <div class="table-wrap"><table class="mf-preview"><thead><tr>${t.columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>
      <tbody>${(t.rows || []).map((row) => `<tr>${row.map((c) => `<td>${esc(c)}</td>`).join("")}</tr>`).join("")
        || `<tr><td colspan="${t.columns.length}" class="muted">No rows sampled.</td></tr>`}</tbody></table></div>`;
  }

  function detail(r) {
    const issues = r.issues.map((i) => `<li><code>${esc(i)}</code> — ${esc(data.issues[i] || "")}</li>`).join("");
    const files = r.file_urls.slice(0, 4).map((u) => `<a class="rec-url" href="${esc(safeUrl(u))}" target="_blank" rel="noopener">${esc(u)}</a>`).join("<br>");
    return `<tr class="mf-detail" data-id="${r.row_id}"><td colspan="9">
      <div class="mf-grid">
        <div><h3>Evidence</h3>${evidenceTable(r)}
          ${r.llm_note ? `<p class="rec-sub">AI note: ${esc(r.llm_note)}</p>` : ""}
          <h3>Issues</h3>${issues ? `<ul class="mf-issues">${issues}</ul>` : '<p class="muted">None.</p>'}</div>
        <div><h3>Columns</h3>${columnTable(r)}</div>
      </div>
      <h3>File <span class="rec-sub">${esc(r.source_path)} · ${r.n_files} file${r.n_files > 1 ? "s" : ""}${r.file_size ? ` · ${size(r.file_size)}` : ""}</span></h3>
      ${preview(r)}<p>${files}${r.file_urls.length > 4 ? `<span class="rec-sub"> … ${r.file_urls.length - 4} more</span>` : ""}</p>
      <h3>Correct and decide</h3>
      <div class="mf-edit">${EDITABLE.map((f) => `<label>${f}<input class="dq-input" data-field="${f}" value="${esc(r[f])}"></label>`).join("")}</div>
      <div class="mf-actions">
        <textarea class="rec-note mf-note" placeholder="Reviewer note…">${esc(r.reviewer_note)}</textarea>
        <button type="button" class="rec-btn" data-act="save">Save changes</button>
        <button type="button" class="rec-btn primary" data-act="approved">Approve</button>
        <button type="button" class="rec-btn" data-act="rejected">Reject</button>
        ${r.review !== "pending" ? '<button type="button" class="rec-btn" data-act="pending">Back to “to check”</button>' : ""}
      </div></td></tr>`;
  }

  function rowHtml(r) {
    const unverified = FACTS.filter(([k]) => r.evidence[k] && r.evidence[k].verified === false).length;
    return `<tr class="mf-row mf-${r.review}" data-id="${r.row_id}">
      <td><span class="badge mf-s-${r.status}">${STATUS[r.status] || r.status}</span>
        ${r.review !== "pending" ? `<div class="rec-sub mf-rev-${r.review}">${r.review}</div>` : ""}</td>
      <td>${esc(r.dataset)}<div class="rec-sub">${esc(r.qtl_type)}</div></td>
      <td><strong>${esc(r.biocontext) || "—"}</strong><div class="rec-sub">${esc(r.level_1_context)}${r.level_2_context ? ` / ${esc(r.level_2_context)}` : ""}</div></td>
      <td>${esc(r.population) || "—"}</td><td>${esc(r.sample_size) || "—"}</td><td>${esc(r.genome_build) || "—"}</td>
      <td class="rec-url">${esc(r.source_path)}<div class="rec-sub">${r.n_files > 1 ? `${r.n_files} files · ` : ""}${size(r.file_size)}</div></td>
      <td>${r.issues.length ? `${r.issues.length} issue${r.issues.length > 1 ? "s" : ""}` : "—"}${unverified ? `<div class="rec-sub mf-bad">${unverified} unverified</div>` : ""}</td>
      <td><button type="button" class="rec-btn mf-toggle">${open.has(r.row_id) ? "Close" : "Check"}</button></td></tr>
      ${open.has(r.row_id) ? detail(r) : ""}`;
  }

  function locationHtml(l) {
    const inv = l.inventory.map((i) => `<tr><td class="rec-url">${esc(i.path)}</td><td>${i.n_files > 1 ? `${i.n_files} files` : ""}</td>
      <td>${size(i.size)}</td><td>${esc(i.holds)}</td><td>${esc(i.why)}</td></tr>`).join("");
    return `<details class="mf-loc"><summary><span class="badge mf-l-${l.status}">${esc(l.status)}</span>
      <a class="rec-url" href="${esc(safeUrl(l.url))}" target="_blank" rel="noopener">${esc(l.url)}</a>
      <span class="rec-sub">${esc([l.resolver, l.n_files && `${l.n_files} files`, `${l.n_rows} row${l.n_rows === 1 ? "" : "s"}`, l.note, l.run_note].filter(Boolean).join(" · "))}</span></summary>
      ${inv ? `<table class="mf-ev"><thead><tr><th>File</th><th></th><th>Size</th><th>Holds</th><th>Why</th></tr></thead><tbody>${inv}</tbody></table>` : '<p class="muted">Nothing was listed here.</p>'}</details>`;
  }

  function render() {
    const q = filter.value.trim().toLowerCase();
    const paper = (id) => data.records[id] || {};
    const shown = data.rows
      .filter((r) => !byStatus.value || r.status === byStatus.value)
      .filter((r) => !byReview.value || r.review === byReview.value)
      .filter((r) => !q || [r.dataset, r.qtl_type, r.biocontext, r.level_1_context, r.level_2_context, r.source_path, r.population,
        r.issues.join(" "), paper(r.record_id).publication_title, paper(r.record_id).doi].join(" ").toLowerCase().includes(q));
    const ids = [...new Set([...shown.map((r) => r.record_id), ...(q || byStatus.value || byReview.value ? [] : data.locations.map((l) => l.record_id))])]
      .sort((a, b) => b - a);
    const todo = data.rows.filter((r) => r.review === "pending").length;
    const mf = (data.files || {})["qtl_manifest.tsv"];
    document.getElementById("mf-file-rows").textContent = mf ? `· ${mf.rows} row${mf.rows === 1 ? "" : "s"}` : "";
    count.textContent = `${shown.length} of ${data.rows.length} rows · ${todo} to check · ${ids.length} paper${ids.length === 1 ? "" : "s"}`;
    body.innerHTML = ids.length ? ids.map((id) => {
      const p = paper(id), rows = shown.filter((r) => r.record_id === id), locs = data.locations.filter((l) => l.record_id === id);
      return `<div class="dq-paper"><div class="dq-paper-head">
          <h2 class="dq-paper-title"><a href="${esc(safeUrl(p.doi ? `https://doi.org/${p.doi}` : p.publication_url))}" target="_blank" rel="noopener">${esc(p.publication_title || `Record #${id}`)}</a></h2>
          <div class="dq-paper-meta">record #${id} · ${esc([p.first_author && `${p.first_author} et al.`, p.journal, p.year, p.qtl_type].filter(Boolean).join(" · "))}
            <button type="button" class="rec-btn mf-rebuild" data-record="${id}" ${busy ? "disabled" : ""} title="List the files and ask the AI again for this paper; approved, rejected and edited rows are kept">Rebuild</button></div>
          ${locs.map(locationHtml).join("")}</div>
        ${rows.length ? `<div class="table-wrap"><table class="rec-table"><thead><tr><th>Status</th><th>Dataset</th><th>Context</th><th>Pop.</th><th>N</th><th>Build</th><th>File</th><th>Issues</th><th></th></tr></thead>
          <tbody>${rows.map(rowHtml).join("")}</tbody></table></div>` : '<div class="rec-empty">No candidate table — see the locations above.</div>'}</div>`;
    }).join("") : `<div class="rec-empty">${data.rows.length || data.locations.length ? "Nothing matches the filter."
      : "No manifest rows yet. Press Start above to build them from the saved records."}</div>`;
  }

  async function load() {
    try { data = await (await fetch("/api/manifest")).json(); } catch { /* keep what is shown */ }
    render();
  }

  async function patch(id, payload) {
    const resp = await fetch(`/api/manifest/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    if (!resp.ok) { alert((await resp.json().catch(() => ({}))).detail || "Could not save."); return; }
    const updated = await resp.json();
    data.rows = data.rows.map((r) => (r.row_id === updated.row_id ? updated : r));
    if (data.files && data.files["qtl_manifest.tsv"]) data.files["qtl_manifest.tsv"].rows = data.rows.filter((r) => r.review === "approved").length;
    render();
  }

  body.addEventListener("click", (e) => {
    const toggle = e.target.closest(".mf-toggle");
    if (toggle) {
      const id = +toggle.closest("tr").dataset.id;
      if (open.has(id)) open.delete(id); else open.add(id);
      render();
      return;
    }
    const act = e.target.closest("[data-act]");
    if (!act) return;
    const box = act.closest(".mf-detail"), id = +box.dataset.id;
    const fields = {};
    box.querySelectorAll("[data-field]").forEach((i) => { fields[i.dataset.field] = i.value; });
    const payload = { fields, reviewer_note: box.querySelector(".mf-note").value };
    const row = data.rows.find((x) => x.row_id === id);
    if (act.dataset.act === "approved" && row && row.status !== "ready" && row.status !== "needs_review"
        && !confirm(`This row is “${STATUS[row.status]}”: the importer cannot load the file as it is (${row.issues.join(", ")}). Approve it anyway?`)) return;
    if (act.dataset.act !== "save") { payload.review = act.dataset.act; if (payload.review !== "pending") open.delete(id); }
    patch(id, payload);
  });

  // ---- building rows: one background run on the server, polled while it lasts ----
  const $ = (id) => document.getElementById(id);
  const runBtn = $("mf-run"), stopBtn = $("mf-stop"), progress = $("mf-progress"), runStatus = $("mf-run-status");
  const END = { done: "Finished", stopped: "Stopped", usage_limit: "Stopped: the AI's usage limit is reached. Finished papers are kept; start again later" };
  let timer = null, seen = -1;

  function showRun(s) {
    const job = s.job, running = !!job && job.state === "running";
    $("mf-waiting").textContent = `${s.waiting} human record${s.waiting === 1 ? "" : "s"} waiting · ${s.processed} of ${s.records} saved records done`;
    runBtn.hidden = running; stopBtn.hidden = !running;
    busy = running;
    document.querySelectorAll(".mf-rebuild").forEach((b) => { b.disabled = running; });
    progress.hidden = !job;
    if (job) {
      $("mf-bar-fill").style.width = `${job.total ? Math.round(100 * job.done / job.total) : 0}%`;
      const tally = `${job.done} of ${job.total} papers · ${job.rows} row${job.rows === 1 ? "" : "s"} · ${job.tokens.toLocaleString()} tokens${job.failed ? ` · ${job.failed} failed` : ""}`;
      runStatus.textContent = running
        ? `${tally} · now #${job.current ? job.current.record_id : "…"} ${job.current ? job.current.title.slice(0, 80) : ""}`
        : `${END[job.state] || job.state} · ${tally}`;
      $("mf-log").innerHTML = job.log.slice().reverse().map((l) => `<li class="${l.ok ? "" : "mf-bad"}">${esc(l.text)}</li>`).join("");
      if (job.done !== seen) { seen = job.done; load(); }
    }
    clearTimeout(timer);
    if (running) timer = setTimeout(poll, 3000);
  }

  async function poll() {
    try { showRun(await (await fetch("/api/manifest/run")).json()); }
    catch { clearTimeout(timer); timer = setTimeout(poll, 6000); }
  }

  async function start(payload) {
    const resp = await fetch("/api/manifest/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    const out = await resp.json().catch(() => ({}));
    if (!resp.ok) { progress.hidden = false; runStatus.textContent = `Not started: ${out.detail || resp.status}`; return; }
    seen = -1;
    showRun(out);
  }

  const options = () => ({ include_supplements: $("mf-supp").checked, all_species: $("mf-species").checked,
    redo: $("mf-redo").checked, no_llm: $("mf-nollm").checked });

  runBtn.addEventListener("click", () => {
    const ids = ($("mf-record").value.match(/\d+/g) || []).map(Number);
    start({ ...options(), record_ids: ids, limit: +$("mf-limit").value || 0 });
  });
  stopBtn.addEventListener("click", async () => {
    stopBtn.disabled = true;
    try { showRun(await (await fetch("/api/manifest/run/stop", { method: "POST" })).json()); } finally { stopBtn.disabled = false; }
  });
  body.addEventListener("click", (e) => {
    const b = e.target.closest(".mf-rebuild");
    if (b) start({ ...options(), record_ids: [+b.dataset.record] });
  });
  poll();

  [filter, byStatus, byReview].forEach((el) => el.addEventListener("input", render));
  filter.value = new URLSearchParams(location.search).get("q") || "";
  load();
})();
