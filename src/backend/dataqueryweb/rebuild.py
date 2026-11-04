"""What a rebuild checks before the model's first step.

A build of a paper that was built before starts from what is on disk, not from scratch:

1. **Skill changes** — the skill text each build used is kept next to its log
   (``.build_logs/<record>/<run>.skill.txt``); the diff against the previous build's copy is
   shown to the model, so it knows which rules are new.
2. **Downloaded files** — every downloaded file of the paper gets the program's format check
   (as inspect_local would), and assessments from earlier builds are reused for files that did
   not change since (``.build_logs/<record>/assessments.json``).
3. **Processed files** — every ``*_processed.tsv(.gz)`` is checked: the gzip stream is read to
   the end (an interrupted write leaves a truncated file), then the required formats are tested
   on its first rows (standard columns, chrom 1–22, variant id ``chr<chrom>_<pos>_<ref>_<alt>``,
   caQTL peak ids ``chr<chrom>_<start>_<end>``) and its rs ids against dbSNP. A file that is
   truncated, unreadable or breaks a format is **deleted** so it is processed again into the
   target format; one whose only gap is rs ids is kept for add_rsid. The full-read result is
   cached by size and modification time (``<folder>/.processed_checks.json``).
"""

from __future__ import annotations

import difflib
import gzip
import json
import re
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from dataqueryweb import dbsnp
from dataqueryweb import manifest_rules as rules

if TYPE_CHECKING:
    from dataqueryweb.agent import Session

STANDARD = ("chrom", "pos", "ref", "alt", "variant_id")
CHECKS_FILE = ".processed_checks.json"
ASSESSMENTS_FILE = "assessments.json"
PEAKISH = re.compile(r"(?i)(chr)?[0-9XY]+[-:_][0-9]+[-:_][0-9]+")
DIFF_CHARS = 12000
CHUNK = 16 << 20


# ── 1. skill changes ─────────────────────────────────────────────────────────


def skill_changes(log_dir: Path, run_id: str, skill_text: str) -> str:
    """Keep this build's skill text and describe how it differs from the previous build's."""
    log_dir.mkdir(parents=True, exist_ok=True)
    earlier = sorted(p for p in log_dir.glob("*.skill.txt") if p.name != f"{run_id}.skill.txt")
    (log_dir / f"{run_id}.skill.txt").write_text(skill_text, encoding="utf-8")
    if not earlier:
        return ("No copy of the skill used by earlier builds is kept (they predate this record), so every rule in "
                "PROCEDURE may be new to their outputs: check earlier work against all of it.")
    before = earlier[-1].read_text(encoding="utf-8")
    if before == skill_text:
        return f"The skill is unchanged since the previous build ({earlier[-1].name.removesuffix('.skill.txt')})."
    diff = "".join(difflib.unified_diff(before.splitlines(keepends=True), skill_text.splitlines(keepends=True),
                                        fromfile="previous build", tofile="this build", n=1))
    if len(diff) > DIFF_CHARS:
        diff = diff[:DIFF_CHARS] + "\n… (diff cut; the full current skill is in PROCEDURE)\n"
    return (f"The skill changed since the previous build ({earlier[-1].name.removesuffix('.skill.txt')}). Check "
            "earlier outputs against the changed rules (lines starting with + are new):\n" + diff)


# ── 2. downloaded files: assessments kept across builds ──────────────────────


def _stamp(p: Path) -> dict[str, Any]:
    st = p.stat()
    return {"size": st.st_size, "mtime": st.st_mtime_ns}


def save_assessments(s: Session) -> None:
    path = s.root / ".build_logs" / str(s.record["record_id"]) / ASSESSMENTS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    out = {}
    for rel, a in s.assessments.items():
        f = s.root / rel.partition("::")[0]
        if f.is_file():
            out[rel] = {**_stamp(f), "assessment": a}
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def load_assessments(s: Session) -> list[str]:
    """Reuse earlier verdicts on downloaded files that are unchanged; returns the reused paths."""
    path = s.root / ".build_logs" / str(s.record["record_id"]) / ASSESSMENTS_FILE
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    sources = set(s.source_files())
    reused = []
    for rel, v in saved.items():
        f = s.root / rel.partition("::")[0]
        if rel.partition("::")[0] in sources and f.is_file() and _stamp(f) == {"size": v["size"], "mtime": v["mtime"]}:
            s.assessments[rel] = v["assessment"]
            reused.append(rel)
    return reused


