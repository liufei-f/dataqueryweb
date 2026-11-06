"""rs ids for chrom / position / ref / alt, from a local copy of NCBI dbSNP (GRCh38).

The reference is NCBI's dbSNP VCF for GRCh38 (``GCF_000001405.40.gz``, every RefSNP) with its
tabix index, kept once for all papers at ``data/reference/dbsnp/`` (or ``$DBSNP_VCF``). Only
the positions a table holds are read, through the index.

A variant gets the rs id of the dbSNP record at the same position with the same ref and alt
allele (one of the record's alt alleles). When none matches, the alleles are tried swapped (a
table whose ref is dbSNP's alt). A variant dbSNP does not hold keeps an empty rs id.
"""

from __future__ import annotations

import gzip
import os
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd

URL = "https://ftp.ncbi.nih.gov/snp/latest_release/VCF/GCF_000001405.40.gz"
FILE_NAME = "GCF_000001405.40.gz"
WINDOW_GAP = 1000  # positions closer than this are read in one tabix query
CHUNK_ROWS = 500_000


def vcf_path(data_root: Path) -> Path:
    env = os.environ.get("DBSNP_VCF")
    return Path(env) if env else data_root / "reference" / "dbsnp" / FILE_NAME


def contig_names(contigs: list[str]) -> dict[str, str]:
    """"1" … "22", "X", "Y", "MT" → dbSNP's RefSeq accession (NC_000001.11 …)."""
    out = {}
    for c in contigs:
        if m := re.fullmatch(r"NC_0000(\d\d)\.\d+", c):
            n = int(m.group(1))
            out[str(n) if n <= 22 else {23: "X", 24: "Y"}.get(n, "")] = c
        elif c.startswith("NC_012920."):
            out["MT"] = c
    out.pop("", None)
    return out


class Lookup:
    """rs ids from a tabix-indexed dbSNP VCF (a local path, or a URL with a local index)."""

    def __init__(self, path: str | Path, index: str | Path | None = None) -> None:
        import pysam  # only needed here

        # INFO fields hold some non-ASCII text; only the first five (ASCII) fields are used.
        self.tbx = pysam.TabixFile(str(path), index=str(index) if index else None, encoding="latin-1")
        self.contigs = contig_names(list(self.tbx.contigs))

    def close(self) -> None:
        self.tbx.close()

    def _records(self, contig: str, positions: list[int]) -> dict[tuple[int, str, str], str]:
        """(pos, ref, alt) → rs id for the dbSNP records at these sorted positions."""
        want = set(positions)
        found: dict[tuple[int, str, str], str] = {}
        start = prev = positions[0]
        for p in [*positions[1:], None]:
            if p is not None and p - prev <= WINDOW_GAP:
                prev = p
                continue
            for line in self.tbx.fetch(contig, start - 1, prev):
                fields = line.split("\t", 5)
                pos = int(fields[1])
                if pos not in want:
                    continue
                for alt in fields[4].split(","):
                    found.setdefault((pos, fields[3], alt), fields[2])
            if p is not None:
                start = prev = p
        return found

    def rsids(self, chrom: pd.Series, pos: pd.Series, ref: pd.Series, alt: pd.Series) -> pd.Series:
        """An rs id per row ("" when dbSNP has none), aligned with the inputs."""
        df = pd.DataFrame({"chrom": chrom.astype(str).str.replace(r"(?i)^chr", "", regex=True),
                           "pos": pd.to_numeric(pos, errors="coerce"),
                           "ref": ref.astype(str).str.upper(), "alt": alt.astype(str).str.upper()})
        out = pd.Series("", index=df.index, dtype=object)
        for c, part in df.dropna(subset=["pos"]).groupby("chrom"):
            contig = self.contigs.get(str(c))
            if not contig:
                continue
            p = part["pos"].astype(int)
            found = self._records(contig, sorted(set(p)))
            keys = list(zip(p, part["ref"], part["alt"], strict=True))
            out[part.index] = [found.get(k) or found.get((k[0], k[2], k[1])) or "" for k in keys]
        return out


