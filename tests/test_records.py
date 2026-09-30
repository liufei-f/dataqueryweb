"""Tests for saved records (dataqueryweb.records) and their API, on a temporary database."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from dataqueryweb import records, web

ROW = {
    "publication_title": "Brain cell-type eQTL",
    "doi": "10.1038/s41593-022-01128-z",
    "authors": "Bryois J, Calini D",
    "qtl_type": ["eQTL"],
    "download_url": "https://doi.org/10.5281/zenodo.5543734",
    "access_route": "open",
}


def test_one_record_per_publication_with_every_location(tmp_path: Path) -> None:
    store = records.RecordStore(tmp_path / "r.sqlite3")
    first = store.save(ROW)
    again = store.save(ROW | {"qtl_context": "microglia"})
    other = store.save(ROW | {"download_url": "https://malhotralab.shinyapps.io/x",
                              "access_route": "request"})
    assert first["record_id"] == again["record_id"] == other["record_id"]
    assert len(store.all()) == 1
    assert other["download_url"] == ("https://doi.org/10.5281/zenodo.5543734; "
                                     "https://malhotralab.shinyapps.io/x")
    assert other["access_route"] == "open; request"
    assert other["qtl_context"] == "microglia"
    assert other["first_author"] == "Bryois J"
    assert other["qtl_type"] == "eQTL"


def test_one_location_can_be_removed_and_the_last_takes_the_record(tmp_path: Path) -> None:
    store = records.RecordStore(tmp_path / "r.sqlite3")
    store.save(ROW)
    rid = store.save(ROW | {"download_url": "https://malhotralab.shinyapps.io/x"})["record_id"]
    left = store.remove_location(rid, "http://www.malhotralab.shinyapps.io/x/")
    assert left["download_url"] == "https://doi.org/10.5281/zenodo.5543734"
    assert store.remove_location(rid, "https://nowhere.example") is None
    assert store.remove_location(rid, ROW["download_url"]) == {"deleted": True}
    assert store.all() == []


def test_duplicate_rows_of_one_paper_are_merged_on_open(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "r.sqlite3"
    store = records.RecordStore(path)
    a = store.save(ROW | {"curator_note": "check files"})
    with sqlite3.connect(path) as db:  # rows written under the old one-per-location rule
        db.execute("UPDATE records SET record_key = 'old-a', locations = '' WHERE record_id = ?",
                   [a["record_id"]])
        db.execute(
            "INSERT INTO records (record_key, publication_title, doi, download_url, "
            "access_route, saved_by, curator_note, saved_at) VALUES "
            "('old-b', ?, ?, 'https://eqtlgen.org', 'open', 'auto', 'second note', 'now')",
            [ROW["publication_title"], ROW["doi"]])
    rows = records.RecordStore(path).all()
    assert len(rows) == 1
    assert rows[0]["record_id"] == a["record_id"]
    assert rows[0]["download_url"] == ROW["download_url"] + "; https://eqtlgen.org"
    assert rows[0]["saved_by"] == "human"
    assert rows[0]["curator_note"] == "check files | second note"
    assert list(tmp_path.glob("r.sqlite3.bak-*"))


def test_export_uses_review_table_columns(tmp_path: Path) -> None:
    store = records.RecordStore(tmp_path / "r.sqlite3")
    store.save(ROW | {"extraction_note": "tab\there\nnewline"})
    header, line = store.export("tsv").splitlines()
    assert header.split("\t") == records.COLUMNS
    assert header.startswith("record_id\tpublication_title\tpublication_url\tdoi")
    assert "tab here newline" in line


def test_records_api(tmp_path: Path) -> None:
    client = TestClient(web.create_app(records.RecordStore(tmp_path / "r.sqlite3")))
    assert client.post("/api/records", json={"doi": "x"}).status_code == 400
    saved = client.post("/api/records", json=ROW).json()
    rid = saved["record_id"]
    assert client.get("/api/records").json()[0]["doi"] == ROW["doi"]
    assert (
        client.patch(f"/api/records/{rid}", json={"curator_note": "checked"}).json()["curator_note"]
        == "checked"
    )
    export = client.get("/api/records/export?fmt=csv")
    assert export.headers["content-disposition"].endswith('.csv"')
    assert "checked" in export.text
    assert client.get("/records").status_code == 200
    assert client.delete(f"/api/records/{rid}").json() == {"deleted": True}
    assert client.delete(f"/api/records/{rid}").status_code == 404


def test_url_variants_match_the_same_record(tmp_path: Path) -> None:
    store = records.RecordStore(tmp_path / "r.sqlite3")
    a = store.save(ROW | {"download_url": "http://www.eqtlgen.org/"})
    b = store.save(ROW | {"download_url": "https://eqtlgen.org"})
    assert a["record_id"] == b["record_id"]
    assert a["record_key"] == "10.1038/s41593-022-01128-z"
    assert b["download_url"] == "https://eqtlgen.org"  # updated in place, not added


def test_old_keys_are_migrated(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "r.sqlite3"
    store = records.RecordStore(path)
    saved = store.save(ROW)
    with sqlite3.connect(path) as db:  # simulate a row written under the old key rule
        db.execute(
            "UPDATE records SET record_key = 'old' WHERE record_id = ?", [saved["record_id"]]
        )
    migrated = records.RecordStore(path).all()
    assert migrated[0]["record_key"] == records.record_key(ROW)


def _paper(confidence: float = 0.95, verdict: str = "yes", pick: str = "qtl_summary_statistics"):
    from dataqueryweb.extract import Source
    from dataqueryweb.finder import Paper

    p = Paper(
        title="Brain eQTL", doi="10.1/x", authors="Doe J, Roe R", url="https://doi.org/10.1/x"
    )
    code = Source(
        url="https://github.com/a/b",
        label="code",
        repository="GitHub",
        kind="repository",
        access="open",
    )
    data = Source(
        url="https://zenodo.org/records/1",
        label="z",
        repository="Zenodo",
        kind="repository",
        access="open",
    )
    data.ai_pick, code.ai_pick = pick, "other"
    p.sources = [data, code]
    p.ai = {
        "new_qtl_data": verdict,
        "confidence": confidence,
        "model": "proxy · gpt-5.5",
        "qtl_types": ["eQTL"],
    }
    return p


def test_autosave_saves_only_confident_qtl_result_locations(tmp_path: Path) -> None:
    store = records.RecordStore(tmp_path / "r.sqlite3")
    out = records.autosave(store, _paper(), search_terms="brain eQTL")
    rows = store.all()
    assert [r["download_url"] for r in rows] == [
        "https://zenodo.org/records/1"
    ]  # not the code link
    assert rows[0]["saved_by"] == "auto" and rows[0]["ai_confidence"] == "0.95"
    assert out == {"saved": [rows[0]["record_id"]], "locations": 1,
                   "reason": "new QTL data, confidence 95%"}


def test_autosave_puts_every_picked_location_of_a_paper_in_one_record(tmp_path: Path) -> None:
    store = records.RecordStore(tmp_path / "r.sqlite3")
    paper = _paper()
    paper.sources[1].ai_pick = "qtl_summary_statistics"
    out = records.autosave(store, paper)
    rows = store.all()
    assert len(rows) == 1
    assert rows[0]["download_url"] == "https://zenodo.org/records/1; https://github.com/a/b"
    assert out["saved"] == [rows[0]["record_id"]] and out["locations"] == 2


def test_autosave_leaves_uncertain_papers_for_a_human(tmp_path: Path) -> None:
    store = records.RecordStore(tmp_path / "r.sqlite3")
    assert records.autosave(store, _paper(confidence=0.8))["reason"] == "confidence 80% < 90%"
    assert records.autosave(store, _paper(verdict="unclear"))["reason"] == "AI unsure"
    assert records.autosave(store, _paper(pick="raw_or_individual_data"))["reason"].startswith(
        "no download"
    )
    fallback = _paper()
    fallback.ai["model"] = "Pollinations · gpt-oss-20b (fallback)"  # type: ignore[index]
    assert records.autosave(store, fallback)["reason"] == "judged by the fallback model"
    assert records.autosave(store, fallback, allow_fallback=True)["saved"]
    assert len(store.all()) == 1


def test_auto_never_overwrites_human_and_notes_survive(tmp_path: Path) -> None:
    store = records.RecordStore(tmp_path / "r.sqlite3")
    human = store.save(
        records.from_paper(_paper(), _paper().sources[0]) | {"qtl_context": "microglia"}
    )
    store.update_note(human["record_id"], "checked by hand")
    out = records.autosave(store, _paper())
    assert out["saved"] == []  # already a human record: untouched
    row = store.all()[0]
    assert (row["saved_by"], row["qtl_context"], row["curator_note"]) == (
        "human",
        "microglia",
        "checked by hand",
    )
    # A human re-save (as the page does, without a note) keeps the note too.
    store.save(records.from_paper(_paper(), _paper().sources[0]))
    assert store.all()[0]["curator_note"] == "checked by hand"


def test_human_confirm_turns_auto_into_human(tmp_path: Path) -> None:
    store = records.RecordStore(tmp_path / "r.sqlite3")
    records.autosave(store, _paper())
    confirmed = store.save(records.from_paper(_paper(), _paper().sources[0]))
    assert confirmed["saved_by"] == "human" and len(store.all()) == 1


def test_confirm_endpoint_marks_auto_as_human(tmp_path: Path) -> None:
    store = records.RecordStore(tmp_path / "r.sqlite3")
    client = TestClient(web.create_app(store))
    records.autosave(store, _paper())
    rid = store.all()[0]["record_id"]
    row = client.patch(f"/api/records/{rid}", json={"saved_by": "human"}).json()
    assert row["saved_by"] == "human"
    assert (
        client.patch(f"/api/records/{rid}", json={"curator_note": "ok"}).json()["saved_by"]
        == "human"
    )


def test_find_paper_matches_doi_published_doi_pmid_title_or_url(tmp_path: Path) -> None:
    from dataqueryweb.finder import Paper

    store = records.RecordStore(tmp_path / "r.sqlite3")
    store.save(
        ROW
        | {
            "publication_title": "Cell-type-specific cis-eQTLs in eight human brain cell types",
            "pmid": "35915177",
            "publication_url": "https://doi.org/10.1038/s41593-022-01128-z",
        }
    )
    assert store.find_paper(Paper(doi="10.1038/S41593-022-01128-Z"))
    assert store.find_paper(Paper(doi="10.1101/2021.10.09.21264604", published_doi=ROW["doi"]))
    assert store.find_paper(Paper(pmid="35915177"))
    assert store.find_paper(
        Paper(title="Cell-type-specific cis-eQTLs in eight human brain cell-types.")
    )
    assert store.find_paper(Paper(url="http://doi.org/10.1038/s41593-022-01128-z/"))
    assert not store.find_paper(Paper(doi="10.1/other", title="Something else entirely here"))
    assert not store.find_paper(Paper(title="eQTL"))  # too short to trust a title match


def test_saved_papers_skip_the_llm(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from dataqueryweb import finder, llm

    async def fake_inspect_ref(http, ref):  # type: ignore[no-untyped-def]
        return finder.Paper(title="Brain eQTL", doi=ROW["doi"])

    calls: list[str] = []

    async def fake_judge(self, paper):  # type: ignore[no-untyped-def]
        calls.append(paper.doi)
        return {"new_qtl_data": "yes", "confidence": 0.5, "data_sources": [],
                "usage": {"total_tokens": 100, "calls": 1}}

    monkeypatch.setattr(finder, "inspect_ref", fake_inspect_ref)
    monkeypatch.setattr(llm.Judge, "judge", fake_judge)
    store = records.RecordStore(tmp_path / "r.sqlite3")
    client = TestClient(web.create_app(store))

    first = client.get("/api/inspect", params={"ref": "xyz", "ai": True}).json()["ai"]
    assert "skipped" not in first and "cached" not in first
    assert calls == [ROW["doi"]]
    # Checked once: the same paper is not sent to the LLM again, saved or not.
    again = client.get("/api/inspect", params={"ref": "xyz", "ai": True}).json()["ai"]
    assert again["cached"]["check_id"] and again["new_qtl_data"] == "yes"
    assert calls == [ROW["doi"]]
    store.save(ROW)
    saved = client.get("/api/inspect", params={"ref": "xyz", "ai": True}).json()["ai"]
    assert saved["cached"] and saved["saved_records"]
    assert calls == [ROW["doi"]]
    client.get("/api/inspect", params={"ref": "xyz", "ai": True, "recheck": True})
    assert calls == [ROW["doi"], ROW["doi"]]  # recheck forces it


def test_papers_saved_before_checks_were_kept_still_skip_the_llm(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from dataqueryweb import finder, llm

    async def fake_inspect_ref(http, ref):  # type: ignore[no-untyped-def]
        return finder.Paper(title="Brain eQTL", doi=ROW["doi"])

    async def fake_judge(self, paper):  # type: ignore[no-untyped-def]
        raise AssertionError("must not be called")

    monkeypatch.setattr(finder, "inspect_ref", fake_inspect_ref)
    monkeypatch.setattr(llm.Judge, "judge", fake_judge)
    store = records.RecordStore(tmp_path / "r.sqlite3")
    store.save(ROW)
    ai = TestClient(web.create_app(store)).get(
        "/api/inspect", params={"ref": "xyz", "ai": True}).json()["ai"]
    assert ai["skipped"] and ai["saved_by"] == ["human"]