# ── 3. processed files ───────────────────────────────────────────────────────


def processed_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.rglob("*_processed.tsv*") if p.is_file() and p.name.endswith((".tsv", ".tsv.gz")))


def read_to_end(p: Path) -> dict[str, Any]:
    """Decompress the whole file: rows, or why it is broken (a truncated gzip from an interrupted write)."""
    if not p.name.endswith(".gz"):
        with p.open("rb") as fh:
            return {"intact": True, "rows": max(0, sum(chunk.count(b"\n") for chunk in iter(lambda: fh.read(CHUNK), b"")) - 1)}
    rows = 0
    try:
        with gzip.open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(CHUNK), b""):
                rows += chunk.count(b"\n")
    except (EOFError, OSError, zlib.error) as exc:
        return {"intact": False, "error": f"{type(exc).__name__}: {exc}"[:200], "rows_read": rows}
    return {"intact": True, "rows": max(0, rows - 1)}


def intact(paths: list[Path], cache_file: Path, step: Any) -> dict[Path, dict[str, Any]]:
    """read_to_end for each file, cached by size and mtime."""
    try:
        cache = json.loads(cache_file.read_text())
    except (OSError, ValueError):
        cache = {}
    out, todo = {}, []
    for p in paths:
        c = cache.get(p.name)
        if c and {"size": c.get("size"), "mtime": c.get("mtime")} == _stamp(p):
            out[p] = c
        else:
            todo.append(p)
    if todo:
        gb = sum(p.stat().st_size for p in todo) / 1e9
        step(f"rebuild: reading {len(todo)} processed file(s) to the end ({gb:.1f} GB) to find truncated ones")
        done = 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            for p, res in zip(todo, pool.map(read_to_end, todo), strict=True):
                done += 1
                out[p] = res | _stamp(p)
                cache[p.name] = out[p]
                cache_file.write_text(json.dumps(cache, indent=1))
                step(f"rebuild: checked {done}/{len(todo)} processed files ({p.name}: "
                     f"{'intact' if res['intact'] else 'TRUNCATED'})")
    return out


def format_verdict(p: Path, lookup: dbsnp.Lookup | None) -> tuple[str, list[str]]:
    """("ok" | "needs_rsid" | "bad_format", problems) from the first rows of a processed table."""
    from dataqueryweb import agent

    try:
        df = pd.read_csv(p, sep="\t", dtype=str, nrows=agent.VALIDATE_ROWS, na_values=agent.NA_VALUES,
                         keep_default_na=True)
    except Exception as exc:
        return "bad_format", [f"cannot be read as a tab-separated table: {type(exc).__name__}: {exc}"[:200]]
    missing = [c for c in STANDARD if c not in df.columns]
    if missing:
        return "bad_format", [f"lacks the standard column(s) {', '.join(missing)}"]
    cols = {"chrom_col": "chrom", "position_col": "pos", "ref_col": "ref", "alt_col": "alt", "variant_id_col": "variant_id"}
    phen = "phenotype_id" if "phenotype_id" in df.columns else ""
    peaks = bool(re.search(r"(?i)caqtl|atac|chromatin|peak", p.name))
    if phen and not peaks:
        vals = df[phen].dropna().astype(str).head(2000)
        peaks = len(vals) > 0 and vals.str.fullmatch(PEAKISH.pattern).mean() > 0.5
    if phen:
        cols["phenotype_id_col"] = phen
    problems = agent.format_problems(df, cols, p.name, "caQTL" if peaks else "")
    if problems:
        return "bad_format", problems
    if "rsid" not in df.columns:
        return "needs_rsid", ["has no rsid column"]
    lifted = "chrom_grch37" in df.columns
    if lookup is not None and (lifted or rules.build_from_text(p.name) != "GRCh37"):
        found, _ = agent.rsid_problems(df, cols | {"rsid_col": "rsid"}, p.name, lookup)
        if found:
            return "needs_rsid", found
    return "ok", []


