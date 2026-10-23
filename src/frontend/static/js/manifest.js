// Manifest rows page: review candidate qtl_manifest rows and the evidence behind each field.
(() => {
  const body = document.getElementById("mf-body");
  const filter = document.getElementById("mf-filter");
  const count = document.getElementById("mf-count");
  let data = { rows: [], locations: [], records: {}, built: [], issues: {}, columns: [], role_columns: [] };
  const open = new Set(); // rows whose evidence is shown
  const openPapers = new Set(); // papers whose rows are shown
  let job = null; // the server's build queue, polled while it runs

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

  const DATA = { processed: ["Processed", "mf-d-ok"], as_is: ["Downloaded", "mf-d-ok"], too_large: ["Too large", "mf-d-warn"],
    download_failed: ["Download failed", "mf-d-bad"], processing_failed: ["Processing failed", "mf-d-bad"],
    not_downloaded: ["Not downloaded", "mf-d-warn"], skipped: ["Skipped", "mf-d-none"] };
  const dataBadge = (r) => { const d = r.evidence.data; if (!d) return '<span class="muted">—</span>';
    const [label, cls] = DATA[d.status] || [d.status, "mf-d-none"]; return `<span class="badge ${cls}">${label}</span>`; };
  const list = (items, more) => items.map((x) => `<code>${esc(x)}</code>`).join("<br>") + (more > items.length ? `<span class="rec-sub"> … ${more - items.length} more</span>` : "");

  function dataSection(r) {
    const d = r.evidence.data;
    if (!d) return '<p class="muted">This row was built before downloading existed — press Rebuild.</p>';
    const facts = [];
    if (d.raw) facts.push(["Downloaded", `${list(d.raw, d.n_raw)}${d.bytes ? `<div class="rec-sub">${size(d.bytes)}${d.fresh_files === 0 ? " · already there, not downloaded again" : ""}</div>` : ""}`]);
    if (d.why_processing) facts.push(["Why processing", esc(d.why_processing.join("; "))]);
    if (d.steps) facts.push(["What was done", `${d.steps.map(esc).join("<br>")}<div class="rec-sub">plan from ${d.plan_source === "LLM code" ? "Python the AI wrote" : d.plan_source === "LLM" ? "the AI" : "the header rules"}${d.plan_reason ? `: ${esc(d.plan_reason)}` : ""}</div>`]);
    if (d.outputs) facts.push(["Written", `${list(d.outputs, d.n_outputs)}${d.rows != null ? `<div class="rec-sub">${d.rows.toLocaleString()} rows, same as read</div>` : ""}`]);
    if (d.tried && d.tried.length) facts.push(["Plans rejected", d.tried.map((t) => `<div><strong>${t.source === "LLM" ? "AI" : "rules"}</strong>: ${esc((t.steps || []).join("; "))} — ${esc(t.result)}</div>`).join("")]);
    if (d.note) facts.push(["Note", esc(d.note)]);
    if (d.kept_share != null) facts.push(["Importer keeps", `${Math.round(d.kept_share * 100)}% of the sampled rows (single-base autosomal SNVs with position and p-value)`]);
    const gid = (d.checks || {}).gene_id, peak = (d.checks || {}).peak_id;
    if (gid) facts.push(["Gene ids", `${esc(gid.mode)}: ${Math.round(gid.ensembl_share * 100)}% Ensembl, e.g. <code>${esc(gid.example)}</code>${gid.warning ? ` — ${esc(gid.warning)}` : ""}`]);
    if (peak) facts.push(["Peak ids", `${Math.round(peak.chr_start_end_share * 100)}% chr_start_end, e.g. <code>${esc(peak.example)}</code>`]);
    if (d.problems && d.problems.length) facts.push(["Check problems", d.problems.map(esc).join("<br>")]);
    const checks = d.checks ? `<table class="mf-ev"><thead><tr><th>Importer needs</th><th>Column</th><th>Values that look right</th><th>Example</th></tr></thead><tbody>${
      Object.entries(d.checks).filter(([k, c]) => k !== "file" && c && c.column !== undefined).map(([role, c]) => `<tr><th>${esc(role)}</th><td><code>${esc(c.column)}</code></td>
        <td class="${c.checked && c.pass === c.checked ? "mf-ok" : "mf-bad"}">${c.problem ? esc(c.problem) : `${c.pass.toLocaleString()} / ${c.checked.toLocaleString()}`}</td><td><code>${esc(c.example)}</code></td></tr>`).join("")}</tbody></table>` : "";
    const pv = d.preview && d.preview.columns ? `<p class="rec-sub">First rows of ${d.status === "processed" ? "the processed file" : "the file"}:</p>
      <div class="table-wrap"><table class="mf-preview"><thead><tr>${d.preview.columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>
      <tbody>${d.preview.rows.map((row) => `<tr>${row.map((c) => `<td>${esc(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>` : "";
    return `<p>${dataBadge(r)} ${r.file_path && ["processed", "as_is"].includes(d.status) ? `<code>${esc(r.file_path)}</code>` : ""}</p>
      ${facts.length ? `<table class="mf-ev mf-data"><tbody>${facts.map(([k, v]) => `<tr><th>${k}</th><td>${v}</td></tr>`).join("")}</tbody></table>` : ""}
      ${checks}${pv}`;
  }

  // What the AI did for this paper: its summary, the scripts it ran and every tool call.
  function agentSection(r) {
    const a = r.evidence.agent;
    if (!a) return "";
    const scripts = (a.scripts || []).map((x) => `<li><code>${esc(x.script)}</code> — ${esc(x.purpose)} <span class="rec-sub">exit ${esc(x.exit_code)}, ${esc(x.seconds)} s</span></li>`).join("");
    const log = (a.log || []).map((x, i) => `<tr><td>${i + 1}</td><td><code>${esc(x.tool)}</code></td><td class="rec-url">${esc(x.args)}</td><td class="rec-url">${esc(x.result)}</td></tr>`).join("");
    return `<h3>AI work</h3>${a.summary ? `<p>${esc(a.summary)}</p>` : ""}
      ${scripts ? `<p class="rec-sub">Python it wrote and ran (kept in the data folder):</p><ul class="mf-issues">${scripts}</ul>` : ""}
      <details class="mf-loc"><summary>${(a.log || []).length} tool calls</summary>
      <div class="table-wrap"><table class="mf-ev"><thead><tr><th>#</th><th>Tool</th><th>Asked</th><th>Got</th></tr></thead><tbody>${log}</tbody></table></div></details>`;
  }

  function detail(r) {
    const issues = r.issues.map((i) => `<li><code>${esc(i)}</code> — ${esc(data.issues[i] || "")}</li>`).join("");
    const files = r.file_urls.slice(0, 4).map((u) => `<a class="rec-url" href="${esc(safeUrl(u))}" target="_blank" rel="noopener">${esc(u)}</a>`).join("<br>");
    return `<tr class="mf-detail" data-id="${r.row_id}"><td colspan="10">
      <div class="mf-grid">
        <div><h3>Evidence</h3>${evidenceTable(r)}
          ${r.llm_note ? `<p class="rec-sub">AI note: ${esc(r.llm_note)}</p>` : ""}
          <h3>Issues</h3>${issues ? `<ul class="mf-issues">${issues}</ul>` : '<p class="muted">None.</p>'}</div>
        <div><h3>Columns</h3>${columnTable(r)}</div>
      </div>
      <h3>Data</h3>${dataSection(r)}
      ${agentSection(r)}
      <h3>Source file <span class="rec-sub">${esc(r.source_path)} · ${r.n_files} file${r.n_files > 1 ? "s" : ""}${r.file_size ? ` · ${size(r.file_size)}` : ""}</span></h3>
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

  // Why a row has its status: the issues that decide it, in a few words each.
  // An issue in a few words: its first sentence, without examples in brackets.
  const shortIssue = (code) => (data.issues[code] || code).replace(/\s*\([^)]*\)/g, "").split(/;|\. | — /)[0].replace(/\.$/, "").trim();
  function deciding(r) {
    const set = new Set(r.status === "not_loadable" ? data.blocking : r.status === "needs_processing" ? data.processing : []);
    return set.size ? r.issues.filter((i) => set.has(i)) : r.issues;
  }
  function statusWhy(r) {
    if (r.status === "ready") return '<div class="rec-sub">no open issue</div>';
    const why = deciding(r);
    return `<div class="rec-sub mf-why" title="${esc(why.map((i) => `${i}: ${data.issues[i] || ""}`).join("\n"))}">${
      esc(why.slice(0, 2).map(shortIssue).join("; "))}${why.length > 2 ? ` (+${why.length - 2})` : ""}</div>`;
  }
  // What happened to the data, with the reason when it is not in place.
  const MANIFEST_TYPES = ["eQTL", "sQTL", "caQTL", "pQTL"];
  // Rows built before skipped downloads said why: work the reason out the same way the server now does.
  function skipWhy(r) {
    if (r.holds === "subset") return "not downloaded: the file is a selected subset (significant or lead hits), not every tested pair";
    if (!r.qtl_type) return "not downloaded: the QTL type is unknown";
    if (!MANIFEST_TYPES.includes(r.qtl_type)) return `not downloaded: ${r.qtl_type} is not a type the manifest holds (${MANIFEST_TYPES.join(", ")})`;
    const blocking = r.issues.filter((i) => (data.blocking || []).includes(i)).map(shortIssue);
    return `not downloaded: ${blocking.join("; ") || "the row cannot be loaded"}`;
  }

  function dataWhy(r) {
    const d = r.evidence.data;
    if (!d) return "";
    if (d.status === "skipped" && /cannot become a manifest row as it is/.test(d.note || "")) d.note = skipWhy(r);
    const note = d.status === "processed" ? `${d.plan_source === "LLM" ? "AI" : "rule"} plan: ${(d.steps || []).join("; ")}`
      : d.status === "as_is" ? "readable as it is" : d.note || "";
    return note ? `<div class="rec-sub mf-why" title="${esc(note)}">${esc(note.slice(0, 90))}${note.length > 90 ? "…" : ""}</div>` : "";
  }

  function rowHtml(r) {
    const unverified = FACTS.filter(([k]) => r.evidence[k] && r.evidence[k].verified === false).length;
    return `<tr class="mf-row mf-${r.review}" data-id="${r.row_id}">
      <td><span class="badge mf-s-${r.status}">${STATUS[r.status] || r.status}</span>${statusWhy(r)}
        ${r.review !== "pending" ? `<div class="rec-sub mf-rev-${r.review}">${r.review}</div>` : ""}</td>
      <td>${esc(r.dataset)}<div class="rec-sub">${esc(r.qtl_type)}</div></td>
      <td><strong>${esc(r.biocontext) || "—"}</strong><div class="rec-sub">${esc(r.level_1_context)}${r.level_2_context ? ` / ${esc(r.level_2_context)}` : ""}</div></td>
      <td>${esc(r.population) || "—"}</td><td>${esc(r.sample_size) || "—"}</td><td>${esc(r.genome_build) || "—"}</td>
      <td class="rec-url">${esc(r.source_path)}<div class="rec-sub">${r.n_files > 1 ? `${r.n_files} files · ` : ""}${size(r.file_size)}</div></td>
      <td>${dataBadge(r)}${dataWhy(r)}</td>
      <td>${r.issues.length ? `${r.issues.length} issue${r.issues.length > 1 ? "s" : ""}` : "—"}${unverified ? `<div class="rec-sub mf-bad">${unverified} unverified</div>` : ""}</td>
      <td><button type="button" class="rec-btn mf-toggle">${open.has(r.row_id) ? "Close" : "Check"}</button></td></tr>
      ${open.has(r.row_id) ? detail(r) : ""}`;
  }

  const LOC = { listed: "files listed", direct: "a direct file link", manual: "needs a person", skipped: "not opened",
    error: "could not be read", duplicate: "same files as another location" };

  function locationHtml(l, expanded = false) {
    const inv = l.inventory.map((i) => `<tr><td class="rec-url">${esc(i.path)}</td><td>${i.n_files > 1 ? `${i.n_files} files` : ""}</td>
      <td>${size(i.size)}</td><td>${esc(i.holds)}</td><td>${esc(reword(i.why) || HOLDS[i.holds] || "")}</td></tr>`).join("");
    return `<details class="mf-loc" ${expanded ? "open" : ""}><summary><span class="badge mf-l-${l.status}">${esc(l.status)}</span>
      <a class="rec-url" href="${esc(safeUrl(l.url))}" target="_blank" rel="noopener">${esc(l.url)}</a>
      <span class="rec-sub">${esc([LOC[l.status] && `${LOC[l.status]}${l.note ? ":" : ""}`, reword(l.note), l.resolver && `via ${l.resolver}`,
        l.n_files && `${l.n_files} files`, `${l.n_rows} row${l.n_rows === 1 ? "" : "s"}`, reword(l.run_note)].filter(Boolean).join(" · ").replace(": ·", ":"))}</span></summary>
      ${inv ? `<table class="mf-ev"><thead><tr><th>File</th><th></th><th>Size</th><th>Holds</th><th>Why</th></tr></thead><tbody>${inv}</tbody></table>` : '<p class="muted">Nothing was listed here.</p>'}</details>`;
  }

  // ---- papers: one line each, with Build manifest and Check ----
  const $ = (id) => document.getElementById(id);
  const show = $("mf-show"), human = $("mf-human"), runbar = $("mf-runbar"), runStatus = $("mf-run-status"), stopBtn = $("mf-stop");
  const END = { stopped: "Stopped.", usage_limit: "Stopped: the AI's usage limit is reached. Built papers are kept; build again later." };
  const PAGE = 100;
  let limit = PAGE, timer = null, seenDone = -1, seenLog = 0;
  const rowsOf = (id) => data.rows.filter((r) => r.record_id === id);
  const isBuilt = (id) => (data.built || []).includes(id);
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

  function stateOf(id) { // exists | building | queued | built | unbuilt
    if ((data.records[id] || {}).already_exists && !isBuilt(id)) return "exists";
    if (job && job.state === "running") {
      if (job.current && job.current.record_id === id) return "building";
      if ((job.queue || []).includes(id)) return "queued";
    }
    return isBuilt(id) ? "built" : "unbuilt";
  }

  // Why a built paper has no row, in plain words: per location, its own note or each file's reason.
  const HOLDS = { doc: "documentation", subset: "a subset of results, not every tested pair", not_qtl: "not a QTL table",
    unknown_scope: "could not tell what it holds",
    other: "not a table file (by its name: e.g. a PDF, image, script, genotype or binary file), so it was not opened" };
  // Notes stored by earlier builds named command-line flags; say what to tick instead.
  const reword = (t) => String(t || "").replace("(--include-supplements opens it)", '(tick "opens supplementary tables" to open it)')
    .replace("LLM step skipped (--no-llm)", 'AI step skipped ("skips the AI" was ticked)');
  function noRowReasons(id) {
    const out = [];
    data.locations.filter((l) => l.record_id === id).forEach((l) => {
      if (l.status === "duplicate") return;
      if (l.status !== "listed" && l.status !== "direct") { out.push([l.url, reword(l.note) || LOC[l.status] || l.status]); return; }
      const files = l.inventory.filter((i) => i.holds !== "doc");
      if (!files.length) out.push([l.url, l.inventory.length ? "only documentation files" : "no files were listed"]);
      files.forEach((i) => out.push([i.path, reword(i.why) || HOLDS[i.holds] || i.holds]));
    });
    return out;
  }

  const needsYou = (id) => (data.needs_you || {})[id] || [];

  function needsBadge(id) {
    const hand = needsYou(id);
    if (!hand.length) return "";
    return `<span class="badge mf-b-hand" title="${esc(hand.map((h) => `${h.what}: ${h.why}`).join("\n"))}">Needs you</span>
      <span class="rec-sub">${esc(hand[0].why.slice(0, 140))}${hand.length > 1 ? ` (+${hand.length - 1} more)` : ""}</span>`;
  }

  function builtSummary(id) {
    const rows = rowsOf(id);
    if (!rows.length) {
      if (needsYou(id).length) return `<span class="rec-sub">no manifest row</span> ${needsBadge(id)}`;
      const why = noRowReasons(id);
      return `<span class="rec-sub">no manifest row — nothing here can become one${why.length ? `: ${esc(why[0][1]).slice(0, 120)}${why.length > 1 ? ` (+${why.length - 1} more)` : ""}` : ""}</span>`;
    }
    const tally = (key) => Object.entries(rows.reduce((m, r) => ((m[key(r)] = (m[key(r)] || 0) + 1), m), {}));
    const states = tally((r) => r.status).map(([s, n]) => `<span class="badge mf-s-${s}">${n} ${STATUS[s] || s}</span>`).join(" ");
    const dataStates = tally((r) => (r.evidence.data || {}).status || "").filter(([s]) => s && s !== "skipped")
      .map(([s, n]) => `${n} ${(DATA[s] || [s])[0].toLowerCase()}`).join(", ");
    const checked = rows.filter((r) => r.review !== "pending").length, approved = rows.filter((r) => r.review === "approved").length;
    return `${states}<span class="rec-sub"> · ${plural(rows.length, "row")}${dataStates ? ` · data: ${esc(dataStates)}` : ""} ·
      ${checked === rows.length ? "all checked" : `${checked} of ${rows.length} checked`}${approved ? `, ${approved} approved` : ""}</span>
      ${needsBadge(id)}${notReadyWhy(rows)}`;
  }

  function notReadyWhy(rows) {
    const counts = {};
    rows.filter((r) => r.status !== "ready" && r.review !== "rejected").forEach((r) => deciding(r).forEach((i) => { counts[i] = (counts[i] || 0) + 1; }));
    const top = Object.entries(counts).sort((a, b) => b[1] - a[1]);
    if (!top.length) return "";
    return `<div class="rec-sub mf-why mf-notready">Not ready because: ${top.slice(0, 3).map(([i, n]) => `${esc(shortIssue(i))} (${n})`).join(" · ")}${top.length > 3 ? ` · +${top.length - 3} more` : ""}</div>`;
  }

  // How the last build of this paper ended, when it did not end well (kept on the server).
  const OUTCOME = { failed: "Last build failed", stopped: "Last build was stopped", usage_limit: "Waiting: the AI's usage limit was reached; the run checks every hour and resumes with this paper" };
  function lastOutcome(id) {
    const run = (data.runs || {})[id];
    if (!run || !OUTCOME[run.outcome] || stateOf(id) === "building" || stateOf(id) === "queued") return "";
    const when = new Date(run.at).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    return `<div class="rec-sub mf-bad">${OUTCOME[run.outcome]} (${esc(when)})${run.note ? `: ${esc(run.note.slice(0, 300))}` : ""}</div>`;
  }

  // What the saved download locations offer, as the search recorded it.
  const KIND = { open_sumstats: ["full summary statistics", "mf-k-good"], closed_sumstats: ["summary statistics, access by request", "mf-k-warn"],
    browser: ["results browser only", "mf-k-none"], supplement: ["supplementary tables only", "mf-k-none"] };
  const kindTag = (p) => {
    const k = KIND[p.data_kind]; return k ? `<span class="mf-kind ${k[1]}">${k[0]}</span>` : "";
  };

  function headHtml(id) {
    const p = data.records[id] || {}, st = stateOf(id), rows = rowsOf(id);
    const toCheck = rows.filter((r) => r.review === "pending").length;
    const status = st === "building" ? `<span class="badge mf-b-run">Building…</span> <span class="rec-sub">${esc(job.current.step || "starting")}</span>`
      : st === "queued" ? `<span class="badge mf-b-queue">Queued</span> <span class="rec-sub">${job.queue.indexOf(id) + 1} in line</span>`
      : st === "built" ? `<span class="badge mf-b-done">Built</span> ${builtSummary(id)}`
      : st === "exists" ? `<span class="badge mf-b-exists">Already exists</span> <span class="rec-sub">in the reference manifest as
          <strong>${esc(p.already_exists)}</strong> — skipped, nothing to build or download</span>`
      : `<span class="muted">Not built</span>${p.not_eligible ? `<span class="rec-sub"> · ${esc(p.not_eligible)}</span>` : ""}`;
    const busy = st === "building" || st === "queued";
    const build = st === "exists" ? "" : `<button type="button" class="rec-btn ${st === "built" ? "" : "primary"} mf-build" data-record="${id}" ${busy ? "disabled" : ""}
      title="${st === "built" ? "Build again; approved, rejected and edited rows are kept" : "List the files, read the paper, fill the manifest rows and download the data"}">${st === "built" ? "Rebuild" : "Build manifest"}</button>`;
    const check = st === "built" || (busy && isBuilt(id))
      ? `<button type="button" class="rec-btn ${toCheck && !openPapers.has(id) ? "primary" : ""} mf-open" data-record="${id}">${openPapers.has(id) ? "Close" : toCheck ? `Check (${toCheck})` : "Check"}</button>` : "";
    return `<div class="mf-paper-main">
        <a class="mf-paper-title" href="${esc(safeUrl(p.doi ? `https://doi.org/${p.doi}` : p.publication_url))}" target="_blank" rel="noopener">${esc(p.publication_title || `Record #${id}`)}</a>
        <div class="dq-paper-meta">${kindTag(p)} #${id} · ${esc([p.first_author && `${p.first_author} et al.`, p.journal, p.year, p.qtl_type, p.species && `species: ${p.species}`,
          p.n_locations > 1 && `${p.n_locations} download locations`, p.saved_by === "auto" ? "saved by the AI" : "saved by you"].filter(Boolean).join(" · "))}</div>
        <div class="mf-paper-status">${status}</div>${lastOutcome(id)}
      </div><div class="mf-paper-btns">${build}${check}</div>`;
  }

  function bodyHtml(id) {
    const rows = rowsOf(id), locs = data.locations.filter((l) => l.record_id === id);
    const hand = needsYou(id);
    const items = (list) => `<ul>${list.map(([what, reason]) => `<li><code>${esc(what)}</code> — ${esc(reason)}</li>`).join("")}</ul>`;
    const why = hand.length
      ? `<div class="mf-norows"><strong>Needs you.</strong> ${rows.length ? "Some data could not be fetched automatically:" : "No manifest row was made, because:"}
          ${items(hand.map((h) => [h.what, h.why]))}
          <span class="rec-sub">Open or download these by hand (or raise the size limit), then press Rebuild.</span></div>`
      : rows.length ? ""
      : `<div class="mf-norows mf-nothing"><strong>No manifest row:</strong> nothing at the download locations can become one.
          ${items(noRowReasons(id).length ? noRowReasons(id) : [["—", "no download location was recorded"]])}</div>`;
    return `<div class="mf-paper-body">${why}${locs.map((l) => locationHtml(l, !rows.length)).join("")}
      ${rows.length ? `<div class="table-wrap"><table class="rec-table"><thead><tr><th>Status</th><th>Dataset</th><th>Context</th><th>Pop.</th><th>N</th><th>Build</th><th>File</th><th>Data</th><th>Issues</th><th></th></tr></thead>
        <tbody>${rows.map(rowHtml).join("")}</tbody></table></div>` : ""}</div>`;
  }

  const paperHtml = (id) => `<div class="dq-paper mf-paper" data-record="${id}"><div class="mf-paper-head">${headHtml(id)}</div>
    ${openPapers.has(id) && isBuilt(id) ? bodyHtml(id) : ""}</div>`;

  function shownIds() {
    const q = filter.value.trim().toLowerCase();
    return Object.keys(data.records).map(Number).filter((id) => {
      const p = data.records[id], rows = rowsOf(id), st = stateOf(id);
      if (human.checked && p.not_eligible) return false;
      if (show.value === "sumstats" && p.data_kind !== "open_sumstats") return false;
      if (show.value === "exists" && st !== "exists") return false;
      if (show.value === "unbuilt" && st !== "unbuilt") return false;
      if (show.value === "built" && st !== "built") return false;
      if (show.value === "needs" && !needsYou(id).length) return false;
      if (show.value === "tocheck" && !rows.some((r) => r.review === "pending")) return false;
      if (show.value === "approved" && !rows.some((r) => r.review === "approved")) return false;
      if (show.value === "processed" && !rows.some((r) => (r.evidence.data || {}).status === "processed")) return false;
      if (show.value === "as_is" && !rows.some((r) => (r.evidence.data || {}).status === "as_is")) return false;
      return !q || [id, p.publication_title, p.doi, p.first_author, p.journal, p.qtl_type, p.qtl_context, p.species,
        ...rows.map((r) => [r.dataset, r.biocontext, r.level_1_context, r.level_2_context, r.source_path, r.issues.join(" ")].join(" "))]
        .join(" ").toLowerCase().includes(q);
    }).sort((a, b) => b - a);
  }

  const buildAll = $("mf-build-all");
  const toBuild = (ids) => ids.filter((id) => stateOf(id) === "unbuilt");

  function updateCount(ids) {
    const n = toBuild(ids).length;
    buildAll.textContent = n ? `Build all shown (${n})` : "Build all shown";
    buildAll.disabled = !n;
    const builtN = ids.filter(isBuilt).length, toCheck = ids.reduce((n, id) => n + rowsOf(id).filter((r) => r.review === "pending").length, 0);
    const handN = ids.filter((id) => needsYou(id).length).length;
    count.textContent = `${plural(ids.length, "paper")} · ${builtN} built · ${handN} need you · ${plural(toCheck, "row")} to check`;
    const mf = (data.files || {})["qtl_manifest.tsv"];
    $("mf-file-rows").textContent = mf ? `· ${plural(mf.rows, "row")}` : "";
  }

  function render() {
    const ids = shownIds();
    updateCount(ids);
    body.innerHTML = ids.length ? ids.slice(0, limit).map(paperHtml).join("")
      + (ids.length > limit ? `<button type="button" class="rec-btn mf-more">Show ${Math.min(PAGE, ids.length - limit)} more (${ids.length - limit} not shown)</button>` : "")
      : '<div class="rec-empty">No paper matches.</div>';
  }

  // While builds run only the paper headers change; redrawing everything would wipe a half-typed correction.
  function refreshHeads() {
    body.querySelectorAll(".mf-paper").forEach((el) => { el.querySelector(".mf-paper-head").innerHTML = headHtml(+el.dataset.record); });
  }
  function refreshPaper(id) {
    const el = body.querySelector(`.mf-paper[data-record="${id}"]`);
    if (el) el.outerHTML = paperHtml(id);
  }

  async function load(redraw = true) {
    try { data = await (await fetch("/api/manifest")).json(); } catch { /* keep what is shown */ }
    if (redraw) render();
  }

  async function patch(id, payload) {
    const resp = await fetch(`/api/manifest/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    if (!resp.ok) { alert((await resp.json().catch(() => ({}))).detail || "Could not save."); return; }
    const updated = await resp.json();
    data.rows = data.rows.map((r) => (r.row_id === updated.row_id ? updated : r));
    if (data.files && data.files["qtl_manifest.tsv"]) data.files["qtl_manifest.tsv"].rows = data.rows.filter((r) => r.review === "approved").length;
    refreshPaper(updated.record_id);
    updateCount(shownIds());
  }

  async function showRun(s) {
    job = s.job;
    const running = !!job && job.state === "running";
    if (job && job.done !== seenDone) {
      // Papers finished since the last look: reload, then redraw only those papers.
      const finished = job.log.slice(seenDone < 0 ? job.log.length : seenLog).map((l) => l.record_id);
      seenDone = job.done; seenLog = job.log.length;
      await load(false);
      refreshHeads();
      finished.forEach(refreshPaper);
    } else {
      refreshHeads();
    }
    updateCount(shownIds());
    runbar.hidden = !running && !(job && END[job.state]);
    if (running) {
      const c = job.current;
      runStatus.textContent = `Building #${c ? c.record_id : "…"} ${c ? c.title.slice(0, 80) : ""}${c && c.step ? ` — ${c.step}` : ""}`
        + (job.queue.length ? ` · ${plural(job.queue.length, "paper")} queued` : "");
    } else if (job && END[job.state]) {
      runStatus.textContent = END[job.state];
    }
    stopBtn.hidden = !running;
    clearTimeout(timer);
    if (running) timer = setTimeout(poll, 3000);
  }

  async function poll() {
    try { await showRun(await (await fetch("/api/manifest/run")).json()); }
    catch { clearTimeout(timer); timer = setTimeout(poll, 6000); }
  }

  const options = () => ({ include_supplements: $("mf-supp").checked, no_llm: $("mf-nollm").checked,
    download: $("mf-download").checked, max_gb: +$("mf-maxgb").value || 0 });

  async function build(ids) {
    const resp = await fetch("/api/manifest/run", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...options(), record_ids: ids }) });
    const out = await resp.json().catch(() => ({}));
    if (!resp.ok) { alert(`Could not build ${ids.length > 1 ? `${ids.length} papers` : `#${ids[0]}`}: ${out.detail || resp.status}`); return; }
    await showRun(out);
    updateCount(shownIds());
  }

  buildAll.addEventListener("click", async () => {
    const ids = toBuild(shownIds());
    if (!ids.length) return;
    const o = options();
    if (!confirm(`Build ${plural(ids.length, "paper")}? They are built one after another on the server`
      + (o.download ? `, downloading and processing up to ${o.max_gb} GB of data per paper` : "")
      + ". You can close this page; Stop ends the queue.")) return;
    buildAll.disabled = true;
    await build(ids.slice().sort((a, b) => b - a));  // in the order they are listed
  });

  body.addEventListener("click", (e) => {
    const b = e.target.closest(".mf-build");
    if (b) { b.disabled = true; build([+b.dataset.record]); return; }
    const o = e.target.closest(".mf-open");
    if (o) {
      const id = +o.dataset.record;
      if (openPapers.has(id)) openPapers.delete(id); else openPapers.add(id);
      refreshPaper(id);
      return;
    }
    if (e.target.closest(".mf-more")) { limit += PAGE; render(); return; }
    const toggle = e.target.closest(".mf-toggle");
    if (toggle) {
      const id = +toggle.closest("tr").dataset.id;
      if (open.has(id)) open.delete(id); else open.add(id);
      refreshPaper(+toggle.closest(".mf-paper").dataset.record);
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
  stopBtn.addEventListener("click", async () => {
    if (!confirm("Stop building? The paper in progress is dropped; queued papers are not built.")) return;
    stopBtn.disabled = true;
    try { await showRun(await (await fetch("/api/manifest/run/stop", { method: "POST" })).json()); } finally { stopBtn.disabled = false; }
  });
  [filter, show, human].forEach((el) => el.addEventListener("input", () => { limit = PAGE; render(); }));
  filter.value = new URLSearchParams(location.search).get("q") || "";
  load().then(poll);
})();
