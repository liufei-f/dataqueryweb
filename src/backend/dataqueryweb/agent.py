"""Build one paper's manifest rows with the LLM doing the investigation itself.

The model works through tools, one call at a time, until it submits:

- ``read_paper`` — search or page through the article text (full text when it was found).
- ``open_url`` / ``list_files`` / ``peek_file`` — follow the data-availability links, list
  repositories (Zenodo, figshare, GitHub, OSF, Hugging Face, plain pages) and read the header
  and first rows of a remote file, archive member or spreadsheet without downloading it.
- ``download`` — fetch files into ``data/<dataset>/`` (resumable; within the per-paper size
  limit and the free disk space; a URL already downloaded for another paper is reused).
- ``inspect_local`` — the first lines of a downloaded file or archive member.
- ``run_python`` — Python the model writes to turn a table into one the importer reads
  (``<name>_processed.tsv.gz``). It runs in the dataset folder with no network, no API keys
  and a time limit; code that reaches for the network, other processes or deletes files is
  refused before it runs.
- ``validate`` — read a file exactly as ``load_gwas_qtl_catalog.py`` will (pandas.read_csv
  with the manifest's delimiter and columns) and check the values. A row can only point at a
  file that passed.
- ``note_location`` — record what a data location turned out to be (manual, error, …).
- ``submit`` / ``give_up`` — the rows with a quote for every fact taken from a text. The
  program checks quotes against the texts the model actually saw, the file paths against
  ``validate``, and the vocabulary; it sends problems back for one more try.

Everything the model saw and did is kept as the row's evidence.
"""

from __future__ import annotations

import asyncio
import fnmatch
import html
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
import pandas as pd

from dataqueryweb import dbsnp, fetch, finder, liftover, llm, probe, records
from dataqueryweb import manifest_rules as rules

MAX_STEPS = int(os.environ.get("MANIFEST_AGENT_STEPS") or 80)  # tool calls per paper
MAX_TOKENS = int(os.environ.get("MANIFEST_AGENT_TOKENS") or 4_000_000)  # per paper
RUN_MAX_SECONDS = int(os.environ.get("MANIFEST_RUN_MAX_SECONDS") or 6 * 3600)
TOOL_CHARS = 14000  # longest tool result the model sees
OLD_TOOL_CHARS = 1800  # older results are cut to this, to keep the context small
KEEP_FULL = 10  # the newest tool results kept whole
CALL_TIMEOUT = 600.0
VALIDATE_ROWS = 20000
TRACE = bool(os.environ.get("MANIFEST_AGENT_TRACE"))  # print every tool call (debugging)
INDEX_FILE = ".downloads.json"  # url → local file, shared by every paper
SCRIPTS_DIR = ".agent_scripts"

# Never QTL summary statistics: reference/annotation data, pages, papers.
NOT_SUMSTATS = re.compile(
    r"hgdownload|ucsc\.edu/goldenpath|ftp\.ensembl|ensembl\.org|gencodegenes|genenames\.org|/hgnc/|dbsnp|snp1[0-9]{2}"
    r"|/geo/query/|/geo/series/.*_RAW\.tar|europepmc/webservices|/pmc/articles/|pmc\.ncbi\.nlm\.nih\.gov/articles"
    r"|api\.figshare\.com/v2/articles/\d+$|zenodo\.org/api/records/\d+$|\.(fa|fasta|fna|2bit|chain|gtf|gff3?|bam|bai|cram"
    r"|vcf|bcf|bed|bigwig|bw|pdf|docx?|html?|xml|json|h5ad|rds|rdata)(\.gz)?([?#].*)?$", re.I)
# Older GTEx releases: the manifest already has GTEx v10.
OLD_GTEX = re.compile(r"gtex[^/]{0,20}?v(6p?|7|8)(?![0-9])|gtex_analysis_v(6p?|7|8)", re.I)
FORBIDDEN = re.compile(
    r"\b(subprocess|socket|ctypes|multiprocessing|requests|urllib|httpx|aiohttp|http\.client|ftplib"
    r"|paramiko|importlib|__import__|shutil\.rmtree|shutil\.move|os\.system|os\.popen|os\.exec\w*"
    r"|os\.spawn\w*|os\.fork|os\.kill|os\.remove|os\.unlink|os\.rmdir|os\.removedirs|os\.chdir"
    r"|os\.environ|pathlib\.Path\.unlink|\.unlink\(|\.rmdir\(|eval\(|exec\(|compile\()")
ABSOLUTE_PATH = re.compile(r"""["'](/|~)[^"']*["']""")


SYSTEM = """You are a senior data curator. For ONE paper you must deliver rows of LocusCompare's \
QTL manifest, each pointing at a downloaded file that the importer can load, with every fact \
backed by the paper, a repository page or the file itself. Work like a careful human curator: \
read the paper, follow every data link, look inside the files, download, fix formats with \
Python, validate, then fill the table. You act only through the tools; call one or several \
tools per turn and keep going until you call submit (or give_up when nothing can be loaded).

WHAT THE MANIFEST NEEDS
- One row per table of FULL QTL summary statistics: every tested variant–phenotype pair \
(nominal results, "allpairs", "full_assoc", "sumstats"). Tables of only significant, lead, \
conditional, fine-mapped or colocalised hits are not wanted while a full table may exist — \
list them in skipped_tables. Only when a careful search of the paper and everything it points \
to (PROCEDURE phase 3b) shows that no full table is public, keep the most complete non-full \
table of that QTL type and context as a row with scope "non_full", a non_full_reason and a \
scope_note that says with numbers why it is not full.
- qtl_type must be one of: eQTL, sQTL, caQTL, pQTL. Other types (mQTL, meQTL, metabolite QTL, \
edQTL, apaQTL, …) go to skipped_tables with the reason.
- Human data only, and only THIS paper's own new QTL results — never public resources the \
paper merely used (eQTLGen, GTEx, eQTL Catalogue, UKB-PPP, deCODE, MetaBrain, PsychENCODE, …).
- Already in LocusCompare (do not download again): {datasets}. GTEx is there as GTEx_v10, so \
older GTEx releases (v6, v6p, v7, v8) — or a paper's copy of GTEx results — are not \
downloaded: list them in skipped_tables as "already in the manifest as GTEx_v10". The same \
holds for any other resource above.
- The importer (load_gwas_qtl_catalog.py) reads each file with \
pandas.read_csv(path, sep=delimiter, compression="infer", dtype=str) and needs these columns, \
named in the row: chrom_col, position_col, ref_col, alt_col, variant_id_col, pval_col \
(nominal p-value, not FDR/q/permutation), phenotype_id_col, and rsid_col (rs ids, empty where \
dbSNP has none; fill it with add_rsid). Optional: beta_col, se_col, maf_col.
- Coordinates must be GRCh38. A GRCh37/hg19 table is converted with liftover (after \
processing, before add_rsid); never submit GRCh37 coordinates.
- REQUIRED VALUE FORMATS, in every row: chromosome digits only (1, not chr1; drop X/Y/MT \
rows), position digits only, ref and alt upper-case A/C/G/T/N, and a variant id written \
chr<chrom>_<pos>_<ref>_<alt> joined by underscores (chr1_12345_A_G — with the chr prefix, \
never colons, never a _b38 suffix) that matches those four columns. Files that differ are processed with \
run_python first; validate refuses them. It keeps only single-base ref/alt on autosomes. file_path is relative to the \
data folder and may use a glob (chr*.tsv.gz) for per-chromosome files of one context.
- gene_id_mode: for eQTL/sQTL/pQTL the importer derives the gene from phenotype_id: "self" \
(the id is an Ensembl gene id, version allowed) or "phenotype_last_segment" (the gene is the \
last ":"-separated part, e.g. leafcutter clusters chr1:100:200:clu_1:ENSG…). caQTL phenotype \
ids MUST be chr_start_end (chr1_1000_2000); rewrite them if they are written otherwise.
- If a table breaks the value formats above, lacks separate chrom/position/ref/alt columns, has a variant id like \
1:12345:A:G, chr1_12345_A_G_b38 or rs ids plus a separate position, -log10 p, z or \
chi-square instead of p, no header, is parquet/xlsx/inside an archive, or uses whitespace: \
write Python with run_python that produces <original name>_processed.tsv.gz (tab-separated, \
with a header) next to the original. Never invent values: if alleles or positions exist \
nowhere, the table cannot be used (say so in skipped_tables).

DOWNLOAD ONLY QTL SUMMARY STATISTICS. Download a file only after peek_file (or a listing \
with a clear README) showed it is a table of QTL associations, or an archive of such tables. \
Never download anything else: no reference or annotation data (dbSNP, gene annotation, HGNC, \
Ensembl/GENCODE, genome FASTA, chain files, array platform annotations), no genotypes, \
expression matrices, peaks or counts, no supplementary bundles, web pages, XML or PDFs \
(read those with open_url). Processing may only use the downloaded summary statistics \
themselves. If a table can only be made loadable with outside reference data (e.g. it has \
rsIDs or positions but no chromosome anywhere), do not process it: list it in \
skipped_tables with what it lacks. Phenotype ids that are not Ensembl (gene symbols, RefSeq, \
probe ids) are kept as they are; the row is flagged for a person.

THE SERVER: 2 CPUs, 3 GB RAM. Process big tables in chunks (pandas chunksize ≤ 200000, \
write with gzip in append mode). A 3 GB .gz file takes ~20 minutes; that is fine. Files \
that exist already are not downloaded again.

HOW TO WORK: follow the PROCEDURE at the end of these instructions, phase by phase.

FIELDS
- dataset: the resource or cohort name the paper uses (OneK1K, SingleBrain, GALA_II, …), \
else FirstAuthor_Year. source_project_id: same as dataset unless the paper names a project id.
- biocontext: the label that identifies the context within the dataset (file label or cell \
type), underscores, no spaces. level_1_context: broad tissue group from {level_1} ("Blood" \
covers whole blood, PBMC, immune cells, plasma, LCLs). level_2_context: cell type, \
sub-region or cell type + condition (CD14_Mono, macrophage_IFNg); "" for a bulk tissue.
- population: EUR, EAS, AFR, AMR, SAS, or ALL (multi-ancestry). sample_size: genotyped \
donors in THIS context's QTL mapping (not cells, not samples before QC).
- genome_build: GRCh38 or GRCh37 of the source file's coordinates, with a quote. Every row \
is delivered on GRCh38: a GRCh37 table is converted with the liftover tool before add_rsid.
- url: the landing page of the data. publication: https://doi.org/<doi>.

EVIDENCE: for biocontext, sample_size, population and genome_build give {{"source": \
"paper|page|file", "quote": "<verbatim sentence copied from what a tool showed you>"}}. \
Quotes are checked word by word against the texts you were shown; never paraphrase. If a \
fact is not stated anywhere, leave the quote empty and say so in note.

Be thorough but do not loop: if a location is unreachable after a retry, note it and move on."""

