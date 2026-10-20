"""The LLM-driven build (agent.py): tools, the importer-style check and the submission."""

from __future__ import annotations

import asyncio
import gzip
import json
import threading
from pathlib import Path

import httpx
import pytest

from dataqueryweb import agent, fetch, finder
from dataqueryweb import manifest_rules as rules

RAW = ("SNP\tgene\tbeta\tt-stat\tp-value\n"
       "1:10177:A:C\tENSG00000186092.4\t0.1\t1.2\t0.23\n"
       "1:10352:T:G\tENSG00000186092.4\t-0.2\t-2.1\t0.04\n"
       "2:20000:G:A\tENSG00000187634.1\t0.05\t0.3\t0.77\n")
RECORD = {"record_id": 7, "doi": "10.1/brain", "first_author": "Wang X", "year": "2025", "qtl_type": "eQTL",
          "publication_title": "Brain eQTL", "publication_url": "https://doi.org/10.1/brain", "species": "human",
          "locations": json.dumps([{"download_url": "https://zenodo.org/records/1", "content": "qtl_summary_statistics",
                                    "access_route": "open"}])}
PAPER = finder.Paper(title="Brain eQTL", full_text=True,
                     paragraphs=["[Methods] We genotyped 412 donors of European ancestry and mapped cis-eQTL in the "
                                 "dorsolateral prefrontal cortex.", "[Methods] Coordinates are on GRCh38."])
SPLIT = """import gzip, pandas as pd
first = True
with gzip.open("eqtl_processed.tsv.gz", "wt") as out:
    for df in pd.read_csv("eqtl.tsv.gz", sep="\\t", dtype=str, chunksize=100000):
        parts = df["SNP"].str.split(":", expand=True)
        df["chrom"], df["pos"], df["ref"], df["alt"] = parts[0], parts[1], parts[2], parts[3]
        df.to_csv(out, sep="\\t", index=False, header=first)
        first = False
print("done")
"""
COLUMNS = {"chrom_col": "chrom", "position_col": "pos", "ref_col": "ref", "alt_col": "alt", "pval_col": "p-value",
           "phenotype_id_col": "gene", "beta_col": "beta"}


