"""Download the files behind manifest rows and make them loadable.

The raw files of each row worth loading go to ``data/<dataset>/<path in the repository>``.
When the importer (``load_gwas_qtl_catalog.py``: ``pandas.read_csv`` of one delimited text
table with chrom, position, ref, alt, p-value and phenotype columns) can read them as they
are, the row points at them. Otherwise a processed copy is written next to each file as
``<name>_processed.tsv.gz``.

What to do is decided by the LLM after it has seen the real first rows, but only from a
fixed menu of operations (split a column, −log10 p → p, chi-square or z → p); the code
carries the plan out — never code the LLM wrote — and checks the values of the result
before the manifest row changes. A plan that fails the checks falls back to the one the
rules derive from the header; when neither passes, the row says so and why.

``file_path`` in the manifest is relative to the data folder, which the importer takes as
``--data-root``.
"""

from __future__ import annotations

import asyncio
import bz2
import fnmatch
import gzip
import json
import lzma
import math
import os
import re
import shutil
import tarfile
import threading
import time
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO, TYPE_CHECKING, Any

import httpx
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from dataqueryweb import llm, probe
from dataqueryweb import manifest_rules as rules

if TYPE_CHECKING:  # manifest imports this module
    from dataqueryweb.manifest import Candidate

MAX_PAPER_GB = float(os.environ.get("MANIFEST_MAX_GB") or 50)  # per paper, by default
MIN_FREE = 20e9  # never fill the disk past this
CHUNK_ROWS = 500_000
CHECK_ROWS = 2000
PASS_SHARE = 0.95  # share of checked values that must look right
PLAN_TIMEOUT = 240.0
OPS = ("split", "neglog10_to_p", "chisq_to_p", "z_to_p")
COMPRESSED = {".gz": "gzip", ".bgz": "gzip", ".bz2": "bz2", ".xz": "xz", ".zip": "zip"}
TABLE_EXT = (".txt", ".tsv", ".csv", ".tab", ".dat", ".parquet", ".pq", ".xlsx")
# Issues a processed copy settles: every file was read, given a header and the columns.
SETTLED = {"columns_need_deriving", "no_header", "format_needs_conversion",
           "header_inferred_from_sibling", "mixed_headers", "phenotype_values_not_seen"}


class Cancelled(Exception):
    pass


class PlanError(ValueError):
    pass


def data_dir(db: Path) -> Path:
    """$DATAQUERY_DATA_DIR, else this project's ``data/`` for its own database, else a
    ``data`` folder next to the database (tests, copies)."""
    if env := os.environ.get("DATAQUERY_DATA_DIR"):
        return Path(env)
    root = Path(__file__).resolve().parents[3]
    return root / "data" if db.resolve().parent == root / "data" else db.parent / "data"


@dataclass
class Options:
    root: Path
    download: bool = True
    max_bytes: float = MAX_PAPER_GB * 1e9


def folder_name(dataset: str) -> str:
    return re.sub(r"[^a-z0-9_\-]+", "_", dataset.lower()).strip("_") or "unnamed"


def safe_rel(path: str) -> str:
    """A repository path made safe to use under the data folder (no "..", no root)."""
    parts = [p for p in PurePosixPath(path.replace("\\", "/")).parts if p not in ("", ".", "..", "/")]
    return "/".join(parts) or "file"


def stem(name: str) -> str:
    """The name without its compression and table extensions."""
    low = name.lower()
    for group in (tuple(COMPRESSED), TABLE_EXT):
        for ext in group:
            if low.endswith(ext):
                name, low = name[: -len(ext)], low[: -len(ext)]
                break
    return name


def processed_name(name: str) -> str:
    return f"{stem(name)}_processed.tsv.gz"


# ── download ─────────────────────────────────────────────────────────────────


