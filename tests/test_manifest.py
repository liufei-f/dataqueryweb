"""Tests for the manifest pipeline: file rules, probing, evidence checks, storage and API."""

from __future__ import annotations

import gzip
import json
import io
import zipfile
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from dataqueryweb import manifest, probe, records, web
from dataqueryweb import manifest_rules as rules

GTEX = ["phenotype_id", "variant_id", "chr", "pos", "ref", "alt", "maf", "pval_nominal", "slope",
        "slope_se", "rs_id_dbSNP155_GRCh38p13"]
GTEX_ROW = ["ENSG00000227232.5", "chr1_13550_G_A_b38", "chr1", "13550", "G", "A", "0.01", "0.3", "0.2",
            "0.19", "rs554008981"]


def test_headers_map_to_manifest_columns_like_the_existing_rows() -> None:
    roles = rules.detect_roles(GTEX, [GTEX_ROW])
    assert {r: roles.columns[r] for r in (*rules.REQUIRED_ROLES, "beta", "se", "rsid", "maf", "variant_id")} == {
        "chrom": "chr", "position": "pos", "ref": "ref", "alt": "alt", "pval": "pval_nominal",
        "phenotype_id": "phenotype_id", "beta": "slope", "se": "slope_se",
        "rsid": "rs_id_dbSNP155_GRCh38p13", "maf": "maf", "variant_id": "variant_id"}
    assert roles.build_hint == "GRCh38" and not roles.issues

    catalogue = ["molecular_trait_id", "chromosome", "position", "ref", "alt", "variant", "ma_samples",
                 "maf", "pvalue", "beta", "se", "type", "an", "gene_id", "median_tpm", "rsid"]
    row = ["ENSG00000187583", "1", "745642", "T", "TAA", "chr1_745642_T_TAA", "40", "0.2", "0.59", "-0.07",
           "0.13", "INDEL", "202", "ENSG00000187583", "1.2", "rs1"]
    roles = rules.detect_roles(catalogue, [row])
    assert roles.columns["phenotype_id"] == "molecular_trait_id" and roles.columns["pval"] == "pvalue"
    assert roles.columns["median_tpm"] == "median_tpm" and roles.file_n == 101  # an / 2


def test_packed_or_missing_columns_are_flagged_not_guessed() -> None:
    # tensorQTL output: chrom/pos/ref/alt live inside the variant id.
    roles = rules.detect_roles(["gene_id", "variant_id", "maf", "pval_nominal", "slope", "slope_se"],
                               [["ENSG00000187583.10", "chr1:14677:G:A", "0.1", "0.5", "0.1", "0.2"]])
    assert roles.derivable == ["chrom", "position", "ref", "alt"] and "columns_need_deriving" in roles.issues
    # RASQUAL: no p-value, an allele frequency, and an effect that is not a slope.
    rasqual = ["Feature_ID", "rs_ID", "Chromosome", "SNP_position", "Ref_Allele", "Alt_Allele",
               "Allele_Frequency", "Log_10_BH_Qvalue", "Chi_Sq_Stat", "Effect_Size"]
    rows = [["peak_1", "rs1", "1", "100", "A", "G", "0.6", "-1.7", "12.7", f"0.{i}3"] for i in (3, 4, 5)]
    roles = rules.detect_roles(rasqual, rows)
    assert "pval" not in roles.columns and "pval" in roles.derivable
    assert {"maf_col_is_allele_frequency", "effect_scale_check"} <= set(roles.issues)
    # A1/A2 are used, and flagged: which allele the effect refers to differs between tools.
    roles = rules.detect_roles(["SNP", "CHR", "BP", "A1", "A2", "P", "gene"], [["rs1", "1", "5", "A", "G", "0.1", "X"]])
    assert roles.columns["alt"] == "A1" and "allele_order_ambiguous" in roles.issues
    assert roles.columns["rsid"] == "SNP"  # a variant column holding rsIDs


