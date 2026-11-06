"""read_table: rows of a local table or spreadsheet sheet, read by the program (not the model's code).

The rows this returns count as text the model was shown, so facts copied from them (a context's
sample size in a supplementary table, for instance) pass the quote check at submit. Output printed
by the model's own run_python code never counts: the code could print anything.

Supported: delimited text (``.tsv``, ``.csv``, ``.txt``, optionally ``.gz``) and ``.xlsx`` (read with
the standard library: the sheet's XML and the shared strings).
"""

from __future__ import annotations

import csv
import gzip
import html
import re
import zipfile
from pathlib import Path
from typing import Any

MAX_ROWS = 200  # rows returned per call
MAX_SCAN = 5_000_000  # rows scanned for `find` in a text table
MAX_SHEET_BYTES = 80_000_000  # uncompressed sheet XML read


def _col(ref: str) -> int:
    n = 0
    for ch in ref:
        n = n * 26 + ord(ch) - 64
    return n - 1


def xlsx_sheets(path: Path) -> list[tuple[str, str]]:
    """(sheet name, its XML member) in workbook order, through the workbook relationships."""
    with zipfile.ZipFile(path) as z:
        wb = z.read("xl/workbook.xml").decode("utf-8", "replace")
        rels = z.read("xl/_rels/workbook.xml.rels").decode("utf-8", "replace")
    target = {m.group(1): m.group(2) for m in re.finditer(r'<Relationship\b[^>]*?Id="([^"]+)"[^>]*?Target="([^"]+)"', rels)}
    target |= {m.group(2): m.group(1) for m in re.finditer(r'<Relationship\b[^>]*?Target="([^"]+)"[^>]*?Id="([^"]+)"', rels)}
    out = []
    for m in re.finditer(r"<sheet\b([^>]*)/?>", wb):
        attrs = m.group(1)
        name = re.search(r'\bname="([^"]*)"', attrs)
        rid = re.search(r'\br:id="([^"]*)"', attrs)
        if name and rid and rid.group(1) in target:
            t = target[rid.group(1)].lstrip("/")
            out.append((html.unescape(name.group(1)), t if t.startswith("xl/") else "xl/" + t))
    return out


def xlsx_rows(path: Path, member: str) -> list[list[str]]:
    """Every row of one sheet as text cells (shared strings resolved, empty cells kept)."""
    with zipfile.ZipFile(path) as z:
        try:
            shared = z.read("xl/sharedStrings.xml").decode("utf-8", "replace")
        except KeyError:
            shared = ""
        with z.open(member) as fh:
            xml = fh.read(MAX_SHEET_BYTES).decode("utf-8", "replace")
    strings = [html.unescape("".join(re.findall(r"<t[^>]*>(.*?)</t>", si, re.S)))
               for si in re.findall(r"<si>(.*?)</si>", shared, re.S)]
    rows = []
    for row_xml in re.findall(r"<row\b[^>]*>(.*?)</row>", xml, re.S):
        row: list[str] = []
        for attrs, body in re.findall(r"<c\b([^>]*?)(?:/>|>(.*?)</c>)", row_xml, re.S):
            ref = re.search(r'\br="([A-Z]+)\d+"', attrs)
            col = _col(ref.group(1)) if ref else len(row)
            v = re.search(r"<v>(.*?)</v>", body or "", re.S)
            text = html.unescape("".join(re.findall(r"<t[^>]*>(.*?)</t>", body or "", re.S)))
            if v is not None:
                text = html.unescape(v.group(1))
                if 't="s"' in attrs and text.isdigit():
                    text = strings[int(text)] if int(text) < len(strings) else ""
            while len(row) < col:
                row.append("")
            row.append(text.strip())
        rows.append(row)
    return rows


def _text_rows(path: Path):
    """An open file and a csv reader over it, with the delimiter guessed from its first 64 KB."""
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="replace", newline="") as probe:
        head = probe.read(65536)
    sep = max(("\t", ",", ";"), key=head.count)
    fh = opener(path, "rt", encoding="utf-8", errors="replace", newline="")
    return fh, csv.reader(fh, delimiter=sep)


def read_table(path: Path, sheet: str = "", find: str = "", start: int = 0, limit: int = 50) -> dict[str, Any]:
    """Header and rows of a table: rows ``start``… or, with ``find``, the rows that contain it.

    ``find`` is a case-insensitive regular expression (plain text works) matched against the whole row.
    """
    limit = max(1, min(int(limit or 50), MAX_ROWS))
    start = max(0, int(start or 0))
    pattern = re.compile(find, re.I) if find else None
    if path.name.lower().endswith(".xlsx"):
        sheets = xlsx_sheets(path)
        if not sheet:
            return {"sheets": [n for n, _ in sheets], "next": "name one sheet (sheet=…) to read its rows"}
        pick = [m for n, m in sheets if n == sheet] or ([sheets[int(sheet)][1]] if sheet.isdigit()
                                                        and int(sheet) < len(sheets) else [])
        if not pick:
            return {"error": f"no sheet {sheet!r}; sheets: {', '.join(n for n, _ in sheets)}"}
        all_rows = [r for r in xlsx_rows(path, pick[0]) if any(c for c in r)]
        header, body = (all_rows[0], all_rows[1:]) if all_rows else ([], [])
        idx = [i for i, r in enumerate(body) if pattern.search("\t".join(r))] if pattern else list(range(len(body)))
        chosen = [(i, body[i]) for i in idx[start:start + limit]]
        return {"sheet": sheet, "header": header, "rows": [r for _, r in chosen],
                "row_numbers": [i + 2 for i, _ in chosen], "matched": len(idx), "total_rows": len(body),
                "more": start + limit < len(idx)}
    fh, reader = _text_rows(path)
    try:
        header = next(reader, [])
        rows, numbers, matched, scanned = [], [], 0, 0
        for n, row in enumerate(reader, start=2):
            scanned += 1
            if scanned > MAX_SCAN:
                break
            if pattern and not pattern.search("\t".join(row)):
                continue
            matched += 1
            if matched > start and len(rows) < limit:
                rows.append(row)
                numbers.append(n)
            if not pattern and len(rows) >= limit:
                break
    finally:
        fh.close()
    return {"header": header, "rows": rows, "row_numbers": numbers, "matched": matched if pattern else None,
            "scanned_rows": scanned, "more": bool(pattern and matched > start + limit) or scanned > MAX_SCAN}


def as_text(result: dict[str, Any]) -> str:
    """The rows as lines of tab-joined cells: what the quote check reads."""
    lines = ["\t".join(result.get("header") or [])]
    lines += ["\t".join(r) for r in result.get("rows") or []]
    return "\n".join(lines)
