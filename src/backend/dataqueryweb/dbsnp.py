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