# The step-by-step procedure, kept as a skill file in the repository so it can be edited
# without touching code; appended to SYSTEM on every build.
SKILL_FILE = Path(os.environ.get("MANIFEST_SKILL") or
                  Path(__file__).resolve().parents[3] / "skills" / "build-manifest" / "SKILL.md")


def skill(path: Path = SKILL_FILE) -> str:
    """The skill's body (front matter dropped), as the PROCEDURE section of the prompt."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise FileNotFoundError(f"manifest skill not found: {path} (set MANIFEST_SKILL)") from None
    if text.startswith("---"):
        text = text.split("---", 2)[2]
    return f"PROCEDURE ({path.name})\n\n{text.strip()}"


TOOLS: list[dict[str, Any]] = [
    {"name": "read_paper", "description": "Search the paper's text or read it in order. With 'query' "
     "(words or a regex, case-insensitive) returns the matching paragraphs; otherwise paragraphs "
     "start..start+count.", "parameters": {"type": "object", "properties": {
         "query": {"type": "string"}, "start": {"type": "integer"}, "count": {"type": "integer"}}}},
    {"name": "open_url", "description": "Open a web page (or a DOI) and return its readable text (8000 characters "
     "at a time; 'offset' reads on) and its links. 'find' (words or a regex) returns the passages that match "
     "instead — use it to read Methods / Data availability of a long article page.",
     "parameters": {"type": "object", "properties": {"url": {"type": "string"}, "find": {"type": "string"},
                                                     "offset": {"type": "integer"}}, "required": ["url"]}},
    {"name": "list_files", "description": "List the files behind a repository or page URL (Zenodo, "
     "figshare, GitHub, Hugging Face, OSF APIs, or the file links of a page) with sizes and download URLs.",
     "parameters": {"type": "object", "properties": {"url": {"type": "string"},
                                                     "pattern": {"type": "string", "description": "optional glob on the path"}},
                    "required": ["url"]}},
    {"name": "peek_file", "description": "Read the header and first rows of a remote file without "
     "downloading it (text, gz/bgz/bz2/xz, zip and tar members, xlsx sheets, parquet columns).",
     "parameters": {"type": "object", "properties": {"url": {"type": "string"}, "name": {"type": "string"}},
                    "required": ["url"]}},
    {"name": "download", "description": "Download files into data/<dataset folder>/. Give 'urls' "
     "(a list) or 'listing_url' plus 'pattern' (a glob on the listed paths) to fetch many at once.",
     "parameters": {"type": "object", "properties": {
         "dataset": {"type": "string", "description": "dataset name (files go to the record's folder, "
                     "named by its record code)"},
         "urls": {"type": "array", "items": {"type": "string"}},
         "listing_url": {"type": "string"}, "pattern": {"type": "string"},
         "subfolder": {"type": "string"}}, "required": ["dataset"]}},
    {"name": "inspect_local", "description": "First lines of a downloaded file (path relative to the "
     "data folder; 'archive.tar.gz::member' for a member), or the listing of a folder or archive.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "lines": {"type": "integer"}},
                    "required": ["path"]}},
    {"name": "run_python", "description": "Run Python 3 (pandas, numpy, pyarrow, gzip, zipfile, tarfile, "
     "math available; no network) with the dataset folder as the working directory. Use relative paths. "
     "Returns the exit code, the end of stdout/stderr and the files that changed.",
     "parameters": {"type": "object", "properties": {
         "dataset": {"type": "string"}, "code": {"type": "string"},
         "purpose": {"type": "string", "description": "one line: what this script does"},
         "timeout_seconds": {"type": "integer"}}, "required": ["dataset", "code", "purpose"]}},
    {"name": "validate", "description": "Read file_path (relative to the data folder; globs allowed) "
     "the way the importer does and check the mapped columns' values, including the required formats: "
     "chrom and position digits only, upper-case alleles, variant id chr<chrom>_<pos>_<ref>_<alt> (chr1_12345_A_G). "
     "Rows may only use paths that passed.",
     "parameters": {"type": "object", "properties": {
         "file_path": {"type": "string"}, "delimiter": {"type": "string", "description": "\\t , ; or a space"},
         "qtl_type": {"type": "string"}, "gene_id_mode": {"type": "string"},
         "columns": {"type": "object", "description": "chrom_col, position_col, ref_col, alt_col, pval_col, "
                     "phenotype_id_col and optional beta_col, se_col, maf_col, rsid_col, variant_id_col"}},
                    "required": ["file_path", "delimiter", "qtl_type", "columns"]}},
    {"name": "add_rsid", "description": "Fill the rs id column of a processed table (<name>_processed.tsv.gz; globs "
     "allowed) from the local dbSNP copy (GRCh38, build 157), matching chrom, position, ref and alt (alleles "
     "also tried swapped). Variants dbSNP does not hold keep an empty rs id. Existing rs ids are kept. Then "
     "validate again with rsid_col.",
     "parameters": {"type": "object", "properties": {
         "file_path": {"type": "string"},
         "columns": {"type": "object", "description": "chrom_col, position_col, ref_col, alt_col of the table"},
         "rsid_col": {"type": "string", "description": "column to fill (default rsid)"}},
                    "required": ["file_path", "columns"]}},
    {"name": "liftover", "description": "Convert a processed GRCh37/hg19 table (<name>_processed.tsv.gz; "
     "globs allowed) to GRCh38 in place with UCSC's hg19ToHg38 chain: chrom and position are converted, alleles "
     "complemented where the variant lands on the minus strand, the variant id rebuilt, and variants with no "
     "GRCh38 position on the same chromosome dropped. The GRCh37 chrom and position are kept as chrom_grch37 "
     "and pos_grch37. Run it once per file, before add_rsid; then validate again.",
     "parameters": {"type": "object", "properties": {
         "file_path": {"type": "string"},
         "columns": {"type": "object", "description": "chrom_col, position_col, ref_col, alt_col and "
                     "variant_id_col of the table"}},
                    "required": ["file_path", "columns"]}},
    {"name": "note_location", "description": "Record what a data location is: status one of listed, "
     "downloaded, manual (a person must act: login, application, browser-only), error, not_data, duplicate.",
     "parameters": {"type": "object", "properties": {"url": {"type": "string"}, "status": {"type": "string"},
                                                     "note": {"type": "string"}}, "required": ["url", "status", "note"]}},
    {"name": "submit", "description": "Finish with the manifest rows. Each row: the manifest fields, "
     "'source_url' (where the files came from), 'genome_build', 'evidence', 'note', and 'scope': 'full' "
     "(default) or 'non_full' (not every tested pair) — only when no full table exists anywhere — with "
     "'non_full_reason' (significant_phenotypes_only, significant_pairs_only, lead_variants_only, "
     "example_subset, some_chromosomes_or_regions, selected_phenotypes) and 'scope_note' saying, with "
     "numbers, why it is not full: what the paper tested and what the table holds. skipped_tables: tables found but not usable, with the reason.",
     "parameters": {"type": "object", "properties": {
         "rows": {"type": "array", "items": {"type": "object"}},
         "skipped_tables": {"type": "array", "items": {"type": "object", "properties": {
             "path": {"type": "string"}, "url": {"type": "string"}, "reason": {"type": "string"}}}},
         "summary": {"type": "string"}}, "required": ["rows", "summary"]}},
    {"name": "give_up", "description": "Finish without rows, saying why nothing can be loaded.",
     "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
]


class Done(Exception):
    pass


@dataclass
class Session:
    record: dict[str, Any]
    paper: finder.Paper | None
    root: Path  # the data folder
    max_bytes: float
    target: rules.Target
    cancel: threading.Event
    step: Callable[[str], None]
    texts: dict[str, str] = field(default_factory=dict)  # what the model was shown, for quotes
    pages: dict[str, tuple[str, list[str]]] = field(default_factory=dict)  # url → (text, links)
    validated: dict[str, dict[str, Any]] = field(default_factory=dict)
    downloads: list[dict[str, Any]] = field(default_factory=list)
    locations: dict[str, dict[str, Any]] = field(default_factory=dict)
    scripts: list[dict[str, Any]] = field(default_factory=list)
    log: list[dict[str, str]] = field(default_factory=list)
    used_bytes: float = 0.0
    folder_by_dataset: dict[str, Path] = field(default_factory=dict)
    rsid_lookup: dbsnp.Lookup | None = None
    lifter: liftover.Lifter | None = None
    lifted: dict[str, dict[str, Any]] = field(default_factory=dict)  # processed file → liftover counts
    result: dict[str, Any] | None = None
    submit_rounds: int = 0

    # ── helpers ──────────────────────────────────────────────────────────────

    def folder_name(self) -> str:
        return fetch.record_folder(self.record, rules.default_dataset(
            self.record.get("first_author", ""), self.record.get("year", "")))

    def folder(self, dataset: str) -> Path:
        """The record's data folder, named by its record code; every file of the paper goes there."""
        d = self.root / fetch.record_folder(self.record, dataset or rules.default_dataset(
            self.record.get("first_author", ""), self.record.get("year", "")))
        d.mkdir(parents=True, exist_ok=True)
        self.folder_by_dataset[dataset] = d
        return d

    def local(self, rel: str) -> Path:
        p = (self.root / fetch.safe_rel(rel)).resolve()
        if self.root.resolve() not in (p, *p.parents):
            raise ValueError("path outside the data folder")
        return p

    def see(self, name: str, text: str) -> None:
        if text:
            self.texts[name] = text[:400_000]

    def location(self, url: str, **kw: Any) -> None:
        loc = self.locations.setdefault(url, {"url": url, "content": "qtl_summary_statistics", "access": "open",
                                              "status": "", "resolver": "", "landing": url, "note": "",
                                              "n_files": 0, "inventory": []})
        for k, v in kw.items():
            if k == "inventory":
                loc["inventory"] = (loc["inventory"] + v)[:300]
            elif v not in (None, ""):
                loc[k] = v

    # ── tools ────────────────────────────────────────────────────────────────

    def read_paper(self, query: str = "", start: int = 0, count: int = 25) -> dict[str, Any]:
        p = self.paper
        paras = [*(f"[Data availability] {a}" for a in p.availability), *p.paragraphs] if p else []
        if query:
            try:
                pat = re.compile(query, re.I)
            except re.error:
                pat = re.compile("|".join(map(re.escape, query.split())), re.I)
            hits = [(i, x) for i, x in enumerate(paras) if pat.search(x)]
            out: dict[str, Any] = {"matches": len(hits), "paragraphs": [{"i": i, "text": x} for i, x in hits[:40]]}
            pages = [{"page": name, "snippets": windows(text, pat, 3)} for name, text in self.texts.items()
                     if name.startswith("page ") and pat.search(text)]
            if pages:  # what opened pages say too (the article page when the paper text was not found)
                out["opened_pages"] = pages[:6]
            return out
        if not paras:
            return {"total": 0, "note": "the paper text could not be fetched here; open the article (PMC, publisher, "
                    "bioRxiv full text) with open_url and use its 'find' and 'offset' to read it"}
        start = max(0, int(start or 0))
        return {"total": len(paras), "paragraphs": [{"i": i, "text": paras[i]}
                                                    for i in range(start, min(len(paras), start + int(count or 25)))]}

    def open_url(self, url: str, find: str = "", offset: int = 0) -> dict[str, Any]:
        url = url.strip()
        if url.startswith("10."):
            url = f"https://doi.org/{url}"
        if url in self.pages:  # read on in a page already fetched
            return self._page_view(url, *self.pages[url], find, offset)
        try:
            with probe.client() as http, http.stream("GET", url, headers={"User-Agent": probe.BROWSER_UA}) as r:
                final, ctype = str(r.url), r.headers.get("content-type", "").lower()
                if r.status_code >= 400:
                    return {"url": final, "error": f"HTTP {r.status_code} ({probe.http_words(r.status_code)})"}
                if "html" not in ctype and "xml" not in ctype and "json" not in ctype and "text/plain" not in ctype:
                    return {"url": final, "content_type": ctype, "size": r.headers.get("content-length"),
                            "note": "this is a file, not a page: use peek_file or download"}
                body = b"".join(probe._take(r.iter_bytes(), 4_000_000)).decode(r.encoding or "utf-8", "replace")
        except httpx.HTTPError as exc:
            return {"url": url, "error": probe.plain_error(exc)[0]}
        if "json" in ctype or "text/plain" in ctype:
            self.see(f"page {final}", body)
            self.pages[url] = self.pages[final] = (body, [])
            return self._page_view(final, body, [], find, offset)
        links, seen = [], set()
        for m in re.finditer(r"""<a\b[^>]*href\s*=\s*["']([^"'#]+)["'][^>]*>(.*?)</a>""", body, re.I | re.S):
            href = urljoin(final, html.unescape(m.group(1)).strip())
            if not href.startswith(("http", "ftp")) or href in seen:
                continue
            seen.add(href)
            links.append(f"{probe._strip_html(m.group(2))[:80]} → {href}")
        text = re.sub(r"<(script|style|noscript)\b.*?</\1>", " ", body, flags=re.I | re.S)
        text = probe._strip_html(text)
        self.see(f"page {final}", text)
        self.pages[url] = self.pages[final] = (text, links)
        return self._page_view(final, text, links, find, offset)

    def _page_view(self, url: str, text: str, links: list[str], find: str, offset: int) -> dict[str, Any]:
        out: dict[str, Any] = {"url": url, "chars": len(text)}
        if find:
            try:
                pat = re.compile(find, re.I)
            except re.error:
                pat = re.compile(re.escape(find), re.I)
            out["matches"] = windows(text, pat, 12)
            return out
        offset = max(0, int(offset or 0))
        out["text"] = text[offset:offset + 8000]
        if offset + 8000 < len(text):
            out["more"] = f"call again with offset={offset + 8000} (or find=...) to read on"
        if offset == 0:
            out["links"] = links[:250]
        return out

    def list_files(self, url: str, pattern: str = "") -> dict[str, Any]:
        with probe.client() as http:
            listing = probe.list_location(http, url)
        for name, text in listing.texts.items():
            self.see(f"{name} ({listing.landing})", text)
        files = [f.to_dict() for f in listing.files]
        if pattern:
            files = [f for f in files if fnmatch.fnmatch(f["path"], pattern) or fnmatch.fnmatch(f["name"], pattern)]
        self.location(url, status=listing.status, resolver=listing.resolver, landing=listing.landing,
                      note=listing.note, n_files=len(listing.files))
        total = sum(f["size"] or 0 for f in files)
        return {"status": listing.status, "resolver": listing.resolver, "landing": listing.landing,
                "note": listing.note, "n_files": len(files), "total_gb": round(total / 1e9, 2),
                "texts": {k: v[:3000] for k, v in listing.texts.items()},
                "files": [{"path": f["path"], "size_mb": round((f["size"] or 0) / 1e6, 1), "url": f["url"]}
                          for f in files[:300]]}

    def peek_file(self, url: str, name: str = "") -> dict[str, Any]:
        f = probe.RemoteFile(name or probe._name_of(url), url)
        with probe.client() as http:
            p = probe.probe_file(http, f)
        tables = []
        for t in p.tables[:12]:
            tables.append({"member": t.name, "columns": t.columns[:80], "first_rows": [r[:80] for r in t.rows[:5]],
                           "has_header": t.has_header, "delimiter": t.delimiter, "fmt": t.fmt,
                           "title_lines": t.preamble[:300]})
            self.see(f"file {f.name} {t.name}", " ".join([*t.columns, t.preamble, *(" ".join(r) for r in t.rows)]))
        return {"kind": p.kind, "compression": p.compression, "note": p.note,
                "members": p.members[:120], "n_members": len(p.members), "tables": tables}

    def _index(self) -> dict[str, Any]:
        try:
            return json.loads((self.root / INDEX_FILE).read_text())
        except (OSError, ValueError):
            return {}

    def _remember(self, url: str, rel: str, size: int) -> None:
        idx = self._index()
        idx[url] = {"path": rel, "size": size, "record_id": self.record["record_id"]}
        tmp = self.root / (INDEX_FILE + ".tmp")
        tmp.write_text(json.dumps(idx, indent=0))
        tmp.replace(self.root / INDEX_FILE)

    def download(self, dataset: str, urls: list[str] | None = None, listing_url: str = "", pattern: str = "",
                 subfolder: str = "") -> dict[str, Any]:
        folder = self.folder(dataset)
        todo: list[probe.RemoteFile] = []
        if listing_url:
            with probe.client() as http:
                listing = probe.list_location(http, listing_url)
            todo += [f for f in listing.files if not pattern or fnmatch.fnmatch(f.path or f.name, pattern)
                     or fnmatch.fnmatch(f.name, pattern)]
            if not todo:
                return {"error": f"nothing in the listing matches {pattern!r} ({listing.status}: {listing.note})"}
        for u in urls or []:
            todo.append(probe.RemoteFile(probe._name_of(u), u))
        refused = [f for f in todo if OLD_GTEX.search(f"{f.url} {f.path or f.name}")]
        not_data = [f for f in todo if f not in refused and NOT_SUMSTATS.search(f"{f.url} {f.path or f.name}")]
        todo = [f for f in todo if f not in refused and f not in not_data]
        idx, out, http = self._index(), [], fetch.http_client()
        try:
            for k, f in enumerate(todo, 1):
                if self.cancel.is_set():
                    raise fetch.Cancelled
                old = idx.get(f.url)
                if old and (self.root / old["path"]).exists() and (self.root / old["path"]).stat().st_size == old["size"]:
                    out.append({"url": f.url, "path": old["path"], "size_mb": round(old["size"] / 1e6, 1),
                                "note": f"already downloaded (by record #{old.get('record_id')}); reused"})
                    continue
                if f.size is None:
                    try:
                        h = http.head(f.url)
                        n = h.headers.get("content-length")
                        f.size = int(n) if n and n.isdigit() and h.status_code < 400 else None
                    except httpx.HTTPError:
                        pass
                free = shutil.disk_usage(self.root).free
                need = f.size or 0
                if self.used_bytes + need > self.max_bytes or free - need < fetch.MIN_FREE:
                    out.append({"url": f.url, "error": f"not downloaded: {need / 1e9:.1f} GB would pass the "
                                f"{self.max_bytes / 1e9:g} GB limit for this paper or the free disk space"})
                    continue
                rel_name = fetch.safe_rel(f"{subfolder}/{f.path or f.name}" if subfolder else (f.path or f.name))
                dest = folder / rel_name
                done, last = 0, 0.0

                def progress(n: int, k: int = k, f: probe.RemoteFile = f) -> None:
                    nonlocal done, last
                    done += n
                    if time.monotonic() - last > 1:
                        last = time.monotonic()
                        size = f" of {f.size / 1e9:.2f}" if f.size else ""
                        self.step(f"downloading file {k} of {len(todo)} ({f.name}): {done / 1e9:.2f}{size} GB")

                try:
                    for attempt in range(4):  # a stalled or dropped transfer resumes from the .part file
                        try:
                            fetch.download(http, f, dest, self.cancel, progress)
                            break
                        except (httpx.TransportError, OSError) as exc:
                            if attempt == 3 or isinstance(exc, FileNotFoundError):
                                raise
                            self.step(f"download of {f.name} broke off ({type(exc).__name__}); resuming")
                            time.sleep(10 * (attempt + 1))
                except fetch.Cancelled:
                    raise
                except Exception as exc:
                    out.append({"url": f.url, "error": f"{type(exc).__name__}: {exc}"[:300]})
                    continue
                with dest.open("rb") as fh:
                    head = fh.read(512).lstrip().lower()
                if head.startswith((b"<!doctype", b"<html", b"<?xml", b"{", b"%pdf")):
                    dest.unlink()  # a page, XML, JSON or PDF: not summary statistics
                    out.append({"url": f.url, "error": "not kept: the link returned a web page / XML / JSON / PDF, "
                                "not a data table; read it with open_url"})
                    continue
                size = dest.stat().st_size
                self.used_bytes += size
                rel = str(dest.relative_to(self.root))
                self._remember(f.url, rel, size)
                kind = fetch.Source(rel, dest).kind()
                item = {"url": f.url, "path": rel, "size_mb": round(size / 1e6, 1), "kind": kind}
                if kind == "zip" or rel.endswith((".tar", ".tar.gz", ".tgz")):
                    item["members"] = archive_members(dest)[:60]
                out.append(item)
                self.downloads.append(item)
        finally:
            http.close()
        out += [{"url": f.url, "error": "not downloaded: an older GTEx release; the manifest already has GTEx_v10 "
                 "(list it in skipped_tables)"} for f in refused]
        out += [{"url": f.url, "error": "not downloaded: only QTL summary statistics are downloaded (this looks like "
                 "reference data, a page, a paper or another file type); read pages with open_url"} for f in not_data]
        return {"folder": str(folder.relative_to(self.root)), "files": out}

    def inspect_local(self, path: str, lines: int = 8) -> dict[str, Any]:
        lines = max(1, min(int(lines or 8), 40))
        arc, _, member = path.partition("::")
        p = self.local(arc)
        if p.is_dir():
            items = sorted(p.rglob("*"))
            return {"folder": path, "files": [{"path": str(x.relative_to(self.root)), "size_mb": round(x.stat().st_size / 1e6, 1)}
                                              for x in items if x.is_file()][:300]}
        if not p.exists():
            return {"error": f"{path} does not exist"}
        if not member and (zipfile_like(p) or p.name.endswith((".tar", ".tar.gz", ".tgz"))):
            return {"archive": path, "members": archive_members(p)[:300]}
        src = fetch.Source(path, p, member, ("zip" if zipfile_like(p) else "tar") if member else "")
        kind = src.kind() if not member else "member"
        if kind == "parquet":
            import pyarrow.parquet as pq
            pf = pq.ParquetFile(p)
            df = pf.read_row_group(0).slice(0, lines).to_pandas()
            text = df.to_csv(sep="\t", index=False)
            out = {"kind": "parquet", "rows_total": pf.metadata.num_rows, "head": text}
        else:
            with src.text() as fh:
                raw = [fh.readline().decode("utf-8", "replace").rstrip("\n") for _ in range(lines)]
            out = {"kind": kind, "size_mb": round(p.stat().st_size / 1e6, 1), "head": [x[:1500] for x in raw]}
        self.see(f"file {path}", json.dumps(out)[:20000])
        return out

    def run_python(self, dataset: str, code: str, purpose: str, timeout_seconds: int = 3600) -> dict[str, Any]:
        if bad := FORBIDDEN.search(code):
            return {"refused": f"'{bad.group(0)}' is not allowed (no network, processes, deleting or environment)."}
        if ABSOLUTE_PATH.search(code) or re.search(r"""["'][^"']*\.\./""", code):
            return {"refused": "use paths relative to the dataset folder (no absolute paths, no '..')"}
        folder = self.folder(dataset)
        sdir = self.root / SCRIPTS_DIR / str(self.record["record_id"])
        sdir.mkdir(parents=True, exist_ok=True)
        script = sdir / f"step{len(self.scripts) + 1:02d}.py"
        script.write_text(f"# {purpose}\n{code}\n", encoding="utf-8")
        before = snapshot(folder)
        cmd = [sys.executable, "-I", str(script)]
        if sys.platform.startswith("linux") and os.geteuid() == 0 and shutil.which("unshare"):
            cmd = ["unshare", "-n", "--", *cmd]  # no network inside
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(folder), "PYTHONIOENCODING": "utf-8",
               "OMP_NUM_THREADS": "2", "LC_ALL": "C.UTF-8"}
        limit = max(10, min(int(timeout_seconds or 3600), RUN_MAX_SECONDS))
        self.step(f"running the AI's Python: {purpose[:80]}")
        started = time.monotonic()
        proc = subprocess.Popen(cmd, cwd=folder, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=True)
        tail: list[str] = []

        def reader() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                tail.append(line.decode("utf-8", "replace"))
                del tail[:-200]
                self.step(f"running the AI's Python ({purpose[:50]}): {tail[-1].strip()[:90]}")

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        status = "finished"
        while proc.poll() is None:
            if self.cancel.is_set() or time.monotonic() - started > limit:
                os.killpg(proc.pid, signal.SIGKILL)
                status = "cancelled" if self.cancel.is_set() else f"killed after {limit} s"
                break
            time.sleep(1)
        proc.wait()
        t.join(5)
        if self.cancel.is_set():
            raise fetch.Cancelled
        changed = [{"path": str(Path(k).relative_to(self.root)), "size_mb": round(v / 1e6, 1)}
                   for k, v in snapshot(folder).items() if before.get(k) != v]
        item = {"script": str(script.relative_to(self.root)), "purpose": purpose, "exit_code": proc.returncode,
                "status": status, "seconds": round(time.monotonic() - started), "changed": changed[:50]}
        self.scripts.append(item)
        return item | {"output": "".join(tail)[-6000:]}

    def validate(self, file_path: str, delimiter: str, qtl_type: str, columns: dict[str, str],
                 gene_id_mode: str = "") -> dict[str, Any]:
        result = validate_file(self.root, file_path, delimiter, qtl_type, columns, gene_id_mode)
        key = file_path.strip()
        self.validated[key] = result | {"columns": dict(columns), "delimiter": delimiter, "qtl_type": qtl_type,
                                        "gene_id_mode": gene_id_mode}
        if result.get("sample"):
            self.see(f"file {key}", " ".join(result["header"]) + " " + " ".join(" ".join(r) for r in result["sample"]))
        return result

    def add_rsid(self, file_path: str, columns: dict[str, str], rsid_col: str = "rsid") -> dict[str, Any]:
        """Fill rsid_col of a processed table from the local dbSNP copy (chrom, pos, ref, alt)."""
        vcf = dbsnp.vcf_path(self.root)
        if not vcf.exists() or not vcf.with_name(vcf.name + ".tbi").exists():
            return {"error": f"the dbSNP reference is not in place ({vcf}); leave rsid_col empty and say so in note"}
        files = expand(self.root, file_path)
        if not files:
            return {"error": f"no file matches {file_path} under the data folder"}
        if any("_processed" not in f.name or not f.name.endswith(".tsv.gz") for f in files):
            return {"error": "add_rsid writes into processed tables only (<name>_processed.tsv.gz); process the file first"}
        need = ("chrom_col", "position_col", "ref_col", "alt_col")
        if missing := [k for k in need if not columns.get(k)]:
            return {"error": f"name the columns: {', '.join(missing)}"}
        if self.rsid_lookup is None:
            self.rsid_lookup = dbsnp.Lookup(vcf)
        out = {"files": len(files), "rows": 0, "with_rsid": 0}
        for f in files:
            res = dbsnp.annotate(f, self.rsid_lookup, columns, rsid_col.strip() or "rsid",
                                 step=lambda m, f=f: self.step(f"AI: add_rsid {f.name}: {m}"))
            out["rows"] += res["rows"]
            out["with_rsid"] += res["with_rsid"]
        out |= {"rsid_col": rsid_col.strip() or "rsid", "share": round(out["with_rsid"] / out["rows"], 4) if out["rows"] else 0.0,
                "next": "validate again with rsid_col set to this column"}
        self.validated.pop(file_path.strip(), None)  # the file changed
        return out

    def liftover(self, file_path: str, columns: dict[str, str]) -> dict[str, Any]:
        """Convert processed GRCh37 tables to GRCh38 in place (UCSC hg19ToHg38 chain)."""
        chain = liftover.chain_path(self.root)
        if not chain.exists():
            return {"error": f"the liftover chain file is not in place ({chain}); say so in note"}
        files = expand(self.root, file_path)
        if not files:
            return {"error": f"no file matches {file_path} under the data folder"}
        if any("_processed" not in f.name or not f.name.endswith(".tsv.gz") for f in files):
            return {"error": "liftover writes into processed tables only (<name>_processed.tsv.gz); process the file first"}
        need = ("chrom_col", "position_col", "ref_col", "alt_col")
        if missing := [k for k in need if not columns.get(k)]:
            return {"error": f"name the columns: {', '.join(missing)}"}
        if self.lifter is None:
            self.lifter = liftover.Lifter(chain)
        out: dict[str, Any] = {"files": len(files), "rows": 0, "lifted": 0, "dropped": 0, "minus_strand": 0}
        for f in files:
            res = liftover.lift(f, self.lifter, columns, step=lambda m, f=f: self.step(f"AI: liftover {f.name}: {m}"))
            if "error" in res:
                return res
            self.lifted[str(f.relative_to(self.root))] = res
            for k in ("rows", "lifted", "dropped", "minus_strand"):
                out[k] += res[k]
        out |= {"share": round(out["lifted"] / out["rows"], 4) if out["rows"] else 0.0,
                "next": "add_rsid, then validate again; submit with genome_build GRCh38"}
        self.validated.pop(file_path.strip(), None)  # the file changed
        return out

    def note_location(self, url: str, status: str, note: str) -> dict[str, Any]:
        self.location(url, status=status if status in ("listed", "direct", "downloaded", "manual", "error",
                                                         "not_data", "duplicate") else "listed", note=note[:400])
        return {"ok": True}