def prepare(s: Session, skill_text: str) -> str:
    """Run the rebuild checks; delete broken processed files; return the report for the first message."""
    lines: list[str] = []
    log_dir = s.root / ".build_logs" / str(s.record["record_id"])
    run_id = s.audit.run_id if s.audit else "run"
    lines += ["SKILL CHANGES:", skill_changes(log_dir, run_id, skill_text)]

    folder = s.root / s.folder_name()
    sources = s.source_files()
    reused = load_assessments(s)
    for rel in sources:
        # A tar archive has no index: listing it decompresses the whole file (GBs), so the
        # model inspects archives itself; only plain tables and zips are checked here.
        if rel.endswith((".tar", ".tar.gz", ".tgz")):
            continue
        if rel not in s.checked:
            try:
                s.inspect_local(rel)
            except Exception as exc:  # a file the format check cannot open is the model's to judge
                s.checked[rel] = {"kind": "unreadable", "error": str(exc)[:200]}
    left = s.unassessed()
    lines += ["", f"DOWNLOADED FILES: {len(sources)} in {s.folder_name()}/, each already format-checked by the "
              "program, except tar archives (inspect_local is optional for the others). Assessments reused from "
              f"earlier builds of unchanged "
              f"files: {len(reused)}; still to assess: {len(left)}. assess_file takes a glob for files with the "
              "same header and contents (e.g. every per-cell-type table), so one call can cover a group."]
    if reused:
        lines.append("Reused (re-assess any that the skill changes affect): " + "; ".join(
            f"{r} → {s.assessments[r].get('contents')}/{s.assessments[r].get('processable')}" for r in reused[:40]))

    processed = processed_files(folder)
    if not processed:
        lines += ["", "PROCESSED FILES: none yet."]
        return "\n".join(lines)
    vcf = dbsnp.vcf_path(s.root)
    if s.rsid_lookup is None and vcf.exists() and vcf.with_name(vcf.name + ".tbi").exists():
        s.rsid_lookup = dbsnp.AlleleLookup(vcf)
    whole = intact(processed, folder / CHECKS_FILE, s.step)
    deleted, rsid, ok = [], [], []
    for p in processed:
        rel = str(p.relative_to(s.root))
        w = whole[p]
        if not w.get("intact"):
            verdict, why = "bad_format", [f"truncated or corrupt ({w.get('error')}); an interrupted write"]
        else:
            verdict, why = format_verdict(p, s.rsid_lookup)
        if verdict == "bad_format" and sources:
            p.unlink()
            deleted.append(f"{rel}: {'; '.join(why)[:300]}")
        elif verdict == "bad_format":
            ok.append(f"{rel} (BROKEN but kept: no downloaded source to rebuild it from): {'; '.join(why)[:200]}")
        elif verdict == "needs_rsid":
            rsid.append(rel)
        else:
            ok.append(f"{rel} ({w.get('rows', '?'):,} rows)" if isinstance(w.get("rows"), int) else rel)
    for tmp in folder.rglob("*.tmp"):  # half-written outputs of add_rsid / fill_alleles / liftover
        tmp.unlink()
        deleted.append(f"{tmp.relative_to(s.root)}: leftover temporary file")
    if s.audit:
        s.audit.write("Rebuild check of processed files", {"deleted": deleted, "needs_rsid": rsid, "ok": ok})
    from dataqueryweb import reasons
    for d in deleted:
        reasons.program_decision(s, "rebuild: delete processed file", d.partition(": ")[2], {"deleted": d.partition(": ")[0]})
    for r in rsid:
        reasons.program_decision(s, "rebuild: keep processed file", "target format holds; rs ids dbSNP has are empty "
                                 "→ add_rsid, not reprocessing", {"kept": r})
    for o in ok:
        reasons.program_decision(s, "rebuild: keep processed file", "whole file readable and first rows in the target "
                                 "format with rs ids", {"kept": o})
    lines += ["", f"PROCESSED FILES: {len(processed)} found, checked by the program (whole file read for "
              "truncation; first rows for the required formats and rs ids)."]
    if deleted:
        lines += [f"DELETED {len(deleted)} processed file(s) that were truncated or not in the target format; "
                  "process their sources again into the target format (any checkpoint or stats file of yours "
                  "that lists them is stale):", *[f"- {d}" for d in deleted[:80]]]
    if rsid:
        lines += [f"KEPT, rs ids missing ({len(rsid)}): the format is right but rs ids dbSNP holds are empty; "
                  "call add_rsid on them (a glob works), do not reprocess:", *[f"- {r}" for r in rsid[:80]]]
    if ok:
        lines += [f"KEPT, in the target format ({len(ok)}): reuse them; validate before submitting:",
                  *[f"- {o}" for o in ok[:80]]]
    return "\n".join(lines)
