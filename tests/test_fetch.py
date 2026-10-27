"""Downloading the files behind manifest rows and making them loadable (fetch.py)."""

from __future__ import annotations

import asyncio
import gzip
import io
from pathlib import Path

import httpx
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dataqueryweb import fetch, manifest, probe
from dataqueryweb import manifest_rules as rules

COLUMNS = ["phenotype_id", "variant_id", "start_distance", "af", "pval_nominal", "slope", "slope_se"]
ROWS = [["ENSG00000187634", "chr1_14677_G_A_b38", "-14677", "0.05", "0.43", "0.12", "0.15"],
        ["ENSG00000187634", "chr1_16841_G_T_b38", "-12513", "0.11", "1.2e-05", "-0.3", "0.07"],
        ["ENSG00000187634", "chr1_17005_A_G_b38", "-12349", "0.2", "0.9", "0.01", "0.05"]]
RECORD = {"record_id": 3, "doi": "10.1/y", "first_author": "Chen L", "year": "2025", "qtl_type": "eQTL",
          "publication_title": "Blood eQTL", "publication_url": "https://doi.org/10.1/y", "species": "human"}
FACTS = {"study": {}, "units": [{"id": "U1", "qtl_type": "eQTL", "holds": "all_pairs", "biocontext": "blood",
                                 "level_1_context": "Blood", "context_quote": ""}]}


def _parquet(rows: list[list[str]]) -> bytes:
    df = pd.DataFrame(rows, columns=COLUMNS)
    for col in ("start_distance",):
        df[col] = df[col].astype(int)
    for col in ("af", "pval_nominal", "slope", "slope_se"):
        df[col] = df[col].astype(float)
    buf = io.BytesIO()
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), buf)
    return buf.getvalue()


def _row(path: str, body: bytes, fmt: str) -> dict:
    # Parquet heads carry the schema but no values, as probe.py reads them.
    table = probe.Table("", list(COLUMNS), [] if fmt == "parquet" else [list(r) for r in ROWS], fmt=fmt)
    c = manifest.Candidate("https://zenodo.org/records/9", "https://zenodo.org/records/9", path,
                           [probe.RemoteFile(path, f"https://zenodo.org/f/{path}", len(body))],
                           table=table, access="open", cid="U1")
    c.roles = rules.detect_roles(table.columns, table.rows, "eQTL")
    c.holds, c.why = "all_pairs", "test"
    [r] = manifest.assemble(RECORD, None, [c], FACTS, {}, rules.Target(level_1=["Blood"]))
    return r


