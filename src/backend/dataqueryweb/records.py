"""Saved records: data locations a curator confirmed as correct, kept in a local SQLite file.

One record is one publication. Every download location saved for it — by a curator on the
Find QTL data page, or by :func:`autosave` — is merged into that record's location list
(``locations``, flattened into ``download_url`` etc. joined by "; "), so a paper never shows
up as several rows. Saving a location already in the list updates it.
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
import shutil
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
    """Identity of a record: the publication. Its locations live inside it."""
    return paper_key(record)


# The per-location fields. A record keeps its locations whole in ``locations`` (JSON) and
# flattens them into these columns, "; "-joined in saved order, for display and export.
LOCATION_FIELDS = ("download_url", "repository", "access_route", "content", "file_urls",
                   "extraction_note")


def _split(value: str) -> list[str]:
    return [v.strip() for v in str(value or "").split("; ") if v.strip()]


def _location(values: dict[str, Any]) -> dict[str, str]:
    return {f: str(values.get(f) or "") for f in LOCATION_FIELDS}


def _locations_of(row: dict[str, Any]) -> list[dict[str, str]]:
    """A stored row's locations; a row from before the list existed is one location."""
    try:
        locs = json.loads(row.get("locations") or "[]")
    except ValueError:
        locs = []
    if isinstance(locs, list) and locs:
        return [_location(loc) for loc in locs if isinstance(loc, dict)]
    return [_location(row)] if row.get("download_url") else []


def _merge_locations(old: list[dict[str, str]], new: list[dict[str, str]]) -> list[dict[str, str]]:
    """``old`` then ``new``; a location already present (same URL) is updated in place."""
    out = list(old)
    for loc in new:
        at = next((i for i, o in enumerate(out)
                   if url_key(o["download_url"]) == url_key(loc["download_url"])), None)
        if at is None:
            out.append(loc)
        else:
            out[at] = {f: loc[f] or out[at][f] for f in LOCATION_FIELDS}
    return out


def _flatten(locs: list[dict[str, str]]) -> dict[str, str]:
    def joined(field: str, sep: str = "; ") -> str:
        seen: list[str] = []
        for loc in locs:
            for v in (_split(loc[field]) if sep == "; " else [loc[field].strip()]):
                if v and v.lower() not in (s.lower() for s in seen):
                    seen.append(v)
        return sep.join(seen)

    flat = {f: joined(f) for f in LOCATION_FIELDS if f != "extraction_note"}
    flat["download_url"] = "; ".join(loc["download_url"] for loc in locs)
    flat["extraction_note"] = joined("extraction_note", " | ")
    return flat


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
            if "locations" not in have:
                db.execute("ALTER TABLE records ADD COLUMN locations TEXT NOT NULL DEFAULT ''")
            db.execute("UPDATE records SET saved_by = 'human' WHERE saved_by = ''")
        self._migrate_keys()

    def _migrate_keys(self) -> None:
        """Re-derive keys, merging rows of one publication into its oldest record.

        Records used to be one per (publication, location), so a paper could fill several
        rows. Their locations join the oldest row; the merged record is human-saved if any
        part was, and keeps every curator note. The database is copied aside first.
        """
        with self._connect() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM records ORDER BY record_id")]
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(record_key(row), []).append(row)
        stale = {k: g for k, g in groups.items()
                 if len(g) > 1 or g[0]["record_key"] != k or not g[0].get("locations")}
        if not stale:
            return
        if any(len(g) > 1 for g in stale.values()):
            stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            shutil.copy2(self.path, self.path.with_name(f"{self.path.name}.bak-{stamp}"))
        with self._connect() as db:
            for key, group in stale.items():
                keep, rest = group[0], group[1:]
                locs: list[dict[str, str]] = []
                for row in group:
                    locs = _merge_locations(locs, _locations_of(row))
                notes = [r["curator_note"] for r in group if r["curator_note"]]
                values = {f: next((r[f] for r in group if r[f]), "") for f in TEXT_FIELDS}
                values |= _flatten(locs)
                values["curator_note"] = " | ".join(dict.fromkeys(notes))
                values["saved_by"] = "human" if any(r["saved_by"] == "human" for r in group) else "auto"
                for row in rest:
                    db.execute("DELETE FROM records WHERE record_id = ?", [row["record_id"]])
                db.execute(
                    f"UPDATE records SET record_key = ?, locations = ?, "
                    f"{', '.join(f'{f} = ?' for f in TEXT_FIELDS)} WHERE record_id = ?",
                    [key, json.dumps(locs, ensure_ascii=False), *values.values(), keep["record_id"]],
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
        """Add this location to the publication's record, creating the record if needed.

        ``auto=True`` marks the record as saved by the LLM and leaves an existing human-saved
        record untouched. A human save of an auto record turns it into a human one. Fields
        about the publication take the newer non-empty value; the curator note is kept
        unless the save brings one.
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
            if existing is None:
                locs = [_location(values)]
                values |= _flatten(locs)
                db.execute(
                    f"INSERT INTO records (record_key, {', '.join(TEXT_FIELDS)}, saved_at, locations) "
                    f"VALUES (?, {', '.join('?' for _ in TEXT_FIELDS)}, ?, ?)",
                    [key, *values.values(), now, json.dumps(locs, ensure_ascii=False)],
                )
            else:
                old = dict(existing)
                if auto and old["saved_by"] == "human":
                    return old
                locs = _merge_locations(_locations_of(old), [_location(values)])
                merged = {f: values[f] or old[f] for f in TEXT_FIELDS}
                merged |= _flatten(locs)
                merged["curator_note"] = values["curator_note"] or old["curator_note"]
                merged["saved_by"] = values["saved_by"]
                db.execute(
                    f"UPDATE records SET {', '.join(f'{f} = ?' for f in TEXT_FIELDS)}, "
                    "saved_at = ?, locations = ? WHERE record_key = ?",
                    [*merged.values(), now, json.dumps(locs, ensure_ascii=False), key],
                )
            row = db.execute("SELECT * FROM records WHERE record_key = ?", [key]).fetchone()
        return dict(row)

    def remove_location(self, record_id: int, url: str) -> dict[str, Any] | None:
        """Drop one location; the record goes with its last one. None if nothing matched."""
        with self._connect() as db:
            row = db.execute("SELECT * FROM records WHERE record_id = ?", [record_id]).fetchone()
            if row is None:
                return None
            locs = _locations_of(dict(row))
            left = [loc for loc in locs if url_key(loc["download_url"]) != url_key(url)]
            if len(left) == len(locs):
                return None
            if not left:
                db.execute("DELETE FROM records WHERE record_id = ?", [record_id])
                return {"deleted": True}
            flat = _flatten(left)
            db.execute(
                f"UPDATE records SET {', '.join(f'{f} = ?' for f in flat)}, locations = ? "
                "WHERE record_id = ?",
                [*flat.values(), json.dumps(left, ensure_ascii=False), record_id],
            )
            updated = db.execute("SELECT * FROM records WHERE record_id = ?", [record_id]).fetchone()
        return dict(updated)

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
    auto = [r for r in saved if r["saved_by"] == "auto"]
    return {
        # Every location of one paper lands in the same record, so this is one id at most.
        "saved": list(dict.fromkeys(r["record_id"] for r in auto)),
        "locations": len(auto),
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
