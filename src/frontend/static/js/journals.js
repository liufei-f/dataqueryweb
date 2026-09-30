// Journal options are derived from loaded papers, independently of other filters.
window.DQJournals = {
  key(paper) { return (paper.journal || "").trim().toLowerCase() || "__unknown__"; },
  update(select, papers) {
    const selected = select.value;
    const journals = new Map();
    for (const paper of papers) {
      const key = this.key(paper);
      const item = journals.get(key) || { label: (paper.journal || "").trim() || "Unknown journal", count: 0 };
      item.count++;
      journals.set(key, item);
    }
    const options = [new Option(`All journals (${papers.length})`, "")];
    for (const [key, item] of [...journals].sort((a, b) => a[1].label.localeCompare(b[1].label))) {
      options.push(new Option(`${item.label} (${item.count})`, key));
    }
    select.replaceChildren(...options);
    select.value = journals.has(selected) ? selected : "";
  },
};