@pytest.fixture
def serve(monkeypatch):
    files: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, content=files[name]) if name in files else httpx.Response(404)

    monkeypatch.setattr(fetch, "http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(fetch, "MIN_FREE", 0)  # tmp_path may sit on a small RAM disk
    return files


def test_split_and_convert_follow_the_menu_and_are_checked() -> None:
    df = pd.DataFrame([["chr1_14677_G_A_b38", "3.2", "g1"]], columns=["variant_id", "log10p", "gene"])
    plan = {"steps": [{"op": "split", "column": "variant_id", "sep": ":_", "into": ["chrom", "pos", "ref", "alt"]},
                      {"op": "neglog10_to_p", "column": "log10p", "into": "pval"}],
            "columns": {"chrom_col": "chrom", "position_col": "pos", "ref_col": "ref", "alt_col": "alt",
                        "pval_col": "pval", "phenotype_id_col": "gene"}}
    out = fetch.apply_plan(df, plan)
    assert out.loc[0, ["chrom", "pos", "ref", "alt"]].tolist() == ["chr1", "14677", "G", "A"]
    assert abs(out.loc[0, "pval"] - 10 ** -3.2) < 1e-12
    assert fetch.check(out, plan["columns"])[0]
    # A plan that maps the p-value to the effect column fails the value checks.
    bad = plan | {"columns": plan["columns"] | {"pval_col": "log10p"}}
    ok, checks = fetch.check(out, bad["columns"])
    assert not ok and checks["pval"]["pass"] == 0
    with pytest.raises(fetch.PlanError):
        fetch.clean_plan({"steps": [{"op": "exec", "column": "x"}]}, probe.Table("", [], []))
    assert fetch.processed_name("TenK10K.cis_qtl_pairs.chr*.parquet") == "TenK10K.cis_qtl_pairs.chr*_processed.tsv.gz"
    assert fetch.safe_rel("../../etc/passwd") == "etc/passwd"


def test_a_parquet_table_is_downloaded_and_processed(tmp_path: Path, serve) -> None:
    serve["eqtl.chr1.parquet"] = _parquet(ROWS)
    r = _row("eqtl.chr1.parquet", serve["eqtl.chr1.parquet"], "parquet")
    assert "columns_need_deriving" in r["issues"] and r["status"] == "needs_processing"
    opts = fetch.Options(tmp_path / "data")
    asyncio.run(fetch.fetch_rows([r], "Chen_2025", None, {}, opts))  # no LLM: the rule plan

    d = r["evidence"]["data"]
    assert d["status"] == "processed" and d["plan_source"] == "rule" and d["rows"] == 3
    assert r["row"]["file_path"] == "Chen_2025/eqtl.chr1_processed.tsv.gz"
    assert (tmp_path / "data/Chen_2025/eqtl.chr1.parquet").exists()  # the raw file is kept
    out = pd.read_csv(tmp_path / "data" / r["row"]["file_path"], sep="\t", dtype=str)
    assert out[["chrom", "pos", "ref", "alt"]].values.tolist()[1] == ["chr1", "16841", "G", "T"]
    assert (r["row"]["chrom_col"], r["row"]["position_col"], r["row"]["pval_col"]) == ("chrom", "pos", "pval_nominal")
    assert "columns_need_deriving" not in r["issues"] and "data_not_downloaded" not in r["issues"]
    assert r["status"] != "needs_processing"


def test_a_loadable_text_file_is_used_as_it_is(tmp_path: Path, serve) -> None:
    cols = [*COLUMNS, "chr", "pos", "ref", "alt"]
    lines = ["\t".join(cols)] + ["\t".join([*row, "1", row[1].split("_")[1], row[1].split("_")[2], row[1].split("_")[3]])
                                 for row in ROWS]
    serve["eqtl.tsv.gz"] = gzip.compress(("\n".join(lines) + "\n").encode())
    r = _row("eqtl.tsv.gz", serve["eqtl.tsv.gz"], "text")
    c = r["candidate"]
    c.table = probe.Table("", cols, [line.split("\t") for line in lines[1:]])
    c.roles = rules.detect_roles(c.table.columns, c.table.rows, "eQTL")
    [r] = manifest.assemble(RECORD, None, [c], FACTS, {}, rules.Target(level_1=["Blood"]))
    asyncio.run(fetch.fetch_rows([r], "Chen_2025", None, {}, fetch.Options(tmp_path)))
    assert r["evidence"]["data"]["status"] == "as_is"
    assert r["row"]["file_path"] == "Chen_2025/eqtl.tsv.gz"
    assert not (tmp_path / "Chen_2025/eqtl_processed.tsv.gz").exists()


def test_the_size_limit_and_switching_off_downloads(tmp_path: Path, serve) -> None:
    serve["eqtl.chr1.parquet"] = _parquet(ROWS)
    r = _row("eqtl.chr1.parquet", serve["eqtl.chr1.parquet"], "parquet")
    asyncio.run(fetch.fetch_rows([r], "Chen_2025", None, {}, fetch.Options(tmp_path, max_bytes=10)))
    assert r["evidence"]["data"]["status"] == "too_large" and "data_too_large" in r["issues"]
    assert not any(tmp_path.rglob("*.parquet"))

    r = _row("eqtl.chr1.parquet", serve["eqtl.chr1.parquet"], "parquet")
    asyncio.run(fetch.fetch_rows([r], "Chen_2025", None, {}, fetch.Options(tmp_path, download=False)))
    assert "data_not_downloaded" in r["issues"] and r["status"] == "needs_processing"


def test_an_llm_plan_that_fails_the_checks_falls_back_to_the_rules(tmp_path: Path, serve) -> None:
    serve["eqtl.chr1.parquet"] = _parquet(ROWS)
    r = _row("eqtl.chr1.parquet", serve["eqtl.chr1.parquet"], "parquet")

    class Judge:  # maps the alleles the wrong way round: positions land in ref
        async def ask(self, system, user, usage, timeout):
            return {"plans": [{"id": "T1", "action": "process", "reason": "split",
                               "steps": [{"op": "split", "column": "variant_id", "sep": "_",
                                          "into": ["chrom", "ref", "pos", "alt"]}],
                               "columns": {"chrom_col": "chrom", "position_col": "pos", "ref_col": "ref",
                                           "alt_col": "alt"}}]}, "test · model"

    model = asyncio.run(fetch.fetch_rows([r], "Chen_2025", Judge(), {}, fetch.Options(tmp_path)))
    d = r["evidence"]["data"]
    assert model == "test · model" and d["status"] == "processed" and d["plan_source"] == "rule"
    assert d["tried"][0]["source"] == "LLM" and "position" in d["tried"][0]["result"]


def test_a_headerless_chi_square_table_gets_a_p_value(tmp_path: Path, serve) -> None:
    # RASQUAL writes no header and a chi-square statistic instead of a p-value.
    cols = ["peak", "rsid", "chr", "pos", "ref", "alt", "chisq", "pi"]
    body = [["chr1_100_900", "rs1", "1", "150", "A", "G", "3.84", "0.6"],
            ["chr1_100_900", "rs2", "1", "250", "C", "T", "0.5", "0.4"],
            ["chr1_100_900", "rs3", "1", "350", "G", "A", "12.1", "0.8"]]
    serve["peaks.alltests.txt.gz"] = gzip.compress("".join("\t".join(r) + "\n" for r in body).encode())
    r = _row("peaks.alltests.txt.gz", serve["peaks.alltests.txt.gz"], "text")
    c = r["candidate"]
    c.table = probe.Table("", cols, body, has_header=False)
    c.roles = rules.detect_roles(cols, body, "caQTL")
    asyncio.run(fetch.fetch_rows([r], "Wenz_2026", None, {}, fetch.Options(tmp_path)))
    d = r["evidence"]["data"]
    assert d["status"] == "processed", d
    out = pd.read_csv(tmp_path / r["row"]["file_path"], sep="\t")
    assert list(out.columns[:8]) == cols and r["row"]["pval_col"] in out.columns
    assert abs(out[r["row"]["pval_col"]][0] - 0.05) < 1e-3  # chi-square 3.84 on 1 df



def test_skipped_rows_say_why() -> None:
    row = lambda **kw: {"holds": "all_pairs", "status": "needs_review", "issues": [], "row": {"qtl_type": "eQTL"}} | kw  # noqa: E731
    assert fetch.skip_reason(row()) == ""
    assert "selected subset" in fetch.skip_reason(row(holds="subset"))
    assert fetch.skip_reason(row(row={"qtl_type": "mQTL"})).startswith("mQTL is not a type the manifest holds")
    assert "header could not be read" in fetch.skip_reason(row(status="not_loadable", issues=["header_not_read"]))
