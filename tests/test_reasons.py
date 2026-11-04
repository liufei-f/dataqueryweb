"""reasons.py: no step without a reason, no judgement without evidence, and both in the log."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

from dataqueryweb import agent, reasons
from dataqueryweb import manifest_rules as rules
from test_agent import PAPER, RECORD, Script


def session(root: Path) -> agent.Session:
    s = agent.Session(RECORD, PAPER, root, 1e9, rules.Target(level_1=["Brain"]), threading.Event(), lambda _: None)
    s.audit = agent.BuildAudit(root, RECORD)
    s.see("paper", "We genotyped 412 donors of European ancestry and mapped cis-eQTL.")
    return s


def test_every_tool_requires_why_and_judgements_require_evidence() -> None:
    tools = {t["name"]: t["parameters"] for t in reasons.with_reasons(agent.TOOLS)}
    assert all("why" in p["required"] for p in tools.values())
    assert {n for n, p in tools.items() if "evidence" in p["required"]} == set(reasons.JUDGEMENTS)
    assert all("why" not in t["parameters"].get("properties", {}) for t in agent.TOOLS)  # originals untouched


def test_calls_without_a_concrete_reason_are_refused(tmp_path: Path) -> None:
    s = session(tmp_path)
    for why in ("", "next", "continue", "short reason"):
        _, refusal, _ = reasons.check(s, "read_paper", {"query": "donors", "why": why})
        assert refusal and "needs `why`" in refusal["refused"], why
    args, refusal, rec = reasons.check(s, "read_paper", {"query": "donors", "why": "the abstract does not give the sample size; look in Methods"})
    assert refusal is None and args == {"query": "donors"} and rec["why"].startswith("the abstract")


def test_judgements_need_quotes_from_what_the_model_was_shown(tmp_path: Path) -> None:
    s = session(tmp_path)
    why = "the Methods paragraph says what was mapped, so the location holds no data"
    _, refusal, _ = reasons.check(s, "give_up", {"reason": "x", "why": why})
    assert "needs `evidence`" in refusal["refused"] and "none was given" in refusal["refused"]
    made_up = [{"quote": "the authors state that no summary statistics will ever be shared", "source": "paper"}]
    _, refusal, rec = reasons.check(s, "give_up", {"reason": "x", "why": why, "evidence": made_up})
    assert "none of the quotes was found" in refusal["refused"] and rec["evidence"][0]["verified"] is False
    real = [{"quote": "We genotyped 412 donors of European ancestry", "source": "paper Methods"}]
    _, refusal, rec = reasons.check(s, "give_up", {"reason": "x", "why": why, "evidence": real})
    assert refusal is None and rec["evidence"][0]["verified"] and rec["evidence"][0]["found_in"] == "paper"


def test_each_step_is_logged_with_its_reason_evidence_and_result(tmp_path: Path) -> None:
    judge = Script([
        [("read_paper", {"query": "donors", "why": "go"})],  # refused: no concrete reason
        [("read_paper", {"query": "donors"})],
        [("give_up", {"reason": "nothing public"})],  # refused: no evidence
        [("give_up", {"reason": "nothing public",
                      "evidence": [{"quote": "We genotyped 412 donors of European ancestry", "source": "paper"}]})],
    ])
    usage = {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
    out = asyncio.run(agent.run(RECORD, judge, root=tmp_path, max_bytes=1e9, target=rules.Target(level_1=["Brain"]),
                                paper=PAPER, step=lambda _: None, cancel=threading.Event(), usage=usage))
    assert out is not None
    ledger = next((tmp_path / ".build_logs" / str(RECORD["record_id"])).glob("*.decisions.jsonl"))
    rows = [json.loads(x) for x in ledger.read_text().splitlines()]
    assert [(r["tool"], r["outcome"]) for r in rows] == [("read_paper", "refused"), ("read_paper", "ok"),
                                                        ("give_up", "refused"), ("give_up", "ok")]
    assert rows[1]["why"].startswith("scripted test step") and rows[3]["evidence"][0]["verified"]
    log = ledger.with_suffix("").with_suffix(".md").read_text()
    assert "Decision: give_up (ok)" in log and "Decision: read_paper (refused)" in log
