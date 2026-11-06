"""Where a paper's latest build is, or where it stopped — read from its Markdown build log.

The log (``data/.build_logs/<record>/<run>.md``) is appended event by event, so it survives a
crash or a server restart. ``summary`` turns the newest log into the few facts a curator needs:

* **state** — running (the queue says so), finished, stopped / failed (the saved outcome), or
  interrupted (the log just ends: the server or machine stopped mid-build);
* **phase** — the skill step of the last tool call (step 2 Download, step 5 Process, …);
* **last step** — the tool, its reason, whether it finished, and its last output;
* **problem** — the last refused or failed step, with the reason the program gave;
* **next** — what a rebuild does from here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

HEADER = re.compile(r"^## (\S+) — (.+)$", re.M)
# The skill's six steps, by the tool the model was using.
PHASES = [
    ("1", "Sources and files on disk", ("read_build_history", "rebuild", "open_url", "list_files", "peek_file",
                                         "note_location")),
    ("2", "Download", ("download",)),
    ("3", "Facts for the manifest", ("read_paper", "read_table")),
    ("4", "Formats and plan", ("inspect_local", "assess_file")),
    ("5", "Process", ("run_python", "fill_positions", "fill_alleles", "liftover", "add_rsid")),
    ("6", "Check and write the manifest", ("validate", "submit", "give_up")),
]
PHASE_OF = {tool: (num, name) for num, name, tools in PHASES for tool in tools}
_cache: dict[Path, tuple[tuple[int, int], dict[str, Any]]] = {}
STOP_EVENT = "Build stopped"  # written by agent.run at every end, and by mark_interrupted after a restart
# A stop record's outcome → the state the page shows.
STATE_OF = {"submitted": "finished", "gave_up": "finished", "stopped": "stopped", "usage_limit": "usage_limit",
            "interrupted": "interrupted", "no_submission": "failed", "llm_failed": "failed", "error": "failed"}
OUTCOME_TEXT = {"submitted": "Submitted", "gave_up": "Gave up", "stopped": "Stopped by a person",
                "usage_limit": "AI usage limit reached", "interrupted": "Interrupted (server or machine stopped)",
                "no_submission": "Ended without a submission", "llm_failed": "The LLM could not be reached",
                "error": "Program error"}


def events(text: str) -> list[tuple[str, str, str]]:
    """(timestamp, event, body) in order."""
    out = []
    heads = list(HEADER.finditer(text))
    for i, m in enumerate(heads):
        body = text[m.end(): heads[i + 1].start() if i + 1 < len(heads) else len(text)].strip()
        lines = body.splitlines()
        if lines and lines[0].startswith("```"):
            fence = re.match(r"`+", lines[0]).group()
            lines = lines[1:]
            # the body is the fenced block only: text after its closing fence (such as the
            # timestamp-less "## Manifest saved" note) is not part of this event
            end = next((j for j, x in enumerate(lines) if x.strip() == fence), len(lines))
            lines = lines[:end]
        out.append((m.group(1), m.group(2).strip(), "\n".join(lines)))
    return out


def _json(body: str) -> Any:
    try:
        return json.loads(body)
    except ValueError:
        return None


def _outcome(value: Any) -> str:
    if not isinstance(value, dict):
        return "ok"
    if "refused" in value:
        return "refused"
    if "error" in value:
        return "error"
    if value.get("accepted") is False:
        return "rejected"
    return "ok"


def _short(text: Any, n: int = 220) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def parse(path: Path) -> dict[str, Any]:
    """The facts of one build log (cached by size and mtime)."""
    st = path.stat()
    key = (st.st_size, st.st_mtime_ns)
    if (hit := _cache.get(path)) and hit[0] == key:
        return hit[1]
    evs = events(path.read_text(encoding="utf-8", errors="replace"))
    out: dict[str, Any] = {"log": path.name, "run_id": path.stem, "started": evs[0][0] if evs else "",
                           "last_at": evs[-1][0] if evs else "", "finished": False, "steps": 0, "tokens": 0,
                           "phase": None, "last": None, "problem": None, "recent": [], "phases_reached": []}
    open_call: dict[str, Any] | None = None
    reached: list[str] = []
    for at, event, body in evs:
        if event == "Build investigation finished":
            out["finished"] = True
            out["result"] = _short(body, 300)
        elif event == STOP_EVENT:
            out["stop"] = (_json(body) or {}) | {"at": at}
        elif event == "AI response":
            v = _json(body) or {}
            out["steps"] += 1
            out["tokens"] = (v.get("usage") or {}).get("total_tokens", out["tokens"])
        elif event.startswith("Rebuild check"):
            open_call = {"tool": "rebuild", "why": "program check of downloaded and processed files", "at": at,
                         "done": True, "outcome": "error" if "failed" in event else "ok", "output": _short(body)}
            out["last"] = open_call
        elif event.startswith("Tool call: "):
            tool = event.removeprefix("Tool call: ")
            args = _json(body) or {}
            open_call = {"tool": tool, "at": at, "done": False, "outcome": "running",
                         "why": _short(args.get("why") or args.get("purpose") or "", 300),
                         "target": _short(args.get("file_path") or args.get("path") or args.get("url")
                                          or args.get("query") or "", 160),
                         "output": ""}
            if tool == "read_skill":  # reading a module is not a step of its own phase
                continue
            out["last"] = open_call
            num = PHASE_OF.get(tool, ("?", tool))[0]
            if num not in reached:
                reached.append(num)
        elif event.startswith("Tool result: ") and open_call:
            v = _json(body)
            open_call["done"] = True
            open_call["outcome"] = _outcome(v)
            open_call["output"] = _short(json.dumps(v, ensure_ascii=False) if v is not None else body)
            if open_call["outcome"] == "ok":
                out["problem"] = None  # a later step went through: the earlier failure did not stop the build
            elif open_call["outcome"] in ("refused", "error", "rejected"):
                why = v.get("refused") or v.get("error") or "; ".join(v.get("problems") or []) if isinstance(v, dict) else body
                out["problem"] = {"tool": open_call["tool"], "at": at, "outcome": open_call["outcome"], "why": _short(why, 400)}
            out["recent"] = (out["recent"] + [{k: open_call[k] for k in ("tool", "why", "outcome", "at")}])[-8:]
        elif event.startswith("Tool interrupted: ") and open_call:
            open_call["done"] = True
            open_call["outcome"] = "interrupted"
        elif event in ("Python stdout / stderr", "Checkpoint") and open_call and not open_call["done"]:
            if event == "Python stdout / stderr" or not open_call["output"]:
                lines = [x for x in body.splitlines() if x.strip()]
                if lines:
                    open_call["output"] = _short(lines[-1])
    if out["last"]:
        num, name = PHASE_OF.get(out["last"]["tool"], ("?", out["last"]["tool"]))
        out["phase"] = {"num": num, "name": name}
    out["phases_reached"] = [{"num": n, "name": next(x[1] for x in PHASES if x[0] == n)} for n in reached
                             if any(x[0] == n for x in PHASES)]
    _cache[path] = (key, out)
    return out


def summary(root: Path, record_id: int, run: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The latest build of a paper: state, phase, last step, problem and next step."""
    logs = sorted((root / ".build_logs" / str(record_id)).glob("*.md"))
    if not logs:
        return None
    facts = dict(parse(logs[-1]))
    run = run or {}
    saved_after = bool(run.get("at")) and run["at"][:19] >= facts["started"][:19]
    stop = facts.get("stop")
    if stop:  # the run's own record of where and why it ended
        facts["state"] = STATE_OF.get(stop.get("outcome", ""), "failed")
        if facts["state"] == "finished" and run.get("outcome") == "failed" and saved_after:
            facts["state"] = "failed"  # submitted, but storing the rows failed
        if stop.get("step"):
            facts["phase"] = {"num": stop["step"], "name": stop.get("step_name", "")}
        facts["stop_reason"] = stop.get("reason", "")
        facts["stop_outcome"] = OUTCOME_TEXT.get(stop.get("outcome", ""), stop.get("outcome", ""))
        facts["stop_at"] = stop.get("at", "")
    elif facts["finished"]:  # logs from before stop records
        facts["state"] = "failed" if run.get("outcome") == "failed" and saved_after else "finished"
    elif saved_after and run.get("outcome") in ("stopped", "failed", "usage_limit"):
        facts["state"] = run["outcome"]
    else:
        facts["state"] = "interrupted"  # unless the queue says it is running (the page knows)
    if not facts.get("stop_reason"):
        facts["stop_reason"] = (run.get("note", "") if saved_after else "") or (
            "the log just ends: the server or machine stopped mid-run" if facts["state"] == "interrupted" else "")
    last = facts.get("last") or {}
    if facts["state"] == "finished":
        facts["next"] = "Review the rows; Reprocess only to redo it with the current skill."
    elif last and facts.get("phase"):
        unfinished = "" if last.get("done") else " (it had not finished)"
        facts["next"] = (f"Reprocess checks the files on disk first, then resumes from "
                         f"step {facts['phase']['num']} ({facts['phase']['name']}): {last['tool']}{unfinished}.")
    else:
        facts["next"] = "Reprocess starts from the beginning (no step was taken)."
    facts["n_logs"] = len(logs)
    return facts


