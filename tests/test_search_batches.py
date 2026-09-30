"""A search can be continued past its first batch with Europe PMC's cursor."""

from __future__ import annotations

import asyncio

from dataqueryweb import finder

ALL = [{"id": str(i), "title": f"Paper {i}", "pmid": str(i)} for i in range(250)]


def _fake_get(calls):  # type: ignore[no-untyped-def]
    async def epmc_get(http, params):  # type: ignore[no-untyped-def]
        start = int(params["cursorMark"]) if params["cursorMark"] != "*" else 0
        page = ALL[start:start + params["pageSize"]]
        calls.append((start, params["pageSize"]))
        return {"hitCount": len(ALL), "resultList": {"result": page},
                "nextCursorMark": str(start + len(page)) if page else params["cursorMark"]}
    return epmc_get


def test_batches_continue_where_the_last_one_stopped(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(finder, "epmc_get", _fake_get(calls))
    monkeypatch.setattr(finder, "paper_from_epmc", lambda x: finder.Paper(title=x["title"], pmid=x["pmid"]))

    hits, first, cursor = asyncio.run(finder.epmc_search(None, "eqtl", limit=100))
    assert hits == 250 and [p.pmid for p in first] == [str(i) for i in range(100)]
    hits, second, cursor = asyncio.run(finder.epmc_search(None, "eqtl", limit=100, cursor=cursor))
    assert [p.pmid for p in second] == [str(i) for i in range(100, 200)]
    hits, last, cursor = asyncio.run(finder.epmc_search(None, "eqtl", limit=100, cursor=cursor))
    assert [p.pmid for p in last] == [str(i) for i in range(200, 250)]
    assert cursor == ""  # nothing left: no Continue button
    assert calls[1] == (100, 100)  # the second batch did not re-read the first


def test_search_pauses_and_resumes_without_rechecking(tmp_path, monkeypatch):
    import json

    from fastapi.testclient import TestClient

    from dataqueryweb import llm, records, web

    calls = []
    quota = [True]

    async def search(http, q, **kwargs):
        return 3, [finder.Paper(title=f"Test literature paper number {i}", pmid=str(i))
                   for i in range(3)], ""

    async def inspect(http, paper):
        return paper

    async def judge(self, paper):
        calls.append(paper.pmid)
        if paper.pmid == "1" and quota[0]:
            return {"error": "usage limit", "usage": llm.new_usage()}
        # Successful checks must be kept even when a provider omits token usage.
        return {"new_qtl_data": "no", "data_sources": [], "usage": llm.new_usage()}

    monkeypatch.setattr(finder, "epmc_search", search)
    monkeypatch.setattr(finder, "inspect", inspect)
    monkeypatch.setattr(llm.Judge, "judge", judge)
    store = records.RecordStore(tmp_path / "resume.sqlite3")
    client = TestClient(web.create_app(store))

    def run(**extra):
        response = client.get("/api/search", params={"q": "eqtl", "ai": True, **extra})
        assert response.status_code == 200
        return [json.loads(line) for line in response.text.splitlines()]

    first = run()
    assert first[-1] == {"type": "paused", "offset": 1, "message": "usage limit"}
    assert calls == ["0", "1"]  # paper 2 cannot spend tokens after quota exhaustion
    quota[0] = False
    resumed = run(offset=1)
    assert [m["rank"] for m in resumed if m["type"] == "paper"] == [1, 2]
    assert resumed[-1]["type"] == "done"
    assert calls == ["0", "1", "1", "2"]
    # A later search reuses all checks, including after recreating the app.
    client = TestClient(web.create_app(store))
    repeated = run()
    assert all(m["paper"]["ai"]["cached"] for m in repeated if m["type"] == "paper")
    assert calls == ["0", "1", "1", "2"]
