"""The LLM-driven build (agent.py): tools, the importer-style check and the submission."""

from __future__ import annotations

import asyncio
import gzip
import json
import threading
from pathlib import Path

import httpx
import pandas as pd
import pytest

from dataqueryweb import agent, dbsnp, fetch, finder
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
        df["variant_id"] = "chr" + df["chrom"] + "_" + df["pos"] + "_" + df["ref"] + "_" + df["alt"]
        df.to_csv(out, sep="\\t", index=False, header=first)
        first = False
print("done")
"""
COLUMNS = {"chrom_col": "chrom", "position_col": "pos", "ref_col": "ref", "alt_col": "alt", "pval_col": "p-value",
           "phenotype_id_col": "gene", "beta_col": "beta", "variant_id_col": "variant_id"}


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


def test_validate_requires_digits_upper_case_and_underscore_variant_ids(tmp_path: Path) -> None:
    def check(rows: list[str]) -> list[str]:
        (tmp_path / "t.tsv").write_text("chrom\tpos\tref\talt\tvariant_id\tp-value\tgene\tbeta\n"
                                       + "".join(r + "\t0.1\tENSG00000186092\t0.2\n" for r in rows))
        return agent.validate_file(tmp_path, "t.tsv", "\\t", "eQTL", COLUMNS, "self")["problems"]

    assert check(["1\t10177\tA\tC\tchr1_10177_A_C", "22\t5\tTG\tT\tchr22_5_TG_T"]) == []
    assert "chrom_col 'chrom' must be the autosome number only, 1 to 22" in " ".join(check(["chr1\t10177\tA\tC\tchr1_10177_A_C"]))
    assert "ref_col 'ref' must be upper-case" in " ".join(check(["1\t10177\ta\tC\t1_10177_a_C"]))
    assert "position_col 'pos' must be digits only" in " ".join(check(["1\t1.0e4\tA\tC\t1_1.0e4_A_C"]))
    for wrong in ("1_10177_A_C", "1:10177:A:C", "chr1:10177:A:C", "chr1_10177_A_C_b38", "rs123"):
        assert "must be chr<chrom>_<pos>_<ref>_<alt>" in " ".join(check([f"1\t10177\tA\tC\t{wrong}"])), wrong
    # A missing number is empty, never a placeholder word.
    (tmp_path / "t.tsv").write_text("chrom\tpos\tref\talt\tvariant_id\tp-value\tgene\tbeta\n"
                                   "1\t10177\tA\tC\tchr1_10177_A_C\t0.1\tENSG00000186092\tnot_available\n"
                                   "1\t10178\tA\tC\tchr1_10178_A_C\t0.2\tENSG00000186092\t\n")
    out = " ".join(agent.validate_file(tmp_path, "t.tsv", "\\t", "eQTL", COLUMNS, "self")["problems"])
    assert "beta_col 'beta' must hold numbers (empty when missing); 1 of 2 rows hold text" in out
    no_id = {k: v for k, v in COLUMNS.items() if k != "variant_id_col"}
    assert "no column named for variant_id_col" in agent.validate_file(tmp_path, "t.tsv", "\\t", "eQTL", no_id)["problems"]


def test_a_non_full_table_is_kept_and_marked(tmp_path: Path) -> None:
    s = session(tmp_path)
    (tmp_path / "d").mkdir()
    (tmp_path / "d/t.tsv").write_text("chrom\tpos\tref\talt\tvariant_id\tp-value\tgene\tbeta\n"
                                     "1\t10177\tA\tC\tchr1_10177_A_C\t0.01\tENSG00000186092\t0.2\n")
    assert s.validate("d/t.tsv", "\\t", "eQTL", COLUMNS, "self")["ok"]
    row = {"dataset": "BrainSeq", "qtl_type": "eQTL", "biocontext": "DLPFC", "level_1_context": "Brain",
           "file_path": "d/t.tsv", "delimiter": "\\t", **COLUMNS, "gene_id_mode": "self", "scope": "non_full"}
    # A non-full row must say why it is not full: a reason, and numbers.
    _, problems = agent.check_submission(s, {"rows": [row]})
    assert any("non_full_reason" in p for p in problems) and any("with numbers" in p for p in problems)
    _, problems = agent.check_submission(s, {"rows": [row | {"non_full_reason": "significant_phenotypes_only",
                                                             "scope_note": "only the significant genes"}]})
    assert any("with numbers" in p for p in problems)
    note = "All cis variants of the 282 significant eGenes only; the paper tested 30,775 genes."
    [r], problems = agent.check_submission(s, {"rows": [row | {"non_full_reason": "significant_phenotypes_only",
                                                                "scope_note": note}]})
    assert not [p for p in problems if "non_full" in p]
    assert r["holds"] == "non_full" and "non_full_summary_statistics" in r["issues"] and r["status"] != "ready"
    two = {"non_full_reason": "significant_phenotypes_only", "scope_note": note,
           "source_url": "https://zenodo.org/records/1; https://github.com/x/y"}
    _, problems = agent.check_submission(s, {"rows": [row | two]})
    assert any("must come from one source" in p for p in problems)  # tables are never merged
    h = r["evidence"]["holds"]
    assert h["reason"] == "significant_phenotypes_only" and h["reason_label"].startswith("every tested variant")
    assert h["note"] == f"Not full summary statistics — {h['reason_label']}. {note}"
    [full], _ = agent.check_submission(s, {"rows": [row | {"scope": "full"}]})
    assert full["holds"] == "all_pairs" and "non_full_summary_statistics" not in full["issues"]


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
        self.system = messages[0]["content"]
        usage["total_tokens"] += 100
        # Every call states a reason (reasons.py refuses calls without one); a test may set its own.
        calls = [(n, {"why": f"scripted test step {n}: the previous tool result leads here"} | a)
                 for n, a in self.turns.pop(0)]
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
            for i, (n, a) in enumerate(calls)]}, "test · model"


def fake_dbsnp(root: Path) -> None:
    """A tabix-indexed dbSNP-like VCF under root/reference/dbsnp, as add_rsid expects."""
    import pysam

    vcf = dbsnp.vcf_path(root)
    vcf.parent.mkdir(parents=True)
    plain = vcf.with_name("dbsnp.vcf")
    plain.write_text("##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
                     "NC_000001.11\t10177\trs1\tA\tC,G\t.\t.\tRS=1\n"
                     "NC_000001.11\t10352\trs2\tG\tT\t.\t.\tRS=2\n"  # the table has these alleles swapped
                     "NC_000002.12\t5\trs3\tC\tT\t.\t.\tRS=3\n")
    pysam.tabix_compress(str(plain), str(vcf))
    pysam.tabix_index(str(vcf), preset="vcf")


def test_the_model_downloads_processes_validates_and_submits(tmp_path: Path, served) -> None:
    fake_dbsnp(tmp_path)
    columns = COLUMNS | {"rsid_col": "rsid"}
    row = {"dataset": "BrainSeq", "qtl_type": "eQTL", "biocontext": "DLPFC", "level_1_context": "Brain",
           "level_2_context": "", "population": "EUR", "sample_size": "412", "file_path": "brainseq/eqtl_processed.tsv.gz",
           "delimiter": "\\t", **columns, "gene_id_mode": "self", "url": "https://zenodo.org/records/1",
           "source_url": "https://zenodo.org/records/1", "genome_build": "GRCh38",
           "evidence": {"biocontext": {"source": "paper", "quote": "mapped cis-eQTL in the dorsolateral prefrontal cortex"},
                        "sample_size": {"source": "paper", "quote": "We genotyped 412 donors of European ancestry"},
                        "population": {"source": "paper", "quote": "We genotyped 412 donors of European ancestry"},
                        "genome_build": {"source": "paper", "quote": "Coordinates are on GRCh38."}}}
    bad_quote = row | {"evidence": row["evidence"] | {"biocontext": {"quote": "a sentence nobody wrote anywhere"}}}
    judge = Script([
        [("read_paper", {"query": "donors"})],
        [("download", {"dataset": "BrainSeq", "urls": ["https://zenodo.org/f/eqtl.tsv.gz"]})],
        [("run_python", {"dataset": "BrainSeq", "code": SPLIT, "purpose": "too early: nothing assessed yet"})],
        [("assess_file", {"path": "brainseq/eqtl.tsv.gz", "contents": "qtl_nominal_all", "description": "x",
                          "processable": "yes", "plan": "split SNP",
                          "evidence": [{"quote": "brainseq/eqtl.tsv.gz", "source": "download result"}]})],
        [("inspect_local", {"path": "brainseq/eqtl.tsv.gz"})],
        [("assess_file", {"path": "brainseq/eqtl.tsv.gz", "contents": "qtl_nominal_all", "description": "SNP gene beta p",
                          "processable": "yes", "plan": "split SNP into chrom/pos/ref/alt",
                          "pval_column": "p-value",
                          "evidence": [{"quote": "SNP gene beta t-stat p-value", "source": "inspect_local head"}]})],
        [("run_python", {"dataset": "BrainSeq", "code": SPLIT, "purpose": "split SNP into chrom/pos/ref/alt"})],
        [("add_rsid", {"file_path": "brainseq/eqtl_processed.tsv.gz", "columns": COLUMNS})],
        [("validate", {"file_path": "brainseq/eqtl_processed.tsv.gz", "delimiter": "\\t", "qtl_type": "eQTL",
                       "columns": columns, "gene_id_mode": "self"})],
        [("submit", {"rows": [bad_quote], "summary": "one context"})],
        [("submit", {"rows": [row], "summary": "one context",
                     "skipped_tables": [{"path": "sig_eqtl.txt", "url": "https://zenodo.org/records/1",
                                         "reason": "significant hits only"}]})],
    ])
    usage = {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
    out = asyncio.run(agent.run(RECORD, judge, root=tmp_path, max_bytes=1e9, target=rules.Target(level_1=["Brain"]),
                                paper=PAPER, step=lambda _: None, cancel=threading.Event(), usage=usage))
    assert "quote was not found" in judge.seen[-1]["content"]  # the first submit was sent back
    told = [m["content"] for m in judge.seen]
    assert any("waits until every downloaded file is checked" in t for t in told)  # processing before assess_file
    assert any("inspect_local 'brainseq/eqtl.tsv.gz' first" in t for t in told)  # assess before inspect
    assert out["assessments"]["brainseq/eqtl.tsv.gz"]["format_check"]["kind"] == "table"
    assert "PROCEDURE (SKILL.md)" in judge.system and "Phase 7: Submit" in judge.system
    [r] = out["rows"]
    assert r["row"]["file_path"] == "brainseq/eqtl_processed.tsv.gz" and r["row"]["chrom_col"] == "chrom"
    assert r["evidence"]["data"]["status"] == "processed" and r["evidence"]["file_path"]["verified"]
    assert r["evidence"]["sample_size"]["verified"] and r["evidence"]["population"]["verified"]
    assert r["status"] == "ready", r["issues"]
    assert (tmp_path / "brainseq/eqtl_processed.tsv.gz").exists()
    # rs ids from dbSNP: exact alleles, swapped alleles, and none for a variant dbSNP lacks.
    ids = pd.read_csv(tmp_path / "brainseq/eqtl_processed.tsv.gz", sep="\t", dtype=str, keep_default_na=False)["rsid"]
    assert ids.tolist() == ["rs1", "rs2", ""]
    assert any(i["holds"] == "skipped" for loc in out["reports"] for i in loc["inventory"])
    # A second paper asking for the same URL reuses the file.
    s = session(tmp_path)
    again = s.download("Other", urls=["https://zenodo.org/f/eqtl.tsv.gz"])
    assert "reused" in again["files"][0]["note"]


def test_the_skill_drops_its_front_matter_and_names_every_tool(tmp_path: Path) -> None:
    f = tmp_path / "SKILL.md"
    f.write_text("---\nname: x\ndescription: y\n---\n\n# Steps\n1. read_paper\n")
    assert agent.skill(f) == "PROCEDURE (SKILL.md)\n\n# Steps\n1. read_paper"
    text = agent.skill()
    assert "description:" not in text
    assert all(t["name"] in text for t in agent.TOOLS)
    with pytest.raises(FileNotFoundError, match="MANIFEST_SKILL"):
        agent.skill(tmp_path / "missing.md")
    assert all(f"`{c}`" in text for c in rules.MANIFEST_COLUMNS)  # every manifest column is described


def test_the_first_message_says_what_the_reference_manifest_has(tmp_path: Path) -> None:
    ref = tmp_path / "qtl_manifest.tsv"
    ref.write_text("\t".join(rules.MANIFEST_COLUMNS) + "\n" + "\t".join(
        {"dataset": "BrainSeq", "qtl_type": "eQTL", "biocontext": "DLPFC", "level_1_context": "Brain",
         "publication": "https://doi.org/10.1000/brain"}.get(c, "") for c in rules.MANIFEST_COLUMNS) + "\n")
    s = session(tmp_path)
    s.record = RECORD | {"doi": "10.1000/brain"}
    s.target = rules.load_target(ref)
    text = agent.first_message(s, [])
    assert "already there as dataset BrainSeq, with 1 row(s)" in text and "- eQTL DLPFC" in text
    s.target = rules.Target()
    assert "this paper is not in it yet" in agent.first_message(s, [])


def test_older_gtex_releases_are_not_downloaded(tmp_path: Path, served) -> None:
    s = session(tmp_path)
    out = s.download("GTEx", urls=["https://storage.googleapis.com/gtex_analysis_v6p/single_tissue_eqtl_data/Whole_Blood.allpairs.txt.gz",
                                   "https://example.org/GTEx_Analysis_v8_eQTL/Lung.txt.gz"])
    assert all("GTEx_v10" in f["error"] for f in out["files"])
    assert agent.OLD_GTEX.search("GTEx_v8") and not agent.OLD_GTEX.search("GTEx_v10")


def test_only_summary_statistics_are_downloaded(tmp_path: Path, served) -> None:
    s = session(tmp_path)
    out = s.download("X", urls=["https://hgdownload.soe.ucsc.edu/goldenPath/hg19/database/snp151Common.txt.gz",
                                "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC1/fullTextXML",
                                "https://storage.googleapis.com/public-download-files/hgnc/tsv/tsv/hgnc_complete_set.txt"])
    assert all("only QTL summary statistics" in f["error"] for f in out["files"])
    assert not agent.NOT_SUMSTATS.search("https://zenodo.org/f/eqtl.allpairs.tsv.gz")


def test_a_run_waits_for_the_usage_limit_and_resumes(tmp_path: Path, monkeypatch) -> None:
    from dataqueryweb import llm, manifest

    calls = []

    async def fake_build(record, store, judge, **kw):
        calls.append(record["record_id"])
        note = "LLM step failed: gpt: usage limit (window)" if len(calls) == 1 else ""
        return {"record_id": record["record_id"], "rows": 0, "statuses": {}, "data": {}, "locations": {},
                "tokens": 10, "model": "", "note": note}

    class FakeJudge:
        def __init__(self):
            self.exhausted = set()
        pings = 0

        async def ping(self):
            FakeJudge.pings += 1
            return FakeJudge.pings >= 2  # still limited at the first check

        async def aclose(self):
            pass

    monkeypatch.setattr(manifest, "build_record", fake_build)
    monkeypatch.setattr(manifest, "QUOTA_CHECK_SECONDS", 0)
    monkeypatch.setattr(llm, "Judge", FakeJudge)
    store = manifest.ManifestStore(tmp_path / "db.sqlite3", tmp_path / "out")
    batch = manifest.Batch([{"record_id": 1, "publication_title": "A"}, {"record_id": 2, "publication_title": "B"}], store)
    asyncio.run(batch.run())
    assert calls == [1, 1, 2] and FakeJudge.pings == 2 and batch.state == "done" and batch.done == 2


def fake_chain(root: Path) -> None:
    """A UCSC-style chain under root/reference/liftover: chr1 shifted by +1000, chr2 onto the minus strand."""
    from dataqueryweb import liftover

    chain = liftover.chain_path(root)
    chain.parent.mkdir(parents=True)
    with gzip.open(chain, "wt") as f:
        f.write("chain 1000 chr1 249250621 + 10000 20000 chr1 248956422 + 11000 21000 1\n10000\n\n"
                "chain 1000 chr2 243199373 + 0 1000 chr2 242193529 - 500 1500 2\n1000\n\n")


def test_liftover_moves_grch37_tables_to_grch38(tmp_path: Path) -> None:
    fake_chain(tmp_path)
    table = tmp_path / "x/eqtl_processed.tsv.gz"
    table.parent.mkdir()
    pd.DataFrame({"chrom": ["1", "2", "3"], "pos": ["10177", "5", "100"], "ref": ["A", "C", "G"],
                  "alt": ["C", "T", "A"], "variant_id": ["chr1_10177_A_C", "chr2_5_C_T", "chr3_100_G_A"],
                  "pvalue": ["0.1", "0.2", "0.3"]}).to_csv(table, sep="\t", index=False)
    s = session(tmp_path)
    cols = {"chrom_col": "chrom", "position_col": "pos", "ref_col": "ref", "alt_col": "alt", "variant_id_col": "variant_id"}
    out = s.liftover("x/eqtl_processed.tsv.gz", cols)
    assert (out["rows"], out["lifted"], out["dropped"], out["minus_strand"]) == (3, 2, 1, 1)
    df = pd.read_csv(table, sep="\t", dtype=str)
    assert df["pos"].tolist()[0] == "11177" and df["variant_id"].tolist()[0] == "chr1_11177_A_C"
    assert df[["ref", "alt"]].values.tolist()[1] == ["G", "A"]  # minus strand: complemented
    assert df["variant_id"].tolist()[1] == f"chr2_{df['pos'][1]}_G_A"
    assert df[["chrom_grch37", "pos_grch37"]].values.tolist() == [["1", "10177"], ["2", "5"]]
    assert "x/eqtl_processed.tsv.gz" in s.lifted
    assert "lifted over before" in s.liftover("x/eqtl_processed.tsv.gz", cols)["error"]


def test_fill_alleles_takes_the_other_allele_of_biallelic_sites_from_dbsnp(tmp_path: Path) -> None:
    fake_dbsnp(tmp_path)  # rs1 at 1:10177 is A/C/G (multiallelic); rs2 at 1:10352 G/T; rs3 at 2:5 C/T
    f = tmp_path / "x" / "eqtl_processed.tsv.gz"
    f.parent.mkdir()
    pd.DataFrame({"chrom": ["1", "1", "1", "2", "2", "1"], "pos": ["10177", "10352", "10352", "5", "5", "999"],
                  "ref": ["", "", "", "", "T", ""], "alt": ["C", "T", "G", "T", "C", "A"],
                  "variant_id": [""] * 6, "pval": ["0.1"] * 6, "gene": ["ENSG00000000001"] * 6}
                 ).to_csv(f, sep="\t", index=False)
    lookup = dbsnp.AlleleLookup(dbsnp.vcf_path(tmp_path))
    cols = {"chrom_col": "chrom", "position_col": "pos", "ref_col": "ref", "alt_col": "alt", "variant_id_col": "variant_id"}
    res = dbsnp.fill_alleles(f, lookup, cols)
    out = pd.read_csv(f, sep="\t", dtype=str, keep_default_na=False)
    assert out["ref"].tolist() == ["", "G", "T", "C", "T", ""]
    assert out["variant_id"].tolist() == ["", "chr1_10352_G_T", "chr1_10352_T_G", "chr2_5_C_T", "chr2_5_T_C", ""]
    assert res["filled"] == 3 and res["had_ref"] == 1 and res["multiallelic"] == 1 and res["not_in_dbsnp"] == 1
    # validate accepts rows whose ref was left empty (with an empty variant id).
    v = agent.validate_file(tmp_path, "x/eqtl_processed.tsv.gz", "\\t", "eQTL",
                            cols | {"pval_col": "pval", "phenotype_id_col": "gene"}, "self")
    assert not [p for p in v.get("problems", []) if "ref_col" in p or "variant_id_col" in p], v


def test_validate_fails_when_rs_ids_dbsnp_holds_are_left_empty(tmp_path: Path) -> None:
    fake_dbsnp(tmp_path)  # rs1 1:10177 A/C,G; rs2 1:10352 G/T; rs3 2:5 C/T
    f = tmp_path / "x" / "eqtl_processed.tsv.gz"
    f.parent.mkdir()
    table = pd.DataFrame({"chrom": ["1", "1", "1"], "pos": ["10177", "10352", "999"], "ref": ["A", "T", "C"],
                          "alt": ["C", "G", "G"], "pval": ["0.1"] * 3, "gene": ["ENSG00000000001"] * 3})
    table["variant_id"] = "chr" + table["chrom"] + "_" + table["pos"] + "_" + table["ref"] + "_" + table["alt"]
    table.assign(rsid="").to_csv(f, sep="\t", index=False)  # an rsid column the script left empty
    cols = {"chrom_col": "chrom", "position_col": "pos", "ref_col": "ref", "alt_col": "alt", "pval_col": "pval",
            "phenotype_id_col": "gene", "variant_id_col": "variant_id", "rsid_col": "rsid"}
    v = agent.validate_file(tmp_path, "x/eqtl_processed.tsv.gz", "\\t", "eQTL", cols, "self")
    assert not v["ok"] and any("dbSNP holds" in p and "add_rsid" in p for p in v["problems"]), v["problems"]
    assert v["checks"]["rsid_vs_dbsnp"]["in_dbsnp_but_empty"] == 2
    # add_rsid fills what dbSNP holds; the variant dbSNP lacks (1:999) stays empty and passes.
    dbsnp.annotate(f, dbsnp.Lookup(dbsnp.vcf_path(tmp_path)), cols)
    assert pd.read_csv(f, sep="\t", dtype=str, keep_default_na=False)["rsid"].tolist() == ["rs1", "rs2", ""]
    v = agent.validate_file(tmp_path, "x/eqtl_processed.tsv.gz", "\\t", "eQTL", cols, "self")
    assert v["ok"], v["problems"]
    assert v["checks"]["rsid_vs_dbsnp"] == {"checked": 1, "in_dbsnp_but_empty": 0, "examples": []}


def test_validate_wants_bare_chrom_and_chr_prefixed_variant_ids_in_every_file(tmp_path: Path) -> None:
    def write(name: str, chrom: str, vid: str) -> None:
        pd.DataFrame({"chrom": [chrom], "pos": ["32523"], "ref": ["A"], "alt": ["T"], "variant_id": [vid],
                      "pval": ["0.1"], "gene": ["ENSG00000000001"]}).to_csv(tmp_path / "d" / name, sep="\t", index=False)
    (tmp_path / "d").mkdir()
    cols = {"chrom_col": "chrom", "position_col": "pos", "ref_col": "ref", "alt_col": "alt", "pval_col": "pval",
            "phenotype_id_col": "gene", "variant_id_col": "variant_id"}
    for i in range(1, 6):
        write(f"t.chr{i}.tsv", "1", "chr1_32523_A_T")
    assert agent.validate_file(tmp_path, "d/t.chr*.tsv", "\\t", "eQTL", cols, "self")["ok"]
    write("t.chr3.tsv", "chr1", "chr1_32523_A_T")  # a middle file, not one of the fully checked ones
    out = " ".join(agent.validate_file(tmp_path, "d/t.chr*.tsv", "\\t", "eQTL", cols, "self")["problems"])
    assert "t.chr3.tsv" in out and "'chr' prefix" in out and "strip it" in out
    write("t.chr3.tsv", "1", "1_32523_A_T")
    out = " ".join(agent.validate_file(tmp_path, "d/t.chr*.tsv", "\\t", "eQTL", cols, "self")["problems"])
    assert "lacks the 'chr' prefix" in out and "chr1_32523_A_T" in out
    write("t.chr3.tsv", "01", "chr01_32523_A_T")
    assert "1 to 22" in " ".join(agent.validate_file(tmp_path, "d/t.chr*.tsv", "\\t", "eQTL", cols, "self")["problems"])


def test_validate_wants_caqtl_peaks_as_chr_start_end(tmp_path: Path) -> None:
    def check(peak: str) -> str:
        pd.DataFrame({"chrom": ["1"], "pos": ["722408"], "ref": ["G"], "alt": ["C"], "variant_id": ["chr1_722408_G_C"],
                      "pval": ["0.1"], "peak": [peak]}).to_csv(tmp_path / "c.tsv", sep="\t", index=False)
        cols = {"chrom_col": "chrom", "position_col": "pos", "ref_col": "ref", "alt_col": "alt", "pval_col": "pval",
                "phenotype_id_col": "peak", "variant_id_col": "variant_id"}
        return " ".join(agent.validate_file(tmp_path, "c.tsv", "\\t", "caQTL", cols)["problems"])

    assert check("chr1_817095_817594") == ""
    for wrong in ("chr1-817095-817594", "chr1:817095-817594", "1_817095_817594"):
        assert "chr<chrom>_<start>_<end>" in check(wrong) and "replace '-' and ':'" in check(wrong), wrong
    assert "chr<chrom>_<start>_<end>" in check("peak_123")
    assert "start must be below end" in check("chr1_817594_817095")


def test_a_z_score_counts_as_a_p_value_source_but_a_t_statistic_does_not() -> None:
    z = agent.format_check(["snp\tgene\tbeta\tz_score", "1:5:A:C\tENSG00000000001\t-0.4\t-2.3",
                            "1:6:A:G\tENSG00000000001\t1.7\t1.1"])
    assert z["zscore_candidates"] == ["z_score"] and not z["pvalue_candidates"]
    t = agent.format_check(["snp\tgene\tbeta\tt_stat", "1:5:A:C\tENSG00000000001\t-0.4\t-2.3"])
    assert t["zscore_candidates"] == [] and not t["pvalue_candidates"]


def test_assess_file_accepts_a_z_column_and_refuses_a_t_only_table(tmp_path: Path) -> None:
    s = session(tmp_path)
    folder = tmp_path / s.folder_name()
    folder.mkdir()
    (folder / "z.tsv").write_text("snp\tgene\tz\n1:5:A:C\tENSG00000000001\t-2.3\n")
    (folder / "t.tsv").write_text("snp\tgene\tt_stat\n1:5:A:C\tENSG00000000001\t-2.3\n")
    (tmp_path / agent.INDEX_FILE).write_text(json.dumps(
        {n: {"path": f"{s.folder_name()}/{n}", "record_id": 7} for n in ("z.tsv", "t.tsv")}))
    for n in ("z.tsv", "t.tsv"):
        s.inspect_local(f"{s.folder_name()}/{n}")
    ok = s.assess_file(f"{s.folder_name()}/z.tsv", "qtl_nominal_all", "snp gene z", "yes", plan="p from z",
                       pval_column="z")
    assert "recorded" in ok, ok
    no = s.assess_file(f"{s.folder_name()}/t.tsv", "qtl_nominal_all", "snp gene t", "yes", plan="p from t",
                       pval_column="t_stat")
    assert "t statistic" in no["error"]


def test_fill_positions_places_rs_ids_from_the_study_map(tmp_path: Path) -> None:
    from dataqueryweb import allelemap

    folder = tmp_path / "x"
    folder.mkdir()
    snp_map = folder / "SNPinfo.tsv"
    snp_map.write_text("CHR\tBP\tSNP\tA1\tA2\n1\t100\trs1\tA\tG\n2\t200\trs2;2_200_C_T\tC\tT\n"
                       "3\t300\trs3\tG\tA\n4\t400\trs3\tG\tA\n")  # rs3 at two positions: ambiguous
    mc = {"chrom": "CHR", "pos": "BP", "id": "SNP", "allele1": "A1", "allele2": "A2"}
    allelemap.build_index(snp_map, mc, True)
    assert allelemap.ensure_id_index(snp_map) == 5
    f = folder / "eqtl_processed.tsv.gz"
    pd.DataFrame({"rsid": ["rs1", "rs2", "rs3", "rs9", "rs1"], "chrom": ["", "", "", "", "7"],
                  "pos": ["", "", "", "", "70"], "ref": ["A", "C", "G", "A", "A"], "alt": ["G", "T", "A", "C", "G"],
                  "variant_id": [""] * 5, "p": ["0.1"] * 5}).to_csv(f, sep="\t", index=False)
    cols = {"rsid_col": "rsid", "chrom_col": "chrom", "position_col": "pos", "ref_col": "ref", "alt_col": "alt",
            "variant_id_col": "variant_id"}
    lookup = allelemap.PositionLookup(snp_map)
    res = allelemap.fill_positions(f, lookup, cols)
    lookup.close()
    out = pd.read_csv(f, sep="\t", dtype=str, keep_default_na=False)
    assert out["variant_id"].tolist() == ["chr1_100_A_G", "chr2_200_C_T", "chr7_70_A_G"]  # existing position kept
    assert res == {"rows": 5, "had_position": 1, "filled": 2, "ambiguous": 1, "not_in_map": 1, "removed": 2,
                   "share_with_position": 0.6}


def test_fill_positions_needs_an_assessed_study_map(tmp_path: Path) -> None:
    s = session(tmp_path)
    f = tmp_path / "x" / "eqtl_processed.tsv.gz"
    f.parent.mkdir()
    pd.DataFrame({"rsid": ["rs1"], "chrom": [""], "pos": [""]}).to_csv(f, sep="\t", index=False)
    out = s.fill_positions("x/eqtl_processed.tsv.gz", {"rsid_col": "rsid", "chrom_col": "chrom", "position_col": "pos"},
                           "x/dbsnp.vcf.gz", {"chrom": "c", "pos": "p", "id": "i", "allele1": "a", "allele2": "b"})
    assert "must be a downloaded file you assessed with contents variant_map" in out["error"]
