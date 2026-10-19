"""Candidate ``qtl_manifest.tsv`` rows for saved records, with the evidence behind each field.

For one saved record (a paper the AI check confirmed releases new QTL data):

1. **Retrieve** (probe.py) — list the files behind each saved download location and read the
   header and first rows of the tables, a bounded number per location.
2. **Rules** (manifest_rules.py) — map headers to manifest columns, decide what each table
   holds, group per-chromosome files, name what varies between sibling files.
3. **Paper** (finder.py) — read the publication again for the facts files do not carry.
4. **LLM** — one call per record: tissue/cell context, sample size, ancestry, genome build
   and dataset name for each candidate table, each with a verbatim quote.
5. **Verify and assemble** — a quote counts only if it is found in the text the model was
   given; every field keeps its value, source, quote and whether it was verified. Rules
   then give the row a status (ready / needs_review / needs_processing / not_loadable).

Rows land in the records database (``manifest_rows``; one ``manifest_locations`` line per
download location, including those a person has to open) for review on ``/manifest`` and
export in the exact column order of LocusCompare's manifest.

Run from the command line::

    dataqueryweb-manifest run --limit 20        # the next 20 unprocessed records
    dataqueryweb-manifest run --record 20 28    # these records, again
    dataqueryweb-manifest export --out candidates.tsv
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import csv
import io
import json
import os
import re
import sqlite3
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from dataqueryweb import fetch, finder, llm, probe, records
from dataqueryweb import manifest_rules as rules

MAX_PROBES = int(os.environ.get("MANIFEST_MAX_PROBES") or 14)  # files opened per location
MAX_CANDIDATES = 120  # tables kept per record
LLM_BATCH = 30  # tables per LLM call; a long list makes one answer slow
LLM_TIMEOUT = 420.0
TEXT_CHARS = int(os.environ.get("MANIFEST_TEXT_CHARS") or 24000)  # paper text per record
HUMAN_RE = re.compile(r"human|homo sapiens", re.I)
# Paragraphs that tend to state cohort size, ancestry, genome build and contexts.
FACT_RE = re.compile(
    r"sample size|individuals|donors|participants|subjects|ancestr|european|asian|african"
    r"|hispanic|ethnic|cohort|GRCh3[78]|hg19|hg38|genome build|reference genome|genotyp"
    r"|imput|cell types?|tissues?|QTL mapping|summary statistics|availab", re.I)


@dataclass
class Candidate:
    """One table that may become a manifest row."""

    location: str  # the saved download URL
    landing: str
    glob: str  # path inside the location, chromosomes globbed
    files: list[probe.RemoteFile]
    token: str = ""
    group: str = ""
    member: str = ""  # zip/tar member or sheet name
    table: probe.Table | None = None
    roles: rules.Roles = field(default_factory=rules.Roles)
    holds: str = "unknown_scope"
    why: str = ""
    type_hint: str = ""
    issues: list[str] = field(default_factory=list)
    header_from: str = ""  # the sibling whose header this one uses
    note: str = ""
    access: str = ""
    cid: str = ""
    needs_hand: bool = False  # the file could not be read here: a person has to fetch or open it

    @property
    def path(self) -> str:
        return f"{self.glob}::{self.member}" if self.member else self.glob

    @property
    def size(self) -> int | None:
        sizes = [f.size for f in self.files if f.size is not None]
        return sum(sizes) if sizes else None


# ── 1–2. retrieve and apply the file rules ───────────────────────────────────


def retrieve(
    record: dict[str, Any], *, include_supplements: bool = False
) -> tuple[list[Candidate], list[dict[str, Any]], dict[str, str]]:
    """(candidate tables, one report per download location, repository texts)."""
    candidates: list[Candidate] = []
    reports: list[dict[str, Any]] = []
    texts: dict[str, str] = {}
    listed: set[str] = set()
    seen_listings: set[frozenset[str]] = set()
    locations = records._locations_of(record)
    # Repositories before single files: a file saved next to its own record is listed once.
    locations.sort(key=lambda loc: bool(rules.TABLE_EXT_RE.search(probe._name_of(loc["download_url"]))))
    with probe.client() as http:
        for loc in locations:
            url, content, access = loc["download_url"], loc["content"], loc["access_route"]
            report = {"url": url, "content": content, "access": access, "status": "", "resolver": "",
                      "landing": url, "note": "", "n_files": 0, "inventory": []}
            reports.append(report)
            if records.url_key(url.split("?")[0]) in listed:
                report.update(status="duplicate", note="this file is listed under the record's repository location")
            elif access in ("controlled", "request"):
                report.update(status="manual", note=f"{access} access: apply or ask the authors")
            elif "qtl_summary_statistics" not in content and not include_supplements:
                if "browser" in content:
                    report.update(status="manual", note="results browser: no file to open")
                else:
                    report.update(status="skipped", note="supplementary table: a selected subset by rule, "
                                                         "not opened (--include-supplements opens it)")
            else:
                found = _retrieve_location(http, loc, report, texts, seen_listings)
                for f in found:
                    listed.update(records.url_key(x.url.split("?")[0]) for x in f.files)
                candidates += found
    candidates.sort(key=lambda c: (-rules.name_score(c.path), -(c.size or 0)))
    del candidates[MAX_CANDIDATES:]
    for i, c in enumerate(candidates, 1):
        c.cid = f"U{i}"
    return candidates, reports, texts


def _retrieve_location(
    http: httpx.Client, loc: dict[str, str], report: dict[str, Any], texts: dict[str, str],
    seen_listings: set[frozenset[str]],
) -> list[Candidate]:
    listing = probe.list_location(http, loc["download_url"])
    landing = listing.landing or loc["download_url"]
    report.update(status=listing.status, resolver=listing.resolver, landing=landing,
                  note=listing.note, n_files=len(listing.files))
    if listing.status not in ("listed", "direct"):
        return []
    files = frozenset(f.url for f in listing.files)
    if files in seen_listings:  # a file link saved next to its repository record lists the same record
        report.update(status="duplicate", note="same files as another location of this record")
        return []
    seen_listings.add(files)
    for name, text in listing.texts.items():
        texts[f"{name} ({landing})"] = text
    kinds = {id(f): rules.file_kind(f.path or f.name, f.size) for f in listing.files}
    for f in [f for f in listing.files if kinds[id(f)] == "doc"][:4]:
        text = probe.fetch_doc(http, f)
        if text:
            texts[f"file {f.path or f.name} ({landing})"] = text
    units = rules.make_units([f for f in listing.files if kinds[id(f)] == "table"])
    # A name that marks a selected subset settles it: such files are listed, not opened.
    subsets = [u for u in units if rules.subset_by_name(u.glob)]
    units = [u for u in units if not rules.subset_by_name(u.glob)]
    probes = {u.uid: probe.probe_file(http, u.files[0]) for u in rules.probe_order(units, MAX_PROBES)}

    out: list[Candidate] = []
    for u in units:
        base = {"location": loc["download_url"], "landing": landing, "glob": u.glob, "files": u.files,
                "token": u.token, "group": u.group, "access": loc["access_route"]}
        p = probes.get(u.uid)
        if p is None:  # not opened: assume the header of an opened sibling of the same group
            sibling = next((s for s in units if s.group == u.group and s.uid in probes
                            and len(probes[s.uid].tables) == 1), None)
            if sibling:
                out.append(Candidate(**base, table=copy.deepcopy(probes[sibling.uid].tables[0]),
                                     header_from=sibling.glob, issues=["header_inferred_from_sibling"]))
                out[-1].table.rows = []  # the sibling's values are not this file's
            else:
                out.append(Candidate(**base, issues=["header_not_read"], note="not opened (probe budget)"))
        elif not p.tables:
            note = p.note or (f"archive with {len(p.members)} members, none a readable table" if p.members else "")
            out.append(Candidate(**base, issues=["header_not_read"], note=note,
                                 needs_hand=p.kind in ("error", "unreadable")))
        else:
            for t in p.tables:
                c = Candidate(**base, table=t, member=t.name, note=p.note)
                if t.fmt != "text" or t.name:
                    c.issues.append("format_needs_conversion")
                out.append(c)
    _apply_file_rules(out)
    report["inventory"] = [
        {"path": c.path, "n_files": len(c.files), "size": c.size, "holds": c.holds, "why": c.why or c.note,
         "needs_hand": c.needs_hand}
        for c in out][:200]
    report["inventory"] += [
        {"path": u.glob, "n_files": len(u.files), "size": u.size, "holds": "subset",
         "why": rules.subset_by_name(u.glob)} for u in subsets][:200]
    report["inventory"] += [{"path": f.path or f.name, "n_files": 1, "size": f.size, "holds": kinds[id(f)], "why": ""}
                            for f in listing.files if kinds[id(f)] != "table"][:60]
    # Rows are made for tables that may hold full results. A table that is not QTL results,
    # a subset, or an unread file whose name does not suggest QTLs stays in the inventory.
    return [c for c in out if c.holds in ("all_pairs", "unknown_scope")
            and (c.table is not None or rules.name_score(c.path) >= 2)]


def _apply_file_rules(cands: list[Candidate]) -> None:
    """Borrow headers for headerless tables, then roles, scope and type hint for each."""
    headed = [c for c in cands if c.table is not None and c.table.has_header and not c.header_from]
    for c in cands:
        t = c.table
        if t is None:
            c.holds, c.why = "unknown_scope", c.note or "header not read"
            c.type_hint = rules.qtl_type_from_text(c.path)
            continue
        has_header = t.has_header
        if not has_header:
            c.issues.append("no_header")
            donor = next((d for d in headed if len(d.table.columns) == len(t.columns)), None)
            if donor:  # same width: use its names, checked below against this file's values
                t.columns, c.header_from, has_header = list(donor.table.columns), donor.path, True
        c.roles = rules.detect_roles(t.columns, t.rows) if has_header else rules.Roles()
        c.holds, c.why = rules.table_holds(c.roles, c.path, c.size, has_header=has_header, fmt=t.fmt)
        c.type_hint = rules.qtl_type_from_text(c.path)
    by_group: dict[tuple[str, str], set[tuple[str, ...]]] = {}
    for c in cands:
        if c.table is not None and not c.header_from:
            by_group.setdefault((c.location, c.group), set()).add(tuple(c.table.columns))
    for c in cands:
        if "header_inferred_from_sibling" in c.issues and len(by_group.get((c.location, c.group), ())) > 1:
            c.issues.append("mixed_headers")


# ── 3. the paper ─────────────────────────────────────────────────────────────


async def read_paper(record: dict[str, Any]) -> finder.Paper | None:
    ref = record.get("doi") or record.get("pmid") or record.get("publication_url") or ""
    if not ref:
        return None
    try:
        async with finder.client() as http:
            return await asyncio.wait_for(finder.inspect_ref(http, ref), timeout=150)
    except (httpx.HTTPError, TimeoutError, ValueError):
        return None


def paper_excerpt(paper: finder.Paper | None, budget: int = TEXT_CHARS) -> str:
    """The paragraphs densest in cohort, ancestry, build and context words, in paper order."""
    if paper is None:
        return ""
    paragraphs = list(paper.availability) + list(paper.paragraphs)
    ranked = sorted(range(len(paragraphs)), key=lambda i: -len(FACT_RE.findall(paragraphs[i])))
    chosen, used = set(), 0
    for i in ranked:
        if not FACT_RE.search(paragraphs[i]) or used + len(paragraphs[i]) > budget:
            continue
        chosen.add(i)
        used += len(paragraphs[i]) + 1
    return "\n".join(paragraphs[i] for i in sorted(chosen))


# ── 4. the LLM ───────────────────────────────────────────────────────────────

SYSTEM = """You are a data curator filling LocusCompare's QTL manifest: one row per table of \
QTL summary statistics. A program has already listed the files, read every header and mapped \
the columns. For each listed unit (U1, U2, …) you supply only what the files cannot say, from \
the paper text and repository texts you are given. A human will check your answers against \
your quotes.