# ── reading files the importer's way ─────────────────────────────────────────


def windows(text: str, pat: re.Pattern[str], n: int, width: int = 500) -> list[str]:
    """Up to ``n`` stretches of ``text`` around matches of ``pat``."""
    out, last = [], -1
    for m in pat.finditer(text):
        if m.start() < last:
            continue
        a, b = max(0, m.start() - width), min(len(text), m.end() + width)
        out.append(text[a:b])
        last = b
        if len(out) >= n:
            break
    return out


def zipfile_like(p: Path) -> bool:
    import zipfile
    try:
        return zipfile.is_zipfile(p)
    except OSError:
        return False


def archive_members(p: Path) -> list[str]:
    import tarfile
    import zipfile
    try:
        if zipfile.is_zipfile(p):
            with zipfile.ZipFile(p) as z:
                return [n for n in z.namelist() if not n.endswith("/")]
        with tarfile.open(p) as tf:
            out = []
            for m in tf:
                if m.isfile():
                    out.append(m.name)
                if len(out) >= 2000:
                    break
            return out
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        return [f"(could not list: {exc})"]


def snapshot(folder: Path) -> dict[str, int]:
    out = {}
    for p in folder.rglob("*"):
        try:
            if p.is_file():
                out[str(p)] = p.stat().st_size
        except OSError:
            pass
    return out