def test_gene_id_mode_follows_the_importer() -> None:
    assert rules.gene_id_mode(["ENSG00000227232.5"], "eQTL") == ("self", "")
    assert rules.gene_id_mode(["chr1:100:200:clu_1_+:ENSG00000227232.5"], "sQTL") == ("phenotype_last_segment", "")
    assert rules.gene_id_mode(["GINS2"], "eQTL") == ("self", "phenotype_id_not_ensembl")
    assert rules.gene_id_mode(["chr1_100_200"], "caQTL") == ("", "")
    assert rules.gene_id_mode(["chr1:100-200"], "caQTL") == ("", "peak_id_needs_reformat")
    assert rules.gene_id_mode(["peak_12"], "caQTL") == ("", "peak_id_not_chr_start_end")


def test_what_a_table_holds() -> None:
    roles = rules.detect_roles(GTEX, [GTEX_ROW])
    assert rules.table_holds(roles, "x/Liver.allpairs.txt.gz", 5_000, has_header=True)[0] == "all_pairs"
    assert rules.table_holds(roles, "Liver.signif_pairs.txt.gz", 9e9, has_header=True)[0] == "subset"
    assert rules.table_holds(roles, "GlomsigeQTLs.txt.gz", 9e9, has_header=True)[0] == "subset"
    assert rules.table_holds(roles, "Table_S3.xlsx", 1e6, has_header=True, fmt="xlsx")[0] == "subset"
    assert rules.table_holds(roles, "liver.txt.gz", 3e9, has_header=True)[0] == "all_pairs"
    assert rules.table_holds(roles, "liver.txt.gz", 3e5, has_header=True)[0] == "unknown_scope"
    assert rules.table_holds(rules.Roles(), "expression.tsv", 3e9, has_header=True)[0] == "not_qtl"
    assert rules.qtl_type_from_text("caQTL_common_TensorQTL/ASDC/x.parquet") == "caQTL"
    assert rules.qtl_type_from_text("GlomsigeQTLsFormated.txt.gz") == "eQTL"
    assert rules.qtl_type_from_text("results/liver.txt.gz") == ""


def test_files_become_units_with_context_labels() -> None:
    def f(path: str) -> probe.RemoteFile:
        return probe.RemoteFile(path.rsplit("/", 1)[-1], f"https://x/{path}", 10, path)

    units = rules.make_units([f(f"caQTL/{ct}/TenK10K.cis_qtl_pairs.chr{c}.parquet")
                              for ct in ("ASDC", "CD14_Mono") for c in (1, 2, 10)])
    assert [(u.glob, len(u.files), u.token) for u in units] == [
        ("caQTL/ASDC/TenK10K.cis_qtl_pairs.chr*.parquet", 3, "ASDC"),
        ("caQTL/CD14_Mono/TenK10K.cis_qtl_pairs.chr*.parquet", 3, "CD14_Mono")]
    assert len({u.group for u in units}) == 1 and units[0].files[-1].path.endswith("chr10.parquet")

    flat = rules.make_units([f(f"OneK1K_{ct}.tsv.gz") for ct in ("B_naive", "B_memory", "NK")])
    assert sorted(u.token for u in flat) == ["B_memory", "B_naive", "NK"]
    # What varies between two files may be the QTL type, which is not a context.
    kinds = rules.make_units([f("skin eQTL summary statistics.txt"), f("skin pQTL summary statistics.txt")])
    assert [u.token for u in kinds] == ["", ""]


