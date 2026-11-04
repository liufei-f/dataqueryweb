"""The other allele of a variant from the study's own SNP info file (a fixed program, not the AI's code).

Some releases give only the effect allele in their association tables and document a companion
SNP info / variant map (GEO's ``GSE227136_ILD_eQTL_SNPinfo.tsv.gz``, for instance) holding each
tested variant's chromosome, position, id and two alleles. The map is indexed once into a SQLite
file next to it (``<map>.alleles.sqlite``) and then read by chromosome and position.

A row gets the other allele only when the map's records at its position (narrowed to the row's
rs id when the map lists it, ids like ``rs1;1_10505_A_T`` split on ``;``) hold exactly two
single-base alleles, one of them the row's effect allele. A multiallelic site, an allele the map
does not list and a position it lacks keep an empty allele: never guessed. ``Lookup`` here has
the same ``other_alleles`` as ``dbsnp.AlleleLookup``, so ``dbsnp.fill_alleles`` uses either.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pandas as pd

SNV = re.compile(r"[ACGT]")
CHUNK_ROWS = 500_000
ROLES = ("chrom", "pos", "id", "allele1", "allele2")


def index_path(map_file: Path) -> Path:
    return map_file.with_name(map_file.name + ".alleles.sqlite")


def _norm_chrom(s: pd.Series) -> pd.Series:
    return s.astype(str).str.strip().str.replace(r"(?i)^chr", "", regex=True)


def build_index(map_file: Path, columns: dict[str, str], header: bool, sep: str = "\t",
                step: Callable[[str], None] = lambda _: None) -> dict[str, int]:
    """Index the map (chrom, pos, id, allele1, allele2) into SQLite; reused when already built for these columns."""
    db_path = index_path(map_file)
    spec = repr(sorted(columns.items())) + f"|{header}|{sep}|{map_file.stat().st_size}"
    if db_path.exists():
        with sqlite3.connect(db_path) as db:
            try:
                row = db.execute("select value from meta where key='spec'").fetchone()
                if row and row[0] == spec:
                    n = db.execute("select value from meta where key='rows'").fetchone()
                    return {"rows": int(n[0]) if n else 0, "reused": 1}
            except sqlite3.DatabaseError:
                pass
        db_path.unlink()
    tmp = db_path.with_name(db_path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    rows = 0
    db = sqlite3.connect(tmp)
    try:
        db.executescript("pragma journal_mode=off; pragma synchronous=off;"
                         "create table m (chrom text, pos integer, id text, a1 text, a2 text);"
                         "create table meta (key text primary key, value text);")
        def col(df: pd.DataFrame, role: str) -> pd.Series:
            name = columns.get(role, "")
            if not name:
                return pd.Series("", index=df.index)
            return df[int(name)] if not header and str(name).isdigit() else df[name]

        reader = pd.read_csv(map_file, sep=sep, dtype=str, header=0 if header else None, chunksize=CHUNK_ROWS,
                             keep_default_na=False, compression="infer")
        for df in reader:
            part = pd.DataFrame({"chrom": _norm_chrom(col(df, "chrom")),
                                 "pos": pd.to_numeric(col(df, "pos"), errors="coerce"),
                                 "id": col(df, "id").astype(str).str.strip(),
                                 "a1": col(df, "allele1").astype(str).str.strip().str.upper(),
                                 "a2": col(df, "allele2").astype(str).str.strip().str.upper()}).dropna(subset=["pos"])
            part["pos"] = part["pos"].astype(int)
            db.executemany("insert into m values (?,?,?,?,?)", part.itertuples(index=False, name=None))
            rows += len(df)
            step(f"indexing the SNP info file: {rows:,} rows")
        step("indexing the SNP info file: building the position index")
        db.execute("create index m_pos on m (chrom, pos)")
        db.executemany("insert into meta values (?,?)", [("spec", spec), ("rows", str(rows))])
        db.commit()
    finally:
        db.close()
    tmp.replace(db_path)
    return {"rows": rows, "reused": 0}


class Lookup:
    """Other alleles from an indexed study SNP info file."""

    def __init__(self, map_file: Path) -> None:
        self.db = sqlite3.connect(index_path(map_file), check_same_thread=False)

    def close(self) -> None:
        self.db.close()

    def other_alleles(self, chrom: pd.Series, pos: pd.Series, known: pd.Series,
                      rsid: pd.Series | None = None) -> tuple[pd.Series, dict[str, int]]:
        df = pd.DataFrame({"chrom": _norm_chrom(chrom), "pos": pd.to_numeric(pos, errors="coerce"),
                           "known": known.astype(str).str.strip().str.upper(),
                           "rsid": rsid.astype(str).str.strip() if rsid is not None else ""})
        out = pd.Series("", index=df.index, dtype=object)
        why = {"filled": 0, "multiallelic": 0, "allele_not_in_map": 0, "not_in_map": 0}
        ok = df.dropna(subset=["pos"])
        why["not_in_map"] += len(df) - len(ok)
        keys = sorted({(c, int(p)) for c, p in zip(ok["chrom"], ok["pos"], strict=True)})
        found: dict[tuple[str, int], list[tuple[set[str], set[str]]]] = {}
        self.db.execute("create temp table if not exists k (chrom text, pos integer)")
        self.db.execute("delete from k")
        self.db.executemany("insert into k values (?,?)", keys)
        for c, p, ids, a1, a2 in self.db.execute(
                "select m.chrom, m.pos, m.id, m.a1, m.a2 from k join m on m.chrom = k.chrom and m.pos = k.pos"):
            alleles = {a for a in (a1, a2) if SNV.fullmatch(a or "")}
            found.setdefault((c, p), []).append((set((ids or "").split(";")), alleles))
        vals = []
        for c, p, a, rs in zip(ok["chrom"], ok["pos"].astype(int), ok["known"], ok["rsid"], strict=True):
            recs = found.get((c, p), [])
            if rs.startswith("rs") and any(rs in r[0] for r in recs):
                recs = [r for r in recs if rs in r[0]]
            alleles = set().union(*(r[1] for r in recs)) if recs else set()
            if not alleles:
                why["not_in_map"] += 1
                vals.append("")
            elif a not in alleles:
                why["allele_not_in_map"] += 1
                vals.append("")
            elif len(alleles) != 2:
                why["multiallelic"] += 1
                vals.append("")
            else:
                why["filled"] += 1
                vals.append(next(iter(alleles - {a})))
        out[ok.index] = vals
        return out, why

