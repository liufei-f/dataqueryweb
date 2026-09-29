"""Saved records: data locations a curator confirmed as correct, kept in a local SQLite file.

One record is one (publication, download location) pair — the unit a curator checks on the
Find QTL data page. Saving the same pair again updates it instead of duplicating it.
The TSV/CSV export uses the column names of locusview's qtl-data-agent review table
(qtl-data-agent/skills/qtl-data-finder/SKILL.md) plus a few DataQuery-specific ones, so a
saved table can be handed to that pipeline.

The database lives at ``data/records.sqlite3`` in the project, or ``$DATAQUERY_DB``.

``saved_by`` is "human" (saved or confirmed on the page) or "auto" (saved by :func:`autosave`
because the LLM was very sure the paper releases new QTL data and named where). An automatic
save never overwrites a human one, and no save wipes an existing curator note.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dataqueryweb import rubric

if TYPE_CHECKING:
    from dataqueryweb.extract import Source
    from dataqueryweb.finder import Paper

# Stored columns, in export order. The first block matches qtl-data-agent's schema.
COLUMNS = [
    "record_id",
    "publication_title",
    "publication_url",
    "doi",
    "pmid",
    "pmcid",
    "first_author",
    "authors",
    "year",
    "journal",
    "preprint_server",
    "qtl_type",
    "qtl_context",
    "species",
    "dataset_name",
    "download_url",
    "repository",
    "access_route",
    "content",
    "file_urls",
    "extraction_note",
    "ai_verdict",
    "ai_confidence",
    "ai_reason",
    "evidence_source",
    "search_terms",
    "curator_note",
    "saved_by",
    "saved_at",
]
TEXT_FIELDS = [c for c in COLUMNS if c not in ("record_id", "saved_at")]


def db_path() -> Path:
    env = os.environ.get("DATAQUERY_DB")
    return Path(env) if env else Path(__file__).resolve().parents[3] / "data" / "records.sqlite3"


def paper_key(record: dict[str, Any]) -> str:
    """Identity of a publication: DOI, else PMID, else title (case-insensitive)."""
    pub = record.get("doi") or record.get("pmid") or record.get("publication_title") or ""
    return str(pub).strip().lower()


def url_key(url: str) -> str:
    """A URL without scheme, "www." and trailing slash, so http/https variants match."""
    url = re.sub(r"^[a-z]+://", "", str(url).strip().lower())
    return url.removeprefix("www.").rstrip("/")


def title_key(title: str) -> str:
    """A title reduced to lowercase letters and digits, for matching across sources."""
    return re.sub(r"[^a-z0-9]+", "", re.sub(r"<[^>]+>", "", str(title).lower()))


def record_key(record: dict[str, Any]) -> str:
    """Identity of a record: the publication plus the download location."""
    return f"{paper_key(record)}|{url_key(str(record.get('download_url', '')))}"


class RecordStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            cols = ", ".join(f"{c} TEXT NOT NULL DEFAULT ''" for c in TEXT_FIELDS)
            db.execute(
                "CREATE TABLE IF NOT EXISTS records ("
                "record_id INTEGER PRIMARY KEY AUTOINCREMENT, "
                f"record_key TEXT NOT NULL UNIQUE, {cols}, saved_at TEXT NOT NULL)"
            )
            # Columns added after a database was created (e.g. saved_by, ai_confidence).
            have = {r["name"] for r in db.execute("PRAGMA table_info(records)").fetchall()}
            for col in TEXT_FIELDS:
                if col not in have:
                    db.execute(f"ALTER TABLE records ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
            db.execute("UPDATE records SET saved_by = 'human' WHERE saved_by = ''")
            # Re-derive keys so rows saved under an older key rule still match (and dedupe).
            for row in db.execute("SELECT * FROM records ORDER BY record_id").fetchall():
                key = record_key(dict(row))
                if key != row["record_key"]:
                    clash = db.execute(
                        "SELECT record_id FROM records WHERE record_key = ?", [key]
                    ).fetchone()
                    if clash:
                        db.execute("DELETE FROM records WHERE record_id = ?", [row["record_id"]])
                    else:
                        db.execute(
                            "UPDATE records SET record_key = ? WHERE record_id = ?",
                            [key, row["record_id"]],
                        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """A connection that commits on success and is always closed."""
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, data: dict[str, Any], *, auto: bool = False) -> dict[str, Any]:
        """Insert, or update the record with the same publication + download URL.

        ``auto=True`` marks the record as saved by the LLM and leaves an existing human-saved
        record untouched. A human save of an auto record turns it into a human one.
        """
        values = {f: _text(data.get(f)) for f in TEXT_FIELDS}
        values["saved_by"] = "auto" if auto else "human"
        if not values["download_url"]:
            raise ValueError("download_url is required")
        if not values["first_author"] and values["authors"]:
            values["first_author"] = values["authors"].split(",")[0].strip()
        key = record_key(values)
        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as db:
            existing = db.execute("SELECT * FROM records WHERE record_key = ?", [key]).fetchone()
            if auto and existing is not None and existing["saved_by"] == "human":
                return dict(existing)
            assignments = ", ".join(
                f"{f} = excluded.{f}" for f in TEXT_FIELDS if f != "curator_note"
            )
            # Keep the curator's note unless the new save brings one.
            assignments += (
                ", curator_note = CASE WHEN excluded.curator_note != '' "
                "THEN excluded.curator_note ELSE records.curator_note END"
            )
            db.execute(
                f"INSERT INTO records (record_key, {', '.join(TEXT_FIELDS)}, saved_at) "
                f"VALUES (?, {', '.join('?' for _ in TEXT_FIELDS)}, ?) "
                f"ON CONFLICT(record_key) DO UPDATE SET {assignments}, saved_at = excluded.saved_at",
                [key, *values.values(), now],
            )
            row = db.execute("SELECT * FROM records WHERE record_key = ?", [key]).fetchone()
        return dict(row)

    def update_note(self, record_id: int, note: str) -> dict[str, Any] | None:
        return self.update(record_id, curator_note=note)

    def update(
        self, record_id: int, *, curator_note: str | None = None, confirm: bool = False
    ) -> dict[str, Any] | None:
        """Edit the note and/or mark an auto-saved record as confirmed by a person."""
        with self._connect() as db:
            if curator_note is not None:
                db.execute(
                    "UPDATE records SET curator_note = ? WHERE record_id = ?",
                    [curator_note, record_id],
                )
            if confirm:
                db.execute("UPDATE records SET saved_by = 'human' WHERE record_id = ?", [record_id])
            row = db.execute("SELECT * FROM records WHERE record_id = ?", [record_id]).fetchone()
        return dict(row) if row else None

    def delete(self, record_id: int) -> bool:
        with self._connect() as db:
            return db.execute("DELETE FROM records WHERE record_id = ?", [record_id]).rowcount > 0

    def find_paper(self, paper: Paper) -> list[dict[str, Any]]:
        """Saved records of this publication, matched by DOI (incl. a preprint's published
        DOI), PMID, title or article URL — whichever identifies it."""
        dois = {d.lower() for d in (paper.doi, paper.published_doi) if d}
        title = title_key(paper.title)
        url = url_key(paper.url) if paper.url else ""
        found = []
        for r in self.all():
            if (
                (r["doi"] and r["doi"].lower() in dois)
                or (paper.pmid and r["pmid"] == paper.pmid)
                or (len(title) > 20 and title_key(r["publication_title"]) == title)
                or (url and r["publication_url"] and url_key(r["publication_url"]) == url)
            ):
                found.append(r)
        return found

    def all(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM records ORDER BY record_id DESC").fetchall()
        return [dict(r) for r in rows]

    def export(self, fmt: str = "tsv") -> str:
        out = io.StringIO()
        writer = csv.writer(out, delimiter="\t" if fmt == "tsv" else ",", lineterminator="\n")
        writer.writerow(COLUMNS)
        for r in reversed(self.all()):  # oldest first, like an append-only review table
            writer.writerow([str(r[c]).replace("\t", " ").replace("\n", " ") for c in COLUMNS])
        return out.getvalue()


# ── automatic saving ─────────────────────────────────────────────────────────

# What an LLM-picked location must hold for the paper to be saved without a human.
AUTO_CONTENT = rubric.RESULT_CONTENT


def from_paper(paper: Paper, source: Source, search_terms: str = "") -> dict[str, Any]:
    """A record for one paper + one of its data locations (mirrors the page's toRecord)."""
    ai = paper.ai if paper.ai and "error" not in paper.ai else {}
    return {
        "publication_title": paper.title,
        "publication_url": paper.url,
        "doi": paper.doi,
        "pmid": paper.pmid,
        "pmcid": paper.pmcid,
        "authors": paper.authors,
        "year": paper.year,
        "journal": paper.journal,
        "preprint_server": paper.preprint_server,
        "qtl_type": ai.get("qtl_types") or paper.qtl_types,
        "qtl_context": ai.get("tissues_or_cells", ""),
        "species": ai.get("species", ""),
        "download_url": source.url,
        "repository": source.repository,
        "access_route": source.access,
        "content": source.ai_pick or source.kind,
        "file_urls": [f.url for f in source.files],
        "extraction_note": " — ".join(x for x in (source.ai_note, source.context) if x),
        "ai_verdict": ai.get("new_qtl_data", ""),
        "ai_confidence": f"{ai['confidence']:.2f}" if "confidence" in ai else "",
        "ai_reason": ai.get("reason", ""),
        "evidence_source": paper.evidence,
        "search_terms": search_terms,
    }


def autosave_decision(
    paper: Paper, min_confidence: float, allow_fallback: bool = False
) -> tuple[list[Source], str]:
    """(sources to save, reason). An empty list means a human should check the paper."""
    ai = paper.ai or {}
    if not ai:
        return [], "no AI verdict"
    if "error" in ai:
        return [], "AI check failed"
    if ai.get("new_qtl_data") != "yes":
        return [], "AI: no new QTL data" if ai.get("new_qtl_data") == "no" else "AI unsure"
    confidence = float(ai.get("confidence") or 0)
    if confidence < min_confidence:
        missing = rubric.unmet_labels(ai)
        detail = f" — not met: {'; '.join(missing)}" if missing else ""
        return [], f"confidence {confidence:.0%} < {min_confidence:.0%}{detail}"
    if "(fallback)" in str(ai.get("model", "")) and not allow_fallback:
        return [], "judged by the fallback model"
    picks = [s for s in paper.sources if s.ai_pick in AUTO_CONTENT]
    if not picks:
        return [], "no download location with QTL results identified"
    return picks, f"new QTL data, confidence {confidence:.0%}"


def autosave(
    store: RecordStore,
    paper: Paper,
    *,
    search_terms: str = "",
    min_confidence: float = 0.9,
    allow_fallback: bool = False,
) -> dict[str, Any]:
    """Save a paper's QTL-result locations when the LLM is sure enough; report what happened."""
    picks, reason = autosave_decision(paper, min_confidence, allow_fallback)
    saved = [store.save(from_paper(paper, s, search_terms), auto=True) for s in picks]
    return {
        "saved": [r["record_id"] for r in saved if r["saved_by"] == "auto"],
        "reason": reason,
    }


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list | tuple):
        return "; ".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()