Fields:
- qtl_type: eQTL, sQTL, caQTL, pQTL, mQTL, or another molecular QTL type the paper uses. One per unit.
- holds: "all_pairs" if the table lists every tested variant–phenotype pair (nominal results); \
"subset" if only significant, lead, conditional or fine-mapped results; "not_qtl" if it is not \
a table of QTL associations (expression, covariates, GWAS, colocalisation, SMR, …); else "unknown".
- tissue_or_cell: the tissue or cell type in plain words. condition: stimulation, treatment, \
time point or disease state; "" if none.
- level_1_context: the broad tissue group. Choose from: {level_1}. "Blood" covers whole blood, \
PBMC, every immune cell type, plasma and LCLs. Use another tissue name only if none fits.
- level_2_context: the cell type, sub-region, or cell type + condition, written with \
underscores (CD14_Mono, macrophage_IFNg, Pancreatic_islet); "" for a bulk tissue with nothing finer.
- biocontext: the label that identifies this unit's context within the dataset. Use the \
file's own label (shown as label="…") when it names the context; otherwise level_2_context; \
otherwise the tissue. Underscores, no spaces.
- context_quote: a verbatim snippet showing that this unit is that context — from the paper, \
a repository text, or the file path itself.
- sample_size: the number of genotyped individuals (donors) whose data entered the QTL \
mapping of THIS unit's context — not cells, not samples before QC, not a GWAS cohort. If the \
texts give one number for the whole study, put it in study.sample_size and leave the unit's null.
- population: EUR, EAS, AFR, AMR or SAS; ALL for a multi-ancestry cohort; "" if the texts do not say.
- genome_build: GRCh38 or GRCh37 for the coordinates in the files; "unknown" if not stated.
- dataset: the name of the resource or cohort if the paper or repository names one \
(like "OneK1K", "CIMA", "GTEx_v10"); "" otherwise.

