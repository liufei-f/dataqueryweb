"""Offline tests for parsing and sanitising LLM replies (dataqueryweb.llm)."""

from __future__ import annotations

import os

from dataqueryweb import finder, llm
from dataqueryweb.extract import Source


def paper() -> finder.Paper:
    p = finder.Paper(title="Brain eQTL", abstract="We mapped <i>cis</i>-eQTLs.")
    p.sources = [
        Source(
            url="https://github.com/x/y",
            label="code",
            repository="GitHub",
            kind="repository",
            access="open",
        ),
        Source(
            url="https://zenodo.org/records/1",
            label="z",
            repository="Zenodo",
            kind="repository",
            access="open",
        ),
    ]
    return p


def test_prompt_numbers_candidates() -> None:
    text, ids = llm.build_prompt(paper())
    assert ids == {"S1": 0, "S2": 1}
    assert "S2 | Zenodo | open" in text
    assert "<i>" not in text


def test_parse_json_tolerates_fences_and_thinking() -> None:
    reply = '<think>hmm {"x": 1}</think>Sure!\n```json\n{"new_qtl_data": "yes"}\n```'
    assert llm.parse_json(reply) == {"new_qtl_data": "yes"}


def test_normalize_drops_invented_sources_and_keeps_answers() -> None:
    raw = {
        "new_qtl_data": "yes",  # ignored: the program decides the verdict
        "confidence": 0.99,  # ignored too
        "answers": {"mapped_qtls": {"answer": "YES", "quote": "We mapped eQTLs in 196 donors."}},
        "data_sources": [
            {"id": "s2", "content": "qtl_summary_statistics", "note": "all cell types"},
            {"id": "S9", "content": "qtl_summary_statistics"},
            {"url": "https://made-up.example"},
        ],
    }
    out = llm.normalize(raw, {"S1": 0, "S2": 1})
    assert "new_qtl_data" not in out and "confidence" not in out
    assert out["answers"]["mapped_qtls"]["answer"] == "yes"
    assert out["answers"]["reuse_only"]["answer"] == "unclear"  # missing → unclear
    assert out["data_sources"] == [
        {"index": 1, "content": "qtl_summary_statistics", "note": "all cell types"}
    ]


def test_apply_moves_picks_first() -> None:
    p = paper()
    llm.apply(
        p,
        llm.normalize({"new_qtl_data": "yes", "data_sources": [{"id": "S2"}]}, {"S1": 0, "S2": 1}),
    )
    assert p.sources[0].url == "https://zenodo.org/records/1"
    assert p.sources[0].ai_pick == "other"
    assert p.ai is not None and "data_sources" not in p.ai


def test_quota_limited_primary_falls_back() -> None:
    import asyncio
    import json

    import httpx

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        if request.url.host == "proxy.example":
            return httpx.Response(429, json={"error": {"type": "usage_limit_reached"}})
        reply = json.dumps(
            {
                "answers": {
                    "mapped_qtls": {"answer": "no", "quote": ""},
                    "reuse_only": {"answer": "yes", "quote": "We used eQTLGen summary statistics."},
                },
                "reason": "MR only",
            }
        )
        return httpx.Response(
            200, json={"model": "gpt-oss-20b", "choices": [{"message": {"content": reply}}]}
        )

    primary = llm.LLMConfig("proxy.example", "http://proxy.example/v1", "k", ["a", "b"], 2)
    judge = llm.Judge([primary, llm.pollinations()])
    judge.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def run() -> list[dict[str, object]]:
        out = [await judge.judge(paper()), await judge.judge(paper())]
        await judge.aclose()
        return out

    first, second = asyncio.run(run())
    assert first["new_qtl_data"] == "no"
    assert first["model"] == "Pollinations · gpt-oss-20b (fallback)"
    # Exhausted models are not retried for the second paper; no waiting on 429s.
    assert calls == ["a", "b", "openai", "openai"]
    assert second["model"] == "Pollinations · gpt-oss-20b (fallback)"


def test_chat_retries_one_transient_timeout(monkeypatch) -> None:
    import asyncio

    import httpx

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("slow proxy", request=request)
        return httpx.Response(
            200,
            json={"model": "m", "choices": [{"message": {"content": "ok"}}]},
        )

    async def no_wait(_seconds: float) -> None:
        pass

    monkeypatch.setattr(llm.asyncio, "sleep", no_wait)
    judge = llm.Judge([llm.LLMConfig("proxy", "http://proxy.example/v1", "k", ["m"], 1)])
    judge.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def run() -> tuple[str, str]:
        try:
            return await judge._chat(judge.config, "m", "paper")
        finally:
            await judge.aclose()

    assert asyncio.run(run()) == ("ok", "m")
    assert calls == 2


def test_gpt_6_1_chat_omits_unsupported_temperature() -> None:
    import asyncio
    import json

    import httpx

    sent: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"model": "gpt-6.1-sol", "choices": [{"message": {"content": "ok"}}]},
        )

    judge = llm.Judge(
        [llm.LLMConfig("proxy", "http://proxy.example/v1", "k", ["gpt-6.1-sol"], 1)]
    )
    judge.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def run() -> None:
        try:
            await judge._chat(judge.config, "gpt-6.1-sol", "paper")
        finally:
            await judge.aclose()

    asyncio.run(run())
    assert sent["model"] == "gpt-6.1-sol"
    assert "temperature" not in sent