def mark_interrupted(root: Path) -> list[str]:
    """After a (re)start nothing is running: give every latest log that has no stop record one.

    A run killed by a restart or a machine stop could not write its own end; this writes it from
    the log: the step of the last tool call and that the server or machine stopped. Returns the logs.
    """
    from datetime import UTC, datetime

    marked = []
    for d in sorted((root / ".build_logs").glob("*")):
        logs = sorted(d.glob("*.md")) if d.is_dir() else []
        if not logs:
            continue
        facts = parse(logs[-1])
        if facts.get("stop") or facts.get("finished"):
            continue
        if run_alive(logs[-1]):  # another live server process is still writing this run
            continue
        last = facts.get("last") or {}
        phase = facts.get("phase") or {}
        what = f"{last['tool']} was running" if last and not last.get("done") else (
            f"after {last['tool']}" if last else "before the first step")
        record = {"outcome": "interrupted", "step": phase.get("num", ""), "step_name": phase.get("name", "not started"),
                  "last_tool": last.get("tool", ""), "last_result": last.get("output", ""),
                  "reason": f"the server or machine stopped ({what}); no end was recorded. Last event "
                            f"{facts.get('last_at', '')}; written when the server started again"}
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        text = json.dumps(record, ensure_ascii=False, indent=2)
        fence = "`" * max(3, max((len(m.group()) + 1 for m in re.finditer(r"`+", text)), default=3))
        with logs[-1].open("a", encoding="utf-8") as fh:
            fh.write(f"\n## {stamp} — {STOP_EVENT}\n\n{fence}text\n{text}\n{fence}\n")
        logs[-1].with_suffix(".pid").unlink(missing_ok=True)
        marked.append(str(logs[-1]))
    return marked


