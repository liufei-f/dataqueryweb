"""Every LLM check, kept: the token ledger, and the verdict reused instead of asking again.

Each time the LLM reads a paper, one row is appended to ``llm_checks`` in the records
database: who the paper is, what the model concluded, which locations it picked, and the
tokens the check spent. Two things follow from that one table:

* **Usage** — total tokens, and the average per checked paper, are sums over it.
* **No second check** — a later search that finds the same paper (by DOI, a preprint's
  published DOI, PMID or title) takes the latest successful verdict from here and does not
  call the LLM at all. ``recheck`` bypasses it and appends a new row.

A check that failed (no verdict) still records its tokens, but is never reused.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from dataqueryweb.records import RecordStore, title_key, url_key

if TYPE_CHECKING:
    from dataqueryweb.finder import Paper

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_checks (
    check_id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_key TEXT NOT NULL,
    doi TEXT NOT NULL DEFAULT '',
    pmid TEXT NOT NULL DEFAULT '',
    title_key TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    ok INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    picks TEXT NOT NULL DEFAULT '[]',
    model TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    calls INTEGER NOT NULL DEFAULT 0,
    search_terms TEXT NOT NULL DEFAULT '',
    checked_at TEXT NOT NULL
)"""


def _paper_key(paper: Paper) -> str:
    return str(paper.doi or paper.pmid or paper.title or "").strip().lower()