def unfilled(lookup: Lookup, chrom: pd.Series, pos: pd.Series, ref: pd.Series, alt: pd.Series,
             rsid: pd.Series, limit: int = 500) -> dict[str, Any]:
    """Rows whose rs id is empty although dbSNP holds the variant (add_rsid was not run, or not finished).

    Up to ``limit`` distinct variants with an empty rs id, spread over the rows, are looked up
    exactly as add_rsid does. A table add_rsid filled has none: an empty rs id is allowed only
    where dbSNP has no record for chrom / pos / ref / alt (or with ref and alt swapped).
    """
    empty = ~rsid.astype("string").str.fullmatch(r"rs\d+").fillna(False)
    keys = pd.DataFrame({"chrom": chrom, "pos": pos, "ref": ref, "alt": alt})[empty.to_numpy()]
    keys = keys.dropna().drop_duplicates()
    if len(keys) > limit:
        keys = keys.iloc[[round(i * (len(keys) - 1) / (limit - 1)) for i in range(limit)]]
    if keys.empty:
        return {"rows": len(rsid), "empty": int(empty.sum()), "checked": 0, "in_dbsnp": 0, "examples": []}
    found = lookup.rsids(keys["chrom"], keys["pos"], keys["ref"], keys["alt"])
    hit = keys[found.ne("").to_numpy()]
    examples = [f"chr{str(r.chrom).removeprefix('chr')}_{r.pos}_{r.ref}_{r.alt} → {found[i]}"
                for i, r in hit.head(3).iterrows()]
    return {"rows": len(rsid), "empty": int(empty.sum()), "checked": len(keys), "in_dbsnp": len(hit),
            "examples": examples}


CACHE_FILE = "rsid_cache.sqlite"
OUT_LEVEL = 1  # gzip level of rewritten tables: level 9 took ~45% of add_rsid / fill_alleles time


class Cache:
    """Every variant already looked up in dbSNP, with its rs id ("" when dbSNP has none).

    Shared by all files and papers (``data/reference/dbsnp/rsid_cache.sqlite``, next to the VCF):
    the tables of one study repeat the same variants in every context and every phenotype, so
    each variant is read from dbSNP once and every later table takes it from here.
    """

    def __init__(self, path: Path) -> None:
        # Autocommit: a lookup never leaves a read transaction open. Two builds share this file;
        # an open read snapshot made the other build's later write fail with "database is locked".
        self.db = sqlite3.connect(path, timeout=600, check_same_thread=False, isolation_level=None)
        self.db.execute("pragma journal_mode=wal")
        self.db.execute("pragma synchronous=normal")
        self.db.execute("create table if not exists v (chrom text, pos integer, ref text, alt text, rsid text, "
                        "primary key (chrom, pos, ref, alt)) without rowid")

    def close(self) -> None:
        self.db.close()

    def get(self, keys: list[tuple[str, int, str, str]]) -> dict[tuple[str, int, str, str], str]:
        self.db.execute("create temp table if not exists q (chrom text, pos integer, ref text, alt text)")
        self.db.execute("delete from q")
        self.db.executemany("insert into q values (?,?,?,?)", keys)
        return {(c, p, r, a): rs for c, p, r, a, rs in self.db.execute(
            "select v.chrom, v.pos, v.ref, v.alt, v.rsid from q join v using (chrom, pos, ref, alt)")}

    def put(self, found: dict[tuple[str, int, str, str], str]) -> None:
        # BEGIN IMMEDIATE takes the write lock first, so a second writer waits (busy timeout)
        # instead of failing on a stale snapshot.
        self.db.execute("begin immediate")
        try:
            self.db.executemany("insert or replace into v values (?,?,?,?,?)",
                                [(*k, v) for k, v in found.items()])
            self.db.execute("commit")
        except BaseException:
            self.db.execute("rollback")
            raise


