"""Curator decisions persist independently of AI results and exclude future builds."""
import asyncio

from fastapi.testclient import TestClient

from dataqueryweb import manifest, records, web

PAPER = {"doi": "10.1234/curator", "publication_title": "Software name containing QTL",
         "download_url": "https://example.org/results", "species": "human"}


def test_review_survives_reopen_and_autosave(tmp_path):
    path = tmp_path / "records.sqlite3"
    saved = records.RecordStore(path)
    row = saved.save(PAPER, auto=True)
    rid = row["record_id"]
    decision = saved.update(rid, qtl_review="not_qtl", qtl_review_note="Simulation inputs, not real associations")
    assert decision["qtl_reviewed_at"]
    saved.save(PAPER | {"qtl_review": "qtl", "qtl_review_note": "AI overwrote"}, auto=True)
    updated = records.RecordStore(path).get(rid)
    assert updated["qtl_review"] == "not_qtl"
    assert updated["qtl_review_note"] == "Simulation inputs, not real associations"
    saved.save(PAPER | {"download_url": "https://example.org/another"})
    assert saved.get(rid)["qtl_review"] == "not_qtl"
    assert "human review" in manifest.eligible(updated)
    assert manifest.select([updated], set(), record_ids=[rid], redo=True, all_species=True) == []
    cleared = saved.update(rid, qtl_review="qtl", qtl_review_note="Actual nominal associations verified")
    assert manifest.select([cleared], set(), record_ids=[rid]) == [cleared]
    assert manifest.eligible(cleared) == ""


def test_review_api_validation_and_persistence(tmp_path):
    saved = records.RecordStore(tmp_path / "records.sqlite3")
    row = saved.save(PAPER)
    rid = row["record_id"]
    with TestClient(web.create_app(saved)) as client:
        r = client.patch(f"/api/records/{rid}", json={"qtl_review": "not_qtl", "qtl_review_note": "No associations"})
        assert r.status_code == 200
        assert r.json()["qtl_review"] == "not_qtl"
        bad = client.patch(f"/api/records/{rid}", json={"qtl_review": "invalid", "curator_note": "must not be saved"})
        assert bad.status_code == 422
        assert saved.get(rid)["curator_note"] == ""
        listed = client.get("/api/manifest").json()["records"][str(rid)]
        assert listed["qtl_review"] == "not_qtl"
        assert "human review" in listed["not_eligible"]
        assert client.post("/api/manifest/run", json={"record_ids": [rid], "no_llm": True}).status_code == 400
        client.patch(f"/api/records/{rid}", json={"qtl_review": ""})
        assert saved.get(rid)["qtl_review"] == ""


def test_queued_review_is_refreshed_before_build(tmp_path):
    saved = records.RecordStore(tmp_path / "records.sqlite3")
    original = saved.save(PAPER)
    store = manifest.ManifestStore(saved.path, tmp_path / "manifest")
    # Queue snapshot predates the curator's exclusion; the worker must reread it.
    batch = manifest.Batch([original], store, no_llm=True, record_store=saved)
    saved.update(original["record_id"], qtl_review="not_qtl", qtl_review_note="Human rejected")
    asyncio.run(batch.run())
    assert batch.done == 1 and batch.tokens == 0 and batch.rows == 0
    assert "human review" in batch.log[0]["text"]
    assert store.rows() == []