class CheckStore:
    """``llm_checks`` in the same SQLite file as the saved records."""

    def __init__(self, records: RecordStore) -> None:
        self.records = records
        with records._connect() as db:
            db.execute(_SCHEMA)
            have = {r["name"] for r in db.execute("PRAGMA table_info(llm_checks)").fetchall()}
            if "paper" not in have:  # the paper as shown: metadata and every location
                db.execute("ALTER TABLE llm_checks ADD COLUMN paper TEXT NOT NULL DEFAULT ''")
            db.execute("CREATE INDEX IF NOT EXISTS llm_checks_doi ON llm_checks (doi)")
            db.execute("CREATE INDEX IF NOT EXISTS llm_checks_pmid ON llm_checks (pmid)")
            db.execute("CREATE INDEX IF NOT EXISTS llm_checks_title ON llm_checks (title_key)")

    def record(self, paper: Paper, verdict: dict[str, Any], search_terms: str = "") -> int:
        """Append one check. ``verdict`` is the paper's AI result after it was applied."""
        usage = verdict.get("usage") or {}
        clean = {k: v for k, v in verdict.items() if k not in ("autosave", "cached")}
        picks = [
            {k: v for k, v in asdict(s).items()} for s in paper.sources if s.ai_pick
        ]
        with self.records._connect() as db:
            cur = db.execute(
                "INSERT INTO llm_checks (paper_key, doi, pmid, title_key, title, ok, verdict, "
                "picks, model, prompt_tokens, completion_tokens, total_tokens, calls, "
                "search_terms, checked_at, paper) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    _paper_key(paper), (paper.doi or "").lower(), paper.pmid or "",
                    title_key(paper.title), paper.title, int("error" not in verdict),
                    json.dumps(clean, ensure_ascii=False),
                    json.dumps(picks, ensure_ascii=False), str(verdict.get("model") or ""),
                    int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0),
                    int(usage.get("total_tokens") or 0), int(usage.get("calls") or 0),
                    search_terms, datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
                    json.dumps({k: v for k, v in paper.to_dict().items() if k != "ai"},
                               ensure_ascii=False),
                ],
            )
            return int(cur.lastrowid or 0)

    def find(self, paper: Paper) -> dict[str, Any] | None:
        """The latest successful check of this paper, matched like ``find_paper``."""
        dois = sorted({d.lower() for d in (paper.doi, paper.published_doi) if d})
        title = title_key(paper.title)
        clauses, args = [], []
        if dois:
            clauses.append(f"doi IN ({', '.join('?' for _ in dois)})")
            args += dois
        if paper.pmid:
            clauses.append("pmid = ?")
            args.append(paper.pmid)
        if len(title) > 20:
            clauses.append("title_key = ?")
            args.append(title)
        if not clauses:
            return None
        with self.records._connect() as db:
            row = db.execute(
                f"SELECT * FROM llm_checks WHERE ok = 1 AND ({' OR '.join(clauses)}) "
                "ORDER BY check_id DESC LIMIT 1",
                args,
            ).fetchone()
        return dict(row) if row else None

    def latest(self) -> list[dict[str, Any]]:
        """Each paper's most recent check, newest first, for the AI-checked papers page."""
        with self.records._connect() as db:
            rows = db.execute(
                "SELECT * FROM llm_checks WHERE check_id IN "
                "(SELECT MAX(check_id) FROM llm_checks GROUP BY paper_key) "
                "ORDER BY check_id DESC"
            ).fetchall()
        out = []
        for r in rows:
            try:
                verdict = json.loads(r["verdict"])
            except ValueError:
                verdict = {}
            try:
                paper = json.loads(r["paper"] or "{}")
            except ValueError:
                paper = {}
            if not paper.get("sources"):  # checked before the paper itself was kept
                paper = {"title": r["title"], "doi": r["doi"], "pmid": r["pmid"],
                         "sources": json.loads(r["picks"] or "[]")}
            out.append({k: r[k] for k in ("check_id", "checked_at", "model", "total_tokens",
                                          "search_terms")}
                       | {"ok": bool(r["ok"]), "verdict": verdict, "paper": paper})
        return out

    def summary(self, recent: int = 50) -> dict[str, Any]:
        """Total tokens, checks and papers, the average per check, per model, latest checks."""
        with self.records._connect() as db:
            t = db.execute(
                "SELECT COUNT(*) AS checks, COUNT(DISTINCT paper_key) AS papers, "
                "COALESCE(SUM(total_tokens), 0) AS total, COALESCE(SUM(prompt_tokens), 0) AS prompt, "
                "COALESCE(SUM(completion_tokens), 0) AS completion, "
                "COALESCE(SUM(calls), 0) AS calls, COALESCE(SUM(1 - ok), 0) AS failed "
                "FROM llm_checks"
            ).fetchone()
            models = db.execute(
                "SELECT model, COUNT(*) AS checks, SUM(total_tokens) AS total FROM llm_checks "
                "GROUP BY model ORDER BY total DESC"
            ).fetchall()
            rows = db.execute(
                "SELECT check_id, title, doi, pmid, ok, model, total_tokens, checked_at, verdict "
                "FROM llm_checks ORDER BY check_id DESC LIMIT ?",
                [recent],
            ).fetchall()
        checks = int(t["checks"])
        latest = []
        for r in rows:
            try:
                verdict = json.loads(r["verdict"])
            except ValueError:
                verdict = {}
            latest.append({k: r[k] for k in ("check_id", "title", "doi", "pmid", "model",
                                             "total_tokens", "checked_at")}
                          | {"ok": bool(r["ok"]),
                             "new_qtl_data": verdict.get("new_qtl_data", ""),
                             "error": verdict.get("error", "")})
        return {
            "checks": checks,
            "papers": int(t["papers"]),
            "failed": int(t["failed"]),
            "calls": int(t["calls"]),
            "total_tokens": int(t["total"]),
            "prompt_tokens": int(t["prompt"]),
            "completion_tokens": int(t["completion"]),
            "avg_tokens_per_check": round(int(t["total"]) / checks) if checks else 0,
            "by_model": [dict(m) for m in models],
            "recent": latest,
        }


def reuse(paper: Paper, row: dict[str, Any]) -> dict[str, Any]:
    """Put a stored check back on ``paper``: its verdict, and the locations it picked.

    A picked location this inspection also found gets the pick; one it did not find (e.g.
    from the earlier web search) is restored from the stored copy. Returns the verdict.
    """
    from dataqueryweb.extract import DataFile, Source

    verdict = json.loads(row["verdict"])
    by_url = {url_key(s.url): s for s in paper.sources}
    for pick in json.loads(row["picks"] or "[]"):
        source = by_url.get(url_key(pick.get("url", "")))
        if source is None:
            fields = {k: v for k, v in pick.items() if k in Source.__dataclass_fields__}
            fields["files"] = [DataFile(**f) for f in pick.get("files") or []
                               if isinstance(f, dict)]
            source = Source(**fields)
            paper.sources.insert(0, source)
        source.ai_pick, source.ai_note = pick.get("ai_pick", ""), pick.get("ai_note", "")
    paper.sources.sort(key=lambda s: not s.ai_pick)
    verdict["cached"] = {"check_id": row["check_id"], "checked_at": row["checked_at"],
                         "tokens_then": row["total_tokens"]}
    verdict["usage"] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                        "calls": 0}
    paper.ai = verdict
    return verdict
