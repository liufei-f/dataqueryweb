"""Every step of the model carries its reason, and every judgement its evidence — checked, then logged.

* **why** — every tool call states why this step is taken now and what it expects to learn or
  change, based on what was already seen. A call without one (or with a filler phrase) is
  refused and not executed.
* **evidence** — judgements (what a file holds, a location's status, giving up) also cite text
  copied word for word from what the model was shown (paper, pages, file listings, file heads,
  tool results). Each quote is checked against those texts; a judgement with no quote found is
  refused. The model cannot assert from nothing.
* **ledger** — every call, accepted or refused, is written to the build log ("Decision") and to
  ``.build_logs/<record>/<run>.decisions.jsonl``: step, tool, why, evidence (verified or not),
  outcome and a short result. The program's own decisions (refusals, rebuild deletions) are
  recorded with their reasons in the same places.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from dataqueryweb import manifest_rules as rules

if TYPE_CHECKING:
    from dataqueryweb.agent import Session

# Judgements: the model states a conclusion about the data, so it must show what it is based on.
JUDGEMENTS = ("assess_file", "note_location", "give_up")
MIN_WHY = 25  # characters
FILLER = re.compile(r"(?i)^\W*(next( step)?|continue|proceed(ing)?|as planned|n/?a|none|ok|done|because|to do it|"
                    r"needed|necessary|required|check|try|step \d+)\W*$")

WHY = {"type": "string", "description": "Why this step, now: what you already saw that leads here (name the "
       "tool result, file or paper text) and what you expect to learn or change. Required; a call without a "
       "concrete reason is refused."}
EVIDENCE = {"type": "array", "description": "What this judgement is based on: text copied word for word from a "
            "tool result, the paper, a page or a file head you were shown (at least 12 letters/digits each). "
            "Each quote is checked; a judgement without a verified quote is refused.",
            "items": {"type": "object", "properties": {
                "quote": {"type": "string"}, "source": {"type": "string", "description": "where you saw it"}},
                "required": ["quote", "source"]}}


def with_reasons(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The tool definitions with ``why`` (all) and ``evidence`` (judgements) required."""
    out = copy.deepcopy(tools)
    for t in out:
        params = t.setdefault("parameters", {"type": "object", "properties": {}})
        params.setdefault("properties", {})["why"] = WHY
        req = params.setdefault("required", [])
        req.append("why")
        if t["name"] in JUDGEMENTS:
            params["properties"]["evidence"] = EVIDENCE
            req.append("evidence")
    return out


def proof(s: Session) -> dict[str, str]:
    """Everything the model was shown, by name: what a quote may come from."""
    files = "\n".join([*s.validated, *(d["path"] for d in s.downloads), *s.source_files()])
    # Output of the model's own run_python code is not evidence: the code could print anything.
    shown = {f"tool result #{i + 1}": e.get("result_full", "") for i, e in enumerate(s.log)
             if e.get("tool") != "run_python"}
    return s.texts | shown | {"file paths": files, "rebuild check": s.rebuild_report}


def check(s: Session, name: str, args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any]]:
    """(args without why/evidence, a refusal or None, the reasoning record)."""
    args = dict(args)
    why = str(args.pop("why", "") or "").strip()
    evidence = args.pop("evidence", None)
    record: dict[str, Any] = {"why": why}
    if len(why) < MIN_WHY or FILLER.match(why):
        return args, {"refused": f"{name} needs `why`: a concrete reason for this step (what you saw that leads "
                                 f"here and what you expect), at least {MIN_WHY} characters; nothing was run"}, record
    if name in JUDGEMENTS:
        items = evidence if isinstance(evidence, list) else []
        shown = proof(s)
        checked = []
        for e in items[:10]:
            quote = str((e or {}).get("quote") or "") if isinstance(e, dict) else str(e)
            where = rules.verify_quote(quote, shown)
            checked.append({"quote": quote[:500], "source": str((e or {}).get("source") or "")[:200]
                            if isinstance(e, dict) else "", "verified": bool(where), "found_in": where})
        record["evidence"] = checked
        if not any(c["verified"] for c in checked):
            return args, {"refused": f"{name} is a judgement and needs `evidence`: text copied word for word from "
                                     "a tool result, page, paper or file head you were shown (12+ letters/digits); "
                                     + ("none of the quotes was found in what you were shown" if checked
                                        else "none was given") + "; nothing was recorded"}, record
    return args, None, record


def outcome(out: dict[str, Any]) -> str:
    if not isinstance(out, dict):
        return "ok"
    if "refused" in out:
        return "refused"
    if "error" in out:
        return "error"
    if out.get("accepted") is False:
        return "rejected"
    return "ok"


def ledger(s: Session, step: int, name: str, args: dict[str, Any], record: dict[str, Any], out: Any,
           by: str = "model") -> None:
    """One line per decision in the build log and the decisions ledger."""
    entry = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "step": step, "by": by, "tool": name,
             "why": record.get("why", ""), "evidence": record.get("evidence", []),
             "args": {k: (v if len(json.dumps(v, default=str)) < 400 else json.dumps(v, default=str)[:400] + "…")
                      for k, v in args.items()},
             "outcome": outcome(out), "result": json.dumps(out, ensure_ascii=False, default=str)[:600]}
    if s.audit is None:
        return
    s.audit.write(f"Decision: {name} ({entry['outcome']})", entry)
    path = s.audit.path.with_suffix(".decisions.jsonl")
    with s.audit.lock, path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def program_decision(s: Session, what: str, reason: str, result: Any) -> None:
    """A decision the program made on its own (e.g. deleting a broken processed file), with its reason."""
    ledger(s, 0, what, {}, {"why": reason}, result, by="program")