NA_VALUES = ["NA", "NaN", ".", "", "nan"]
ENSG = re.compile(r"^ENSG\d{11}$")


def resolve_delimiter(raw: str) -> str:
    return "\t" if raw in ("\\t", "t", "tab", "\t") else raw


def expand(root: Path, file_path: str) -> list[Path]:
    pattern = str(root / fetch.safe_rel(file_path))
    if any(ch in pattern for ch in "*?["):
        import glob
        found = [Path(p) for p in glob.glob(pattern) if Path(p).is_file()]
        return sorted(found, key=lambda p: rules.natural_key(p.name))
    p = Path(pattern)
    return [p] if p.is_file() else []


# The values every manifest file must hold exactly, in every row: chromosome and position as
# plain digits (1, not chr1), upper-case alleles, and a variant id chr<chrom>_<pos>_<ref>_<alt>
# joined by underscores (chr1_12345_A_G) that matches those four columns.
FORMAT_REQUIRED = ("chrom_col", "position_col", "ref_col", "alt_col", "variant_id_col")
# Why a kept table is not full summary statistics (it lacks some tested variant–phenotype pairs).
NON_FULL_REASONS = {
    "significant_phenotypes_only": "every tested variant, but only for the significant phenotypes (e.g. eGenes)",
    "significant_pairs_only": "only the variant–phenotype pairs that passed a significance threshold",
    "lead_variants_only": "only the lead (top) variant of each phenotype",
    "example_subset": "an example or truncated extract of the results",
    "some_chromosomes_or_regions": "only some chromosomes or regions",
    "selected_phenotypes": "only some phenotypes, selected for another reason than significance",
}
FORMAT_RULES = {
    "chrom_col": (re.compile(r"[0-9]+"), "digits only (1, not chr1; drop X/Y/MT rows)"),
    "position_col": (re.compile(r"[0-9]+"), "digits only"),
    "ref_col": (re.compile(r"[ACGTN]+"), "upper-case A/C/G/T/N"),
    "alt_col": (re.compile(r"[ACGTN]+"), "upper-case A/C/G/T/N"),
}
NUMERIC_COLS = ("pval_col", "beta_col", "se_col", "maf_col")


