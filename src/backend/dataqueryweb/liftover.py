"""GRCh37 (hg19) → GRCh38 coordinates for a processed table, with UCSC's chain file.

The chain file is UCSC's ``hg19ToHg38.over.chain.gz``, kept once for all papers at
``data/reference/liftover/`` (or ``$LIFTOVER_CHAIN``). Each variant's position is converted
one base at a time; a variant that lands on the minus strand gets its alleles complemented.
A variant with no GRCh38 position, or one that moves to another chromosome or to an alt /
unplaced contig, is dropped. The GRCh37 chromosome and position are kept in ``chrom_grch37``
and ``pos_grch37``, and the variant id (when the table has one) is rebuilt on GRCh38.
"""

from __future__ import annotations

import gzip
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd

URL = "https://hgdownload.soe.ucsc.edu/goldenPath/hg19/liftOver/hg19ToHg38.over.chain.gz"
FILE_NAME = "hg19ToHg38.over.chain.gz"
CHUNK_ROWS = 500_000
COMPLEMENT = str.maketrans("ACGTN", "TGCAN")
OLD_COLUMNS = ("chrom_grch37", "pos_grch37")


def chain_path(data_root: Path) -> Path:
    env = os.environ.get("LIFTOVER_CHAIN")
    return Path(env) if env else data_root / "reference" / "liftover" / FILE_NAME


class Lifter:
    """GRCh38 positions for GRCh37 ones, cached per (chrom, pos)."""

    def __init__(self, chain: str | Path) -> None:
        from pyliftover import LiftOver  # only needed here

        self.lo = LiftOver(str(chain))
        self.cache: dict[tuple[str, int], tuple[int, str] | None] = {}

    def one(self, chrom: str, pos: int) -> tuple[int, str] | None:
        """(GRCh38 position, strand), or None when the variant does not map to the same chromosome."""
        key = (chrom, pos)
        if key not in self.cache:
            hits = self.lo.convert_coordinate("chr" + chrom, pos - 1) or []
            hit = next((h for h in hits if h[0] == "chr" + chrom), None)
            self.cache[key] = (int(hit[1]) + 1, hit[2]) if hit else None
        return self.cache[key]


def lift(path: Path, lifter: Lifter, columns: dict[str, str],
         step: Callable[[str], None] = lambda _: None) -> dict[str, Any]:
    """Rewrite a tab-separated .tsv.gz table on GRCh38 in place; returns the counts."""
    tmp = path.with_name(path.name + ".lift.tmp")
    chrom_c, pos_c, ref_c, alt_c = (columns[k] for k in ("chrom_col", "position_col", "ref_col", "alt_col"))
    vid_c = columns.get("variant_id_col") or ""
    rows = kept = flipped = 0
    first = True
    with gzip.open(tmp, "wt") as out:
        for df in pd.read_csv(path, sep="\t", dtype=str, chunksize=CHUNK_ROWS, keep_default_na=False):
            if first and set(OLD_COLUMNS) & set(df.columns):
                tmp.unlink(missing_ok=True)
                return {"error": f"{path.name} already holds {', '.join(OLD_COLUMNS)}: it was lifted over before"}
            chrom = df[chrom_c].str.replace(r"(?i)^chr", "", regex=True)
            hits = [lifter.one(c, int(p)) if p.isdigit() else None for c, p in zip(chrom, df[pos_c], strict=True)]
            ok = pd.Series([h is not None for h in hits], index=df.index)
            minus = pd.Series([bool(h and h[1] == "-") for h in hits], index=df.index)
            df.insert(len(df.columns), "chrom_grch37", chrom)
            df.insert(len(df.columns), "pos_grch37", df[pos_c])
            df[chrom_c] = chrom
            df[pos_c] = [str(h[0]) if h else "" for h in hits]
            for c in (ref_c, alt_c):
                df.loc[minus, c] = df.loc[minus, c].str.upper().str.translate(COMPLEMENT)
            df = df[ok]
            if vid_c and vid_c in df.columns:
                df[vid_c] = "chr" + df[chrom_c] + "_" + df[pos_c] + "_" + df[ref_c] + "_" + df[alt_c]
            rows += len(ok)
            kept += len(df)
            flipped += int(minus[ok].sum())
            df.to_csv(out, sep="\t", index=False, header=first)
            first = False
            step(f"liftover: {rows:,} rows, {kept:,} on GRCh38")
    tmp.replace(path)
    return {"rows": rows, "lifted": kept, "dropped": rows - kept, "minus_strand": flipped,
            "share": round(kept / rows, 4) if rows else 0.0}