def cache_path(data_root: Path) -> Path:
    return vcf_path(data_root).with_name(CACHE_FILE)


def _keys(chrom: pd.Series, pos: pd.Series, ref: pd.Series, alt: pd.Series) -> pd.DataFrame:
    return pd.DataFrame({"chrom": chrom.astype(str).str.strip().str.replace(r"(?i)^chr", "", regex=True),
                         "pos": pd.to_numeric(pos, errors="coerce"),
                         "ref": ref.astype(str).str.strip().str.upper(),
                         "alt": alt.astype(str).str.strip().str.upper()})


def cached_rsids(lookup: Lookup, cache: Cache | None, chrom: pd.Series, pos: pd.Series, ref: pd.Series,
                 alt: pd.Series) -> tuple[pd.Series, dict[str, int]]:
    """An rs id per row like ``Lookup.rsids``, reading each distinct variant from the cache when it
    is there and from dbSNP (then stored in the cache) when it is not."""
    df = _keys(chrom, pos, ref, alt)
    ok = df.dropna(subset=["pos"]).astype({"pos": int})
    uniq = list(dict.fromkeys(zip(ok["chrom"], ok["pos"], ok["ref"], ok["alt"], strict=True)))
    cache_error = ""
    try:
        known = cache.get(uniq) if cache is not None and uniq else {}
    except sqlite3.Error as exc:  # the cache only saves time: never fail add_rsid because of it
        known, cache_error = {}, f"{type(exc).__name__}: {exc}"
    miss = [k for k in uniq if k not in known]
    if miss:
        m = pd.DataFrame(miss, columns=["chrom", "pos", "ref", "alt"])
        fresh = dict(zip(miss, lookup.rsids(m["chrom"], m["pos"], m["ref"], m["alt"]), strict=True))
        if cache is not None and not cache_error:
            try:
                cache.put(fresh)
            except sqlite3.Error as exc:
                cache_error = f"{type(exc).__name__}: {exc}"
        known |= fresh
    out = pd.Series("", index=df.index, dtype=object)
    out[ok.index] = [known.get(k, "") for k in zip(ok["chrom"], ok["pos"], ok["ref"], ok["alt"], strict=True)]
    return out, {"variants": len(uniq), "from_cache": len(uniq) - len(miss), "from_dbsnp": len(miss),
                 "cache_errors": int(bool(cache_error))}


def annotate(path: Path, lookup: Lookup, columns: dict[str, str], rsid_col: str = "rsid",
             step: Callable[[str], None] = lambda _: None, cache: Cache | None = None) -> dict[str, Any]:
    """Write ``rsid_col`` into a tab-separated .tsv.gz table in place; returns the counts.

    An rs id the table already holds is kept; only empty ones are filled from dbSNP, each distinct
    variant once (through ``cache`` when given, so other tables of the study reuse it).
    """
    tmp = path.with_name(path.name + ".rsid.tmp")
    rows = matched = 0
    stats = {"variants": 0, "from_cache": 0, "from_dbsnp": 0, "cache_errors": 0}
    first = True
    with gzip.open(tmp, "wt", compresslevel=OUT_LEVEL) as out:
        for df in pd.read_csv(path, sep="\t", dtype=str, chunksize=CHUNK_ROWS, keep_default_na=False):
            have = (df[rsid_col].where(df[rsid_col].str.fullmatch(r"rs\d+").fillna(False), "")
                    if rsid_col in df.columns else pd.Series("", index=df.index, dtype=object))
            need = df.index[have == ""]
            if len(need):
                found, st = cached_rsids(lookup, cache, df.loc[need, columns["chrom_col"]],
                                         df.loc[need, columns["position_col"]], df.loc[need, columns["ref_col"]],
                                         df.loc[need, columns["alt_col"]])
                have.loc[need] = found
                for k, v in st.items():
                    stats[k] += v
            df[rsid_col] = have
            rows += len(df)
            matched += int((df[rsid_col] != "").sum())
            df.to_csv(out, sep="\t", index=False, header=first)
            first = False
            step(f"rs ids: {rows:,} rows, {matched:,} found ({stats['from_cache']:,} variants from the cache, "
                 f"{stats['from_dbsnp']:,} from dbSNP"
                 + (f"; cache unavailable in {stats['cache_errors']} chunk(s), read dbSNP directly" if stats["cache_errors"] else "")
                 + ")")
    tmp.replace(path)
    return {"rows": rows, "with_rsid": matched, "share": round(matched / rows, 4) if rows else 0.0,
            "rsid_col": rsid_col, **stats}