def test_a_quote_counts_only_when_it_is_in_the_text() -> None:
    texts = {"paper": "We mapped eQTLs (P < 0.05) in 1,267 individuals of European ancestry; MAF > 1%.",
             "README": "Summary statistics for 28 cell types from 922 donors."}
    assert rules.verify_quote("in 1,267 individuals of European ancestry", texts) == "paper"
    assert rules.verify_quote("eQTLs (P < 0.05) in 1,267 individuals", texts) == "paper"  # "<" is not a tag
    assert rules.verify_quote("28 cell types … 922 donors", texts) == "README"
    assert rules.verify_quote("in 1,500 individuals of European ancestry", texts) == ""
    assert rules.verify_quote("922", texts) == ""  # too short to prove anything
    assert rules.number_in(1267, "in 1,267 individuals") and not rules.number_in(126, "in 1,267 individuals")
    assert rules.population_code("individuals of European ancestry") == "EUR"
    assert rules.population_code("European and African American donors") == "ALL"
    contrast = "GTEx is predominantly of European ancestry, whereas our cohort is of Chinese ancestry."
    assert rules.quote_supports_population("EAS", contrast) and not rules.quote_supports_population("AFR", contrast)
    assert rules.n_from_name("rasqual_liver_175samples_fdr5.txt.gz") == 175


def test_reading_the_head_of_tables() -> None:
    text = "##comment\n#CHROM\tPOS\tP\n1\t100\t0.5\n1\t200\t0.1\n1\t3"
    t = probe.parse_text_table(text.encode())
    assert t.columns == ["CHROM", "POS", "P"] and t.has_header and t.rows == [["1", "100", "0.5"], ["1", "200", "0.1"]]
    headerless = probe.parse_text_table(b"peak_0\trs1\t1\t14599\tT\tA\t0.05\npeak_0\trs2\t1\t14604\tA\tG\t0.05\n", complete=True)
    assert not headerless.has_header and headerless.columns[0] == "col1" and len(headerless.rows) == 2
    # bgzip is a chain of gzip members; the head must read through them.
    blocks = b"".join(gzip.compress(part) for part in (b"gene\tp\n", b"ENSG1\t0.5\n", b"ENSG2\t0.1\n"))
    raw, compression = probe.decompress_head(blocks)
    assert compression == "gzip" and raw == b"gene\tp\nENSG1\t0.5\nENSG2\t0.1\n"
    # Spreadsheets carry title lines above the header.
    assert probe.pick_header([["Table S9. cis-eQTLs at FDR 5%"], ["gene", "snp", "p"], ["A", "rs1", "0.1"]]) == (1, True)


def test_spreadsheet_sheets_are_read_from_the_zip() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", '<workbook><sheets><sheet name="eQTLs" sheetId="1"/></sheets></workbook>')
        z.writestr("xl/sharedStrings.xml", "<sst><si><t>gene</t></si><si><t>pvalue</t></si><si><t>GINS2</t></si></sst>")
        z.writestr("xl/worksheets/sheet1.xml",
                   '<worksheet><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
                   '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>0.01</v></c></row></sheetData></worksheet>')
    tables = probe._xlsx_tables(zipfile.ZipFile(buf))
    assert [(t.name, t.columns, t.rows, t.fmt) for t in tables] == [("eQTLs", ["gene", "pvalue"], [["GINS2", "0.01"]], "xlsx")]