Quotes: every "quote" is copied VERBATIM (at most one sentence) from the supplied texts. \
Never invent or paraphrase a quote. If you know a value but have no sentence for it, give the \
value with "" as quote — it will be flagged for the human. If you do not know, use null or "".

Reply with one JSON object and nothing else:
{{"study": {{"dataset": {{"value": "", "quote": ""}}, "genome_build": {{"value": "GRCh38|GRCh37|unknown", "quote": ""}}, \
"population": {{"value": "", "quote": ""}}, "sample_size": {{"value": null, "quote": ""}}}}, \
"units": [{{"id": "U1", "qtl_type": "", "holds": "all_pairs|subset|not_qtl|unknown", \
"tissue_or_cell": "", "condition": "", "level_1_context": "", "level_2_context": "", \
"biocontext": "", "context_quote": "", "sample_size": {{"value": null, "quote": ""}}, \
"population": {{"value": "", "quote": ""}}, "note": ""}}]}}"""


def build_prompt(
    record: dict[str, Any], paper: finder.Paper | None, candidates: list[Candidate],
    texts: dict[str, str], reports: list[dict[str, Any]],
) -> str:
    abstract = llm._strip_tags(paper.abstract)[:2500] if paper else ""
    parts = [
        f"Title: {record.get('publication_title', '')}",
        f"Journal: {record.get('journal', '')} {record.get('year', '')}   DOI: {record.get('doi', '')}",
        f"Earlier AI check — QTL types: {record.get('qtl_type', '')}; context: {record.get('qtl_context', '')}; "
        f"species: {record.get('species', '')}",
        f"\nAbstract: {abstract or '(not available)'}",
        f"\nPAPER TEXT ({'full text' if paper and paper.full_text else 'full text NOT available'}, "
        f"paragraphs about cohort, ancestry, genome build and contexts):\n{paper_excerpt(paper) or '(none)'}",
    ]
    for name, text in texts.items():
        parts.append(f"\nREPOSITORY TEXT — {name}:\n{text[:5000]}")
    parts.append("\nUNITS (candidate QTL tables; the program's reading of each):")
    shown: dict[str, str] = {}
    for c in candidates:
        size = f"{c.size / 1e6:.1f} MB" if c.size else "size unknown"
        parts.append(
            f"{c.cid} | {c.path} | {len(c.files)} file{'s' if len(c.files) > 1 else ''}, {size} | "
            f'label="{c.token}" | program: {c.holds} ({c.why}){f"; type from path: {c.type_hint}" if c.type_hint else ""}')
        if c.header_from and c.header_from in shown:
            parts.append(f"    columns: same as {shown[c.header_from]}")
        elif c.table is not None:
            shown.setdefault(c.glob, c.cid)
            parts.append(f"    columns: {', '.join(c.table.columns[:40])}")
            if c.table.preamble:
                parts.append(f"    title lines: {c.table.preamble[:300]}")
            if c.table.rows:
                parts.append(f"    first row: {' | '.join(c.table.rows[0][:40])}")
    others = [i["path"] for r in reports for i in r["inventory"] if i["holds"] in ("not_qtl", "doc", "other")]
    if others:
        parts.append("\nOTHER FILES at the same locations: " + "; ".join(others[:40]))
    return "\n".join(parts)


def evidence_texts(
    paper: finder.Paper | None, candidates: list[Candidate], texts: dict[str, str]
) -> dict[str, str]:
    """Every text the model was shown, by name — a quote must be found in one of them."""
    out: dict[str, str] = {}
    if paper is not None:
        body = "\n".join([paper.title, paper.abstract, *paper.availability, *paper.paragraphs])
        out["paper full text" if paper.full_text else "paper abstract / page"] = body
    out.update(texts)
    out["file path"] = "\n".join(f"{c.path} {c.token}" for c in candidates)
    out["file header"] = "\n".join(
        " ".join([*c.table.columns, c.table.preamble, *(" ".join(r) for r in c.table.rows)])
        for c in candidates if c.table is not None)
    return out


# ── 5. verify and assemble ───────────────────────────────────────────────────


def _item(raw: Any) -> tuple[Any, str]:
    """(value, quote) of a {"value": …, "quote": …} answer, tolerating a bare value."""
    if isinstance(raw, dict):
        return raw.get("value"), str(raw.get("quote") or "").strip()
    return raw, ""


def _int(value: Any) -> int | None:
    try:
        n = int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def ev(value: Any, source: str, quote: str = "", verified: bool | None = None, note: str = "") -> dict[str, Any]:
    return {"value": value, "source": source, "quote": quote, "verified": verified, "note": note}


def assemble(
    record: dict[str, Any], paper: finder.Paper | None, candidates: list[Candidate],
    facts: dict[str, Any] | None, texts: dict[str, str], target: rules.Target,
) -> list[dict[str, Any]]:
    """Manifest rows (with evidence, issues and status) for one record's candidates."""
    proof = evidence_texts(paper, candidates, texts)
    study = (facts or {}).get("study") or {}
    by_id = {str(u.get("id", "")).strip().upper(): u for u in (facts or {}).get("units") or []
             if isinstance(u, dict)}
    record_types = [rules.canonical_qtl_type(t) for t in records._split(record.get("qtl_type", ""))]
    doi = (record.get("doi") or "").lower()

    def checked(value: Any, quote: str, kind: str = "LLM") -> dict[str, Any]:
        where = rules.verify_quote(quote, proof) if quote else ""
        return ev(value, where or f"{kind} (no supporting text found)", quote, bool(where))

    # Study-level facts, shared by every row of the record.
    name, name_quote = _item(study.get("dataset"))
    name = rules.label(name or "")
    dataset_ev = checked(name, name_quote)
    if not (name and dataset_ev["verified"] and rules.LABEL_OK.match(name)):
        name = rules.default_dataset(record.get("first_author", ""), record.get("year", ""))
        dataset_ev = ev(name, "rule: <first author>_<year>", note="no resource name with a verified quote")
    if doi and target.publications.get(doi):  # keep the name the manifest already uses
        name = target.publications[doi]
        dataset_ev = ev(name, "reference manifest", note="the reference manifest already lists this publication under this dataset")
    study_n, study_n_quote = _item(study.get("sample_size"))
    study_pop, study_pop_quote = _item(study.get("population"))
    build, build_quote = _item(study.get("genome_build"))

    rows = []
    for c in candidates:
        u = by_id.get(c.cid, {})
        issues = list(c.issues)
        evidence: dict[str, Any] = {"dataset": dataset_ev}
        row = dict.fromkeys(rules.MANIFEST_COLUMNS, "")
        row.update(dataset=name, source_project_id=name, use_in_lc2="FALSE",
                   url=c.landing, publication=f"https://doi.org/{doi}" if doi else record.get("publication_url", ""))

        # QTL type: the path if it names one, else the LLM, else the record's only type.
        llm_type = rules.canonical_qtl_type(u.get("qtl_type") or "")
        qtl_type = c.type_hint or llm_type or (record_types[0] if len(record_types) == 1 else "")
        evidence["qtl_type"] = ev(qtl_type, "file path" if c.type_hint else "LLM" if llm_type else "saved record")
        row["qtl_type"] = qtl_type
        if not qtl_type:
            issues.append("qtl_type_unknown")
        elif qtl_type not in rules.MANIFEST_QTL_TYPES:
            issues.append("qtl_type_not_in_manifest")

        # Scope: the rule's reading stands; the LLM can only narrow it.
        holds, why = c.holds, c.why
        llm_holds = str(u.get("holds") or "")
        if llm_holds == "subset" and holds != "subset":
            holds, why = "subset", f"LLM: a selected subset ({str(u.get('note') or '')[:120]})"
        elif llm_holds == "not_qtl":
            holds, why = "subset", f"LLM: not a table of QTL associations ({str(u.get('note') or '')[:120]})"
        evidence["holds"] = ev(holds, "rule" if holds == c.holds else "LLM", note=why
                               + (f"; LLM agrees: {llm_holds}" if llm_holds == "all_pairs" else ""))
        if holds == "subset":
            issues.append("not_full_summary_statistics")
        elif holds == "unknown_scope":
            issues.append("scope_unknown")

        # Columns, from the header.
        t = c.table
        seen_values = bool(t and t.rows)
        if t is not None:
            for role, col in rules.ROLE_COLUMN.items():
                if role in c.roles.columns:
                    row[col] = c.roles.columns[role]
                    evidence[col] = ev(row[col], f"file header ({c.header_from or c.path})",
                                       note=c.roles.how.get(role, ""))
            missing = [r for r in rules.REQUIRED_ROLES if r not in c.roles.columns]
            # Without sample values a variant id cannot be seen to hold chrom:pos:ref:alt.
            unseen = not seen_values and "variant_id" in c.roles.columns
            lost = [r for r in missing if r not in c.roles.derivable
                    and not (unseen and r in ("chrom", "position", "ref", "alt"))]
            if lost and "no_header" not in issues:
                issues.append("required_column_missing")
                evidence["missing_columns"] = ev(", ".join(lost), "rule", note="not found in the header")
            if missing and len(lost) < len(missing) and "columns_need_deriving" not in c.roles.issues:
                issues.append("columns_need_deriving")
            issues += c.roles.issues
            phen = c.roles.columns.get("phenotype_id")
            values = [r[t.columns.index(phen)] for r in t.rows if phen and t.columns.index(phen) < len(r)]
            mode, problem = rules.gene_id_mode(values, qtl_type)
            row["gene_id_mode"] = mode
            if problem and phen:
                issues.append(problem)
            row["delimiter"] = {"\t": "\\t", ",": ",", ";": ";", " ": " "}.get(t.delimiter, "\\t")
            evidence["table"] = {"columns": t.columns, "rows": t.rows, "has_header": t.has_header,
                                 "fmt": t.fmt, "preamble": t.preamble, "n_rows": t.n_rows,
                                 "header_from": c.header_from, "note": c.note}
        else:
            evidence["table"] = {"columns": [], "rows": [], "note": c.note}
        row["file_path"] = f"{fetch.folder_name(name)}/{fetch.safe_rel(c.glob)}"
        evidence["file_path"] = ev(row["file_path"], "rule: <dataset>/<path in the repository>",
                                   note="relative to the data folder; not downloaded")

        # Context.
        l1 = rules.label(u.get("level_1_context") or "")
        l2 = rules.label(u.get("level_2_context") or "")
        bio = rules.label(u.get("biocontext") or "")
        if not bio or rules.NOT_CONTEXT_RE.search(bio):  # a file-kind label is not a context
            bio = l2 or l1
        row.update(biocontext=bio, level_1_context=l1, level_2_context=l2)
        described = ", ".join(x for x in (str(u.get("tissue_or_cell") or ""), str(u.get("condition") or "")) if x)
        evidence["context"] = checked(described or bio, str(u.get("context_quote") or ""))
        if not facts or c.cid not in by_id:
            issues.append("llm_facts_missing")
        else:
            if not evidence["context"]["verified"] or not l1:
                issues.append("context_unverified")
            if l1 and l1 not in target.level_1:
                issues.append("new_level_1_context")
            if any(x and not rules.LABEL_OK.match(x) for x in (bio, l1, l2)):
                issues.append("label_format")

        # Sample size: this context's from the texts, else the file's own N column, else the
        # study's, else a number in the file name.
        n, n_quote = _item(u.get("sample_size"))
        n = _int(n)
        if n and rules.verify_quote(n_quote, proof) and rules.number_in(n, n_quote):
            evidence["sample_size"] = checked(n, n_quote)
        elif c.roles.file_n:
            n = c.roles.file_n
            evidence["sample_size"] = ev(n, "file column", note=c.roles.how.get("file_n", ""), verified=True)
        elif (sn := _int(study_n)) and rules.verify_quote(study_n_quote, proof) and rules.number_in(sn, study_n_quote):
            n, evidence["sample_size"] = sn, checked(sn, study_n_quote)
            evidence["sample_size"]["note"] = "study-level number"
            issues.append("sample_size_study_level")
        elif (fn := rules.n_from_name(c.path)):
            n = fn
            evidence["sample_size"] = ev(n, "file path", quote=c.path, verified=True)
        elif n or _int(study_n):
            n = n or _int(study_n)
            evidence["sample_size"] = ev(n, "LLM (no supporting text found)", n_quote or study_n_quote, False,
                                         "the quote is missing, not in the texts, or does not contain the number")
            issues.append("sample_size_unverified")
        else:
            issues.append("sample_size_missing")
        row["sample_size"] = str(n or "")

        # Ancestry.
        pop, pop_quote = _item(u.get("population"))
        if not rules.population_code(pop or ""):
            pop, pop_quote = study_pop, study_pop_quote
        code = rules.population_code(pop or "")
        row["population"] = code
        if code:
            evidence["population"] = checked(code, pop_quote)
            if evidence["population"]["verified"] and not rules.quote_supports_population(code, pop_quote):
                evidence["population"].update(verified=False, note="the quote does not name this ancestry")
            if not evidence["population"]["verified"]:
                issues.append("population_unverified")
        else:
            issues.append("population_missing")

        # Genome build: what the files show, else what the texts say.
        seen = c.roles.build_hint or rules.build_from_text(f"{c.path} {' '.join(t.columns) if t else ''}")
        if seen:
            genome, evidence["genome_build"] = seen, ev(seen, "file path or variant ids", verified=True)
        else:
            genome = build if build in ("GRCh38", "GRCh37") else ""
            evidence["genome_build"] = checked(genome, build_quote)
            if evidence["genome_build"]["verified"] and rules.build_from_text(build_quote) != genome:
                evidence["genome_build"].update(verified=False, note="the quote does not name this build")
            if not (genome and evidence["genome_build"]["verified"]):
                issues.append("genome_build_unknown")
        if genome == "GRCh37":
            issues.append("needs_liftover")

        if paper is None or not paper.full_text:
            issues.append("paper_full_text_not_read")
        if doi and doi in target.publications:
            issues.append("publication_already_in_manifest")
        if c.access not in ("open", ""):
            issues.append("access_not_open")
        rows.append({
            "row": row, "issues": list(dict.fromkeys(i for i in issues if i in rules.ISSUES)),
            "genome_build": genome, "holds": holds, "evidence": evidence, "candidate": c,
            "llm_note": str(u.get("note") or "")[:300],
        })

    seen_bio: dict[tuple[str, str], int] = {}
    for r in rows:
        key = (r["row"]["qtl_type"], r["row"]["biocontext"])
        seen_bio[key] = seen_bio.get(key, 0) + (r["holds"] != "subset")
    for r in rows:
        key = (r["row"]["qtl_type"], r["row"]["biocontext"])
        if r["holds"] != "subset" and seen_bio[key] > 1:
            r["issues"].append("duplicate_biocontext")
        # A study-level N is the context's own when the record has a single context.
        if len({k[1] for k in seen_bio}) == 1 and "sample_size_study_level" in r["issues"]:
            r["issues"].remove("sample_size_study_level")
        r["status"] = rules.status_of(r["issues"])
    return rows