def format_problems(df: pd.DataFrame, cols: dict[str, str], name: str) -> list[str]:
    """What breaks the required value formats in ``df`` (every row read, not a share)."""
    problems = []
    values = {k: df[cols[k]].astype("string") for k in FORMAT_REQUIRED}
    for key, (pattern, rule) in FORMAT_RULES.items():
        bad = ~values[key].str.fullmatch(pattern.pattern).fillna(False)
        if bad.any():
            problems.append(f"{name}: {key} {cols[key]!r} must be {rule}; {int(bad.sum())} of {len(df)} rows "
                            f"are not, e.g. {values[key][bad].iloc[0]!r}")
    expected = "chr" + values["chrom_col"] + "_" + values["position_col"] + "_" + values["ref_col"] + "_" + values["alt_col"]
    bad = ~(values["variant_id_col"] == expected).fillna(False)
    if bad.any():
        i = bad.idxmax()
        problems.append(f"{name}: variant_id_col {cols['variant_id_col']!r} must be chr<chrom>_<pos>_<ref>_<alt> joined by "
                        f"underscores, matching those columns; {int(bad.sum())} of {len(df)} rows are not, e.g. "
                        f"{values['variant_id_col'][i]!r} (expected {expected[i]!r})")
    # rs ids are rs<digits>, or empty where dbSNP holds no such variant.
    if cols.get("rsid_col") and cols["rsid_col"] in df.columns:
        rs = df[cols["rsid_col"]]
        bad = rs.notna() & ~rs.astype("string").str.fullmatch(r"rs\d+").fillna(False)
        if bad.any():
            problems.append(f"{name}: rsid_col {cols['rsid_col']!r} must hold rs ids (rs<digits>) or be empty; "
                            f"{int(bad.sum())} of {len(df)} rows do not, e.g. {rs[bad].iloc[0]!r}")
    # Numbers stay numbers: a missing value is empty, never a placeholder word.
    for key in NUMERIC_COLS:
        if not cols.get(key) or cols[key] not in df.columns:
            continue
        raw = df[cols[key]]
        text = raw.notna() & pd.to_numeric(raw, errors="coerce").isna()
        if text.any():
            problems.append(f"{name}: {key} {cols[key]!r} must hold numbers (empty when missing); {int(text.sum())} "
                            f"of {len(df)} rows hold text, e.g. {raw[text].iloc[0]!r}")
    return problems


