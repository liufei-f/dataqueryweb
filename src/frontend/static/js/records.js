// Saved records page: list, filter, annotate and delete rows saved from Find QTL data.
(() => {
  const body = document.getElementById("rec-body");
  const filter = document.getElementById("rec-filter");
  const count = document.getElementById("rec-count");
  const by = document.getElementById("rec-by");
  let rows = [];

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const safeUrl = (u) => /^(https?|ftp):\/\//i.test(u || "") ? u : "#";
  const BADGE = { open: "open", controlled: "controlled", request: "request" };

  function row(r) {
    const pubUrl = r.doi ? `https://doi.org/${r.doi}` : r.publication_url;
    const ids = [r.doi && `doi:${r.doi}`, r.pmid && `PMID ${r.pmid}`, r.pmcid, r.preprint_server && `${r.preprint_server} preprint`]
      .filter(Boolean).map(esc).join(" · ");
    const files = r.file_urls ? r.file_urls.split("; ").length : 0;
    return `
      <tr data-id="${r.record_id}"${r.saved_by === "auto" ? ' class="rec-is-auto"' : ""}>
        <td class="muted">${r.record_id}</td>
        <td>
          <a class="rec-title" href="${esc(safeUrl(pubUrl))}" target="_blank" rel="noopener">${esc(r.publication_title || r.doi)}</a>
          <div class="rec-sub">${esc([r.first_author && `${r.first_author} et al.`, r.journal, r.year].filter(Boolean).join(" · "))}</div>
          <div class="rec-sub">${ids}</div>
        </td>
        <td>${esc(r.qtl_type)}</td>
        <td>${esc(r.qtl_context)}${r.species ? `<div class="rec-sub">${esc(r.species)}</div>` : ""}</td>
        <td>
          ${String(r.download_url || "").split("; ").filter(Boolean).map((u) =>
            `<a class="rec-url" href="${esc(safeUrl(u))}" target="_blank" rel="noopener">${esc(u)}</a>`).join("<br>")}
          <div class="rec-sub">${esc([r.repository, r.content, files && `${files} file${files > 1 ? "s" : ""}`].filter(Boolean).join(" · "))}</div>
        </td>
        <td>${(r.access_route ? r.access_route.split("; ") : ["unknown"]).map((a) =>
            `<span class="badge badge-${BADGE[a] || "unknown"}">${esc(a)}</span>`).join(" ")}
            <div class="rec-sub">saved ${esc(r.saved_at.slice(0, 10))}</div>
            ${r.saved_by === "auto" ? `<span class="rec-auto" title="${esc(r.ai_reason)}">🤖 auto-saved${r.ai_confidence ? ` · ${Math.round(r.ai_confidence * 100)}%` : ""}</span>
            <button type="button" class="rec-confirm" title="The AI got this right: mark it as checked by you">Confirm</button>` : ""}</td>
        <td><textarea class="rec-note" placeholder="Add a note…" aria-label="Curator note">${esc(r.curator_note)}</textarea></td>
        <td><button type="button" class="rec-del" title="Delete this record" aria-label="Delete">&times;</button></td>
      </tr>`;
  }

  function render() {
    const q = filter.value.trim().toLowerCase();
    const shown = rows
      .filter((r) => !by.value || r.saved_by === by.value)
      .filter((r) => !q || Object.values(r).join(" ").toLowerCase().includes(q));
    const autos = rows.filter((r) => r.saved_by === "auto").length;
    count.textContent = (shown.length !== rows.length ? `${shown.length} of ${rows.length} records` : `${rows.length} record${rows.length === 1 ? "" : "s"}`)
      + (autos ? ` · ${autos} auto-saved to check` : "");
    body.innerHTML = shown.length ? shown.map(row).join("") : `<tr><td colspan="8" class="rec-empty">${rows.length
      ? "No record matches the filter."
      : 'No saved records yet. On <a href="/">Find QTL data</a>, click <strong>Save</strong> on a row that is correct.'}</td></tr>`;
  }

  async function load() {
    try {
      rows = await (await fetch("/api/records")).json();
    } catch {
      rows = [];
    }
    render();
  }

  filter.addEventListener("input", render);
  by.addEventListener("change", render);

  body.addEventListener("click", async (e) => {
    const ok = e.target.closest(".rec-confirm");
    if (ok) {
      const id = +ok.closest("tr").dataset.id;
      const resp = await fetch(`/api/records/${id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ saved_by: "human" }),
      });
      if (resp.ok) { const updated = await resp.json(); rows = rows.map((x) => (x.record_id === id ? updated : x)); render(); }
      return;
    }
    const btn = e.target.closest(".rec-del");
    if (!btn) return;
    const tr = btn.closest("tr"), id = +tr.dataset.id;
    const r = rows.find((x) => x.record_id === id);
    if (!confirm(`Delete record #${id}?\n${r ? r.publication_title : ""}`)) return;
    const resp = await fetch(`/api/records/${id}`, { method: "DELETE" });
    if (resp.ok || resp.status === 404) { rows = rows.filter((x) => x.record_id !== id); render(); }
  });

  // Notes save when the field loses focus.
  body.addEventListener("change", async (e) => {
    const box = e.target.closest(".rec-note");
    if (!box) return;
    const id = +box.closest("tr").dataset.id;
    const resp = await fetch(`/api/records/${id}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ curator_note: box.value }),
    });
    if (resp.ok) {
      const updated = await resp.json();
      rows = rows.map((x) => (x.record_id === id ? updated : x));
      box.classList.add("ok");
      setTimeout(() => box.classList.remove("ok"), 1200);
    }
  });

  // /records?q=… (linked from Find QTL data) opens with that filter applied.
  const params = new URLSearchParams(location.search);
  filter.value = params.get("q") || "";
  by.value = params.get("by") || "";
  load();
})();