# ── storage ──────────────────────────────────────────────────────────────────

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS manifest_rows (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    row_key TEXT NOT NULL UNIQUE,
    record_id INTEGER NOT NULL,
    {", ".join(f"{c} TEXT NOT NULL DEFAULT ''" for c in rules.MANIFEST_COLUMNS)},
    status TEXT NOT NULL DEFAULT '',
    issues TEXT NOT NULL DEFAULT '[]',
    holds TEXT NOT NULL DEFAULT '',
    genome_build TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    source_path TEXT NOT NULL DEFAULT '',
    file_urls TEXT NOT NULL DEFAULT '[]',
    n_files INTEGER NOT NULL DEFAULT 0,
    file_size INTEGER,
    evidence TEXT NOT NULL DEFAULT '{{}}',
    llm_note TEXT NOT NULL DEFAULT '',
    review TEXT NOT NULL DEFAULT 'pending',
    reviewer_note TEXT NOT NULL DEFAULT '',
    edited TEXT NOT NULL DEFAULT '[]',
    rules_version TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS manifest_rows_record ON manifest_rows (record_id);
CREATE TABLE IF NOT EXISTS manifest_locations (
    loc_key TEXT PRIMARY KEY,
    record_id INTEGER NOT NULL,
    url TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    access TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    resolver TEXT NOT NULL DEFAULT '',
    landing TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    n_files INTEGER NOT NULL DEFAULT 0,
    n_rows INTEGER NOT NULL DEFAULT 0,
    inventory TEXT NOT NULL DEFAULT '[]',
    run_note TEXT NOT NULL DEFAULT '',
    tokens INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL DEFAULT '',
    run_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS manifest_locations_record ON manifest_locations (record_id);
"""
REVIEWS = ("pending", "approved", "rejected")
# Extra columns of the review export, after the manifest's own 27.
REVIEW_COLUMNS = ["row_id", "record_id", "status", "review", "issues", "genome_build", "holds",
                  "source_url", "source_path", "n_files", "file_urls", "evidence_summary",
                  "reviewer_note", "edited"]


MANIFEST_FILE = "qtl_manifest.tsv"  # approved rows, the 27 manifest columns
CANDIDATES_FILE = "qtl_manifest_candidates.tsv"  # every row, with status and evidence


def output_dir(db: Path) -> Path:
    """Where the manifest files are written: $DATAQUERY_MANIFEST_DIR, else ``manifest/`` in
    this project for the project's own database, else next to the database (tests, copies)."""
    if env := os.environ.get("DATAQUERY_MANIFEST_DIR"):
        return Path(env)
    root = Path(__file__).resolve().parents[3]
    return root / "manifest" if db.resolve().parent == root / "data" else db.parent / "manifest"


# Row issues that only a person can settle (fetch the files elsewhere, raise the limit).
HAND_ISSUES = {"download_failed", "data_too_large"}


def needs_you(rows: list[dict[str, Any]], locations: list[dict[str, Any]]) -> list[dict[str, str]]:
    """What a person has to do before this paper can give (more) manifest rows: locations
    to open by hand, files the server would not hand over, downloads that failed or were
    over the limit. Empty when nothing waits on a person."""
    out = []
    for loc in locations:
        if loc["status"] in ("manual", "error"):
            out.append({"what": loc["url"], "why": loc["note"] or loc["status"]})
        for item in loc["inventory"]:
            if item.get("needs_hand"):
                out.append({"what": item["path"], "why": item["why"]})
    for r in rows:
        if r["review"] == "rejected":
            continue
        for issue in HAND_ISSUES & set(r["issues"]):
            note = (r["evidence"].get("data") or {}).get("note", "")
            out.append({"what": r["source_path"], "why": f"{rules.ISSUES[issue]}{f' ({note})' if note else ''}"})
    return out


def row_key(record_id: int, c: Candidate) -> str:
    return f"{record_id}|{records.url_key(c.location)}|{c.path}"


class ManifestStore:
    """Candidate rows and per-location reports, in the records database."""

    def __init__(self, path: Path | None = None, out_dir: Path | None = None) -> None:
        self.path = path or records.db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.out_dir = out_dir or output_dir(self.path)
        with self._connect() as db:
            db.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def save_run(
        self, record_id: int, rows: list[dict[str, Any]], reports: list[dict[str, Any]],
        *, model: str = "", tokens: int = 0, run_note: str = "",
    ) -> None:
        """Replace this record's rows, except those a person approved, rejected or edited."""
        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as db:
            kept = {r["row_key"] for r in db.execute(
                "SELECT row_key FROM manifest_rows WHERE record_id = ? AND (review != 'pending' OR edited != '[]')",
                [record_id])}
            db.execute("DELETE FROM manifest_rows WHERE record_id = ? AND review = 'pending' AND edited = '[]'",
                       [record_id])
            per_location: dict[str, int] = {}
            for r in rows:
                c: Candidate = r["candidate"]
                per_location[c.location] = per_location.get(c.location, 0) + 1
                key = row_key(record_id, c)
                if key in kept:
                    continue
                values = {
                    "row_key": key, "record_id": record_id, **r["row"], "status": r["status"],
                    "issues": json.dumps(r["issues"]), "holds": r["holds"], "genome_build": r["genome_build"],
                    "source_url": c.location, "source_path": c.path,
                    "file_urls": json.dumps([f.url for f in c.files][:30]), "n_files": len(c.files),
                    "file_size": c.size, "evidence": json.dumps(r["evidence"], ensure_ascii=False),
                    "llm_note": r["llm_note"], "rules_version": rules.RULES_VERSION, "model": model,
                    "updated_at": now,
                }
                db.execute(f"INSERT INTO manifest_rows ({', '.join(values)}) "
                           f"VALUES ({', '.join('?' for _ in values)})", list(values.values()))
            db.execute("DELETE FROM manifest_locations WHERE record_id = ?", [record_id])
            for rep in reports:
                db.execute(
                    "INSERT OR REPLACE INTO manifest_locations (loc_key, record_id, url, content, access, status, "
                    "resolver, landing, note, n_files, n_rows, inventory, run_note, tokens, model, run_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [f"{record_id}|{records.url_key(rep['url'])}", record_id, rep["url"], rep["content"],
                     rep["access"], rep["status"], rep["resolver"], rep["landing"], rep["note"], rep["n_files"],
                     per_location.get(rep["url"], 0), json.dumps(rep["inventory"], ensure_ascii=False),
                     run_note, tokens, model, now])
        self.write_files()

    def kept(self, record_id: int) -> set[str]:
        """Row keys a rebuild leaves alone: approved, rejected or edited by a person."""
        with self._connect() as db:
            return {r[0] for r in db.execute(
                "SELECT row_key FROM manifest_rows WHERE record_id = ? AND (review != 'pending' OR edited != '[]')",
                [record_id])}

    def processed(self) -> set[int]:
        with self._connect() as db:
            return {r[0] for r in db.execute("SELECT DISTINCT record_id FROM manifest_locations")}

    def rows(self, record_id: int | None = None) -> list[dict[str, Any]]:
        where, args = ("WHERE record_id = ?", [record_id]) if record_id is not None else ("", [])
        with self._connect() as db:
            found = db.execute(f"SELECT * FROM manifest_rows {where} ORDER BY record_id DESC, row_id", args).fetchall()
        out = []
        for r in found:
            d = dict(r)
            for col in ("issues", "file_urls", "evidence", "edited"):
                d[col] = json.loads(d[col] or "null")
            out.append(d)
        return out

    def locations(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            found = db.execute("SELECT * FROM manifest_locations ORDER BY record_id DESC, url").fetchall()
        return [dict(r) | {"inventory": json.loads(r["inventory"] or "[]")} for r in found]

    def update(self, row_id: int, *, review: str | None = None, reviewer_note: str | None = None,
               fields: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """A curator's verdict, note, or corrected manifest fields (remembered as edited)."""
        if review is not None and review not in REVIEWS:
            raise ValueError(f"review must be one of {', '.join(REVIEWS)}")
        fields = {k: str(v).strip() for k, v in (fields or {}).items()}
        if unknown := [k for k in fields if k not in rules.MANIFEST_COLUMNS]:
            raise ValueError(f"not manifest columns: {', '.join(unknown)}")
        with self._connect() as db:
            row = db.execute("SELECT * FROM manifest_rows WHERE row_id = ?", [row_id]).fetchone()
            if row is None:
                return None
            sets: dict[str, Any] = dict(fields)
            if fields:
                changed = [k for k, v in fields.items() if row[k] != v]
                sets["edited"] = json.dumps(sorted({*json.loads(row["edited"]), *changed}))
            if review is not None:
                sets["review"] = review
            if reviewer_note is not None:
                sets["reviewer_note"] = reviewer_note
            if sets:
                db.execute(f"UPDATE manifest_rows SET {', '.join(f'{k} = ?' for k in sets)} WHERE row_id = ?",
                           [*sets.values(), row_id])
        self.write_files()
        return next((r for r in self.rows() if r["row_id"] == row_id), None)

    def write_files(self) -> dict[str, Any]:
        """Rewrite the two manifest files from the database, which stays the source of truth."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        out = {}
        for name, scope, fmt in ((MANIFEST_FILE, "approved", "manifest"), (CANDIDATES_FILE, "all", "review")):
            text = self.export(scope, fmt)
            tmp = self.out_dir / (name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self.out_dir / name)
            out[name] = {"path": str(self.out_dir / name), "rows": text.count("\n") - 1}
        return out

    def export(self, scope: str = "approved", fmt: str = "manifest") -> str:
        """TSV in the manifest's column order. ``scope``: approved | ready | all.

        ``fmt="review"`` adds status, issues, source files and a one-line evidence summary.
        """
        rows = self.rows()
        if scope == "approved":
            rows = [r for r in rows if r["review"] == "approved"]
        elif scope == "ready":
            rows = [r for r in rows if r["review"] == "approved"
                    or (r["status"] == "ready" and r["review"] != "rejected")]
        rows.sort(key=lambda r: (r["dataset"].lower(), r["qtl_type"], rules.natural_key(r["biocontext"])))
        columns = rules.MANIFEST_COLUMNS + (REVIEW_COLUMNS if fmt == "review" else [])
        out = io.StringIO()
        writer = csv.writer(out, delimiter="\t", lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(columns)
        for r in rows:
            r["evidence_summary"] = evidence_summary(r["evidence"])
            for col in ("issues", "file_urls", "edited"):
                r[col] = "; ".join(r[col])
            writer.writerow([str(r.get(c, "") if r.get(c) is not None else "").replace("\t", " ").replace("\n", " ")
                             for c in columns])
        return out.getvalue()


def evidence_summary(evidence: dict[str, Any]) -> str:
    parts = []
    for key in ("context", "sample_size", "population", "genome_build", "dataset", "holds"):
        e = evidence.get(key)
        if not e:
            continue
        mark = {True: "verified", False: "UNVERIFIED", None: "rule"}[e.get("verified")]
        quote = f' "{e["quote"][:160]}"' if e.get("quote") else ""
        parts.append(f"{key}={e.get('value')} [{e.get('source')}; {mark}]{quote}")
    return " || ".join(parts)


# ── running ──────────────────────────────────────────────────────────────────


def already_exists(record: dict[str, Any], target: rules.Target) -> str:
    """The dataset name under which the reference manifest already holds this publication
    (matched by DOI), or "". Such papers are not built: LocusCompare has their data."""
    return target.publications.get((record.get("doi") or "").strip().lower(), "")


def eligible(record: dict[str, Any], *, all_species: bool = False) -> str:
    """"" when the record should be processed, else why not."""
    if not all_species and not HUMAN_RE.search(record.get("species", "")):
        return f"species is {record.get('species') or 'not stated'}, not human"
    return ""


async def build_record(
    record: dict[str, Any], store: ManifestStore, judge: llm.Judge | None, *,
    target: rules.Target | None = None, include_supplements: bool = False,
    step: Callable[[str], None] | None = None, data: fetch.Options | None = None,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    """Run the whole pipeline for one saved record and store the result.

    ``step`` hears what is being done, for a progress line."""
    step = step or (lambda _: None)
    target = target or rules.load_target()
    if exists := already_exists(record, target):
        return {"record_id": record["record_id"], "rows": 0, "statuses": {}, "locations": {}, "tokens": 0,
                "model": "", "note": "", "skipped": f"already exists in the reference manifest as {exists} — skipped"}
    step("listing the files and reading their headers")
    candidates, reports, texts = await asyncio.to_thread(
        retrieve, record, include_supplements=include_supplements)
    facts, model, note = None, "", ""
    usage = llm.new_usage()
    paper = None
    if candidates:
        step(f"reading the paper ({len(candidates)} candidate table{'s' if len(candidates) != 1 else ''})")
        paper = await read_paper(record)
        if judge is not None:
            system = SYSTEM.format(level_1=", ".join(target.level_1))
            for start in range(0, len(candidates), LLM_BATCH):
                batch = candidates[start:start + LLM_BATCH]
                prompt = build_prompt(record, paper, batch, texts, reports)
                for attempt in (1, 2):  # a dropped stream or timeout is worth one more try
                    step(f"asking the AI about tables {start + 1}–{start + len(batch)} of {len(candidates)}"
                         + (" (second try)" if attempt == 2 else ""))
                    try:
                        answer, model = await judge.ask(system, prompt, usage, LLM_TIMEOUT)
                    except llm.LLMFailed as exc:
                        note = f"LLM step failed for {batch[0].cid}–{batch[-1].cid}: {exc}"
                        if "usage limit" in str(exc) or attempt == 2:
                            break
                        continue
                    note = ""
                    if facts is None:
                        facts = {"study": answer.get("study") or {}, "units": []}
                    facts["units"] += [x for x in answer.get("units") or [] if isinstance(x, dict)]
                    break
                if note:
                    break
        else:
            note = "LLM step skipped (--no-llm)"
    step("checking the quotes")
    rows = assemble(record, paper, candidates, facts, texts, target)
    if data is not None and rows:
        rid = record["record_id"]
        plan_model = await fetch.fetch_rows(
            rows, rows[0]["row"]["dataset"], judge, usage, data, skip=store.kept(rid),
            key=lambda c: row_key(rid, c), step=step, cancel=cancel)
        model = model or plan_model
    step("saving")
    store.save_run(record["record_id"], rows, reports, model=model, tokens=usage["total_tokens"], run_note=note)
    statuses: dict[str, int] = {}
    for r in rows:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    data_states: dict[str, int] = {}
    for r in rows:
        if d := r["evidence"].get("data"):
            data_states[d["status"]] = data_states.get(d["status"], 0) + 1
    return {"record_id": record["record_id"], "rows": len(rows), "statuses": statuses, "data": data_states,
            "locations": {r["url"]: r["status"] for r in reports}, "tokens": usage["total_tokens"],
            "model": model, "note": note}


def select(
    saved: list[dict[str, Any]], done: set[int], *, record_ids: list[int] | None = None,
    limit: int = 0, redo: bool = False, all_species: bool = False, target: rules.Target | None = None,
) -> list[dict[str, Any]]:
    """The records a run would process: those asked for, in the order asked; else the
    waiting ones, oldest first."""
    if record_ids:
        by_id = {r["record_id"]: r for r in saved}
        return [by_id[i] for i in dict.fromkeys(record_ids) if i in by_id]
    todo = []
    for record in sorted(saved, key=lambda r: r["record_id"]):
        if record_ids:
            if record["record_id"] in record_ids:
                todo.append(record)
        elif ((redo or record["record_id"] not in done) and not eligible(record, all_species=all_species)
              and not already_exists(record, target or rules.load_target())):
            todo.append(record)
    return todo[:limit] if limit and not record_ids else todo


def describe(record: dict[str, Any], result: dict[str, Any]) -> str:
    if result.get("skipped"):
        return f"#{record['record_id']} {record['publication_title'][:60]!r}: {result['skipped']}"
    states = ", ".join(f"{n} {s}" for s, n in sorted(result["statuses"].items())) or "no candidate table"
    where = ", ".join(sorted(set(result["locations"].values())))
    data = ", ".join(f"{n} {s.replace('_', ' ')}" for s, n in sorted(result.get("data", {}).items()) if s != "skipped")
    return (f"#{record['record_id']} {record['publication_title'][:60]!r}: {states} · locations: {where}"
            + (f" · data: {data}" if data else "")
            + f" · {result['tokens']:,} tokens{' · ' + result['note'] if result['note'] else ''}")


class Batch:
    """One run over several records. The web app keeps a single Batch going in the
    background so it outlives the page that started it; the CLI awaits one directly."""

    def __init__(self, todo: list[dict[str, Any]], store: ManifestStore, *, no_llm: bool = False,
                 include_supplements: bool = False, target: rules.Target | None = None,
                 data: fetch.Options | None = None) -> None:
        self.todo, self.store, self.no_llm, self.data = todo, store, no_llm, data
        self.cancel = threading.Event()  # tells download and processing threads to stop
        self.include_supplements, self.target = include_supplements, target
        self.state = "running"  # running | done | stopped | usage_limit
        self.done = self.failed = self.rows = self.tokens = 0
        self.current: dict[str, Any] | None = None
        self.log: list[dict[str, Any]] = []
        self.started = datetime.now().astimezone().isoformat(timespec="seconds")
        self.finished = ""
        self.task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self.state == "running"

    def add(self, todo: list[dict[str, Any]]) -> int:
        """Queue more records on a running batch; returns how many were new."""
        have = {r["record_id"] for r in self.todo[self.done:]}
        new = [r for r in todo if r["record_id"] not in have]
        self.todo += new  # the loop below walks the list by position and sees these
        return len(new)

    def queued(self) -> list[int]:
        start = self.done + (1 if self.current else 0)
        return [r["record_id"] for r in self.todo[start:]] if self.running else []

    def snapshot(self) -> dict[str, Any]:
        return {"state": self.state, "total": len(self.todo), "done": self.done, "failed": self.failed,
                "rows": self.rows, "tokens": self.tokens, "current": self.current, "log": self.log[-200:],
                "queue": self.queued(),
                "started": self.started, "finished": self.finished, "no_llm": self.no_llm,
                "include_supplements": self.include_supplements,
                "download": bool(self.data and self.data.download),
                "max_gb": self.data.max_bytes / 1e9 if self.data else 0}

    def stop(self) -> None:
        """Drop the record in progress (nothing of it is stored yet) and end the run."""
        if self.running and self.task is not None:
            self.cancel.set()
            self.task.cancel()

    async def run(self, echo: Callable[[str, bool], None] | None = None) -> None:
        judge = None if self.no_llm else llm.Judge()
        try:
            for record in self.todo:
                rid = record["record_id"]
                self.current = current = {"record_id": rid, "title": record["publication_title"], "step": "",
                                          "since": datetime.now().astimezone().isoformat(timespec="seconds")}
                try:
                    result = await build_record(record, self.store, judge, target=self.target,
                                                include_supplements=self.include_supplements,
                                                data=self.data, cancel=self.cancel,
                                                step=lambda s, c=current: c.update(step=s, step_since=datetime.now().astimezone().isoformat(timespec="seconds")))
                except Exception as exc:  # one bad record must not stop a batch
                    self.failed += 1
                    self.done += 1
                    line = f"#{rid}: failed — {type(exc).__name__}: {exc}"
                    self.log.append({"record_id": rid, "ok": False, "text": line})
                    if echo:
                        echo(line, False)
                    continue
                self.done += 1
                self.rows += result["rows"]
                self.tokens += result["tokens"]
                line = describe(record, result)
                self.log.append({"record_id": rid, "ok": not result["note"], "text": line})
                if echo:
                    echo(line, True)
                if "usage limit" in result["note"]:
                    self.state = "usage_limit"
                    return
            self.state = "done"
        except asyncio.CancelledError:
            self.state = "stopped"
        finally:
            self.current = None
            self.finished = datetime.now().astimezone().isoformat(timespec="seconds")
            if judge is not None:
                await judge.aclose()


async def run(args: argparse.Namespace) -> int:
    saved = records.RecordStore()
    store = ManifestStore(saved.path)
    target = rules.load_target(args.target)
    todo = select(saved.all(), store.processed(), record_ids=args.record, limit=args.limit,
                  redo=args.redo, all_species=args.all_species)
    print(f"{len(todo)} record(s) to process · reference manifest: {target.path or 'not found (built-in vocabulary)'}")
    data = fetch.Options(fetch.data_dir(store.path), download=not args.no_download, max_bytes=args.max_gb * 1e9)
    batch = Batch(todo, store, no_llm=args.no_llm, include_supplements=args.include_supplements, target=target,
                  data=data)
    await batch.run(lambda line, ok: print(line, file=sys.stdout if ok else sys.stderr))
    if batch.state == "usage_limit":
        print("Stopping: the LLM's usage limit is reached. Re-run later; finished records are kept.")
        return 2
    print(f"Done. {batch.tokens:,} tokens. Review at /manifest; files are in {store.out_dir}")
    return 0


def main() -> None:  # pragma: no cover
    ap = argparse.ArgumentParser(description="Build candidate qtl_manifest rows from saved records.")
    sub = ap.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="retrieve files, apply the rules and ask the LLM")
    r.add_argument("--record", type=int, nargs="*", help="record ids to (re)process; default: unprocessed ones")
    r.add_argument("--limit", type=int, default=0, help="stop after this many records")
    r.add_argument("--redo", action="store_true", help="also reprocess records already done")
    r.add_argument("--no-llm", action="store_true", help="files and rules only: no tokens, no context or sample size")
    r.add_argument("--include-supplements", action="store_true", help="also open supplementary tables")
    r.add_argument("--all-species", action="store_true", help="do not restrict to human records")
    r.add_argument("--no-download", action="store_true", help="fill the rows only; leave the files where they are")
    r.add_argument("--max-gb", type=float, default=fetch.MAX_PAPER_GB, help="download at most this much per paper")
    r.add_argument("--target", help="path to the reference qtl_manifest.tsv, read only (default: $LC2_QTL_MANIFEST)")
    e = sub.add_parser("export", help="write the candidate rows as TSV")
    e.add_argument("--out", required=True)
    e.add_argument("--scope", choices=("approved", "ready", "all"), default="all")
    e.add_argument("--format", choices=("manifest", "review"), default="review")
    args = ap.parse_args()
    if args.command == "run":
        raise SystemExit(asyncio.run(run(args)))
    text = ManifestStore().export(args.scope, args.format)
    Path(args.out).write_text(text, encoding="utf-8")
    print(f"{text.count(chr(10)) - 1} row(s) written to {args.out}")


if __name__ == "__main__":  # pragma: no cover
    main()