def run_alive(log: Path) -> bool:
    """Whether the server process that started this run is still alive (its <run_id>.pid file)."""
    import os

    try:
        pid = int(log.with_suffix(".pid").read_text().strip())
    except (OSError, ValueError):
        return False
    if pid == os.getpid():
        return False  # this very process: a run it had would not have outlived its own restart
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def current_step(root: Path, record_id: int, live_step: str, since: str = "") -> dict[str, str]:
    """The step a paper the queue is processing is in now, from the queue's step text and the run's log.

    One answer for the paper's row and its Details panel. Before the run's log exists ("reading the
    paper", the rebuild check) the latest log is the previous run's, so it is not used.
    """
    text = live_step or ""
    num = ""
    if m := re.match(r"AI: (\w+)", text):
        num = PHASE_OF.get(m.group(1), ("",))[0]
    elif text.startswith("running the AI's Python"):
        num = PHASE_OF["run_python"][0]
    elif text.startswith("downloading file"):
        num = PHASE_OF["download"][0]
    elif text.startswith(("reading the paper", "rebuild:", "starting", "listing the files")) or not text:
        num = "1"
    if not num:  # between tool calls ("AI working (step N …)", "saving"): the run's own last tool call
        logs = sorted((root / ".build_logs" / str(record_id)).glob("*.md"))
        facts = parse(logs[-1]) if logs else {}
        # The log belongs to this run only if it started when the queue started the paper, or later.
        fresh = bool(facts) and (not since or facts.get("started", "")[:19] >= since[:19])
        num = ((facts.get("phase") or {}).get("num") if fresh else "") or "1"
    name = next((n for k, n, _ in PHASES if k == num), "")
    return {"num": num, "name": name}
