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


def annotate(path: Path, lookup: Lookup, columns: dict[str, str], rsid_col: str = "rsid",
             step: Callable[[str], None] = lambda _: None) -> dict[str, Any]:
    """Write ``rsid_col`` into a tab-separated .tsv.gz table in place; returns the counts.

    An rs id the table already holds is kept; only empty ones are filled from dbSNP.
    """
    tmp = path.with_name(path.name + ".rsid.tmp")
    rows = matched = 0
    first = True
    with gzip.open(tmp, "wt") as out:
        for df in pd.read_csv(path, sep="\t", dtype=str, chunksize=CHUNK_ROWS, keep_default_na=False):
            found = lookup.rsids(df[columns["chrom_col"]], df[columns["position_col"]],
                                 df[columns["ref_col"]], df[columns["alt_col"]])
            if rsid_col in df.columns:
                have = df[rsid_col].where(df[rsid_col].str.fullmatch(r"rs\d+").fillna(False), "")
                found = have.where(have != "", found)
            df[rsid_col] = found
            rows += len(df)
            matched += int((df[rsid_col] != "").sum())
            df.to_csv(out, sep="\t", index=False, header=first)
            first = False
            step(f"rs ids: {rows:,} rows, {matched:,} found")
    tmp.replace(path)
    return {"rows": rows, "with_rsid": matched, "share": round(matched / rows, 4) if rows else 0.0,
            "rsid_col": rsid_col}


SNV = re.compile(r"[ACGT]")


class AlleleLookup(Lookup):
    """The other allele of a biallelic SNV, from the dbSNP records at a GRCh38 position."""

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
        why = {"filled": 0, "multiallelic": 0, "allele_not_in_dbsnp": 0, "not_in_dbsnp": 0}
        for c, part in df.dropna(subset=["pos"]).groupby("chrom"):
            contig = self.contigs.get(str(c))
            if not contig:
                why["not_in_dbsnp"] += len(part)
                continue
            p = part["pos"].astype(int)
            found = self._alleles(contig, sorted(set(p)))
            vals = []
            for q, a, rs in zip(p, part["known"], part["rsid"], strict=True):
                recs = found.get(q, [])
                if rs.startswith("rs") and any(r[0] == rs for r in recs):
                    recs = [r for r in recs if r[0] == rs]
                alleles = set().union(*(r[1] for r in recs)) if recs else set()
                if not alleles:
                    why["not_in_dbsnp"] += 1
                    vals.append("")
                elif a not in alleles:
                    why["allele_not_in_dbsnp"] += 1
                    vals.append("")
                elif len(alleles) != 2:
                    why["multiallelic"] += 1
                    vals.append("")
                else:
                    why["filled"] += 1
                    vals.append(next(iter(alleles - {a})))
            out[part.index] = vals
        why["not_in_dbsnp"] += int(df["pos"].isna().sum())
        return out, why


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
    with gzip.open(tmp, "wt") as out:
        for df in pd.read_csv(path, sep="\t", dtype=str, chunksize=CHUNK_ROWS, keep_default_na=False):
            if ref_c not in df.columns:
                df[ref_c] = ""
            have = df[ref_c].str.strip().str.upper().str.fullmatch(r"[ACGT]").fillna(False)
            need = df.index[~have]
            if len(need):
                other, why = lookup.other_alleles(df.loc[need, chrom_c], df.loc[need, pos_c], df.loc[need, alt_c],
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