SNV = re.compile(r"[ACGT]")


class AlleleLookup(Lookup):
    """The other allele of a biallelic SNV, from the dbSNP records at a GRCh38 position."""

    REASONS = ("filled", "multiallelic", "allele_not_in_dbsnp", "not_in_dbsnp")

    def _alleles(self, contig: str, positions: list[int]) -> dict[int, list[tuple[str, set[str]]]]:
        """pos → [(rs id, {single-base alleles of that record})] for these sorted positions."""
        want = set(positions)
        found: dict[int, list[tuple[str, set[str]]]] = {}
        start = prev = positions[0]
        for p in [*positions[1:], None]:
            if p is not None and p - prev <= WINDOW_GAP:
                prev = p
                continue
            for line in self.tbx.fetch(contig, start - 1, prev):
                fields = line.split("\t", 5)
                pos = int(fields[1])
                if pos not in want or not SNV.fullmatch(fields[3]):
                    continue
                alleles = {fields[3], *(a for a in fields[4].split(",") if SNV.fullmatch(a))}
                found.setdefault(pos, []).append((fields[2], alleles))
            if p is not None:
                start = prev = p
        return found

    def other_alleles(self, chrom: pd.Series, pos: pd.Series, known: pd.Series,
                      rsid: pd.Series | None = None) -> tuple[pd.Series, dict[str, int]]:
        """The other allele per row ("" when it is not one single base), and why rows stay empty.

        The records at the position are narrowed to the row's rs id when it has one. The row's
        allele must be one of theirs and, with it, exactly two single-base alleles must remain;
        a multiallelic site, an allele dbSNP does not list or a position it lacks stays empty.
        """
        df = pd.DataFrame({"chrom": chrom.astype(str).str.replace(r"(?i)^chr", "", regex=True),
                           "pos": pd.to_numeric(pos, errors="coerce"),
                           "known": known.astype(str).str.strip().str.upper(),
                           "rsid": rsid.astype(str).str.strip() if rsid is not None else ""})
        out = pd.Series("", index=df.index, dtype=object)
        reason = pd.Series("not_in_dbsnp", index=df.index, dtype=object)
        for c, part in df.dropna(subset=["pos"]).groupby("chrom"):
            contig = self.contigs.get(str(c))
            if not contig:
                continue
            p = part["pos"].astype(int)
            found = self._alleles(contig, sorted(set(p)))
            vals, why_row = [], []
            for q, a, rs in zip(p, part["known"], part["rsid"], strict=True):
                recs = found.get(q, [])
                if rs.startswith("rs") and any(r[0] == rs for r in recs):
                    recs = [r for r in recs if r[0] == rs]
                alleles = set().union(*(r[1] for r in recs)) if recs else set()
                if not alleles:
                    why_row.append("not_in_dbsnp")
                    vals.append("")
                elif a not in alleles:
                    why_row.append("allele_not_in_dbsnp")
                    vals.append("")
                elif len(alleles) != 2:
                    why_row.append("multiallelic")
                    vals.append("")
                else:
                    why_row.append("filled")
                    vals.append(next(iter(alleles - {a})))
            out[part.index] = vals
            reason[part.index] = why_row
        self.last_reasons = reason  # per row, for callers that look up distinct variants only
        why = {k: int((reason == k).sum()) for k in ("filled", "multiallelic", "allele_not_in_dbsnp", "not_in_dbsnp")}
        return out, why


