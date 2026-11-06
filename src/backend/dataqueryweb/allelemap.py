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

import gzip
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

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

    REASONS = ("filled", "multiallelic", "allele_not_in_map", "not_in_map")

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
        ok = df.dropna(subset=["pos"])
        keys = sorted({(c, int(p)) for c, p in zip(ok["chrom"], ok["pos"], strict=True)})
        found: dict[tuple[str, int], list[tuple[set[str], set[str]]]] = {}
        self.db.execute("create temp table if not exists k (chrom text, pos integer)")
        self.db.execute("delete from k")
        self.db.executemany("insert into k values (?,?)", keys)
        for c, p, ids, a1, a2 in self.db.execute(
                "select m.chrom, m.pos, m.id, m.a1, m.a2 from k join m on m.chrom = k.chrom and m.pos = k.pos"):
            alleles = {a for a in (a1, a2) if SNV.fullmatch(a or "")}
            found.setdefault((c, p), []).append((set((ids or "").split(";")), alleles))
        vals, why_row = [], []
        for c, p, a, rs in zip(ok["chrom"], ok["pos"].astype(int), ok["known"], ok["rsid"], strict=True):
            recs = found.get((c, p), [])
            if rs.startswith("rs") and any(rs in r[0] for r in recs):
                recs = [r for r in recs if rs in r[0]]
            alleles = set().union(*(r[1] for r in recs)) if recs else set()
            if not alleles:
                why_row.append("not_in_map")
                vals.append("")
            elif a not in alleles:
                why_row.append("allele_not_in_map")
                vals.append("")
            elif len(alleles) != 2:
                why_row.append("multiallelic")
                vals.append("")
            else:
                why_row.append("filled")
                vals.append(next(iter(alleles - {a})))
        out[ok.index] = vals
        reason = pd.Series("not_in_map", index=df.index, dtype=object)
        reason[ok.index] = why_row
        self.last_reasons = reason  # per row, for callers that look up distinct variants only
        why = {k: int((reason == k).sum()) for k in self.REASONS}
        return out, why



# ── chromosome and position from the rs id (association tables that give only rs ids) ──


def ensure_id_index(map_file: Path, step: Callable[[str], None] = lambda _: None) -> int:
    """A token → (chrom, pos) table in the map's index, one row per ';'-separated id; built once."""
    with sqlite3.connect(index_path(map_file)) as db:
        done = db.execute("select value from meta where key='ids'").fetchone()
        if done:
            return int(done[0])
        db.executescript("drop table if exists ids; create table ids (token text, chrom text, pos integer);")
        n = 0
        cur = db.execute("select id, chrom, pos from m where id is not null and id != ''")
        while batch := cur.fetchmany(200_000):
            rows = [(t.strip(), c, p) for ids, c, p in batch for t in str(ids).split(";") if t.strip()]
            db.executemany("insert into ids values (?,?,?)", rows)
            n += len(rows)
            step(f"indexing the SNP info file's ids: {n:,}")
        db.execute("create index ids_token on ids (token)")
        db.execute("insert or replace into meta values ('ids', ?)", (str(n),))
        db.commit()
    return n


class PositionLookup:
    """Chromosome and position for rs ids, from an indexed study SNP info file."""

    def __init__(self, map_file: Path) -> None:
        self.db = sqlite3.connect(index_path(map_file), check_same_thread=False)

    def close(self) -> None:
        self.db.close()

    def positions(self, ids: pd.Series) -> tuple[pd.Series, pd.Series, dict[str, int]]:
        """(chrom, pos, why) per row: filled only when the map gives the id exactly one position.

        An id listed at two or more positions is ambiguous and left empty; an id the map lacks too.
        """
        keys = ids.astype(str).str.strip()
        wanted = sorted({k for k in keys if k})
        self.db.execute("create temp table if not exists q (token text)")
        self.db.execute("delete from q")
        self.db.executemany("insert into q values (?)", [(k,) for k in wanted])
        found: dict[str, set[tuple[str, int]]] = {}
        for t, c, p in self.db.execute("select ids.token, ids.chrom, ids.pos from q join ids on ids.token = q.token"):
            found.setdefault(t, set()).add((c, p))
        chrom, pos, why = [], [], {"filled": 0, "ambiguous": 0, "not_in_map": 0}
        for k in keys:
            hit = found.get(k, set())
            if len(hit) == 1:
                c, p = next(iter(hit))
                chrom.append(c)
                pos.append(str(p))
                why["filled"] += 1
            else:
                chrom.append("")
                pos.append("")
                why["ambiguous" if len(hit) > 1 else "not_in_map"] += 1
        return pd.Series(chrom, index=ids.index), pd.Series(pos, index=ids.index), why


def fill_positions(path: Path, lookup: PositionLookup, columns: dict[str, str],
                   step: Callable[[str], None] = lambda _: None) -> dict[str, Any]:
    """Fill the empty chrom_col / position_col of a processed .tsv.gz table from its rs ids, in place.

    Positions the table already holds are kept. A row whose position stays unknown (the map lacks
    its id, or lists it at several positions) cannot be imported, so it is removed and counted.
    The variant id (when named) is rebuilt where chrom, pos, ref and alt are all known.
    """
    tmp = path.with_name(path.name + ".positions.tmp")
    chrom_c, pos_c, rs_c = columns["chrom_col"], columns["position_col"], columns["rsid_col"]
    ref_c, alt_c, vid_c = (columns.get(k) or "" for k in ("ref_col", "alt_col", "variant_id_col"))
    total = {"rows": 0, "had_position": 0, "filled": 0, "ambiguous": 0, "not_in_map": 0, "removed": 0}
    first = True
    with gzip.open(tmp, "wt", compresslevel=1) as out:
        for df in pd.read_csv(path, sep="\t", dtype=str, chunksize=CHUNK_ROWS, keep_default_na=False):
            for c in (chrom_c, pos_c):
                if c not in df.columns:
                    df[c] = ""
            have = df[pos_c].str.strip().str.fullmatch(r"\d+").fillna(False) & df[chrom_c].str.strip().ne("")
            need = df.index[~have]
            if len(need):
                c, p, why = lookup.positions(df.loc[need, rs_c])
                df.loc[need, chrom_c] = c
                df.loc[need, pos_c] = p
                for k, v in why.items():
                    total[k] += v
            total["rows"] += len(df)
            total["had_position"] += int(have.sum())
            df[chrom_c] = df[chrom_c].astype(str).str.strip().str.replace(r"(?i)^chr", "", regex=True)
            keep = df[pos_c].astype(str).str.fullmatch(r"\d+").fillna(False)
            total["removed"] += int((~keep).sum())
            df = df[keep]
            if vid_c and ref_c and alt_c and ref_c in df.columns and alt_c in df.columns:
                ok = df[ref_c].ne("") & df[alt_c].ne("")
                df[vid_c] = ("chr" + df[chrom_c] + "_" + df[pos_c] + "_" + df[ref_c] + "_" + df[alt_c]).where(ok, "")
            df.to_csv(out, sep="\t", index=False, header=first)
            first = False
            step(f"positions: {total['rows']:,} rows, {total['filled']:,} filled from the map")
    tmp.replace(path)
    known = total["had_position"] + total["filled"]
    return total | {"share_with_position": round(known / total["rows"], 4) if total["rows"] else 0.0}