def validate_file(root: Path, file_path: str, delimiter: str, qtl_type: str, columns: dict[str, str],
                  gene_id_mode: str = "") -> dict[str, Any]:
    """Read the first rows of (some of) the files as load_gwas_qtl_catalog.py will and check them."""
    files = expand(root, file_path)
    if not files:
        return {"ok": False, "problems": [f"no file matches {file_path} under the data folder"]}
    sep = resolve_delimiter(delimiter)
    cols = {k: v for k, v in columns.items() if v and k in rules.ROLE_COLUMN.values()}
    missing_map = [k for k in (*FORMAT_REQUIRED, "pval_col", "phenotype_id_col") if k not in cols]
    problems: list[str] = [f"no column named for {k}" for k in missing_map]
    canonical = rules.canonical_qtl_type(qtl_type) or qtl_type
    if canonical not in rules.MANIFEST_QTL_TYPES:
        problems.append(f"{qtl_type} is not a manifest QTL type ({', '.join(rules.MANIFEST_QTL_TYPES)})")
    checks: dict[str, Any] = {}
    header: list[str] = []
    sample: list[list[str]] = []
    kept_share = None
    pick = files[:2] + ([files[-1]] if len(files) > 2 else [])
    for f in pick:
        try:
            df = pd.read_csv(f, sep=sep, compression="infer", dtype=str, nrows=VALIDATE_ROWS,
                             na_values=NA_VALUES, keep_default_na=True)
        except Exception as exc:
            problems.append(f"{f.name}: pandas cannot read it with sep={delimiter!r}: {type(exc).__name__}: {exc}"[:300])
            continue
        header = list(map(str, df.columns))
        sample = df.head(3).astype(str).values.tolist()
        absent = [f"{k}={v}" for k, v in cols.items() if v not in df.columns]
        if absent:
            problems.append(f"{f.name}: columns not in the header: {', '.join(absent)} (header: {', '.join(header[:40])})")
            continue
        for role in (*rules.REQUIRED_ROLES, "beta", "se", "maf"):
            col = cols.get(rules.ROLE_COLUMN[role])
            if not col:
                continue
            vals = [str(v).strip() for v in df[col].dropna().head(5000)]
            test = rules.VALUE_TESTS.get(role, lambda v: bool(v))
            good = sum(1 for v in vals if test(v))
            share = good / len(vals) if vals else 0.0
            checks.setdefault(role, {"column": col, "checked": 0, "pass": 0, "example": vals[0] if vals else ""})
            checks[role]["checked"] += len(vals)
            checks[role]["pass"] += good
            if role in rules.REQUIRED_ROLES and share < fetch.PASS_SHARE:
                bad = next((v for v in vals if not test(v)), "")
                problems.append(f"{f.name}: {role} ({col}): only {share:.0%} of values look right, e.g. {bad!r}")
        if missing_map:
            continue
        problems += format_problems(df, cols, f.name)
        chrom = df[cols["chrom_col"]].astype("string").str.replace(r"(?i)^chr", "", regex=True)
        ref = df[cols["ref_col"]].astype("string").str.strip().str.upper()
        alt = df[cols["alt_col"]].astype("string").str.strip().str.upper()
        keep = (~chrom.str.upper().eq("X").fillna(False)) & ref.str.len().eq(1).fillna(False) & alt.str.len().eq(1).fillna(False)
        keep &= pd.to_numeric(df[cols["position_col"]], errors="coerce").notna()
        keep &= pd.to_numeric(df[cols["pval_col"]], errors="coerce").notna()
        keep &= df[cols["phenotype_id_col"]].notna()
        kept_share = float(keep.mean()) if len(df) else 0.0
        phen = df[cols["phenotype_id_col"]].dropna().astype(str)
        if canonical in rules.GENE_QTL_TYPES:
            mode = gene_id_mode or "self"
            if mode not in ("self", "phenotype_last_segment"):
                problems.append(f"gene_id_mode must be self or phenotype_last_segment, not {mode!r}")
            else:
                ids = phen.str.rsplit(":", n=1).str[-1] if mode == "phenotype_last_segment" else phen
                ids = ids.str.replace(r"\.\d+$", "", regex=True)
                ens = float(ids.str.match(ENSG).mean()) if len(ids) else 0.0
                checks["gene_id"] = {"mode": mode, "ensembl_share": round(ens, 3), "example": ids.iloc[0] if len(ids) else ""}
                if ens < fetch.PASS_SHARE:
                    checks["gene_id"]["warning"] = ("phenotype ids are not Ensembl gene ids after this gene_id_mode; "
                                                    "the importer will store meaningless gene ids")
        elif canonical == "caQTL":
            ok = float(phen.str.match(r"(?i)^chr([0-9]+|X|Y|MT)_[0-9]+_[0-9]+$").mean()) if len(phen) else 0.0
            checks["peak_id"] = {"chr_start_end_share": round(ok, 3), "example": phen.iloc[0] if len(phen) else ""}
            if ok < 1.0:
                problems.append("caQTL phenotype ids must all be chr_start_end; the importer stops otherwise")
    if kept_share is not None and kept_share == 0:
        problems.append("the importer would keep no row (single-base autosomal SNVs with position and p-value)")
    size = sum(f.stat().st_size for f in files)
    return {"ok": not problems, "problems": problems[:12], "n_files": len(files), "total_gb": round(size / 1e9, 2),
            "files": [str(f.relative_to(root)) for f in files[:5]], "checks": checks,
            "kept_share_after_importer_filters": None if kept_share is None else round(kept_share, 3),
            "header": header[:60], "sample": [r[:60] for r in sample]}


# ── turning a submission into stored rows ────────────────────────────────────


def _q(evidence: dict[str, Any], key: str) -> tuple[str, str]:
    e = evidence.get(key) if isinstance(evidence, dict) else None
    if isinstance(e, dict):
        return str(e.get("quote") or "").strip(), str(e.get("source") or "")
    return (str(e).strip(), "") if isinstance(e, str) else ("", "")


