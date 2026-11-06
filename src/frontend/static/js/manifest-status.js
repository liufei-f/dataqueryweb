// Interpret saved build reports without treating every failed URL as an access request.
(() => {
  const labels = {
    request: "Access request required", browser: "Browser download needed", software: "Tool / processing issue",
    network: "Network / publisher error", limit: "Download limit exceeded", waiting: "Awaiting data release",
    unsupported: "Unsupported QTL type", not_applicable: "No eligible new QTL data",
    metadata: "Metadata verification needed", unknown: "Access not confirmed",
  };
  const human = new Set(["request", "browser", "limit"]);
  const priority = ["unsupported", "not_applicable", "software", "limit", "request", "waiting", "browser", "metadata", "network", "unknown"];
  const match = (text, pattern) => pattern.test(text || "");
  function category(note) {
    // Test a positive claim within its sentence; an explicit negation is never an application requirement.
    const sentences = String(note || "").split(/(?:[.!?]\s+|;|\n)/);
    const positiveRequest = sentences.some((s) =>
      match(s, /(?:require\w*|need\w*)[^.]{0,65}(?:access application|author request|application|access request|request form)|(?:available|access)[^.]{0,55}(?:upon|on|by|following)[^.]{0,20}request|fill[^.]{0,35}(?:online )?form|submit[^.]{0,35}(?:request|application)|instructions[^.]{0,50}sent[^.]{0,25}(?:e-mail|email)/i)
      && !match(s, /not (?:a |an )?(?:confirmed|established|evidence)|unconfirmed|not verified|no (?:statement|evidence|verified)|not[^.]{0,25}(?:on.request|controlled.access)|not confirmed controlled/i));
    if (match(note, /(?:download(?: tool)?|tool)[^.]{0,100}(?:refus|reject)|(?:refus|reject)[^.]{0,90}(?:download|non-QTL|non-summary)|processing failed|validation failed/i)) return "software";
    if (match(note, /(?:exceed|over|above)[^.]{0,55}(?:limit|budget)|(?:too large|over the download limit)|cannot[^.]{0,50}within budget/i)) return "limit";
    if (positiveRequest) return "request";
    if (match(note, /saved screening|saved description/i) && match(note, /unverified|could not[^.]{0,70}verified|not[^.]{0,25}verified/i) && match(note, /timeout|timed out|HTTP 403|ConnectTimeout/i)) return "network";
    if (match(note, /(?:will be|await\w*|upon acceptance|upon publication)[^.]{0,100}(?:availab|public|publish|deposit|release)|(?:available|publicly available)[^.]{0,60}(?:with publication|upon acceptance|upon publication)/i)) return "waiting";
    if (match(note, /unsupported[^.]{0,50}(?:QTL|type)|(?:QTL type|meQTL|isoQTL|apaQTL|glyQTL|pcQTL)[^.]{0,50}unsupported|importer support[^.]{0,35}required/i)) return "unsupported";
    if (match(note, /(?:only|exclusively)[^.]{0,45}(?:cattle|non-human)|not (?:a |an )?(?:independently documented )?new QTL release|no (?:loadable new|eligible new) QTL|protocol[^.]{0,100}earlier|not (?:QTL|an association)[^.]{0,20}(?:statistics|table)/i)) return "not_applicable";
    if (match(note, /(?:genome|coordinate)[^.]{0,50}build[^.]{0,60}(?:unknown|not establish|unverified)|(?:establish|verify)[^.]{0,60}(?:coordinate build|ancestry|mapping context)|no[^.]{0,60}verified GRCh38/i)) return "metadata";
    if (match(note, /browser(?:\/session)?[^.]{0,90}(?:needed|required|inspect|interaction|download)|(?:need\w*|require\w*|manual)[^.]{0,35}browser|script-built[^.]{0,80}(?:no|not|expose)|javascript[^.]{0,70}(?:no|only)|downloads need a browser session/i)) return "browser";
    if (match(note, /HTTP\s*(?:403|404|429|5\d\d)|timeout|timed out|connection reset|ReadError|network|client challenge|reCAPTCHA|transport failure|unreachable|unexpected (?:end|EOF)/i)) return "network";
    return "unknown";
  }
  function evaluate({ rows = [], locations = [], legacyNeeds = [], run = {}, built = false } = {}) {
    rows = rows.filter((r) => r.review !== "rejected");
    const good = rows.filter((r) => ["processed", "as_is"].includes(r.evidence?.data?.status));
    const partial = good.some((r) => ["non_full", "subset"].includes(r.holds) || (r.issues || []).some((i) => /non_full_summary_statistics|not_full_summary_statistics/.test(i)));
    const candidates = [];
    const add = (what, why, kind) => {
      if (!why) return;
      const type = kind || category(why);
      if (!candidates.some((r) => r.kind === type && r.what === what && r.why === why)) candidates.push({what, why, kind: type});
    };
    // The build's final account outranks a failed source URL or an early inventory entry.
    const summaries = [...new Set(locations.map((l) => l.run_note).filter(Boolean))];
    rows.forEach((r) => { if (r.evidence?.agent?.summary) summaries.push(r.evidence.agent.summary); });
    summaries.forEach((s) => add("Processing report", s));
    locations.forEach((l) => {
      if (["manual", "error"].includes(l.status)) {
        const note = l.note || "Access conditions not established";
        const unrelated = match(note, /source sequencing, not|(?:raw|source|participant)[^.]{0,60}(?:data|sequencing)[^.]{0,70}not[^.]{0,60}(?:QTL|association|summary-statistics)/i);
        if (unrelated) return;
        // A publisher failure already resolved through a mirror is historical, not a blocker.
        const resolved = match(note, /(?:read|available|obtained|accessible|used|successfully)[^.]{0,85}(?:instead|mirror|PMC|fullTextXML|supplied|published version)|(?:full article|article|paper)[^.]{0,50}(?:read via|read through|supplied|available via)/i);
        if (!resolved) add(l.url, note);
      }
      (l.inventory || []).forEach((i) => { if (i.needs_hand) add(i.path, i.why); });
    });
    legacyNeeds.forEach((r) => {
      // Locations are handled above with source and resolution context. Keep row-only failures.
      if (!locations.some((l) => l.url === r.what)) add(r.what, r.why);
    });
    rows.forEach((r) => {
      const status = r.evidence?.data?.status;
      if (status === "too_large") add(r.source_path, r.evidence.data.note || "File exceeds download limit", "limit");
      if (["download_failed", "processing_failed"].includes(status)) add(r.source_path, r.evidence.data.note || status, status === "processing_failed" ? "software" : "network");
    });
    let reasons = candidates;
    // Full usable tables make failed alternate URLs informational. Row review is separate.
    const complete = good.length > 0 && good.length === rows.length && !partial;
    if (complete) reasons = [];
    else {
      const summaryKinds = summaries.map(category);
      const decisive = ["unsupported", "not_applicable"].find((k) => summaryKinds.includes(k));
      if (decisive && !good.length) reasons = candidates.filter((r) => r.kind === decisive);
      else {
        // Do not turn an early 'no links' report into manual work when files were found later.
        const stronger = candidates.some((r) => !["unknown", "network"].includes(r.kind));
        reasons = candidates.filter((r) => r.kind !== "unknown" || !stronger);
      }
    }
    const kinds = priority.filter((k) => reasons.some((r) => r.kind === k));
    let label = good.length ? (partial || good.length < rows.length ? "Partial success" : rows.some((r) => r.review === "pending") ? "Processed — review pending" : "Downloaded / processed")
      : kinds.length ? labels[kinds[0]] : run.outcome === "failed" ? "Processing failed" : built ? "No usable data found" : "Not attempted";
    return { label, good, rows, partial, reasons, kinds, human: kinds.some((k) => human.has(k)), complete };
  }
  function requestLinks(reasons, publicationUrl = "") {
    const urls = [...new Set(reasons.filter((r) => r.kind === "request").flatMap((r) =>
      [r.what, ...(String(r.why || "").match(/https?:\/\/[^\s<>"']+/g) || [])])
      .map((u) => String(u || "").replace(/[),.;\]]+$/, "")).filter((u) => /^https?:\/\//i.test(u)))];
    if (urls.length) return urls.map((url) => ({url, fallback: false}));
    return /^https?:\/\//i.test(publicationUrl) ? [{url: publicationUrl, fallback: true}] : [];
  }
  globalThis.ManifestStatus = { evaluate, category, requestLinks, labels, human, priority };
})();