@pytest.fixture
def served(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("eqtl.tsv.gz"):
            return httpx.Response(200, content=gzip.compress(RAW.encode()))
        return httpx.Response(404)
    monkeypatch.setattr(fetch, "http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(fetch, "MIN_FREE", 0)


def session(tmp_path: Path) -> agent.Session:
    return agent.Session(RECORD, PAPER, tmp_path, 1e9, rules.Target(level_1=["Brain", "Blood"]), threading.Event(),
                         lambda _: None)


def test_validate_reads_files_the_importer_way(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    (tmp_path / "d/raw.tsv.gz").write_bytes(gzip.compress(RAW.encode()))
    bad = agent.validate_file(tmp_path, "d/raw.tsv.gz", "\\t", "eQTL", COLUMNS, "self")
    assert not bad["ok"] and any("columns not in the header" in p for p in bad["problems"])
    ok = agent.validate_file(tmp_path, "d/raw.tsv.gz", "\\t", "eQTL",
                             COLUMNS | {"chrom_col": "SNP", "position_col": "beta"}, "self")
    assert not ok["ok"]  # "1:10177:A:C" is no chromosome
    assert agent.validate_file(tmp_path, "d/missing*.tsv.gz", "\\t", "eQTL", COLUMNS)["problems"][0].startswith("no file")
    assert "mQTL is not a manifest QTL type" in " ".join(agent.validate_file(tmp_path, "d/raw.tsv.gz", "\\t", "mQTL", COLUMNS)["problems"])


def test_run_python_refuses_network_and_deleting(tmp_path: Path) -> None:
    s = session(tmp_path)
    assert "refused" in s.run_python("x", "import urllib.request", "net")
    assert "refused" in s.run_python("x", "import os\nos.remove('a')", "rm")
    assert "refused" in s.run_python("x", "open('/etc/passwd')", "abs")
    out = s.run_python("x", "print(sum(range(4)))", "add")
    assert out["exit_code"] == 0 and out["output"].strip() == "6"
    assert (tmp_path / agent.SCRIPTS_DIR / "7" / "step01.py").exists()


class Script:
    """A stand-in model that calls the tools in a fixed order."""

    def __init__(self, turns: list[list[tuple[str, dict]]]) -> None:
        self.turns, self.seen = turns, []

    async def chat_tools(self, messages, tools, usage, timeout):
        self.seen.append(messages[-1])
        usage["total_tokens"] += 100
        calls = self.turns.pop(0)
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
            for i, (n, a) in enumerate(calls)]}, "test · model"


def test_the_model_downloads_processes_validates_and_submits(tmp_path: Path, served) -> None:
    row = {"dataset": "BrainSeq", "qtl_type": "eQTL", "biocontext": "DLPFC", "level_1_context": "Brain",
           "level_2_context": "", "population": "EUR", "sample_size": "412", "file_path": "brainseq/eqtl_processed.tsv.gz",
           "delimiter": "\\t", **COLUMNS, "gene_id_mode": "self", "url": "https://zenodo.org/records/1",
           "source_url": "https://zenodo.org/records/1", "genome_build": "GRCh38",
           "evidence": {"biocontext": {"source": "paper", "quote": "mapped cis-eQTL in the dorsolateral prefrontal cortex"},
                        "sample_size": {"source": "paper", "quote": "We genotyped 412 donors of European ancestry"},
                        "population": {"source": "paper", "quote": "We genotyped 412 donors of European ancestry"},
                        "genome_build": {"source": "paper", "quote": "Coordinates are on GRCh38."}}}
    bad_quote = row | {"evidence": row["evidence"] | {"biocontext": {"quote": "a sentence nobody wrote anywhere"}}}
    judge = Script([
        [("read_paper", {"query": "donors"})],
        [("download", {"dataset": "BrainSeq", "urls": ["https://zenodo.org/f/eqtl.tsv.gz"]})],
        [("run_python", {"dataset": "BrainSeq", "code": SPLIT, "purpose": "split SNP into chrom/pos/ref/alt"})],
        [("validate", {"file_path": "brainseq/eqtl_processed.tsv.gz", "delimiter": "\\t", "qtl_type": "eQTL",
                       "columns": COLUMNS, "gene_id_mode": "self"})],
        [("submit", {"rows": [bad_quote], "summary": "one context"})],
        [("submit", {"rows": [row], "summary": "one context",
                     "skipped_tables": [{"path": "sig_eqtl.txt", "url": "https://zenodo.org/records/1",
                                         "reason": "significant hits only"}]})],
    ])
    usage = {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
    out = asyncio.run(agent.run(RECORD, judge, root=tmp_path, max_bytes=1e9, target=rules.Target(level_1=["Brain"]),
                                paper=PAPER, step=lambda _: None, cancel=threading.Event(), usage=usage))
    assert "quote was not found" in judge.seen[-1]["content"]  # the first submit was sent back
    [r] = out["rows"]
    assert r["row"]["file_path"] == "brainseq/eqtl_processed.tsv.gz" and r["row"]["chrom_col"] == "chrom"
    assert r["evidence"]["data"]["status"] == "processed" and r["evidence"]["file_path"]["verified"]
    assert r["evidence"]["sample_size"]["verified"] and r["evidence"]["population"]["verified"]
    assert r["status"] == "ready", r["issues"]
    assert (tmp_path / "brainseq/eqtl_processed.tsv.gz").exists()
    assert any(i["holds"] == "skipped" for loc in out["reports"] for i in loc["inventory"])
    # A second paper asking for the same URL reuses the file.
    s = session(tmp_path)
    again = s.download("Other", urls=["https://zenodo.org/f/eqtl.tsv.gz"])
    assert "reused" in again["files"][0]["note"]


def test_older_gtex_releases_are_not_downloaded(tmp_path: Path, served) -> None:
    s = session(tmp_path)
    out = s.download("GTEx", urls=["https://storage.googleapis.com/gtex_analysis_v6p/single_tissue_eqtl_data/Whole_Blood.allpairs.txt.gz",
                                   "https://example.org/GTEx_Analysis_v8_eQTL/Lung.txt.gz"])
    assert all("GTEx_v10" in f["error"] for f in out["files"])
    assert agent.OLD_GTEX.search("GTEx_v8") and not agent.OLD_GTEX.search("GTEx_v10")
