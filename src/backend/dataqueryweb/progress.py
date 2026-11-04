"""Where a paper's latest build is, or where it stopped — read from its Markdown build log.

The log (``data/.build_logs/<record>/<run>.md``) is appended event by event, so it survives a
crash or a server restart. ``summary`` turns the newest log into the few facts a curator needs:

* **state** — running (the queue says so), finished, stopped / failed (the saved outcome), or
  interrupted (the log just ends: the server or machine stopped mid-build);
* **phase** — the skill phase of the last step (Phase 4 Download, Phase 5 Process, …);
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
# The skill's phases, by the tool the model was using.
PHASES = [
    ("0", "Resume / rebuild check", ("read_build_history", "rebuild")),
    ("1", "Read the paper", ("read_paper",)),
    ("2", "Find data locations", ("open_url", "note_location")),
    ("3", "Choose the tables", ("list_files", "peek_file")),
    ("4", "Download", ("download",)),
    ("4b", "Check each downloaded file", ("inspect_local", "assess_file")),
    ("5", "Process to the target format", ("run_python",)),
    ("5a", "Liftover / fill positions / fill alleles", ("liftover", "fill_positions", "fill_alleles")),
    ("5b", "Add rs ids", ("add_rsid",)),
    ("6", "Validate", ("validate",)),
    ("7", "Submit", ("submit", "give_up")),
]
PHASE_OF = {tool: (num, name) for num, name, tools in PHASES for tool in tools}
_cache: dict[Path, tuple[tuple[int, int], dict[str, Any]]] = {}


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
            if lines and lines[-1].strip() == fence:
                lines = lines[:-1]
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
    if facts["finished"]:
        facts["state"] = "failed" if run.get("outcome") == "failed" and saved_after else "finished"
    elif saved_after and run.get("outcome") in ("stopped", "failed", "usage_limit"):
        facts["state"] = run["outcome"]
    else:
        facts["state"] = "interrupted"  # unless the queue says it is running (the page knows)
    last = facts.get("last") or {}
    if facts["state"] == "finished":
        facts["next"] = "Review the rows; Rebuild only to redo it with the current skill."
    elif last:
        unfinished = "" if last.get("done") else " (it had not finished)"
        facts["next"] = (f"Rebuild checks the files on disk first, then resumes from "
                         f"Phase {facts['phase']['num']} {facts['phase']['name']}: {last['tool']}{unfinished}.")
    else:
        facts["next"] = "Rebuild starts from the beginning (no step was taken)."
    facts["n_logs"] = len(logs)
    return facts