def test_text_excerpt_puts_data_paragraphs_first_within_budget() -> None:
    paras = [
        "[Intro] " + "x" * 50,
        "[Data availability] eQTL summary statistics on Zenodo",
        "[Methods] " + "y" * 50,
    ]
    out = llm.text_excerpt(paras, 115)
    assert "Zenodo" in out and "x" * 50 in out and "y" * 50 not in out
    assert out.index("Intro") < out.index("Zenodo")  # document order is kept


def test_needs_research_only_without_full_text_or_download() -> None:
    cfg = llm.LLMConfig("proxy", "http://p/v1", "k", ["m"], 1, web_search=True)
    judge = llm.Judge([cfg])
    p = paper()
    assert judge.needs_research(p, {"new_qtl_data": "yes", "data_sources": []})
    p.full_text = True
    assert not judge.needs_research(p, {"new_qtl_data": "yes", "data_sources": [{"index": 1}]})
    assert judge.needs_research(p, {"new_qtl_data": "yes", "data_sources": []})
    assert not judge.needs_research(p, {"new_qtl_data": "no"})
    judge.config.web_search = False
    assert not judge.needs_research(p, {"new_qtl_data": "yes", "data_sources": []})


def test_research_keeps_only_existing_urls() -> None:
    import asyncio
    import json

    reply = {
        "full_text_found": True,
        "answers": {
            "mapped_qtls": {"answer": "yes", "quote": "We mapped cis-eQTLs in 196 donors."},
            "reuse_only": {"answer": "no", "quote": "We mapped cis-eQTLs in 196 donors."},
            "results_released": {"answer": "yes", "quote": "Summary statistics are on Zenodo."},
        },
        "download_status": "open",
        "data_sources": [
            {
                "url": "https://zenodo.org/records/1",
                "content": "qtl_summary_statistics",
                "found_on": "nature",
            },
            {"url": "https://zenodo.org/records/999", "content": "qtl_summary_statistics"},
            {
                "url": "https://example.org/sumstats.tsv.gz",
                "content": "qtl_summary_statistics",
                "quote": "Sumstats",
            },
        ],
    }
    data = {
        "model": "gpt-x",
        "output": [
            {
                "type": "web_search_call",
                "action": {"type": "search", "query": "paper data availability"},
            },
            {
                "type": "web_search_call",
                "action": {"type": "open_page", "url": "https://nature.com/a"},
            },
            {"type": "message", "content": [{"text": json.dumps(reply)}]},
        ],
    }
    cfg = llm.LLMConfig("proxy", "http://p/v1", "k", ["m"], 1, web_search=True)
    judge = llm.Judge([cfg])
    status = {"https://zenodo.org/records/1": "ok", "https://zenodo.org/records/999": "missing"}

    async def fake_verify(url: str) -> str:
        return status.get(url, "blocked")

    judge.verify_url = fake_verify  # type: ignore[method-assign]
    p = paper()

    async def run() -> dict[str, object]:
        out = await judge._apply_research(p, data, cfg, "m")
        await judge.aclose()
        return out

    out = asyncio.run(run())
    assert out["web"]["dropped"] == ["https://zenodo.org/records/999"]  # type: ignore[index]
    assert out["web"]["queries"] == ["paper data availability"]  # type: ignore[index]
    assert out["model"] == "proxy · gpt-x + web search"
    assert (out["new_qtl_data"], out["confidence"]) == ("yes", 1.0)  # all five criteria met
    picked = [s for s in p.sources if s.ai_pick]
    assert [s.url for s in picked] == [
        "https://example.org/sumstats.tsv.gz",
        "https://zenodo.org/records/1",
    ]
    assert picked[0].kind == "web" and "(link blocked)" in picked[0].ai_note
    assert picked[1].kind == "repository"  # an existing candidate is marked, not duplicated
    assert len(p.sources) == 3


def test_dotenv_is_reread_and_shell_values_win(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    env = tmp_path / ".env"
    monkeypatch.setattr(llm, "_from_dotenv", {})
    monkeypatch.delenv("DQ_TEST_KEY", raising=False)
    monkeypatch.setenv("DQ_SHELL", "from-shell")

    env.write_text("DQ_TEST_KEY=sk-old\nDQ_SHELL=from-file\n")
    llm._load_dotenv(env)
    assert os.environ["DQ_TEST_KEY"] == "sk-old"
    assert os.environ["DQ_SHELL"] == "from-shell"  # the shell wins over .env

    env.write_text("DQ_TEST_KEY=sk-new\n")  # key swapped, no restart
    llm._load_dotenv(env)
    assert os.environ["DQ_TEST_KEY"] == "sk-new"

    env.write_text("# key removed\n")
    llm._load_dotenv(env)
    assert "DQ_TEST_KEY" not in os.environ
    assert os.environ["DQ_SHELL"] == "from-shell"


def test_mask_key_never_shows_the_secret() -> None:
    key = "sk-Z0EZVWsgbdP1WWLsBYliexiRcLL45nXm92CTyZT6gZVYwMZq"
    assert llm.mask_key(key) == "sk-Z0E…wMZq"
    assert llm.mask_key("") == ""
    assert llm.key_id(key) != llm.key_id(key + "x") and len(llm.key_id(key)) == 10