def test_zenodo_records_are_listed_through_the_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/records/123456"
        return httpx.Response(200, json={
            "id": 123456, "metadata": {"title": "Liver caQTL", "description": "<p>All 175 samples.</p>"},
            "files": [{"key": "liver.alltests.txt.gz", "size": 5, "links": {"self": "https://zenodo.org/f/1"}}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        listing = probe.list_location(http, "https://doi.org/10.5281/zenodo.123456")
        manual = probe.list_location(http, "https://www.dropbox.com/sh/abc?dl=0")
    assert listing.status == "listed" and [f.name for f in listing.files] == ["liver.alltests.txt.gz"]
    assert "All 175 samples" in listing.texts["repository description"]
    assert manual.status == "manual" and "Dropbox" in manual.note


RECORD = {"record_id": 7, "doi": "10.1/x", "first_author": "Wenz A", "year": "2026", "qtl_type": "caQTL",
          "publication_title": "Liver caQTL", "publication_url": "https://doi.org/10.1/x", "species": "human"}


def _candidate(path: str = "liver.alltests.txt.gz") -> manifest.Candidate:
    table = probe.Table("", list(GTEX), [list(GTEX_ROW)])
    c = manifest.Candidate("https://zenodo.org/records/1", "https://zenodo.org/records/1", path,
                           [probe.RemoteFile(path, f"https://zenodo.org/f/{path}", 3_000_000_000)],
                           table=table, access="open", cid="U1")
    c.roles = rules.detect_roles(table.columns, table.rows)
    c.holds, c.why = rules.table_holds(c.roles, path, c.size, has_header=True)
    return c


def test_a_row_keeps_the_evidence_for_every_fact() -> None:
    texts = {"README": "RASQUAL results from caQTL mapping performed in all 175 samples of human liver."}
    facts = {"study": {"genome_build": {"value": "GRCh38", "quote": ""}},
             "units": [{"id": "U1", "qtl_type": "caQTL", "holds": "all_pairs", "tissue_or_cell": "liver",
                        "level_1_context": "Liver", "level_2_context": "", "biocontext": "liver",
                        "context_quote": "caQTL mapping performed in all 175 samples of human liver",
                        "sample_size": {"value": 175, "quote": "performed in all 175 samples of human liver"},
                        "population": {"value": "EUR", "quote": "all donors were of European descent"}}]}
    [r] = manifest.assemble(RECORD, None, [_candidate()], facts, texts, rules.Target(level_1=["Blood", "Liver"]))
    row, evidence = r["row"], r["evidence"]
    assert (row["dataset"], row["qtl_type"], row["biocontext"], row["sample_size"]) == ("Wenz_2026", "caQTL", "liver", "175")
    assert row["pval_col"] == "pval_nominal" and row["use_in_lc2"] == "FALSE" and row["delimiter"] == "\\t"
    assert evidence["sample_size"]["verified"] and evidence["sample_size"]["source"] == "README"
    assert evidence["genome_build"]["source"] == "file path or variant ids" and r["genome_build"] == "GRCh38"
    # The ancestry quote is not in any text: the value is kept, and flagged.
    assert row["population"] == "EUR" and evidence["population"]["verified"] is False
    assert "population_unverified" in r["issues"] and "paper_full_text_not_read" in r["issues"]
    # caQTL phenotype ids without peak coordinates stop the importer: processing is needed.
    assert "peak_id_not_chr_start_end" in r["issues"] and r["status"] == "needs_processing"


def test_rules_decide_the_status() -> None:
    [no_llm] = manifest.assemble(RECORD, None, [_candidate()], None, {}, rules.Target())
    assert "llm_facts_missing" in no_llm["issues"] and no_llm["row"]["sample_size"] == ""
    [named] = manifest.assemble(RECORD, None, [_candidate("liver_175samples.alltests.txt.gz")], None, {}, rules.Target())
    assert named["row"]["sample_size"] == "175" and named["evidence"]["sample_size"]["source"] == "file path"
    assert rules.status_of([]) == "ready"
    assert rules.status_of(["sample_size_unverified"]) == "needs_review"
    assert rules.status_of(["sample_size_unverified", "columns_need_deriving"]) == "needs_processing"
    assert rules.status_of(["columns_need_deriving", "not_full_summary_statistics"]) == "not_loadable"
    assert set(rules.BLOCKING | rules.PROCESSING) <= set(rules.ISSUES)


def test_rows_are_stored_reviewed_and_exported_in_manifest_order(tmp_path: Path) -> None:
    saved = records.RecordStore(tmp_path / "r.sqlite3")
    rid = saved.save({"publication_title": "Liver caQTL", "doi": "10.1/x", "authors": "Wenz A", "year": "2026",
                      "download_url": "https://zenodo.org/records/1", "species": "human"})["record_id"]
    store = manifest.ManifestStore(saved.path)
    record = RECORD | {"record_id": rid}
    report = {"url": "https://zenodo.org/records/1", "content": "qtl_summary_statistics", "access": "open",
              "status": "listed", "resolver": "Zenodo API", "landing": "", "note": "", "n_files": 1, "inventory": []}
    rows = manifest.assemble(record, None, [_candidate()], None, {}, rules.Target())
    store.save_run(rid, rows, [report])
    client = TestClient(web.create_app(saved))
    listed = client.get("/api/manifest").json()
    assert len(listed["rows"]) == 1 and listed["locations"][0]["n_rows"] == 1
    row_id = listed["rows"][0]["row_id"]

    fixed = client.patch(f"/api/manifest/{row_id}", json={
        "fields": {"biocontext": "Liver", "level_1_context": "Liver", "sample_size": "175", "population": "ALL"},
        "review": "approved", "reviewer_note": "N from the README"}).json()
    assert fixed["review"] == "approved" and fixed["edited"] == ["biocontext", "level_1_context", "population", "sample_size"]
    assert client.patch(f"/api/manifest/{row_id}", json={"fields": {"nope": 1}}).status_code == 400

    # A rebuild never overwrites what a person approved or corrected.
    store.save_run(rid, rows, [report])
    assert store.rows()[0]["biocontext"] == "Liver" and len(store.rows()) == 1

    lines = client.get("/api/manifest/export?scope=approved&fmt=manifest").text.splitlines()
    assert lines[0].split("\t") == rules.MANIFEST_COLUMNS
    exported = dict(zip(lines[0].split("\t"), lines[1].split("\t"), strict=True))
    assert exported["biocontext"] == "Liver" and exported["sample_size"] == "175" and exported["delimiter"] == "\\t"
    # The approved rows are also kept as a file, rewritten on every change.
    on_disk = (tmp_path / "manifest" / manifest.MANIFEST_FILE).read_text().splitlines()
    assert on_disk == lines and listed["files"][manifest.MANIFEST_FILE]["rows"] == 0
    client.patch(f"/api/manifest/{row_id}", json={"review": "pending"})
    assert len((tmp_path / "manifest" / manifest.MANIFEST_FILE).read_text().splitlines()) == 1
    assert len((tmp_path / "manifest" / manifest.CANDIDATES_FILE).read_text().splitlines()) == 2
    assert client.get("/manifest").status_code == 200
    assert manifest.eligible({"species": "mouse"}) and not manifest.eligible({"species": "Homo sapiens"})


def test_a_run_is_started_watched_and_stopped_from_the_page(tmp_path: Path, monkeypatch) -> None:
    saved = records.RecordStore(tmp_path / "r.sqlite3")
    for i, species in enumerate(["Human", "Mouse", "Homo sapiens"]):
        saved.save({"publication_title": f"Paper {i}", "doi": f"10.1/x{i}", "species": species,
                    "qtl_type": "eQTL", "download_url": f"https://zenodo.org/records/{i + 1}"})
    built: list[int] = []

    async def fake_build(record, store, judge, **kw):
        built.append(record["record_id"])
        report = {"url": "https://zenodo.org/records/1", "content": "", "access": "", "status": "listed", "resolver": "",
                  "landing": "", "note": "", "n_files": 0, "inventory": []}
        store.save_run(record["record_id"], [], [report])
        return {"record_id": record["record_id"], "rows": 0, "statuses": {}, "locations": {},
                "tokens": 7, "model": "", "note": ""}

    monkeypatch.setattr(manifest, "build_record", fake_build)
    with TestClient(web.create_app(saved)) as client:
        before = client.get("/api/manifest/run").json()
        assert (before["job"], before["records"], before["waiting"]) == (None, 3, 2)
        assert client.post("/api/manifest/run", json={"record_ids": [99]}).status_code == 400
        started = client.post("/api/manifest/run", json={"limit": 5, "no_llm": True})
        assert started.status_code == 200 and started.json()["job"]["total"] == 2
        for _ in range(50):
            status = client.get("/api/manifest/run").json()
            if status["job"]["state"] != "running":
                break
        assert status["job"]["state"] == "done" and status["job"]["tokens"] == 14
        assert len(built) == 2 and status["waiting"] == 0
        assert client.post("/api/manifest/run", json={}).status_code == 400  # nothing waits
        papers = client.get("/api/manifest").json()["records"]
        assert {p["n_locations"] for p in papers.values()} == {1} and papers[str(built[0])]["not_eligible"] == ""
        assert sorted(client.get("/api/manifest").json()["built"]) == sorted(built)
        assert client.post("/api/manifest/run/stop").json()["job"]["state"] == "done"


def test_build_clicks_join_the_queue_of_a_running_batch(tmp_path: Path) -> None:
    store = manifest.ManifestStore(tmp_path / "r.sqlite3")
    one, two = {"record_id": 1, "publication_title": "A"}, {"record_id": 2, "publication_title": "B"}
    batch = manifest.Batch([one], store)
    batch.current = {"record_id": 1}  # record 1 is being built
    assert batch.queued() == []
    assert batch.add([one, two]) == 1 and batch.queued() == [2]  # the one in progress is not queued twice
    assert batch.add([two]) == 0 and batch.snapshot()["queue"] == [2]


def test_a_dropped_connection_is_tried_again_and_explained(monkeypatch) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadError("[Errno 54] Connection reset by peer")
        return httpx.Response(200, content=b"gene\tsnp\tpval\nA\trs1\t0.1\n")

    monkeypatch.setattr(probe.time, "sleep", lambda s: None)
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        got = probe.probe_file(http, probe.RemoteFile("x.tsv", "https://example.org/x.tsv"))
    assert got.kind == "table" and len(calls) == 2

    with httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ReadError("reset by peer")))) as http:
        got = probe.probe_file(http, probe.RemoteFile("x.tsv", "https://example.org/x.tsv"))
    assert got.kind == "error" and got.note.startswith("the server cut the connection")