def http_client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": probe.UA, "Accept": "*/*"}, follow_redirects=True,
                        timeout=httpx.Timeout(60.0, connect=20.0, read=300.0))


def download(http: httpx.Client, f: probe.RemoteFile, dest: Path, cancel: threading.Event,
             progress: Callable[[int], None]) -> bool:
    """Fetch ``f`` to ``dest``, resuming a ``.part`` file. False when it was already there."""
    if dest.exists() and (f.size is None or dest.stat().st_size == f.size):
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    with http.stream("GET", f.url, headers={"Range": f"bytes={have}-"} if have else {}) as r:
        if not (r.status_code == 416 and have and have == f.size):
            r.raise_for_status()
            if have and r.status_code != 206:  # the server ignored the range: start again
                have = 0
            with part.open("ab" if have else "wb") as out:
                for block in r.iter_bytes(1 << 20):
                    if cancel.is_set():
                        raise Cancelled
                    out.write(block)
                    progress(len(block))
    got = part.stat().st_size
    if f.size and got != f.size:
        raise OSError(f"{f.name}: got {got:,} bytes, expected {f.size:,}")
    part.replace(dest)
    return True


# ── reading local tables ─────────────────────────────────────────────────────


@dataclass
class Source:
    """One table on disk: a file, or a member of a downloaded archive."""

    label: str  # path shown to people
    path: Path  # the file, or the archive holding the member
    member: str = ""
    archive: str = ""  # "zip" | "tar"

    def raw(self) -> IO[bytes]:
        if self.archive == "zip":
            return zipfile.ZipFile(self.path).open(self.member)
        if self.archive == "tar":
            tf = tarfile.open(self.path)  # noqa: SIM115 — must stay open while the member is read
            fh = tf.extractfile(self.member)
            if fh is None:
                raise OSError(f"{self.member} is not a file in {self.path.name}")
            return fh
        return self.path.open("rb")

    def magic(self) -> bytes:
        with self.raw() as fh:
            return fh.read(6)

    def kind(self) -> str:
        m = self.magic()
        if m.startswith(b"\x1f\x8b"):
            return "gzip"
        if m.startswith(b"BZh"):
            return "bz2"
        if m.startswith(b"\xfd7zXZ"):
            return "xz"
        if m.startswith(b"PAR1"):
            return "parquet"
        if m.startswith(b"PK"):
            return "zip"
        return "text"

    def text(self) -> IO[bytes]:
        """The decompressed bytes."""
        kind, fh = self.kind(), self.raw()
        if kind == "gzip":
            return gzip.GzipFile(fileobj=fh)  # bgzip is gzip too
        if kind == "bz2":
            return bz2.BZ2File(fh)
        if kind == "xz":
            return lzma.LZMAFile(fh)
        if kind == "zip":
            z = zipfile.ZipFile(fh)
            names = [n for n in z.namelist() if not n.endswith("/")]
            if len(names) != 1:
                raise PlanError(f"{self.label} is an archive of {len(names)} files")
            return z.open(names[0])
        return fh


def chunks(src: Source, table: probe.Table, rows: int = CHUNK_ROWS) -> Iterator[pd.DataFrame]:
    """The table in pieces, its columns named as in ``table`` (borrowed header included)."""
    if src.kind() == "parquet":
        with src.raw() as fh:
            pf = pq.ParquetFile(fh)
            for batch in pf.iter_batches(batch_size=rows):
                df = batch.to_pandas()
                if len(df.columns) != len(table.columns):
                    raise PlanError(f"{src.label}: {len(df.columns)} columns, expected {len(table.columns)}")
                df.columns = table.columns
                yield df
        return
    sep = r"\s+" if table.delimiter == " " else table.delimiter
    with src.text() as fh:
        reader = pd.read_csv(fh, sep=sep, header=0 if table.has_header else None, dtype=str,
                             na_filter=False, chunksize=rows, engine="c")
        for df in reader:
            if len(df.columns) != len(table.columns):
                raise PlanError(f"{src.label}: {len(df.columns)} columns, expected {len(table.columns)} "
                                f"({', '.join(map(str, table.columns[:8]))}…)")
            df.columns = table.columns
            yield df


def first_rows(src: Source, table: probe.Table, n: int = CHECK_ROWS) -> pd.DataFrame:
    return next(chunks(src, table, n))


# ── plans: what to do with a table ───────────────────────────────────────────


def _unique(name: str, taken: set[str]) -> str:
    out, i = name, 2
    while out in taken:
        out, i = f"{name}_{i}", i + 1
    taken.add(out)
    return out


def rule_plan(c: Candidate) -> dict[str, Any] | None:
    """The plan the header rules imply: split the variant id or locus, convert the p-value."""
    roles, der, t = c.roles.columns, c.roles.derivable, c.table
    if t is None:
        return None
    taken = set(t.columns)
    columns = {rules.ROLE_COLUMN[r]: h for r, h in roles.items() if r in rules.ROLE_COLUMN}
    steps: list[dict[str, Any]] = []
    parts = ("chrom", "position", "ref", "alt")
    if any(r in der for r in parts):
        src = roles.get("variant_id") or roles.get("locus")
        if not src:
            return None
        names = {"chrom": "chrom", "position": "pos", "ref": "ref", "alt": "alt"}
        into = [(_unique(names[r], taken) if r in der else "") for r in parts[: 4 if roles.get("variant_id") else 2]]
        steps.append({"op": "split", "column": src, "sep": ":_/>-", "into": into})
        for r, name in zip(parts, into, strict=False):
            if name:
                columns[rules.ROLE_COLUMN[r]] = name
    if "pval" in der:
        name = _unique("pval", taken)
        if "neglog10p" in roles:
            steps.append({"op": "neglog10_to_p", "column": roles["neglog10p"], "into": name})
        elif "chisq" in roles:
            steps.append({"op": "chisq_to_p", "column": roles["chisq"], "into": name})
        columns["pval_col"] = name
    return {"steps": steps, "columns": columns, "source": "rule",
            "reason": "derived from the header rules" if steps else "format conversion only"}


def clean_plan(raw: Any, table: probe.Table) -> dict[str, Any]:
    """An LLM plan reduced to what the menu allows; PlanError when it asks for anything else."""
    if not isinstance(raw, dict):
        raise PlanError("not an object")
    steps = []
    for s in raw.get("steps") or []:
        if not isinstance(s, dict) or s.get("op") not in OPS:
            raise PlanError(f"unknown operation {s!r:.80}")
        step = {"op": s["op"], "column": str(s.get("column") or "")}
        if s["op"] == "split":
            into = s.get("into")
            if not isinstance(into, list) or not 2 <= len(into) <= 8:
                raise PlanError("split needs 'into': 2–8 names")
            step |= {"sep": str(s.get("sep") or ":_")[:6], "into": [str(x or "") for x in into]}
        else:
            step["into"] = str(s.get("into") or "pval")
        steps.append(step)
    columns = {str(k): str(v) for k, v in (raw.get("columns") or {}).items()
               if k in rules.ROLE_COLUMN.values() and v}
    return {"steps": steps, "columns": columns, "source": "LLM", "reason": str(raw.get("reason") or "")[:400]}


def _erfc(x: pd.Series) -> pd.Series:
    return pd.Series(np.frompyfunc(math.erfc, 1, 1)(x.to_numpy(dtype=float)).astype(float), index=x.index)


def apply_plan(df: pd.DataFrame, plan: dict[str, Any]) -> pd.DataFrame:
    for s in plan["steps"]:
        col = s["column"]
        if col not in df.columns:
            raise PlanError(f"no column {col!r} in the table")
        if s["op"] == "split":
            pieces = df[col].astype(str).str.split(f"[{re.escape(s['sep'])}]", regex=True, expand=True)
            for i, name in enumerate(s["into"]):
                if name:
                    df[name] = pieces[i] if i in pieces.columns else ""
            continue
        x = pd.to_numeric(df[col], errors="coerce")
        if s["op"] == "neglog10_to_p":
            df[s["into"]] = np.power(10.0, -x)
        elif s["op"] == "chisq_to_p":  # one degree of freedom
            df[s["into"]] = _erfc(np.sqrt(x.clip(lower=0) / 2))
        else:  # z_to_p, two-sided
            df[s["into"]] = _erfc(x.abs() / math.sqrt(2))
    return df


def check(df: pd.DataFrame, columns: dict[str, str]) -> tuple[bool, dict[str, Any]]:
    """Do the columns the importer requires exist and hold the values it expects?"""
    out: dict[str, Any] = {}
    ok = True
    for role in rules.REQUIRED_ROLES:
        col = columns.get(rules.ROLE_COLUMN[role], "")
        if not col or col not in df.columns:
            out[role] = {"column": col, "problem": "missing"}
            ok = False
            continue
        vals = [str(v).strip() for v in df[col].head(CHECK_ROWS)]
        vals = [v for v in vals if v.lower() not in rules.NA]
        test = rules.VALUE_TESTS.get(role, lambda v: bool(v))
        good = sum(1 for v in vals if test(v))
        out[role] = {"column": col, "checked": len(vals), "pass": good, "example": vals[0] if vals else ""}
        if not vals or good / len(vals) < PASS_SHARE:
            ok = False
    return ok, out


# ── one record ───────────────────────────────────────────────────────────────


PLAN_SYSTEM = """You prepare QTL summary-statistics tables for an importer that reads one \
delimited text table with pandas.read_csv and needs, as separate columns: chromosome, \
position, reference allele, alternative allele, nominal p-value and phenotype id (gene, \
protein, peak or intron id). Other columns (beta, se, maf, rsid, variant id) are optional.

For each table you get its columns, its first rows and how fixed rules mapped the columns. \
Decide how to make it loadable, using ONLY these operations, applied in order:
- {"op":"split","column":C,"sep":S,"into":[names]}: split column C at any of the characters \
in S (e.g. ":_" for 1:12345:A:G or chr1_12345_A_G_b38); part i goes to into[i], "" drops it.
- {"op":"neglog10_to_p","column":C,"into":N}: p = 10^-C.
- {"op":"chisq_to_p","column":C,"into":N}: p from a 1-df chi-square statistic.
- {"op":"z_to_p","column":C,"into":N}: two-sided p from a z score.
New column names must not clash with existing ones. Never invent data: if a required \
column cannot be made from what is there (e.g. no alleles anywhere), answer "cannot".

Answer JSON only:
{"plans":[{"id":"T1","action":"process"|"cannot","steps":[...],
  "columns":{"chrom_col":..,"position_col":..,"ref_col":..,"alt_col":..,"pval_col":..,
             "phenotype_id_col":..,"beta_col":..,"se_col":..,"maf_col":..,"rsid_col":..,
             "variant_id_col":..},
  "reason":"one sentence"}]}
"columns" names the column (existing or new) for each manifest field; leave out the ones \
the table does not have. pval_col must be the nominal p-value, not an adjusted one."""


def plan_prompt(groups: list[tuple[str, Candidate, pd.DataFrame, str]]) -> str:
    out = []
    for gid, c, sample, qtl_type in groups:
        rows = sample.head(5).astype(str).map(lambda v: v[:40]).values.tolist()
        mapped = {rules.ROLE_COLUMN.get(r, r): h for r, h in c.roles.columns.items()}
        missing = [r for r in rules.REQUIRED_ROLES if r not in c.roles.columns]
        out.append(json.dumps({"id": gid, "qtl_type": qtl_type, "file": c.path, "columns": list(map(str, sample.columns)),
                               "first_rows": rows, "rules_mapped": mapped, "rules_missing": missing},
                              ensure_ascii=False))
    return "Tables:\n" + "\n".join(out)


def skip_reason(r: dict[str, Any]) -> str:
    """Why a row's files are not worth downloading, or "" when they are."""
    qtl_type = r["row"]["qtl_type"]
    if r["holds"] == "subset":
        return "the file is a selected subset (significant or lead hits), not every tested pair"
    if not qtl_type:
        return "the QTL type is unknown"
    if qtl_type not in rules.MANIFEST_QTL_TYPES:
        return f"{qtl_type} is not a type the manifest holds ({', '.join(rules.MANIFEST_QTL_TYPES)})"
    if r["status"] == "not_loadable":
        blocking = [rules.ISSUES[i].split(";")[0].rstrip(".") for i in r["issues"] if i in rules.BLOCKING]
        return "; ".join(blocking) or "the row cannot be loaded"
    return ""


@dataclass
class RowJob:
    r: dict[str, Any]  # the assembled row
    c: Candidate
    targets: list[tuple[probe.RemoteFile, Path]] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)  # evidence["data"]
    plan: dict[str, Any] = field(default_factory=dict)  # the plan that was carried out
    phenotypes: list[str] = field(default_factory=list)  # sample phenotype ids of the output


def _sources(job: RowJob, folder: Path) -> list[Source]:
    c = job.c
    if not c.member:
        return [Source(str(p.relative_to(folder.parent)), p) for _, p in job.targets]
    archive = job.targets[0][1]
    kind = "zip" if zipfile.is_zipfile(archive) else "tar"
    if kind == "zip":
        with zipfile.ZipFile(archive) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
    else:
        with tarfile.open(archive) as tf:
            names = [m.name for m in tf.getmembers() if m.isfile()]
    hits = sorted((n for n in names if fnmatch.fnmatchcase(n, c.member)), key=rules.natural_key)
    if not hits:
        raise PlanError(f"no member {c.member!r} in {archive.name}")
    base = str(archive.relative_to(folder.parent))
    return [Source(f"{base}::{n}", archive, n, kind) for n in hits]


def _out_path(src: Source, folder: Path) -> Path:
    if src.member:
        archive_dir = src.path.parent / stem(src.path.name)
        return archive_dir / processed_name(PurePosixPath(src.member).name)
    return src.path.with_name(processed_name(src.path.name))


def _manifest_glob(job: RowJob, folder: Path, processed: bool) -> str:
    c, name = job.c, folder.name
    if c.member:
        archive = PurePosixPath(safe_rel(c.files[0].path or c.files[0].name))
        member = PurePosixPath(c.member).name
        return f"{name}/{archive.parent / stem(archive.name)}/{processed_name(member)}".replace("/./", "/")
    rel = PurePosixPath(safe_rel(c.glob))
    return f"{name}/{rel.parent / processed_name(rel.name) if processed else rel}".replace("/./", "/")


def refresh_roles(job: RowJob) -> None:
    """Map the columns again on the real first rows: before the download a parquet head has
    no values, so a packed variant id could not be recognised. Empty mappings are filled."""
    t = job.c.table
    df = first_rows(job.sources[0], t, 50)
    roles = rules.detect_roles(t.columns, df.astype(str).values.tolist(), job.r["row"]["qtl_type"])
    job.c.roles = roles
    for role, header in roles.columns.items():
        col = rules.ROLE_COLUMN.get(role)
        if col and not job.r["row"].get(col):
            job.r["row"][col] = header


def loadable_as_is(job: RowJob) -> list[str]:
    """Why the importer cannot read these files as they are ([] when it can)."""
    c, t, why = job.c, job.c.table, []
    if t is None:
        return ["the header was not read"]
    if c.member:
        why.append("inside an archive")
    if t.fmt != "text":
        why.append(f"{t.fmt} file")
    if not t.has_header:
        why.append("no header row")
    if c.roles.derivable:
        why.append("must derive " + ", ".join(c.roles.derivable))
    if t.delimiter not in ("\t", ",", ";", "|"):
        why.append("whitespace-delimited")
    for src in job.sources[:1] if not c.member else []:
        kind, ext = src.kind(), src.path.suffix.lower()
        if kind == "parquet":
            why.append("parquet file")
        elif (kind != "text" and COMPRESSED.get(ext) != kind) or ext == ".bgz":
            why.append(f"{kind}-compressed but named {src.path.name}")
    return list(dict.fromkeys(why))


def _verify_as_is(job: RowJob) -> tuple[bool, dict[str, Any], pd.DataFrame]:
    """Read every file the way the importer will and check the mapped columns."""
    cols = {k: job.r["row"][k] for k in rules.ROLE_COLUMN.values() if job.r["row"].get(k)}
    first = None
    for src in job.sources:
        df = first_rows(src, job.c.table)
        ok, checks = check(df, cols)
        if not ok:
            return False, checks | {"file": src.label}, df
        first = df if first is None else first
    return True, checks, first


def _process(job: RowJob, plan: dict[str, Any], folder: Path, cancel: threading.Event,
             step: Callable[[str], None]) -> dict[str, Any]:
    stats: dict[str, Any] = {"rows": 0, "outputs": [], "preview": None, "checks": None}
    for k, src in enumerate(job.sources, 1):
        out = _out_path(src, folder)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".part")
        first = True
        try:
            with gzip.open(tmp, "wt", compresslevel=3, newline="") as fh:
                for df in chunks(src, job.c.table):
                    if cancel.is_set():
                        raise Cancelled
                    df = apply_plan(df, plan)
                    if first:
                        ok, checks = check(df, plan["columns"])
                        if not ok:
                            raise PlanError(f"{src.label}: values do not pass the checks: {_failed(checks)}")
                        if stats["preview"] is None:
                            stats["checks"] = checks
                            stats["preview"] = {"columns": list(map(str, df.columns)),
                                                "rows": df.head(5).astype(str).values.tolist(),
                                                "phenotypes": df[plan["columns"]["phenotype_id_col"]].astype(str).head(50).tolist()}
                    df.to_csv(fh, sep="\t", index=False, header=first, lineterminator="\n", float_format="%.6g")
                    stats["rows"] += len(df)
                    first = False
                    step(f"processing {src.label}" + (f" (file {k} of {len(job.sources)})" if len(job.sources) > 1 else "")
                         + f": {stats['rows']:,} rows written")
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        tmp.replace(out)
        stats["outputs"].append(str(out.relative_to(folder.parent)))
    return stats


def _failed(checks: dict[str, Any]) -> str:
    bad = []
    for role, c in checks.items():
        if not isinstance(c, dict):
            continue
        if c.get("problem"):
            bad.append(f"{role} {c['problem']}")
        elif c.get("checked") is not None and (not c["checked"] or c["pass"] / c["checked"] < PASS_SHARE):
            bad.append(f"{role} ({c.get('column')}: {c['pass']}/{c['checked']} look right, e.g. {c.get('example')!r})")
    return "; ".join(bad) or "unknown"


def _describe(plan: dict[str, Any]) -> list[str]:
    out = []
    for s in plan["steps"]:
        if s["op"] == "split":
            names = ", ".join(n for n in s["into"] if n)
            out.append(f"split {s['column']} at any of {s['sep']!r} into {names}")
        else:
            out.append(f"{s['into']} = {s['op'].replace('_to_p', '').replace('neglog10', '10^-')}→p of {s['column']}")
    return out or ["rewrite as tab-separated .tsv.gz with a header"]


def _settle(r: dict[str, Any], issues_add: list[str] = (), issues_drop: set[str] = frozenset()) -> None:
    r["issues"] = [i for i in r["issues"] if i not in issues_drop]
    r["issues"] += [i for i in issues_add if i not in r["issues"]]
    r["status"] = rules.status_of(r["issues"])


async def fetch_rows(
    rows: list[dict[str, Any]], dataset: str, judge: llm.Judge | None, usage: dict[str, int],
    opts: Options, *, skip: set[str] = frozenset(), key: Callable[[Candidate], str] = str,
    step: Callable[[str], None] = lambda _: None, cancel: threading.Event | None = None,
) -> str:
    """Download, inspect and if needed process each row's files; update the rows in place.
    Returns "provider · model" of the planning call, if one was made."""
    cancel = cancel or threading.Event()
    folder = opts.root / folder_name(dataset)
    jobs: list[RowJob] = []
    for r in rows:
        c: Candidate = r["candidate"]
        if key(c) in skip:
            continue
        if why := skip_reason(r):
            r["evidence"]["data"] = {"status": "skipped", "note": f"not downloaded: {why}"}
            continue
        job = RowJob(r, c)
        job.targets = [(f, folder / safe_rel(f.path or f.name)) for f in (c.files[:1] if c.member else c.files)]
        r["row"]["file_path"] = _manifest_glob(job, folder, False)
        r["evidence"]["file_path"] = {"value": r["row"]["file_path"], "source": "rule: <dataset>/<path in the repository>",
                                      "quote": "", "verified": None, "note": "relative to the data folder (the importer's --data-root)"}
        jobs.append(job)
    if not opts.download:
        for job in jobs:
            job.r["evidence"]["data"] = {"status": "not_downloaded", "note": "downloading was switched off for this run"}
            _settle(job.r, ["data_not_downloaded"])
        return ""

    # 1. Download, within the per-paper budget and the free disk space.
    used = 0.0
    http = http_client()
    try:
        for i, job in enumerate(jobs, 1):
            size = sum(f.size or 0 for f, _ in job.targets)
            have = sum(p.stat().st_size for _, p in job.targets if p.exists())
            free = shutil.disk_usage(opts.root if opts.root.exists() else opts.root.parent).free
            if used + size > opts.max_bytes or free - (size - have) < MIN_FREE:
                why = (f"{size / 1e9:.1f} GB would pass the {opts.max_bytes / 1e9:g} GB limit for this paper"
                       if used + size > opts.max_bytes else f"only {free / 1e9:.0f} GB free on the disk")
                job.data = {"status": "too_large", "note": why, "bytes": size}
                continue
            used += size
            done, last = 0, 0.0
            label = f"downloading row {i} of {len(jobs)}"

            def progress(n: int, label: str = label, size: int = size) -> None:
                nonlocal done, last
                done += n
                if time.monotonic() - last > 1:
                    last = time.monotonic()
                    step(f"{label}: {done / 1e9:.2f} of {size / 1e9:.2f} GB")

            try:
                step(label)
                fresh = 0
                for f, dest in job.targets:
                    fresh += await _thread(download, http, f, dest, cancel, progress)
                job.sources = await _thread(_sources, job, folder)
                job.data = {"status": "downloaded", "bytes": size, "fresh_files": fresh,
                            "raw": [str(p.relative_to(opts.root)) for _, p in job.targets][:30],
                            "n_raw": len(job.targets)}
            except Cancelled:
                raise
            except Exception as exc:
                job.data = {"status": "download_failed", "note": f"{type(exc).__name__}: {exc}"[:400]}
    finally:
        http.close()

    # 2. Readable as it is? Else find a plan, asking the LLM once for all distinct headers.
    todo: list[tuple[RowJob, list[str]]] = []
    for job in jobs:
        if job.data.get("status") != "downloaded":
            continue
        try:
            await _thread(refresh_roles, job)
            why = await _thread(loadable_as_is, job)
            if not why:
                ok, checks, df = await _thread(_verify_as_is, job)
                if ok:
                    job.data |= {"status": "as_is", "checks": checks,
                                 "preview": {"columns": list(map(str, df.columns)), "rows": df.head(5).astype(str).values.tolist()}}
                    continue
                why = [f"read as it is, the values fail: {_failed(checks)}"]
            todo.append((job, why))
        except Cancelled:
            raise
        except Exception as exc:
            job.data |= {"status": "processing_failed", "note": f"{type(exc).__name__}: {exc}"[:400]}

    model = ""
    llm_plans: dict[str, Any] = {}
    groups: dict[tuple, tuple[str, Candidate, pd.DataFrame, str]] = {}
    job_group: dict[int, str] = {}
    for job, _ in todo:
        sig = (tuple(map(str, job.c.table.columns)), job.c.table.fmt, job.c.table.has_header)
        if sig not in groups:
            try:
                sample = await _thread(first_rows, job.sources[0], job.c.table, 20)
            except Exception as exc:
                job.data |= {"status": "processing_failed", "note": f"could not read {job.sources[0].label}: {exc}"[:400]}
                continue
            groups[sig] = (f"T{len(groups) + 1}", job.c, sample, job.r["row"]["qtl_type"])
        job_group[id(job)] = groups[sig][0]
    if groups and judge is not None:
        step(f"asking the AI how to process {len(groups)} kind{'s' if len(groups) > 1 else ''} of table")
        try:
            answer, model = await judge.ask(PLAN_SYSTEM, plan_prompt(list(groups.values())), usage, PLAN_TIMEOUT)
            llm_plans = {str(p.get("id")): p for p in answer.get("plans") or [] if isinstance(p, dict)}
        except llm.LLMFailed as exc:
            llm_plans = {"_error": str(exc)[:300]}

    # 3. Carry the chosen plan out, file by file.
    for job, why in todo:
        if job.data.get("status") != "downloaded" or id(job) not in job_group:
            continue
        gid = job_group[id(job)]
        raw = llm_plans.get(gid)
        tried: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        if isinstance(raw, dict) and raw.get("action") == "process":
            try:
                candidates.append(clean_plan(raw, job.c.table))
            except PlanError as exc:
                tried.append({"source": "LLM", "result": f"rejected: {exc}"})
        elif isinstance(raw, dict):
            tried.append({"source": "LLM", "result": f"said it cannot: {raw.get('reason', '')}"[:300]})
        elif "_error" in llm_plans:
            tried.append({"source": "LLM", "result": f"no answer: {llm_plans['_error']}"})
        if (rp := rule_plan(job.c)) is not None:
            candidates.append(rp)
        done = None
        for plan in candidates:
            plan["columns"] = {**{k: v for k, v in job.r["row"].items() if k in rules.ROLE_COLUMN.values() and v},
                               **plan["columns"]}
            try:
                sample = apply_plan(await _thread(first_rows, job.sources[0], job.c.table), plan)
                ok, checks = check(sample, plan["columns"])
                if not ok:
                    raise PlanError(f"on the first rows: {_failed(checks)}")
                step(f"processing {job.c.path} with the {'AI' if plan['source'] == 'LLM' else 'rule'} plan")
                done = (plan, await _thread(_process, job, plan, folder, cancel, step))
                break
            except Cancelled:
                raise
            except Exception as exc:
                tried.append({"source": plan["source"], "steps": _describe(plan), "result": f"failed: {exc}"[:400]})
        if done is None:
            job.data |= {"status": "processing_failed", "why_processing": why, "tried": tried,
                         "note": "no plan produced values the importer accepts"}
            continue
        plan, stats = done
        job.data |= {"status": "processed", "why_processing": why, "plan_source": plan["source"],
                     "steps": _describe(plan), "plan_reason": plan.get("reason", ""), "tried": tried,
                     "rows": stats["rows"], "outputs": stats["outputs"][:30], "n_outputs": len(stats["outputs"]),
                     "checks": stats["checks"], "preview": {k: v for k, v in stats["preview"].items() if k != "phenotypes"}}
        job.plan, job.phenotypes = plan, stats["preview"]["phenotypes"]

    # 4. Point the rows at the data and settle the issues.
    for job in jobs:
        r, d, row = job.r, job.data, job.r["row"]
        status = d.get("status")
        if status in ("as_is", "processed"):
            processed = status == "processed"
            row["file_path"] = _manifest_glob(job, folder, processed)
            r["evidence"]["file_path"] = {"value": row["file_path"], "source": "downloaded" + (" and processed" if processed else ""),
                                          "quote": "", "verified": True, "note": f"relative to the data folder {opts.root}"}
            if processed:
                row["delimiter"] = "\\t"
                row.update({k: v for k, v in job.plan["columns"].items() if k in rules.ROLE_COLUMN.values()})
                mode, problem = rules.gene_id_mode(job.phenotypes, row["qtl_type"])
                row["gene_id_mode"] = mode
                drop = SETTLED | {"phenotype_id_not_ensembl", "peak_id_needs_reformat", "peak_id_not_chr_start_end"}
                _settle(r, [problem] if problem else [], drop)
            else:
                _settle(r, [], {"header_inferred_from_sibling", "mixed_headers"})
        else:
            issue = {"too_large": "data_too_large", "download_failed": "download_failed",
                     "processing_failed": "processing_failed"}.get(status, "data_not_downloaded")
            _settle(r, [issue])
        r["evidence"]["data"] = d
    return model


async def _thread(fn: Callable[..., Any], *args: Any) -> Any:
    return await asyncio.to_thread(fn, *args)