def check_submission(s: Session, sub: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """(rows in manifest.py's format, problems for the model). Rows come back even with
    problems; the caller decides whether to ask again."""
    from dataqueryweb.manifest import Candidate  # manifest imports this module

    doi = (s.record.get("doi") or "").lower()
    rows, problems = [], []
    for i, raw in enumerate(sub.get("rows") or [], 1):
        if not isinstance(raw, dict):
            problems.append(f"row {i}: not an object")
            continue
        row = dict.fromkeys(rules.MANIFEST_COLUMNS, "")
        row.update({k: str(raw.get(k) if raw.get(k) is not None else "").strip() for k in rules.MANIFEST_COLUMNS if k in raw})
        row["use_in_lc2"] = "FALSE"
        row["dataset"] = rules.label(row["dataset"]) or rules.default_dataset(s.record.get("first_author", ""), s.record.get("year", ""))
        row["source_project_id"] = rules.label(row["source_project_id"]) or row["dataset"]
        if doi and s.target.publications.get(doi):
            row["dataset"] = s.target.publications[doi]
        row["publication"] = row["publication"] or (f"https://doi.org/{doi}" if doi else s.record.get("publication_url", ""))
        row["qtl_type"] = rules.canonical_qtl_type(row["qtl_type"]) or row["qtl_type"]
        for k in ("biocontext", "level_1_context", "level_2_context"):
            row[k] = rules.label(row[k])
        row["population"] = rules.population_code(row["population"])
        row["sample_size"] = str(n) if (n := _int(row["sample_size"])) else ""
        evidence_in = raw.get("evidence") or {}
        issues: list[str] = []
        evidence: dict[str, Any] = {}
        tag = f"row {i} ({row['biocontext'] or row['file_path']})"

        if row["qtl_type"] not in rules.MANIFEST_QTL_TYPES:
            problems.append(f"{tag}: qtl_type {row['qtl_type']!r} is not one the manifest holds; move it to skipped_tables")
            issues.append("qtl_type_not_in_manifest")
        v = s.validated.get(row["file_path"])
        if not v:
            problems.append(f"{tag}: file_path {row['file_path']!r} was not validated; call validate with it")
            issues.append("data_not_downloaded")
        else:
            same = all(row.get(k, "") == (v["columns"].get(k) or "") for k in rules.ROLE_COLUMN.values() if k != "median_tpm")
            if not v.get("ok"):
                problems.append(f"{tag}: validate failed for {row['file_path']}: {'; '.join(v.get('problems') or [])[:300]}")
                issues.append("processing_failed")
            elif not same or resolve_delimiter(row["delimiter"]) != resolve_delimiter(v["delimiter"]):
                problems.append(f"{tag}: the row's delimiter or columns differ from those validated for {row['file_path']}")
            if (v.get("checks") or {}).get("gene_id", {}).get("warning"):
                issues.append("phenotype_id_not_ensembl")
        if not row["rsid_col"]:
            issues.append("rsid_not_added")
            problems.append(f"{tag}: no rsid_col; call add_rsid on {row['file_path']!r}, validate again and name the column")
        row["delimiter"] = "\\t" if resolve_delimiter(row["delimiter"]) == "\t" else row["delimiter"]

        proof = s.texts | {"file path": "\n".join([*s.validated, *(d["path"] for d in s.downloads)])}

        def checked(key: str, value: Any, *, needed: bool = True, evidence_in: Any = evidence_in,
                    proof: dict[str, str] = proof, evidence: dict[str, Any] = evidence, tag: str = tag) -> dict[str, Any]:
            quote, source = _q(evidence_in, key)
            where = rules.verify_quote(quote, proof) if quote else ""
            evidence[key] = {"value": value, "source": where or (f"LLM ({source or 'no source'}; quote not found in what it read)"
                                                                 if quote else "LLM (no quote)"),
                             "quote": quote, "verified": bool(where), "note": ""}
            if needed and quote and not where:
                problems.append(f"{tag}: the {key} quote was not found word for word in any text you were shown")
            return evidence[key]

        bio = checked("biocontext", row["biocontext"])
        if not bio["verified"] or not row["level_1_context"]:
            issues.append("context_unverified")
        if row["level_1_context"] and row["level_1_context"] not in s.target.level_1:
            issues.append("new_level_1_context")
        if any(x and not rules.LABEL_OK.match(x) for x in (row["biocontext"], row["level_1_context"], row["level_2_context"])):
            issues.append("label_format")

        n = checked("sample_size", row["sample_size"])
        if not row["sample_size"]:
            issues.append("sample_size_missing")
        elif not (n["verified"] and rules.number_in(int(row["sample_size"]), n["quote"])):
            n["verified"] = False
            issues.append("sample_size_unverified")

        pop = checked("population", row["population"])
        if not row["population"]:
            issues.append("population_missing")
        elif not (pop["verified"] and rules.quote_supports_population(row["population"], pop["quote"])):
            pop["verified"] = False
            issues.append("population_unverified")

        build = str(raw.get("genome_build") or "").strip()
        build = {"hg38": "GRCh38", "hg19": "GRCh37"}.get(build.lower(), build)
        lifted = [s.lifted[f] for f in (v or {}).get("files") or [] if f in s.lifted]
        seen = "" if lifted else rules.build_from_text(row["file_path"] + " " + " ".join((v or {}).get("header", [])))
        gb = checked("genome_build", build, needed=not seen)
        if lifted:
            n, kept = sum(x["rows"] for x in lifted), sum(x["lifted"] for x in lifted)
            gb.update(value="GRCh38", note=f"lifted over from GRCh37 (UCSC hg19ToHg38 chain): {kept:,} of {n:,} "
                      f"variants kept, {n - kept:,} without a GRCh38 position dropped; GRCh37 coordinates kept "
                      "in chrom_grch37 / pos_grch37")
            build = "GRCh38"
        elif seen:
            build = seen
            gb.update(value=seen, verified=True, source="file path or header")
        elif build not in ("GRCh38", "GRCh37") or not gb["verified"]:
            issues.append("genome_build_unknown")
        if build == "GRCh37":
            issues.append("needs_liftover")
            problems.append(f"{tag}: the coordinates are GRCh37; call liftover on {row['file_path']!r}, then "
                            "add_rsid and validate again")

        mode = row["gene_id_mode"]
        if row["qtl_type"] in rules.GENE_QTL_TYPES and mode not in ("self", "phenotype_last_segment"):
            row["gene_id_mode"] = "self"
        elif row["qtl_type"] not in rules.GENE_QTL_TYPES:
            row["gene_id_mode"] = ""
        if s.paper is None or not s.paper.full_text:
            issues.append("paper_full_text_not_read")

        source_url = str(raw.get("source_url") or row["url"] or "")
        processed = "_processed" in row["file_path"]
        evidence["dataset"] = {"value": row["dataset"], "source": "LLM", "quote": "", "verified": None, "note": ""}
        evidence["file_path"] = {"value": row["file_path"], "source": "downloaded" + (" and processed by the AI's Python" if processed else ""),
                                 "quote": "", "verified": bool(v and v.get("ok")), "note": f"relative to the data folder {s.root}"}
        # A non-full table (not every tested pair) is kept only when no full one exists anywhere,
        # and says why it is not full: a reason from NON_FULL_REASONS and the numbers behind it.
        non_full = str(raw.get("scope") or "full").strip().lower().replace("-", "_") in ("non_full", "partial", "subset")
        reason = str(raw.get("non_full_reason") or "").strip().lower()
        scope_note = str(raw.get("scope_note") or "").strip()[:300]
        if non_full:
            issues.append("non_full_summary_statistics")
            if reason not in NON_FULL_REASONS:
                problems.append(f"{tag}: scope is non_full; set non_full_reason to one of {', '.join(NON_FULL_REASONS)}")
            if len(re.findall(r"(?:https?|ftp)://", str(raw.get("source_url") or ""))) > 1:
                problems.append(f"{tag}: a non-full table must come from one source; give one source_url and do not "
                                "merge tables from different files, deposits or analyses")
            if not re.search(r"\d", scope_note):
                problems.append(f"{tag}: scope is non_full; say in scope_note why the table is not full, with numbers: "
                                "what the paper tested and what the table holds (counts or threshold)")
        holds = "non_full" if non_full else "all_pairs"
        label = NON_FULL_REASONS.get(reason, "reason not given") if non_full else ""
        evidence["holds"] = {"value": holds, "source": "LLM", "quote": "", "verified": None,
                             "reason": reason if non_full else "", "reason_label": label,
                             "note": (f"Not full summary statistics — {label}. {scope_note}" if non_full
                                      else str(raw.get("note") or ""))[:400]}
        evidence["qtl_type"] = {"value": row["qtl_type"], "source": "LLM", "quote": "", "verified": None, "note": ""}
        evidence["context"] = bio | {"value": ", ".join(x for x in (row["level_1_context"], row["level_2_context"], row["biocontext"]) if x)}
        if v:
            evidence["table"] = {"columns": v.get("header", []), "rows": v.get("sample", []), "has_header": True, "fmt": "text",
                                 "note": f"read the importer's way: {v.get('n_files')} file(s), {v.get('total_gb')} GB"}
            evidence["data"] = {"status": "processed" if processed else "as_is", "plan_source": "LLM code" if processed else "",
                                "checks": v.get("checks"), "kept_share": v.get("kept_share_after_importer_filters"),
                                "steps": [f"{x['purpose']} ({x['script']}, exit {x['exit_code']})" for x in s.scripts][:12],
                                "outputs": v.get("files"), "n_outputs": v.get("n_files"),
                                "preview": {"columns": v.get("header", []), "rows": v.get("sample", [])},
                                "problems": v.get("problems")}
        c = Candidate(location=source_url, landing=row["url"] or source_url, glob=row["file_path"],
                      files=[probe.RemoteFile(probe._name_of(d["url"]), d["url"], int(d["size_mb"] * 1e6), d["path"])
                             for d in s.downloads if v and d["path"] in (v.get("files") or [])][:30],
                      access="open")
        rows.append({"row": row, "issues": list(dict.fromkeys(x for x in issues if x in rules.ISSUES)),
                     "genome_build": build, "holds": holds, "evidence": evidence, "candidate": c,
                     "llm_note": str(raw.get("note") or "")[:300]})
    seen_bio: dict[tuple[str, str], int] = {}
    for r in rows:
        k = (r["row"]["qtl_type"], r["row"]["biocontext"])
        seen_bio[k] = seen_bio.get(k, 0) + 1
    for r in rows:
        if seen_bio[(r["row"]["qtl_type"], r["row"]["biocontext"])] > 1:
            r["issues"].append("duplicate_biocontext")
            problems.append(f"two rows share qtl_type {r['row']['qtl_type']} and biocontext {r['row']['biocontext']}")
        r["status"] = rules.status_of(r["issues"])
    return rows, list(dict.fromkeys(problems))


def _int(value: Any) -> int | None:
    try:
        n = int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


# ── the loop ─────────────────────────────────────────────────────────────────


def first_message(s: Session, previous: list[str]) -> str:
    r, p = s.record, s.paper
    locs = records._locations_of(r)
    lines = [
        f"Title: {r.get('publication_title', '')}",
        f"Authors: {r.get('authors', '')[:300]}",
        f"Journal: {r.get('journal', '')} {r.get('year', '')}   DOI: {r.get('doi', '')}   PMID: {r.get('pmid', '')}",
        f"Article: {r.get('publication_url', '')}",
        f"Record code: {r.get('record_code') or '(none)'} — every download and processed file of this paper goes "
        f"to the folder {s.folder_name()}/, so each file_path starts with {s.folder_name()}/",
        f"Earlier AI check — QTL types: {r.get('qtl_type', '')}; context: {r.get('qtl_context', '')}; species: {r.get('species', '')}",
        "",
        "Saved data locations (found when the paper was screened; may be incomplete or wrong):",
        *[f"- {x.get('download_url')} [{x.get('content')}; {x.get('access_route')}] {x.get('extraction_note', '')[:200]}" for x in locs],
    ]
    if p is not None:
        lines += ["", f"Paper text: {'FULL TEXT' if p.full_text else 'abstract/page only'}, "
                  f"{len(p.paragraphs) + len(p.availability)} paragraphs (read_paper).",
                  f"Abstract: {llm._strip_tags(p.abstract)[:2000]}",
                  "Data availability statements:", *[f"- {a[:1500]}" for a in p.availability[:6]]]
        sections = list(dict.fromkeys(m.group(1) for x in p.paragraphs if (m := re.match(r"\[([^\]]{1,60})\]", x))))
        if sections:
            lines.append("Sections: " + "; ".join(sections[:40]))
    else:
        lines += ["", "The paper text could not be fetched: open the article with open_url."]
    doi = (r.get("doi") or "").strip().lower()
    if s.target.publications.get(doi):
        have = s.target.rows.get(doi, [])
        lines += ["", f"Reference manifest ({s.target.path or 'LocusCompare qtl_manifest.tsv'}): this paper is already "
                  f"there as dataset {s.target.publications[doi]}, with {len(have)} row(s) (qtl_type biocontext):",
                  *[f"- {x}" for x in have[:60]],
                  "Use that dataset name; add only QTL types or contexts it does not have yet."]
    else:
        lines += ["", "Reference manifest: this paper is not in it yet."]
    if previous:
        lines += ["", "Files already in the data folder from an earlier build of this paper (reuse them):",
                  *[f"- {x}" for x in previous[:40]]]
    lines += ["", f"Download limit for this paper: {s.max_bytes / 1e9:g} GB. "
              f"Free disk: {shutil.disk_usage(s.root).free / 1e9:.0f} GB."]
    return "\n".join(lines)


def prune(messages: list[dict[str, Any]]) -> None:
    """Cut old tool results so the context stays small; the newest stay whole."""
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    for i in tool_idx[:-KEEP_FULL]:
        c = messages[i]["content"]
        if len(c) > OLD_TOOL_CHARS:
            messages[i]["content"] = c[:OLD_TOOL_CHARS] + f" …[cut; {len(c)} chars]"


def short(args: dict[str, Any]) -> str:
    keep = {k: (v if not isinstance(v, str) else v[:120]) for k, v in args.items() if k not in ("code", "rows")}
    if "code" in args:
        keep["code_lines"] = args["code"].count("\n") + 1
    if "rows" in args:
        keep["rows"] = len(args.get("rows") or [])
    return json.dumps(keep, ensure_ascii=False)[:300]


async def run(
    record: dict[str, Any], judge: llm.Judge, *, root: Path, max_bytes: float, target: rules.Target,
    paper: finder.Paper | None, step: Callable[[str], None], cancel: threading.Event,
    usage: dict[str, int], previous: list[str] = (),
) -> dict[str, Any]:
    """Let the model build this paper. Returns rows, location reports, model, note, log."""
    s = Session(record, paper, root, max_bytes, target, cancel, step)
    root.mkdir(parents=True, exist_ok=True)
    if paper is not None:
        s.see("paper full text" if paper.full_text else "paper abstract / page",
              "\n".join([paper.title, paper.abstract, *paper.availability, *paper.paragraphs]))
    for loc in records._locations_of(record):
        s.location(loc["download_url"], content=loc.get("content", ""), access=loc.get("access_route", ""))
    system = SYSTEM.format(level_1=", ".join(target.level_1), datasets=", ".join(target.datasets) or "GTEx_v10")
    system += "\n\n" + skill()
    messages: list[dict[str, Any]] = [{"role": "system", "content": system},
                                      {"role": "user", "content": first_message(s, list(previous))}]
    tools = [{"type": "function", "function": t} for t in TOOLS]
    model, note, nudges = "", "", 0
    for n in range(MAX_STEPS + 6):
        if cancel.is_set():
            raise fetch.Cancelled
        if n == MAX_STEPS or usage["total_tokens"] > MAX_TOKENS:
            messages.append({"role": "user", "content": "The step budget for this paper is used up. Call submit now "
                             "with the rows that passed validate (or give_up)."})
        prune(messages)
        step(f"AI working (step {n + 1}, {usage['total_tokens']:,} tokens so far)")
        try:
            msg, model = await call_with_patience(judge, messages, tools, usage, step, cancel)
        except llm.LLMFailed as exc:
            note = f"LLM step failed: {exc}"
            break
        calls = msg.get("tool_calls") or []
        messages.append({"role": "assistant", "content": msg.get("content") or "", **({"tool_calls": calls} if calls else {})})
        if not calls:
            nudges += 1
            if nudges > 3:
                note = "the AI stopped without submitting"
                break
            messages.append({"role": "user", "content": "Keep working with the tools; finish with submit or give_up."})
            continue
        for call in calls:
            name = call.get("function", {}).get("name", "")
            try:
                args = json.loads(call.get("function", {}).get("arguments") or "{}")
            except ValueError:
                args = {}
            out = await dispatch(s, name, args)
            s.log.append({"tool": name, "args": short(args), "result": json.dumps(out, ensure_ascii=False)[:300]})
            if TRACE:
                print(f"[#{record['record_id']} step {n + 1}] {name} {short(args)} -> {s.log[-1]['result']}",
                      file=sys.stderr, flush=True)
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                             "content": json.dumps(out, ensure_ascii=False, default=str)[:TOOL_CHARS]})
        if s.result is not None:
            break
    else:
        note = note or "the AI did not submit within the step budget"
    res = s.result or {"rows": [], "skipped": [], "summary": "", "reason": note or "no submission"}
    reports = list(s.locations.values())
    for t in res.get("skipped") or []:
        if isinstance(t, dict):
            url = str(t.get("url") or "")
            target_loc = s.locations.get(url) or (reports[0] if reports else None)
            if target_loc is None:
                s.location(url or "(paper)", status="listed")
                target_loc = s.locations[url or "(paper)"]
            target_loc["inventory"].append({"path": str(t.get("path") or url)[:200], "n_files": 1, "size": None,
                                            "holds": "skipped", "why": str(t.get("reason") or "")[:300]})
    for loc in reports:
        loc["status"] = loc["status"] or "not_checked"
    return {"rows": res.get("rows") or [], "reports": reports, "model": model,
            "note": note if not res.get("rows") else "",
            "summary": res.get("summary") or res.get("reason") or note, "log": s.log, "scripts": s.scripts}