def test_what_needs_a_person_is_collected_per_paper() -> None:
    loc = {"url": "https://data.bris.ac.uk/x", "status": "listed", "note": "", "inventory": [
        {"path": "all.zip", "holds": "unknown_scope", "why": "the server cut the connection", "needs_hand": True},
        {"path": "lead_snps.txt", "holds": "subset", "why": "lead SNPs only"},
        {"path": "README.txt", "holds": "doc", "why": ""}]}
    ftp = {"url": "ftp://ftp.example.org/qtl", "status": "manual", "note": "FTP server: open it with an FTP client", "inventory": []}
    too_big = {"review": "pending", "issues": ["data_too_large"], "source_path": "chr*.parquet",
               "evidence": {"data": {"status": "too_large", "note": "1244 GB would pass the 50 GB limit"}}}
    hand = manifest.needs_you([too_big], [loc, ftp])
    assert [h["what"] for h in hand] == ["all.zip", "ftp://ftp.example.org/qtl", "chr*.parquet"]
    assert "50 GB" in hand[2]["why"]
    # Subsets and documentation are not a person's job; a rejected row is not either.
    assert manifest.needs_you([too_big | {"review": "rejected"}], [loc | {"inventory": loc["inventory"][1:]}]) == []


def test_papers_are_tagged_by_what_their_locations_offer() -> None:
    loc = lambda content, access="open": {"content": content, "access_route": access}  # noqa: E731
    as_json = lambda *locs: {"locations": json.dumps(list(locs))}  # noqa: E731
    assert web.data_kind(as_json(loc("supplementary_table"), loc("qtl_summary_statistics"))) == "open_sumstats"
    assert web.data_kind(as_json(loc("qtl_summary_statistics", "controlled"))) == "closed_sumstats"
    assert web.data_kind(as_json(loc("supplementary_table"))) == "supplement"
    assert web.data_kind(as_json(loc("qtl_results_browser"), loc("supplementary_table"))) == "browser"
