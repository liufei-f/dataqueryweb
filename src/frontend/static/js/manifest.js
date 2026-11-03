// Manifest rows page: review candidate qtl_manifest rows and the evidence behind each field.
(() => {
  const body = document.getElementById("mf-body");
  const filter = document.getElementById("mf-filter");
  const count = document.getElementById("mf-count");
  let data = { rows: [], locations: [], records: {}, built: [], issues: {}, columns: [], role_columns: [] };
  const open = new Set(); // rows whose evidence is shown
  const reviewDrafts = new Map();
  const chatMessages = new Map();
  const chatDrafts = new Map();
  const chatLoaded = new Set();
  const chatLoading = new Set();
  let chatAvailable = null;
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

  function sampleTable(table) {
    const samples = (table.rows || []).slice(0, 5);
    return `<div class="table-wrap"><table class="mf-preview"><thead><tr><th>Column</th>${samples.map((_, i) => `<th>Sample ${i + 1}</th>`).join("")}</tr></thead>
      <tbody>${(table.columns || []).map((column, i) => `<tr><th scope="row">${esc(column)}</th>${samples.map((row) => `<td>${esc(row[i])}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
  }

  function preview(r) {
    const t = r.evidence.table || {};
    if (!(t.columns || []).length) return `<p class="muted">Header not read${t.note ? `: ${esc(t.note)}` : ""}.</p>`;
    const notes = [t.header_from && `header taken from ${t.header_from}`, t.has_header === false && "the file itself has no header row",
      t.fmt && t.fmt !== "text" && t.fmt, t.n_rows && `${t.n_rows.toLocaleString()} rows`, t.note].filter(Boolean).map(esc).join(" · ");
    return `${t.preamble ? `<p class="rec-sub">Title lines: ${esc(t.preamble)}</p>` : ""}${notes ? `<p class="rec-sub">${notes}</p>` : ""}
      ${sampleTable(t)}`;
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
    const pv = d.preview && d.preview.columns ? `<p class="rec-sub">Sample values from ${d.status === "processed" ? "the processed file" : "the file"}:</p>${sampleTable(d.preview)}` : "";
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
    const d = r.evidence.data || {};
    const processed = d.status === "processed";
    const table = d.preview?.columns?.length ? d.preview : r.evidence.table || {};
    const previewLabel = processed ? "Processed file — first 5 rows" : "Downloaded file — first 5 rows";
    const aliases = {biocontext: "context", level_1_context: "context", level_2_context: "context"};
    const fields = data.columns.map(field => {
      const e = r.evidence[field] || r.evidence[aliases[field]] || {};
      const mapping = (data.role_columns || []).includes(field);
      const reason = [e.note, e.quote && `Evidence: ${e.quote}`, e.source && `Source: ${e.source}`].filter(Boolean).join(" · ")
        || (mapping && r[field] ? `Read column ${r[field]} from the validated output file.`
          : r[field] === "" || r[field] == null ? "Not provided. Verify whether this field is required."
          : "No field-specific reason recorded; verify against the paper and file.");
      return `<tr><th scope="row">${esc(field)}</th><td><input class="dq-input" data-field="${esc(field)}" aria-label="${esc(field)}" value="${esc(r[field])}"></td><td>${esc(reason)}</td></tr>`;
    }).join("");
    return `<tr class="mf-detail" data-id="${r.row_id}"><td colspan="10">
      <h3>${previewLabel}</h3>
      <p class="rec-sub">${esc(r.file_path || r.source_path)}</p>
      ${table.columns?.length ? sampleTable(table) : '<p class="muted">No saved file preview available.</p>'}
      ${(table.rows || []).length < 5 ? '<p class="rec-sub">This build saved fewer than 5 preview rows. A new build will retain up to 5.</p>' : ""}
      ${processed ? '<p class="rec-sub">This preview is from the converted output. A separate original-file preview was not retained by this build.</p>' : ""}
      <h3>Correct and decide — manifest values</h3>
      <div class="table-wrap"><table class="mf-ev"><thead><tr><th>Manifest column</th><th>Value to export</th><th>Note / reason</th></tr></thead><tbody>${fields}</tbody></table></div>
      ${r.llm_note ? `<p class="rec-sub"><strong>Build note:</strong> ${esc(r.llm_note)}</p>` : ""}
      ${r.issues.length ? `<p class="rec-sub"><strong>Checks to resolve:</strong> ${r.issues.map(i => esc(data.issues[i] || i)).join("; ")}</p>` : ""}
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

  // A row kept although its table is not full summary statistics: say why, in plain words.
  function nonFull(r) {
    if (r.holds !== "non_full") return "";
    const h = r.evidence.holds || {};
    return `<div class="mf-non-full"><strong>Not full summary statistics:</strong> ${esc(h.reason_label || "reason not given")}
      ${h.note ? `<div class="rec-sub">${esc(h.note.replace(/^Not full summary statistics — [^.]*\.\s*/, ""))}</div>` : ""}</div>`;
  }

  function rowHtml(r) {
    const unverified = FACTS.filter(([k]) => r.evidence[k] && r.evidence[k].verified === false).length;
    return `<tr class="mf-row mf-${r.review}" data-id="${r.row_id}">
      <td><span class="badge mf-s-${r.status}">${STATUS[r.status] || r.status}</span>${statusWhy(r)}
        ${r.review !== "pending" ? `<div class="rec-sub mf-rev-${r.review}">${r.review}</div>` : ""}</td>
      <td>${esc(r.dataset)}<div class="rec-sub">${esc(r.qtl_type)}</div></td>
      <td><strong>${esc(r.biocontext) || "—"}</strong><div class="rec-sub">${esc(r.level_1_context)}${r.level_2_context ? ` / ${esc(r.level_2_context)}` : ""}</div>${nonFull(r)}</td>
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

  function buildChatHtml(id) {
    if (chatAvailable === false) return "";
    const messages = chatMessages.get(id) || [];
    const transcript = messages.map((m) => "<article class=\"mf-chat-message mf-chat-" + esc(m.role) + "\"><strong>" +
      (m.role === "user" ? "You" : "AI") + (m.model ? " · " + esc(m.model) : "") + "</strong><div>" +
      esc(m.content) + "</div></article>").join("");
    const draft = chatDrafts.get(id) || "";
    return "<section class=\"mf-build-chat\" data-chat-panel=\"" + id + "\"><h3>Build conversation</h3>" +
      "<p class=\"rec-sub\">Paste a registration email, README text, URL, or local-file detail. The AI can discuss it here; " +
      "this conversation is saved and included the next time you build this paper.</p>" +
      "<div class=\"mf-chat-history\" aria-live=\"polite\">" + (transcript || "<p class=\"muted\">No messages yet.</p>") + "</div>" +
      "<label class=\"mf-chat-label\" for=\"mf-chat-input-" + id + "\">Message to the build AI</label>" +
      "<textarea id=\"mf-chat-input-" + id + "\" class=\"dq-input mf-chat-input\" data-chat-input=\"" + id + "\" rows=\"5\" " +
      "placeholder=\"Paste the relevant email or documentation here.\">" + esc(draft) + "</textarea>" +
      "<div class=\"mf-chat-actions\"><button type=\"button\" class=\"rec-btn mf-chat-send\" data-chat-send=\"" + id + "\" " +
      (chatLoading.has(id) ? "disabled" : "") + ">" + (chatLoading.has(id) ? "Waiting for AI…" : "Send to AI") + "</button>" +
      "<button type=\"button\" class=\"rec-btn primary mf-chat-build\" data-chat-build=\"" + id + "\" " +
      (messages.some((m) => m.role === "user") ? "" : "disabled") + ">Continue build with this context</button></div>" +
      "<p class=\"rec-sub\">Continuing starts a build for this paper only. The build agent will verify the provided details " +
      "and reuse files already in its data folder.</p></section>";
  }

  document.addEventListener("focusin", e => {
    const input = e.target.closest("[data-chat-input]");
    if (!input) return;
    const id = +input.dataset.chatInput;
    if (chatLoaded.has(id) || chatLoading.has(id)) return;
    loadChat(id).then(() => {
      const replacement = document.getElementById(`mf-chat-input-${id}`);
      replacement?.focus({preventScroll: true});
      if (replacement) replacement.setSelectionRange(replacement.value.length, replacement.value.length);
    });
  });

  async function loadChat(id) {
    if (chatLoaded.has(id) || chatLoading.has(id)) return;
    chatLoading.add(id);
    try {
      const response = await fetch("/api/manifest/" + id + "/chat");
      const result = await response.json();
      if (response.status === 404) { chatAvailable = false; return; }
      if (!response.ok) throw new Error(result.detail || "Could not load conversation");
      chatAvailable = true;
      chatMessages.set(id, result.messages || []);
      chatLoaded.add(id);
    } catch (error) { console.error(error); }
    finally { chatLoading.delete(id); refreshPaper(id); }
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
    if ((data.records[id] || {}).qtl_review === "not_qtl") return "excluded";
    if ((data.records[id] || {}).already_exists && !isBuilt(id)) return "exists";
    if (job && job.state === "running") {
      if ((job.active || [job.current]).some(c => c && c.record_id === id)) return "building";
      if ((job.queue || []).includes(id)) return "queued";
    }
    return isBuilt(id) ? "built" : "unbuilt";
  }

  // Why a built paper has no row, in plain words: per location, its own note or each file's reason.
  const HOLDS = { doc: "documentation", subset: "a subset of results, not every tested pair", not_qtl: "not a QTL table",
    non_full: "not every tested pair (e.g. significant pairs only), kept because no full table is public",
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

  function followUp(id) {
    const state = ManifestStatus.evaluate({ rows: rowsOf(id), locations: data.locations.filter((l) => l.record_id === id),
      legacyNeeds: (data.needs_you || {})[id] || [], run: (data.runs || {})[id] || {}, built: isBuilt(id) });
    if ((data.records[id] || {}).qtl_review === "not_qtl") {
      state.label = "Human decision: not QTL data";
      state.kinds = []; state.reasons = []; state.human = false; state.complete = false;
    }
    if ((data.records[id] || {}).qtl_review === "needs_recheck") {
      state.label = "Needs recheck";
      state.kinds = []; state.reasons = []; state.human = false; state.complete = false;
    }
    return state;
  }

  const needsYou = (id) => followUp(id).reasons.filter((r) => ManifestStatus.human.has(r.kind));

  function needsBadge(id, exceptLabel = "") {
    const state = followUp(id);
    return state.kinds.filter((kind) => ManifestStatus.labels[kind] !== exceptLabel).map((kind) => `<span class="badge ${["network", "software"].includes(kind) ? "mf-d-bad" : "mf-d-warn"}" title="${esc(state.reasons.filter((r) => r.kind === kind).map((r) => r.why).join("\n"))}">${esc(ManifestStatus.labels[kind])}</span>`).join(" ");
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

  const unique = (values) => [...new Set(values.filter(Boolean))];
  const locsOf = (id) => data.locations.filter((l) => l.record_id === id);
  function aiStatus(id) {
    const rows = rowsOf(id), locs = locsOf(id);
    if (rows.some((r) => r.model || (r.evidence || {}).agent) || locs.some((l) => l.model || l.tokens > 0)) return "Yes";
    if (locs.some((l) => /LLM step skipped|AI step skipped/.test([l.note, l.run_note].join(" ")))) return "No (AI skipped)";
    return isBuilt(id) || (data.runs || {})[id] ? "Unknown (legacy build)" : "No";
  }

  function dataResult(id) {
    const state = followUp(id), st = stateOf(id), run = (data.runs || {})[id] || {};
    if (!state.good.length && st === "queued") state.label = "Queued — build not started";
    else if (!state.good.length && st === "building") state.label = "Building — result not yet available";
    else if (!state.good.length && run.outcome === "stopped") state.label = "Stopped";
    else if (!state.good.length && run.outcome === "usage_limit") state.label = "Waiting for AI quota";
    else if (!state.good.length && st === "exists") state.label = "In reference manifest";
    if ((data.records[id] || {}).qtl_review === "not_qtl") {
      state.label = "Human decision: not QTL data";
      state.kinds = []; state.reasons = []; state.human = false; state.complete = false;
    }
    return state;
  }

  function humanBottlenecks(state) {
    const actions = {
      request: "Open the access page, submit the application, then paste the access instructions into the conversation.",
      browser: "Open the source page in your browser, download the required files, and provide their server paths in the conversation.",
      limit: "Review the reported file size and download limit before approving a larger download or providing the files.",
    };
    return state.reasons.filter(r => ManifestStatus.human.has(r.kind)).map(r => {
      const urls = unique([r.what, ...(String(r.why || "").match(/https?:\/\/[^\s<>"']+/g) || [])]
        .map(u => String(u || "").replace(/[),.;\]]+$/, ""))).filter(u => safeUrl(u) !== "#" && /^https?:\/\//i.test(u));
      return `<div class="rec-sub mf-why"><strong>Needs you — Bottleneck:</strong> ${esc(r.why)}
        <div><strong>Your next step:</strong> ${esc(actions[r.kind])}</div>
        ${urls.map(u => `<div><a href="${esc(safeUrl(u))}" target="_blank" rel="noopener">${esc(u)}</a></div>`).join("")}
        ${!urls.length && r.kind === "request" ? '<div>Access request URL was not recorded; it still needs verification.</div>' : ""}</div>`;
    }).join("");
  }

  function resultOf(id) {
    const state = dataResult(id);
    const cls = state.complete ? "mf-d-ok" : ["network", "software"].includes(state.kinds[0]) ? "mf-d-bad" : "mf-d-warn";
    return `<span class="badge ${cls}">${esc(state.label)}</span>
      ${state.good.length ? `<div class="rec-sub">${state.good.length}/${state.rows.length} rows downloaded or processed</div>` : ""}
      ${state.partial ? '<div class="rec-sub">Partial statistics only; full data not ready</div>' : ""}
      ${needsBadge(id, state.label)}${humanBottlenecks(state)}`;
  }

  const columnFilters = {};
  const COLUMNS = [
    ["index", "Paper index"], ["paper", "Paper / URL"], ["journal", "Journal"], ["data_url", "Data URL"],
    ["qtl", "QTL type(s)"], ["context", "Context"], ["sample_size", "Sample size"], ["build", "Build status"], ["result", "Data result / success"],
    ["files", "Files / rows"], ["review", "Review"], ["human", "Human decision"], ["date", "Last build"], ["actions", "Actions"],
  ];
  function dataUrls(id) {
    const p = data.records[id] || {};
    return unique([...(p.download_url || "").split(/;\s*/), ...locsOf(id).map((l) => l.url), ...rowsOf(id).map((r) => r.source_url)])
      .filter((u) => safeUrl(u) !== "#");
  }
  function buildStatusOf(id) {
    const st = stateOf(id), run = (data.runs || {})[id];
    return st === "excluded" ? "Excluded by human" : st === "building" ? `Building${job.no_llm ? " (AI skipped)" : " with AI"}…`
      : st === "queued" ? "Queued" : run?.outcome === "failed" ? "Build failed"
      : run?.outcome === "stopped" ? "Build stopped" : run?.outcome === "usage_limit" ? "Waiting for AI quota"
      : isBuilt(id) || run ? "Build completed" : "Not built";
  }
  function matchesColumns(id) {
    const p = data.records[id], rows = rowsOf(id), run = (data.runs || {})[id];
    const text = {
      index: `#${id} ${p.record_code || ""}`,
      paper: [p.publication_title, p.first_author, p.year, p.journal, p.publication_url, p.doi].join(" "),
      journal: p.journal || p.preprint_server || "",
      paper_url: [p.publication_url, p.doi, p.doi && `https://doi.org/${p.doi}`].join(" "),
      data_url: dataUrls(id).join(" "),
      qtl: [p.qtl_type, ...rows.map((r) => r.qtl_type)].join(" "),
      context: [p.qtl_context, ...rows.map((r) => [r.biocontext, r.level_1_context, r.level_2_context].join(" "))].join(" "),
      sample_size: rows.map(r => [r.sample_size, r.biocontext].filter(Boolean).join(" ")).join(" "),
      files: `${rows.filter((r) => r.evidence.data?.status === "processed").length} processed ${rows.filter((r) => r.evidence.data?.status === "as_is").length} downloaded ${rows.length} candidate rows ` + rows.map((r) => r.file_path).join(" "),
      date: run?.at ? `${run.at} ${new Date(run.at).toLocaleString()}` : "",
    };
    return Object.entries(columnFilters).every(([key, value]) => {
      if (!value.trim()) return true;
      if (key === "build") {
        if (value === "ai_yes") return aiStatus(id) === "Yes";
        if (value === "ai_no") return aiStatus(id).startsWith("No");
        if (value === "ai_unknown") return aiStatus(id).startsWith("Unknown");
        return buildStatusOf(id) === value;
      }
      if (key === "result") return value.startsWith("reason:") ? followUp(id).kinds.includes(value.slice(7)) : dataResult(id).label === value;
      if (key === "human") return (p.qtl_review || "") === (value === "unreviewed" ? "" : value);
      if (key === "review") return value === "none" ? !rows.length : rows.some((r) => r.review === value);
      return String(text[key] || "").toLowerCase().includes(value.trim().toLowerCase());
    });
  }
  function columnFilterHtml(key, label) {
    if (key === "actions") return '<button type="button" class="rec-btn mf-clear-columns">Clear filters</button>';
    const options = key === "build" ? [
      ["ai_yes", "AI build: Yes"], ["ai_no", "AI build: No"], ["ai_unknown", "AI build: Unknown"],
      ...["Excluded by human", "Not built", "Build completed", "Building with AI…", "Building (AI skipped)…", "Queued", "Build failed", "Build stopped", "Waiting for AI quota"].map((s) => [s, s]),
    ] : key === "result" ? [
      ...["Needs recheck", "Human decision: not QTL data", "Processed — review pending", "Downloaded / processed", "Partial success", "Build failed", "Pending", "Stopped", "Waiting for AI quota", "No usable data found", "In reference manifest", "Not attempted"].map((s) => [s, s]),
      ...ManifestStatus.priority.map((kind) => [`reason:${kind}`, ManifestStatus.labels[kind]]),
    ]
      : key === "human" ? [["unreviewed", "Not reviewed"], ["qtl", "QTL data"], ["not_qtl", "Not QTL data"], ["needs_recheck", "Needs recheck"]]
      : key === "review" ? [["pending", "To review"], ["approved", "Has approved rows"], ["rejected", "Has rejected rows"], ["none", "No candidate rows"]] : null;
    const attrs = `data-column-filter="${key}" aria-label="Filter ${esc(label)}"`;
    if (options) return `<select class="dq-input mf-column-filter" ${attrs}><option value="">All</option>${options.map(([v, t]) => `<option value="${esc(v)}" ${columnFilters[key] === v ? "selected" : ""}>${esc(t)}</option>`).join("")}</select>`;
    return `<input type="search" class="dq-input mf-column-filter" ${attrs} value="${esc(columnFilters[key] || "")}" placeholder="${key === "date" ? "e.g. 2026-11-02" : "Filter…"}">`;
  }

  function sampleSizeHtml(rows) {
    const values = unique(rows.filter(r => r.sample_size !== null && r.sample_size !== undefined && String(r.sample_size).trim() !== "").map(r => `${r.sample_size}${r.biocontext ? ` (${r.biocontext})` : ""}`));
    if (!values.length) return '<span class="muted">Unknown</span>';
    const render = items => items.map(value => `<div>${esc(value)}</div>`).join("");
    return render(values.slice(0, 3)) + (values.length > 3 ? `<details><summary>${values.length - 3} more contexts</summary>${render(values.slice(3))}</details>` : "");
  }

  function headHtml(id) {
    const p = data.records[id] || {}, st = stateOf(id), rows = rowsOf(id), run = (data.runs || {})[id];
    const busy = st === "building" || st === "queued";
    const attempted = isBuilt(id) || !!run;
    const toCheck = rows.filter((r) => r.review === "pending").length;
    const status = buildStatusOf(id);
    const build = st === "excluded" ? '<span class="rec-sub">Excluded from future builds</span>' : st === "exists" ? "" : `<button type="button" class="rec-btn mf-build" data-record="${id}" ${busy ? "disabled" : ""}>${attempted ? "Rebuild" : "Build manifest"}</button>`;
    const check = attempted || p.curator_note || p.qtl_review ? `<button type="button" class="rec-btn mf-open" data-record="${id}" aria-expanded="${openPapers.has(id)}">${openPapers.has(id) ? "Close" : "Details"}${toCheck ? ` (${toCheck})` : ""}</button>` : "";
    const urls = dataUrls(id);
    const link = (u) => `<a href="${esc(safeUrl(u))}" target="_blank" rel="noopener">${esc(u)}</a>`;
    const urlList = urls.slice(0, 2).map(link).join("<br>") + (urls.length > 2 ? `<details><summary>${urls.length - 2} more URLs</summary>${urls.slice(2).map(link).join("<br>")}</details>` : "");
    const types = unique([p.qtl_type, ...rows.map((r) => r.qtl_type)]).join("; ");
    const contexts = unique([p.qtl_context, ...rows.map((r) => r.biocontext)]);
    const processed = rows.filter((r) => r.evidence.data?.status === "processed").length;
    const downloaded = rows.filter((r) => r.evidence.data?.status === "as_is").length;
    const paperUrl = p.publication_url || (p.doi ? `https://doi.org/${p.doi}` : "");
    const reviewDraft = reviewDrafts.get(id) || { value: p.qtl_review || "", note: p.qtl_review_note || "" };
    let cellIndex = 0;
    return `<td><strong>#${id}</strong>${p.record_code ? `<div class="rec-sub">${esc(p.record_code)}</div>` : ""}</td>
      <td class="mf-paper-cell"><strong>${paperUrl ? `<a class="mf-paper-title" href="${esc(safeUrl(paperUrl))}" target="_blank" rel="noopener">${esc(p.publication_title || `Record #${id}`)}</a>` : esc(p.publication_title || `Record #${id}`)}</strong><div class="rec-sub">${esc([p.first_author, p.year].filter(Boolean).join(" · "))}</div>${paperUrl ? `<div class="rec-sub mf-url-cell">${link(paperUrl)}</div>` : ""}</td>
      <td>${esc(p.journal || p.preprint_server) || "—"}</td>
      <td class="mf-url-cell">${urlList || '<span class="muted">No data URL recorded</span>'}</td>
      <td>${esc(types) || "—"}</td><td class="mf-context-cell">${esc(contexts.slice(0, 3).join("; ")) || "—"}${contexts.length > 3 ? `<details><summary>${contexts.length - 3} more contexts</summary>${esc(contexts.slice(3).join("; "))}</details>` : ""}</td>
      <td>${sampleSizeHtml(rows)}</td>
      <td><strong>${esc(status)}</strong><div class="rec-sub">AI build: ${esc(aiStatus(id))}</div>${busy && (job.active || [job.current]).some(c => c?.record_id === id) ? `<div class="rec-sub">${esc((job.active || [job.current]).find(c => c?.record_id === id)?.step || "starting")}</div>` : ""}${lastOutcome(id)}</td>
      <td>${resultOf(id)}</td>
      <td><span title="Unique registered source files present on the server; excludes generated outputs and unregistered manually copied files">Downloaded: ${data.downloaded_files?.[id] ?? "—"}</span><br>
        <span title="Manifest rows ready to use without conversion">No need to process: ${downloaded}</span><br>
        <span title="Manifest rows still requiring conversion, including failed processing attempts">Need to process: ${rows.filter(r => !["processed", "as_is"].includes(r.evidence.data?.status) && (r.status === "needs_processing" || r.evidence.data?.status === "processing_failed" || (r.issues || []).some(i => (data.processing || []).includes(i)))).length}</span><br>
        <span title="Manifest rows whose conversion has completed">Processed: ${processed}</span>
        <div class="rec-sub" title="Planned manifest data entries; a row can represent multiple files">Manifest rows: ${rows.length}</div></td>
      <td>${rows.filter((r) => r.review === "approved").length} approved<div class="rec-sub">${toCheck} to review</div>${rows.length ? `<button type="button" class="rec-btn mf-review-rows" data-record="${id}">Review rows</button><div class="rec-sub">Check evidence, then Approve / Reject each row</div>` : '<div class="rec-sub">No manifest rows to review</div>'}${notReadyWhy(rows)}</td>
      <td class="mf-human-decision"><label class="rec-sub" for="mf-human-review-${id}">Curator classification</label>
        <select id="mf-human-review-${id}" class="dq-input mf-qtl-review" data-record="${id}">
          ${[["", "Not reviewed"], ["qtl", "QTL data"], ["not_qtl", "Not QTL data"], ["needs_recheck", "Needs recheck"]].map(([value, label]) => `<option value="${value}" ${reviewDraft.value === value ? "selected" : ""}>${label}</option>`).join("")}</select>
        <textarea class="dq-input mf-qtl-review-note" data-record="${id}" aria-label="Review reason for paper ${id}" placeholder="Reason / evidence URL…">${esc(reviewDraft.note)}</textarea>
        <button type="button" class="rec-btn mf-save-qtl-review" data-record="${id}">Save decision</button>
        <div class="rec-sub">${p.qtl_reviewed_at ? `Saved ${esc(new Date(p.qtl_reviewed_at).toLocaleString())}` : ""}</div></td>
      <td>${run?.at ? esc(new Date(run.at).toLocaleString()) : "—"}</td>
      <td><div class="mf-paper-btns">${build}${check}</div></td>`.replace(/<td(?=\s|>)/g, () => `<td data-label="${esc(COLUMNS[cellIndex++][1])}"`);
  }

  function bodyHtml(id) {
    const rows = rowsOf(id), locs = data.locations.filter((l) => l.record_id === id);
    const state = followUp(id);
    const p = data.records[id] || {};
    const requests = state.reasons.filter((r) => r.kind === "request");
    const requestUrls = ManifestStatus.requestLinks(state.reasons, p.publication_url || (p.doi ? `https://doi.org/${p.doi}` : ""));
    const curatorSection = p.curator_note ? `<section class="mf-curator-note"><h3>Curator notes / recheck evidence</h3><p>${esc(p.curator_note)}</p></section>` : "";
    const requestSection = requests.length ? `<section class="mf-request-links"><h3>Access request — pages and instructions</h3>
      <p>Open the recorded application page or author-request instructions:</p>
      ${requestUrls.length ? `<ul>${requestUrls.map((r) => `<li><a class="rec-url" href="${esc(safeUrl(r.url))}" target="_blank" rel="noopener">${esc(r.url)}</a>${r.fallback ? '<div class="rec-sub">No separate application URL recorded. Check this paper’s Data availability statement for author contact instructions.</div>' : ""}</li>`).join("")}</ul>` : '<p>No application URL was recorded. The report calls for contacting the authors; verify their contact details in the paper.</p>'}
      <ul>${requests.map((r) => `<li>${esc(r.why)}</li>`).join("")}</ul></section>` : "";
    const why = state.reasons.length
      ? `<div class="mf-norows"><strong>Follow-up by cause</strong><ul>${state.reasons.map((r) => `<li><strong>${esc(ManifestStatus.labels[r.kind])}</strong>: <code>${esc(r.what)}</code> — ${esc(r.why)}</li>`).join("")}</ul>
        <span class="rec-sub">Network and tool failures do not establish an access application requirement. Review the cause before retrying.</span></div>`
      : state.good.length ? '<p class="rec-sub">Data downloaded and processed. Review candidate rows below; failed alternate URLs remain in source history.</p>'
      : `<div class="mf-norows mf-nothing"><strong>No usable data found.</strong> Inspect source reports below.</div>`;
    return `<div class="mf-paper-body"><p><a class="rec-btn" href="/api/manifest/${id}/logs" target="_blank" rel="noopener">View / download build logs (.md)</a></p>${buildChatHtml(id)}${curatorSection}${requestSection}${why}${locs.map((l) => locationHtml(l, !rows.length)).join("")}
      ${rows.length ? `<div class="table-wrap"><table class="rec-table"><thead><tr><th>Status</th><th>Dataset</th><th>Context</th><th>Pop.</th><th>N</th><th>Build</th><th>File</th><th>Data</th><th>Issues</th><th></th></tr></thead>
        <tbody>${rows.map(rowHtml).join("")}</tbody></table></div>` : ""}</div>`;
  }

  const paperHtml = (id) => `<tbody class="mf-paper" data-record="${id}"><tr class="mf-paper-head">${headHtml(id)}</tr>
    ${openPapers.has(id) ? `<tr><td colspan="${COLUMNS.length}">${bodyHtml(id)}</td></tr>` : ""}</tbody>`;

  function shownIds() {
    const q = filter.value.trim().toLowerCase();
    return Object.keys(data.records).map(Number).filter((id) => {
      const p = data.records[id], rows = rowsOf(id), st = stateOf(id);
      if (!matchesColumns(id)) return false;
      if (human.checked && !/\bhuman\b|homo sapiens/i.test(p.species || "")) return false;
      if (show.value === "not_qtl" && p.qtl_review !== "not_qtl") return false;
      if (show.value === "sumstats" && p.data_kind !== "open_sumstats") return false;
      if (show.value === "exists" && st !== "exists") return false;
      if (show.value === "unbuilt" && st !== "unbuilt") return false;
      if (show.value === "built" && st !== "built") return false;
      if (show.value === "needs" && !followUp(id).human) return false;
      if (ManifestStatus.priority.includes(show.value) && !followUp(id).kinds.includes(show.value)) return false;
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
  // Builds only take human records (manifest.eligible), so the others are never queued.
  const toBuild = (ids) => ids.filter((id) => stateOf(id) === "unbuilt" && !data.records[id].not_eligible);

  function updateCount(ids) {
    const n = toBuild(ids).length;
    buildAll.textContent = n ? `Build all shown (${n})` : "Build all shown";
    buildAll.disabled = !n;
    const builtN = ids.filter(isBuilt).length, toCheck = ids.reduce((n, id) => n + rowsOf(id).filter((r) => r.review === "pending").length, 0);
    const handN = ids.filter((id) => needsYou(id).length).length;
    count.textContent = `${plural(ids.length, "paper")} · ${builtN} built · ${handN} need an access / browser / budget action · ${plural(toCheck, "row")} to check`;
    const mf = (data.files || {})["qtl_manifest.tsv"];
    $("mf-file-rows").textContent = mf ? `· ${plural(mf.rows, "row")}` : "";
  }

  // Column widths the user dragged on the overview header, as % of the table, kept in this browser.
  // The table always spans exactly the page width: dragging a column edge moves width between
  // that column and its right-hand neighbour, so the total stays 100%.
  const WIDTHS_KEY = "dq.manifest.columnWidthsPct";
  const MIN_COL_PX = 48;
  let columnWidths = {};
  try { columnWidths = JSON.parse(localStorage.getItem(WIDTHS_KEY) || "{}") || {}; } catch { columnWidths = {}; }
  try { localStorage.removeItem("dq.manifest.columnWidths"); } catch { /* old px widths, no longer used */ }
  function saveColumnWidths() {
    try { localStorage.setItem(WIDTHS_KEY, JSON.stringify(columnWidths)); } catch { /* storage unavailable */ }
  }
  function applyColumnWidths() {
    const table = body.querySelector?.(".mf-overview");
    if (!table) return;
    const cols = table.querySelectorAll("colgroup col");
    COLUMNS.forEach(([key], i) => {
      const pct = columnWidths[key];
      if (cols[i]) cols[i].style.width = pct ? `${pct}%` : "";
    });
  }
  let resizing = null;
  body.addEventListener("pointerdown", (e) => {
    const handle = e.target.closest?.(".mf-col-resize");
    if (!handle) return;
    const key = handle.dataset.resize;
    const i = COLUMNS.findIndex(([k]) => k === key);
    if (i < 0 || i === COLUMNS.length - 1) return;
    e.preventDefault();
    const table = handle.closest("table");
    const tableWidth = table.getBoundingClientRect().width;
    // Snapshot every column as a percentage so dragging one pair leaves the rest in place.
    const ths = [...table.querySelectorAll("thead tr:first-child th[data-col]")];
    ths.forEach((th) => { columnWidths[th.dataset.col] = +(th.getBoundingClientRect().width / tableWidth * 100).toFixed(3); });
    const next = COLUMNS[i + 1][0];
    resizing = { key, next, startX: e.clientX, tableWidth, left: columnWidths[key], right: columnWidths[next] };
    handle.setPointerCapture?.(e.pointerId);
    document.body.classList.add("mf-col-resizing");
    applyColumnWidths();
  });
  body.addEventListener("pointermove", (e) => {
    if (!resizing) return;
    const { key, next, startX, tableWidth, left, right } = resizing;
    const minPct = MIN_COL_PX / tableWidth * 100;
    const pair = left + right;
    const newLeft = Math.min(pair - minPct, Math.max(minPct, left + (e.clientX - startX) / tableWidth * 100));
    columnWidths[key] = +newLeft.toFixed(3);
    columnWidths[next] = +(pair - newLeft).toFixed(3);
    applyColumnWidths();
  });
  const endResize = () => {
    if (!resizing) return;
    resizing = null;
    document.body.classList.remove("mf-col-resizing");
    saveColumnWidths();
  };
  body.addEventListener("pointerup", endResize);
  body.addEventListener("pointercancel", endResize);
  body.addEventListener("dblclick", (e) => {
    if (!e.target.closest?.(".mf-col-resize")) return;
    columnWidths = {};
    saveColumnWidths();
    applyColumnWidths();
  });

  function render() {
    const active = document.activeElement;
    const activeKey = active?.dataset?.columnFilter;
    const caret = activeKey && active.type === "search" ? active.selectionStart : null;
    const scroll = body.querySelector?.(".mf-overview-wrap")?.scrollLeft || 0;
    const ids = shownIds();
    updateCount(ids);
    body.innerHTML = `<p class="muted mf-overview-note">One row per paper. Column filters combine with each other and the filters above.</p><div class="table-wrap mf-overview-wrap"><table class="rec-table mf-overview" role="table" aria-label="Manifest papers"><colgroup>${COLUMNS.map(([key]) => `<col class="mf-col-${key}">`).join("")}</colgroup><thead><tr>
      ${COLUMNS.map(([key, label]) => `<th scope="col" data-col="${key}">${label}<span class="mf-col-resize" data-resize="${key}" title="Drag to resize · double-click to reset all columns" aria-hidden="true"></span></th>`).join("")}
      </tr><tr class="mf-column-filters">${COLUMNS.map(([key, label]) => `<th data-label="${esc(label)}">${columnFilterHtml(key, label)}</th>`).join("")}</tr></thead>
      ${ids.length ? ids.slice(0, limit).map(paperHtml).join("") : `<tbody><tr><td colspan="${COLUMNS.length}" class="rec-empty">No paper matches. Adjust or clear the filters above.</td></tr></tbody>`}</table></div>`
      + (ids.length > limit ? `<button type="button" class="rec-btn mf-more">Show ${Math.min(PAGE, ids.length - limit)} more (${ids.length - limit} not shown)</button>` : "");
    applyColumnWidths();
    const wrap = body.querySelector?.(".mf-overview-wrap");
    if (wrap) wrap.scrollLeft = scroll;
    if (activeKey) {
      const input = body.querySelector(`[data-column-filter="${activeKey}"]`);
      input?.focus({ preventScroll: true });
      if (input && caret !== null) input.setSelectionRange(caret, caret);
    }
  }

  // While builds run only the paper headers change; redrawing everything would wipe a half-typed correction.
  function refreshHeads() {
    const editing = document.activeElement?.closest?.(".mf-human-decision")?.closest(".mf-paper");
    body.querySelectorAll(".mf-paper").forEach((el) => {
      if (el === editing) return; // Keep the focused curator editor intact while polling.
      el.querySelector(".mf-paper-head").innerHTML = headHtml(+el.dataset.record);
    });
  }
  function refreshPaper(id) {
    const el = body.querySelector(`.mf-paper[data-record="${id}"]`);
    if (el) el.outerHTML = paperHtml(id);
  }

  async function load(redraw = true) {
    try {
      const [manifest, records] = await Promise.all([
        fetch("/api/manifest").then((r) => { if (!r.ok) throw new Error("Manifest unavailable"); return r.json(); }),
        fetch("/api/records").then((r) => r.ok ? r.json() : []).catch(() => []),
      ]);
      data = manifest;
      records.forEach((r) => { if (data.records[r.record_id]) Object.assign(data.records[r.record_id], {download_url: r.download_url, curator_note: r.curator_note, qtl_review: r.qtl_review || "", qtl_review_note: r.qtl_review_note || "", qtl_reviewed_at: r.qtl_reviewed_at || ""}); });
    } catch { /* keep what is shown */ }
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
      const active = job.active || (job.current ? [job.current] : []);
      runStatus.textContent = active.map(c => `#${c.record_id} ${c.title.slice(0, 55)} — ${c.step || "starting"}`).join(" · ")
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

  body.addEventListener("input", (e) => {
    const chatInput = e.target.closest(".mf-chat-input");
    if (chatInput) { chatDrafts.set(+chatInput.dataset.chatInput, chatInput.value); return; }
    const edit = e.target.closest(".mf-qtl-review, .mf-qtl-review-note");
    if (edit) {
      const id = +edit.dataset.record, row = edit.closest(".mf-paper-head");
      reviewDrafts.set(id, {value: row.querySelector(".mf-qtl-review").value, note: row.querySelector(".mf-qtl-review-note").value});
      return;
    }
    const input = e.target.closest("[data-column-filter]");
    if (!input) return;
    columnFilters[input.dataset.columnFilter] = input.value;
    limit = PAGE;
    render();
  });
  async function saveQtlReview(button) {
    const id = +button.dataset.record, row = button.closest(".mf-paper-head");
    const decision = row.querySelector(".mf-qtl-review").value;
    const note = row.querySelector(".mf-qtl-review-note").value;
    button.disabled = true;
    try {
      const response = await fetch(`/api/records/${id}`, {method: "PATCH", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({qtl_review: decision, qtl_review_note: note})});
      const saved = await response.json();
      if (!response.ok) throw new Error(saved.detail || "Could not save curator decision");
      if ((saved.qtl_review || "") !== decision || (saved.qtl_review_note || "") !== note) throw new Error("Server did not persist this decision. Reload and retry.");
      Object.assign(data.records[id], {qtl_review: saved.qtl_review, qtl_review_note: saved.qtl_review_note, qtl_reviewed_at: saved.qtl_reviewed_at});
      reviewDrafts.delete(id);
      refreshPaper(id); updateCount(shownIds());
    } catch (err) { alert(err.message); button.disabled = false; }
  }

  body.addEventListener("click", async (e) => {
    const reviewButton = e.target.closest(".mf-save-qtl-review");
    if (reviewButton) { saveQtlReview(reviewButton); return; }
    const chatSend = e.target.closest(".mf-chat-send");
    if (chatSend) {
      const id = +chatSend.dataset.chatSend;
      const input = body.querySelector(`[data-chat-input="${id}"]`);
      const message = (input?.value || "").trim();
      if (!message) { input?.focus(); return; }
      chatLoading.add(id); chatDrafts.set(id, message); refreshPaper(id);
      try {
        const response = await fetch(`/api/manifest/${id}/chat`, {method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({message})});
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || "Could not send message");
        chatMessages.set(id, result.messages || []); chatLoaded.add(id); chatDrafts.delete(id);
      } catch (error) { alert(error.message); }
      finally { chatLoading.delete(id); refreshPaper(id); }
      return;
    }
    const chatBuild = e.target.closest(".mf-chat-build");
    if (chatBuild) {
      const id = +chatBuild.dataset.chatBuild;
      if (!confirm(`Continue the build for #${id} using the saved conversation? This may download and process data.`)) return;
      chatBuild.disabled = true; await build([id]); return;
    }
    if (e.target.closest(".mf-clear-columns")) {
      Object.keys(columnFilters).forEach((key) => delete columnFilters[key]);
      limit = PAGE; render(); return;
    }
    const b = e.target.closest(".mf-build");
    if (b) { b.disabled = true; build([+b.dataset.record]); return; }
    const reviewRows = e.target.closest(".mf-review-rows");
    if (reviewRows) {
      const id = +reviewRows.dataset.record;
      const rows = rowsOf(id);
      const first = rows.find(r => r.review === "pending") || rows[0];
      openPapers.add(id);
      if (first) open.add(first.row_id);
      refreshPaper(id);
      if (first) body.querySelector(`.mf-paper[data-record="${id}"] .mf-detail[data-id="${first.row_id}"]`)?.scrollIntoView({behavior: "smooth", block: "start"});
      loadChat(id);
      return;
    }
    const o = e.target.closest(".mf-open");
    if (o) {
      const id = +o.dataset.record;
      if (openPapers.has(id)) openPapers.delete(id); else openPapers.add(id);
      refreshPaper(id);
      if (openPapers.has(id)) loadChat(id);
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