QUOTA_WAITS = int(os.environ.get("MANIFEST_QUOTA_WAITS") or 30)  # × 2 minutes


async def call_with_patience(judge: llm.Judge, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                             usage: dict[str, int], step: Callable[[str], None], cancel: threading.Event,
                             ) -> tuple[dict[str, Any], str]:
    """One model turn. A usage limit is often a rolling window: wait for it (up to an hour by
    default) instead of throwing away the work done on this paper so far."""
    for wait in range(QUOTA_WAITS + 1):
        try:
            return await judge.chat_tools(messages, tools, usage, CALL_TIMEOUT)
        except llm.LLMFailed as exc:
            if "usage limit" not in str(exc) or wait == QUOTA_WAITS:
                raise
            step(f"the LLM's usage limit is reached; waiting to retry ({wait + 1} of {QUOTA_WAITS}, every 2 min)")
            for _ in range(120):
                if cancel.is_set():
                    raise fetch.Cancelled from exc
                await asyncio.sleep(1)
    raise llm.LLMFailed("unreachable")


async def dispatch(s: Session, name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "submit":
        rows, problems = check_submission(s, args)
        s.submit_rounds += 1
        if problems and s.submit_rounds < 3:
            return {"accepted": False, "problems": problems,
                    "hint": "fix these (validate again, copy quotes verbatim, move unusable tables to skipped_tables) and submit again"}
        s.result = {"rows": rows, "skipped": args.get("skipped_tables") or [], "summary": str(args.get("summary") or "")[:1000]}
        return {"accepted": True, "rows": len(rows), "remaining_problems": problems}
    if name == "give_up":
        s.result = {"rows": [], "skipped": [], "reason": str(args.get("reason") or "")[:1000],
                    "summary": str(args.get("reason") or "")[:1000]}
        return {"accepted": True}
    fn = {"read_paper": s.read_paper, "open_url": s.open_url, "list_files": s.list_files, "peek_file": s.peek_file,
          "download": s.download, "inspect_local": s.inspect_local, "run_python": s.run_python,
          "validate": s.validate, "liftover": s.liftover, "add_rsid": s.add_rsid,
          "note_location": s.note_location}.get(name)
    if fn is None:
        return {"error": f"no tool {name!r}"}
    s.step(f"AI: {name} {short(args)[:100]}")
    try:
        return await asyncio.to_thread(fn, **args)
    except fetch.Cancelled:
        raise
    except TypeError as exc:
        return {"error": f"bad arguments: {exc}"}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"[:500]}


