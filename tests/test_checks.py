"""LLM checks are kept: token usage is summed, and a checked paper is never sent again."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from dataqueryweb import checks, finder, llm, records, web
from dataqueryweb.extract import DataFile, Source


def test_usage_reads_chat_and_responses_blocks() -> None:
    usage = llm.new_usage()
    llm.add_usage(usage, {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200})
    llm.add_usage(usage, {"input_tokens": 3000, "output_tokens": 500})  # Responses API
    llm.add_usage(usage, None)  # a provider that reports nothing
    assert usage == {"prompt_tokens": 4000, "completion_tokens": 700, "total_tokens": 4700,
                     "calls": 2}


def _paper() -> finder.Paper:
    p = finder.Paper(title="A single-cell eQTL atlas of the human brain", doi="10.1/ABC")
    p.sources = [Source(url="https://zenodo.org/records/1", label="z", repository="Zenodo",
                        kind="repository", access="open")]
    return p


def test_checks_are_summed_and_the_latest_good_one_is_reused(tmp_path: Path) -> None:
    store = checks.CheckStore(records.RecordStore(tmp_path / "r.sqlite3"))
    failed = _paper()
    store.record(failed, {"error": "bad JSON", "usage": {"total_tokens": 300, "calls": 1}})
    assert store.find(_paper()) is None  # a failed check spent tokens but is not reused

    judged = _paper()
    judged.sources[0].ai_pick = "qtl_summary_statistics"
    web_found = Source(url="https://example.org/sumstats", label="w", repository="example.org",
                       kind="web", access="open", files=[DataFile("a.tsv", "https://x/a.tsv")],
                       ai_pick="qtl_summary_statistics", ai_note="Found by web search")
    judged.sources.insert(0, web_found)
    store.record(judged, {"new_qtl_data": "yes", "confidence": 0.9, "model": "p · gpt-6-luna",
                          "usage": {"prompt_tokens": 900, "completion_tokens": 100,
                                    "total_tokens": 1000, "calls": 2}})

    fresh = _paper()  # the same paper, found again by a later search (by DOI, any case)
    fresh.doi = "10.1/abc"
    row = store.find(fresh)
    verdict = checks.reuse(fresh, row)
    assert verdict["new_qtl_data"] == "yes" and verdict["cached"]["tokens_then"] == 1000
    assert verdict["usage"]["total_tokens"] == 0
    picked = {s.url: s.ai_pick for s in fresh.sources}
    assert picked == {"https://example.org/sumstats": "qtl_summary_statistics",
                      "https://zenodo.org/records/1": "qtl_summary_statistics"}
    assert fresh.sources[0].files[0].name == "a.tsv"

    u = store.summary()
    assert (u["checks"], u["papers"], u["failed"], u["total_tokens"]) == (2, 1, 1, 1300)
    assert u["avg_tokens_per_check"] == 650
    assert u["by_model"][0]["model"] == "p · gpt-6-luna"


def test_usage_endpoint(tmp_path: Path) -> None:
    store = records.RecordStore(tmp_path / "r.sqlite3")
    client = TestClient(web.create_app(store))
    assert client.get("/api/usage").json()["checks"] == 0


def test_checked_papers_page_lists_each_papers_latest_check(tmp_path: Path) -> None:
    rs = records.RecordStore(tmp_path / "r.sqlite3")
    store = checks.CheckStore(rs)
    old = _paper()
    store.record(old, {"new_qtl_data": "no", "usage": {"total_tokens": 10}})
    newer = _paper()
    newer.sources[0].ai_pick = "qtl_summary_statistics"
    newer.authors, newer.journal = "Doe J, Roe R", "Nature"
    store.record(newer, {"new_qtl_data": "yes", "confidence": 0.8, "usage": {"total_tokens": 20}})
    other = finder.Paper(title="Another paper entirely about liver sQTL", pmid="123")
    store.record(other, {"error": "timed out"})

    client = TestClient(web.create_app(rs))
    rows = client.get("/api/checks").json()
    assert [(r["paper"]["title"][:7], r["ok"]) for r in rows] == [("Another", False), ("A singl", True)]
    latest = rows[1]
    assert latest["verdict"]["new_qtl_data"] == "yes" and latest["total_tokens"] == 20
    assert latest["paper"]["journal"] == "Nature"
    assert latest["paper"]["sources"][0]["ai_pick"] == "qtl_summary_statistics"
    assert "ai" not in latest["paper"]
    page = client.get("/checked")
    assert page.status_code == 200 and "AI-checked papers" in page.text


def test_a_paper_refused_for_quota_is_not_recorded(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def fake_inspect_ref(http, ref):  # type: ignore[no-untyped-def]
        return _paper()

    async def out_of_quota(self, paper):  # type: ignore[no-untyped-def]
        return {"error": "gpt-6-luna: usage limit; gpt-5.6-luna: usage limit",
                "usage": llm.new_usage()}

    monkeypatch.setattr(finder, "inspect_ref", fake_inspect_ref)
    monkeypatch.setattr(llm.Judge, "judge", out_of_quota)
    rs = records.RecordStore(tmp_path / "r.sqlite3")
    client = TestClient(web.create_app(rs))
    ai = client.get("/api/inspect", params={"ref": "xyz", "ai": True}).json()["ai"]
    assert "usage limit" in ai["error"]
    assert client.get("/api/usage").json()["checks"] == 0
