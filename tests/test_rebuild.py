"""rebuild.py: what a rebuild checks on disk before the model's first step."""

from __future__ import annotations

import gzip
import json
import threading
from pathlib import Path

import pandas as pd

from dataqueryweb import agent, rebuild
from dataqueryweb import manifest_rules as rules
from test_agent import PAPER, RECORD, fake_dbsnp


def session(root: Path) -> agent.Session:
    s = agent.Session(RECORD, PAPER, root, 1e9, rules.Target(level_1=["Brain"]), threading.Event(), lambda _: None)
    s.audit = agent.BuildAudit(root, RECORD)
    return s


def table(**over: list[str]) -> pd.DataFrame:
    d = {"chrom": ["1", "1"], "pos": ["10177", "999"], "ref": ["A", "C"], "alt": ["C", "G"],
         "variant_id": ["chr1_10177_A_C", "chr1_999_C_G"], "pval": ["0.1", "0.2"],
         "phenotype_id": ["ENSG00000000001"] * 2, "rsid": ["rs1", ""]}
    return pd.DataFrame(d | over)


def test_rebuild_deletes_broken_processed_files_and_keeps_the_rest(tmp_path: Path) -> None:
    fake_dbsnp(tmp_path)
    s = session(tmp_path)
    folder = tmp_path / s.folder_name()
    folder.mkdir()
    (folder / "a.tsv.gz").write_bytes(gzip.compress(b"x\n"))  # the downloaded source
    (tmp_path / agent.INDEX_FILE).write_text(json.dumps({"u": {"path": f"{s.folder_name()}/a.tsv.gz", "record_id": 7}}))
    table().to_csv(folder / "good_processed.tsv.gz", sep="\t", index=False)
    table(rsid=["", ""]).to_csv(folder / "norsid_processed.tsv.gz", sep="\t", index=False)
    table(chrom=["chr1", "chr1"]).to_csv(folder / "chrom_processed.tsv.gz", sep="\t", index=False)
    table(variant_id=["1_10177_A_C", "1_999_C_G"]).to_csv(folder / "vid_processed.tsv.gz", sep="\t", index=False)
    table(phenotype_id=["chr1-100-200"] * 2).to_csv(folder / "x.caQTL_processed.tsv.gz", sep="\t", index=False)
    whole = gzip.compress(table().to_csv(sep="\t", index=False).encode() * 50)
    (folder / "cut_processed.tsv.gz").write_bytes(whole[: len(whole) // 2])  # an interrupted write
    (folder / "b_processed.tsv.gz.rsid.tmp").write_text("half")

    report = rebuild.prepare(s, "PROCEDURE v2")
    left = sorted(p.name for p in folder.iterdir())
    assert left == [".processed_checks.json", "a.tsv.gz", "good_processed.tsv.gz", "norsid_processed.tsv.gz"]
    assert "DELETED 5" in report and "truncated" in report and "'chr' prefix" in report
    assert "rs ids missing (1)" in report and "norsid_processed" in report
    assert "in the target format (1)" in report and "good_processed" in report
    assert f"{s.folder_name()}/a.tsv.gz" in s.checked  # the download was format-checked by the program
    assert "No copy of the skill used by earlier builds" in report


def test_rebuild_keeps_broken_files_without_a_source(tmp_path: Path) -> None:
    s = session(tmp_path)
    folder = tmp_path / s.folder_name()
    folder.mkdir()
    table(chrom=["chr1", "chr1"]).to_csv(folder / "chrom_processed.tsv.gz", sep="\t", index=False)
    report = rebuild.prepare(s, "PROCEDURE")
    assert (folder / "chrom_processed.tsv.gz").exists() and "BROKEN but kept" in report


def test_skill_changes_are_shown_as_a_diff(tmp_path: Path) -> None:
    log = tmp_path / "logs"
    rebuild.skill_changes(log, "20260101T000000Z-a", "rule one\nrule two\n")
    assert "unchanged" in rebuild.skill_changes(log, "20260102T000000Z-b", "rule one\nrule two\n")
    diff = rebuild.skill_changes(log, "20260103T000000Z-c", "rule one\nrule two\nchrom has no chr\n")
    assert "+chrom has no chr" in diff and "20260102T000000Z-b" in diff


def test_assessments_are_kept_across_builds_and_a_glob_assesses_a_group(tmp_path: Path) -> None:
    s = session(tmp_path)
    folder = tmp_path / s.folder_name()
    folder.mkdir()
    index = {}
    for name in ("t.B.tsv", "t.NK.tsv"):
        (folder / name).write_text("chrom\tpos\tref\talt\tpval\tgene\n1\t5\tA\tC\t0.1\tENSG00000000001\n")
        index[name] = {"path": f"{s.folder_name()}/{name}", "record_id": 7}
    (tmp_path / agent.INDEX_FILE).write_text(json.dumps(index))
    rebuild.prepare(s, "P")
    out = s.assess_file(f"{s.folder_name()}/t.*.tsv", "qtl_nominal_all", "chrom pos ref alt pval gene", "ready",
                        pval_column="pval")
    assert out["recorded"] == 2 and not s.unassessed()
    again = session(tmp_path)
    report = rebuild.prepare(again, "P")
    assert not again.unassessed() and "reused from earlier builds of unchanged files: 2" in report
    (folder / "t.NK.tsv").write_text("chrom\tpos\tref\talt\tpval\tgene\n1\t6\tA\tC\t0.1\tENSG00000000001\n")
    third = session(tmp_path)
    rebuild.prepare(third, "P")
    assert third.unassessed() == [f"{s.folder_name()}/t.NK.tsv"]  # a changed file is assessed again