def distinct_other_alleles(lookup: Any, chrom: pd.Series, pos: pd.Series, known: pd.Series,
                           rsid: pd.Series | None) -> tuple[pd.Series, dict[str, int]]:
    """``lookup.other_alleles`` for each distinct (chrom, pos, allele, rs id) once, spread back over
    the rows. A QTL table repeats every variant for each phenotype; the per-row result and counts
    are the same as asking row by row."""
    keys = pd.DataFrame({"c": chrom.astype(str), "p": pos.astype(str), "k": known.astype(str),
                         "r": rsid.astype(str) if rsid is not None else ""}, index=chrom.index)
    uniq = keys.drop_duplicates()
    other_u, _ = lookup.other_alleles(uniq["c"], uniq["p"], uniq["k"], uniq["r"] if rsid is not None else None)
    table = uniq.assign(other=other_u.to_numpy(), why=lookup.last_reasons.to_numpy())
    rows = keys.reset_index().merge(table, on=["c", "p", "k", "r"], how="left").set_index("index").reindex(keys.index)
    counts = rows["why"].value_counts()
    return rows["other"].fillna(""), {k: int(counts.get(k, 0)) for k in _in_order(lookup)}


def _in_order(lookup: Any) -> tuple[str, ...]:
    """The reason names a lookup reports, in its own order."""
    return tuple(lookup.REASONS)


def fill_alleles(path: Path, lookup: Any, columns: dict[str, str],
                 step: Callable[[str], None] = lambda _: None) -> dict[str, Any]:
    """Fill the empty ref_col (the non-effect allele) of a .tsv.gz table in place.

    ``lookup`` is an ``AlleleLookup`` (dbSNP) or an ``allelemap.Lookup`` (the study's SNP info file).

    alt_col holds the effect allele the file gives. A ref the table already holds is kept;
    an empty one is filled only where dbSNP makes the site a biallelic SNV holding that alt.
    The variant id (when named) is rebuilt where both alleles are known and left empty otherwise.
    """
    tmp = path.with_name(path.name + ".alleles.tmp")
    chrom_c, pos_c, ref_c, alt_c = (columns[k] for k in ("chrom_col", "position_col", "ref_col", "alt_col"))
    vid_c, rs_c = columns.get("variant_id_col") or "", columns.get("rsid_col") or ""
    total = {"rows": 0, "had_ref": 0, "filled": 0, "multiallelic": 0}
    first = True
    with gzip.open(tmp, "wt", compresslevel=OUT_LEVEL) as out:
        for df in pd.read_csv(path, sep="\t", dtype=str, chunksize=CHUNK_ROWS, keep_default_na=False):
            if ref_c not in df.columns:
                df[ref_c] = ""
            have = df[ref_c].str.strip().str.upper().str.fullmatch(r"[ACGT]").fillna(False)
            need = df.index[~have]
            if len(need):
                other, why = distinct_other_alleles(
                    lookup, df.loc[need, chrom_c], df.loc[need, pos_c], df.loc[need, alt_c],
                    df.loc[need, rs_c] if rs_c and rs_c in df.columns else None)
                df.loc[need, ref_c] = other
                for k, v in why.items():
                    total[k] = total.get(k, 0) + v
            total["rows"] += len(df)
            total["had_ref"] += int(have.sum())
            if vid_c:
                ok = df[ref_c].ne("") & df[alt_c].ne("")
                chrom = df[chrom_c].str.replace(r"(?i)^chr", "", regex=True)
                df[vid_c] = ("chr" + chrom + "_" + df[pos_c] + "_" + df[ref_c] + "_" + df[alt_c]).where(ok, "")
            df.to_csv(out, sep="\t", index=False, header=first)
            first = False
            step(f"alleles: {total['rows']:,} rows, {total['filled']:,} filled")
    tmp.replace(path)
    known = total["had_ref"] + total["filled"]
    return total | {"share_with_both_alleles": round(known / total["rows"], 4) if total["rows"] else 0.0}
