"""What the Details panel of one paper shows: the six steps and every file, with status.

Computed on demand (``GET /api/manifest/{record_id}/details``) from what is on disk:

* **steps** — the skill's six steps, each ``running`` / ``pending`` / ``complete`` / ``failed``.
  They come from the latest build log (``progress.parse``): the step of the last tool call, the
  run's stop record (where and why it ended) and whether the queue is running the paper now.
  A failed step carries the reason and a suggested solution.
* **files** — every file of the paper: downloaded sources (with the model's assessment), the
  processed tables made from them (written → liftover → rs ids → validate → in the manifest),
  and files that were meant to be downloaded but are not on disk (download errors, skipped
  tables). Each says how far it got and where it failed.
"""

from __future__ import annotations

import fnmatch
import glob
import gzip
import json
import re
from pathlib import Path
from typing import Any

from dataqueryweb import fetch, progress

# What each step does, in one line (the skill's six steps).
STEP_TEXT = {
    "1": "Which data locations exist, which tables are needed, whether they can be downloaded, and "
         "what is already in the paper's folder.",
    "2": "Download what is needed and not yet on disk.",
    "3": "Find the facts the rows need in the paper: QTL type, context, sample size, ancestry, genome build.",
    "4": "Inspect every downloaded file: its format, whether it can be converted, and the plan.",
    "5": "Convert to the standard format and run the fixed programs (positions, alleles, liftover, rs ids).",
    "6": "Validate every processed file and write the manifest rows.",
}
PROCESSED_RE = re.compile(r"_processed\.tsv(\.gz)?$")
STEM_RE = re.compile(r"(\.(tsv|txt|csv|tab|bed|parquet|xlsx|tar|zip|tgz))?(\.(gz|bgz|bz2|xz))?$", re.I)


# ── the six steps ────────────────────────────────────────────────────────────


def solution(outcome: str, reason: str, step: str) -> str:
    """What to do about a failed step, from how the run ended and why."""
    r = reason.lower()
    if outcome == "interrupted":
        return ("The server or machine stopped during this step. Click Reprocess: the files already on disk are "
                "checked first (a half-written one is deleted and redone) and the run resumes from this step.")
    if outcome == "stopped":
        return "Someone stopped the run. Click Reprocess to resume; finished files are reused."
    if outcome == "usage_limit":
        return ("The AI's usage limit was reached. The queue checks every hour and resumes this paper by itself; "
                "otherwise click Reprocess once the limit has reset.")
    if outcome == "llm_failed":
        if re.search(r"connect|timeout|unreachable|refused|network", r):
            return ("The LLM service could not be reached. Check the LLM endpoint (LLM_BASE_URL in .env) and that "
                    "the service is up, then click Reprocess. The rows saved by the last run are kept.")
        return "The LLM call failed. Check the LLM settings (/api/llm/check), then click Reprocess."
    if outcome == "no_submission":
        if "step budget" in r:
            return ("The AI used its whole step budget without submitting. Look at the last step and its problem "
                    "below; give guidance in the conversation (or raise MANIFEST_AGENT_STEPS for papers with many "
                    "files) and click Continue processing.")
        return ("The AI stopped without submitting. Look at the last step and its problem below, give "
                "instructions in the conversation and click Continue processing.")
    if outcome == "error":
        return ("A program error stopped the run (the reason is the error). Open the full processing log, report "
                "it for a fix, then click Reprocess.")
    if outcome == "gave_up":
        return ("No usable table was found. Check the paper's data availability. When the data need an "
                "application, a login or a browser download, do that and paste the access instructions or the "
                "server file paths into the conversation, then click Continue processing.")
    if outcome == "nothing_converted":
        return ("No table could be converted to the standard format: each file's reason is in the file list. When "
                "a companion file exists that the AI did not use (a variant map, peak coordinates, a README "
                "describing the columns), point to it in the conversation and click Continue processing; "
                "otherwise the data cannot be loaded as they are.")
    return "Open the full processing log for details, then click Reprocess."


def steps(facts: dict[str, Any] | None, running: str | None = None,
          phase: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """The six steps with their status. ``running`` is the queue's current step text when it runs the
    paper, and ``phase`` the step it is in (progress.current_step, the same as the paper's row shows)."""
    names = [(n, name) for n, name, _ in progress.PHASES]
    out = [{"num": n, "name": name, "text": STEP_TEXT.get(n, ""), "status": "pending"} for n, name in names]
    if running is not None:
        cur = (phase or {}).get("num") or "1"
        for s in out:
            s["status"] = "complete" if s["num"] < cur else "running" if s["num"] == cur else "pending"
            if s["status"] == "running":
                s["now"] = running
        return out
    if not facts:
        return out  # never processed: every step pending
    stop = facts.get("stop") or {}
    outcome = stop.get("outcome") or {"finished": "submitted", "interrupted": "interrupted", "stopped": "stopped",
                                      "failed": "error", "usage_limit": "usage_limit"}.get(facts.get("state", ""), "")
    reason = facts.get("stop_reason") or stop.get("reason") or ""
    cur = (facts.get("phase") or {}).get("num") or "1"
    reached = {p["num"] for p in facts.get("phases_reached") or []}
    failed_at = None
    if outcome == "gave_up":
        failed_at = "1"
    elif outcome == "submitted" and re.match(r"submitted 0 row", reason):
        outcome, failed_at = "nothing_converted", ("4" if "4" in reached else "1")
    elif outcome != "submitted":
        failed_at = cur
    for s in out:
        if failed_at is None or s["num"] < failed_at:
            s["status"] = "complete"
            if s["num"] not in reached and failed_at is None and s["num"] != "6":
                s["note"] = "not needed in this run"
        elif s["num"] == failed_at:
            s["status"] = "failed"
            s["outcome"] = facts.get("stop_outcome") or outcome
            s["reason"] = reason or "not recorded"
            s["solution"] = solution(outcome, reason, failed_at)
            if facts.get("problem"):
                s["problem"] = facts["problem"]
            if facts.get("last"):
                s["last"] = {k: facts["last"].get(k) for k in ("tool", "why", "done", "output")}
    if failed_at is None and reason:
        out[-1]["note"] = reason[:300]
    return out


# ── the files ────────────────────────────────────────────────────────────────


def _stem(name: str) -> str:
    return STEM_RE.sub("", name)


def _tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[._\-+]+", _stem(name).lower()) if len(t) > 2}


def _header(p: Path) -> list[str]:
    try:
        opener = gzip.open if p.name.endswith(".gz") else open
        with opener(p, "rt", encoding="utf-8", errors="replace") as fh:
            return fh.readline().rstrip("\n").split("\t")
    except (OSError, EOFError):
        return []


def _log_tool_results(path: Path | None) -> list[tuple[str, dict[str, Any], Any]]:
    """(tool, arguments, result) of every tool call in a build log, in order."""
    if path is None or not path.is_file():
        return []
    calls: list[tuple[str, dict[str, Any], Any]] = []
    pending: tuple[str, dict[str, Any]] | None = None
    for _, event, body in progress.events(path.read_text(encoding="utf-8", errors="replace")):
        if event.startswith("Tool call: "):
            try:
                pending = (event.removeprefix("Tool call: "), json.loads(body))
            except ValueError:
                pending = (event.removeprefix("Tool call: "), {})
        elif event.startswith("Tool result: ") and pending:
            try:
                result = json.loads(body)
            except ValueError:
                result = body
            calls.append((pending[0], pending[1], result))
            pending = None
    return calls


def files(root: Path, record: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Every file of the paper, with how far it got."""
    rid = record["record_id"]
    folder_name = fetch.record_folder(record)
    folder = root / folder_name
    try:
        index = json.loads((root / ".downloads.json").read_text())
    except (OSError, ValueError):
        index = {}
    url_of = {v["path"]: k for k, v in index.items()
              if isinstance(v, dict) and v.get("path") and (v.get("record_id") == rid or v["path"].startswith(folder_name + "/"))}
    try:
        assessed = json.loads((root / ".build_logs" / str(rid) / "assessments.json").read_text())
    except (OSError, ValueError):
        assessed = {}
    try:
        checks = json.loads((folder / ".processed_checks.json").read_text())
    except (OSError, ValueError):
        checks = {}

    on_disk = sorted(p for p in folder.rglob("*") if p.is_file() and not p.name.startswith(".")) if folder.is_dir() else []
    processed = [p for p in on_disk if PROCESSED_RE.search(p.name)]
    noise = re.compile(r"\.(tmp|sqlite|json|failure|tbi|csi)$|\.alleles\.sqlite", re.I)
    sources = [p for p in on_disk if p not in processed and not noise.search(p.name)]

    logs = sorted((root / ".build_logs" / str(rid)).glob("*.md"))
    calls = [c for log in logs[-3:] for c in _log_tool_results(log)]  # the latest runs, oldest first
    validated: dict[str, dict[str, Any]] = {}
    download_errors: list[dict[str, Any]] = []
    skipped: dict[str, str] = {}
    for tool, args, result in calls:
        if tool == "validate" and isinstance(result, dict):
            for f in glob.glob(str(root / fetch.safe_rel(str(args.get("file_path") or "")))):
                validated[str(Path(f).resolve())] = result
        elif tool == "download" and isinstance(result, dict):
            download_errors += [f for f in result.get("files") or [] if isinstance(f, dict) and f.get("error")]
        elif tool == "submit" and isinstance(args, dict):
            for t in args.get("skipped_tables") or []:
                if isinstance(t, dict) and t.get("path"):
                    skipped[str(t["path"])] = str(t.get("reason") or "")
    in_manifest: dict[str, dict[str, Any]] = {}
    for r in rows:
        for f in glob.glob(str(root / fetch.safe_rel(r.get("file_path") or ""))):
            in_manifest[str(Path(f).resolve())] = r

    def skipped_reason(p: Path) -> str:
        rel = str(p.relative_to(root))
        for pattern, why in skipped.items():
            if rel == pattern or p.name == Path(pattern).name or fnmatch.fnmatch(rel, pattern):
                return why
        return ""

    def processed_entry(p: Path) -> dict[str, Any]:
        c = checks.get(p.name) or {}
        stamp_ok = c.get("size") == p.stat().st_size
        st: list[dict[str, Any]] = [{"name": "written", "status": "complete"}]
        if stamp_ok and c.get("intact") is False:
            st[0] = {"name": "written", "status": "failed", "reason": c.get("error") or "truncated: an interrupted write"}
        header = _header(p)
        if "chrom_grch37" in header:
            st.append({"name": "liftover", "status": "complete"})
        if stamp_ok and c.get("rsid_complete"):
            st.append({"name": "rs ids", "status": "complete"})
        else:
            st.append({"name": "rs ids", "status": "pending",
                       "reason": "" if "rsid" in header else "no rsid column yet"})
        v = validated.get(str(p.resolve()))
        if v is None:
            st.append({"name": "validate", "status": "pending"})
        elif v.get("ok"):
            st.append({"name": "validate", "status": "complete"})
        else:
            why = v.get("refused") or "; ".join(v.get("problems") or [])
            st.append({"name": "validate", "status": "failed", "reason": why[:400],
                       "attempt": v.get("attempt", "")})
        row = in_manifest.get(str(p.resolve()))
        reason = skipped_reason(p)
        if row:
            st.append({"name": "in manifest", "status": "complete",
                       "reason": f"row {row.get('qtl_type', '')} {row.get('biocontext', '')}: {row.get('status', '')}"
                                 f"{', ' + row['review'] if row.get('review') not in (None, '', 'pending') else ''}"})
        elif reason:
            st.append({"name": "in manifest", "status": "failed", "reason": f"skipped: {reason}"[:400]})
        else:
            st.append({"name": "in manifest", "status": "pending"})
        return {"name": p.name, "path": str(p.relative_to(root)), "size": p.stat().st_size, "stages": st,
                **_summary(st)}

    proc_entries = {p: processed_entry(p) for p in processed}
    owner: dict[Path, Path | None] = {}
    for q in processed:  # which source a processed table was made from
        base = PROCESSED_RE.sub("", q.name)
        exact = [s for s in sources if _stem(s.name) == base]
        if exact:
            owner[q] = exact[0]
            continue
        scores = [(len(_tokens(s.name) & _tokens(base)), s) for s in sources]
        best = max((n for n, _ in scores), default=0)
        tops = [s for n, s in scores if n == best and best > 0]
        owner[q] = tops[0] if len(tops) == 1 else None

    out = []
    for s in sources:
        rel = str(s.relative_to(root))
        a = (assessed.get(rel) or {}).get("assessment")
        made = [proc_entries[q] for q in processed if owner.get(q) == s]
        st = [{"name": "downloaded", "status": "complete"}]
        if a:
            ok = a.get("processable") in ("ready", "yes", "companion")
            # A README, a supplement or a map judged not processable is expected, not a failure; only a
            # QTL table that cannot be converted failed here.
            qtl = a.get("contents") in ("qtl_nominal_all", "qtl_significant_only", "qtl_lead_only")
            st.append({"name": "assessed", "status": "complete" if ok else "failed" if qtl else "skipped",
                       "reason": f"{a.get('contents')}, processable {a.get('processable')}"
                                 + (f": {a.get('reason')}" if a.get("reason") else "")})
        else:
            st.append({"name": "assessed", "status": "pending"})
        if a and a.get("processable") == "companion":
            st.append({"name": "processed", "status": "complete", "reason": "a variant map: read by fill_alleles / "
                                                                            "fill_positions, not processed itself"})
        elif a and a.get("processable") == "ready":
            st.append({"name": "processed", "status": "complete", "reason": "already in the standard format"})
        elif made:
            bad = [m for m in made if m.get("failed")]
            st.append({"name": "processed", "status": "failed" if bad and len(bad) == len(made) else "complete",
                       "reason": f"{len(made)} processed table(s)"
                                 + (f", {len(bad)} failed" if bad else "")})
        elif a and a.get("processable") == "no":
            st.append({"name": "processed", "status": "skipped",
                       "reason": "not processable" if a.get("contents") in ("qtl_nominal_all", "qtl_significant_only",
                                                                            "qtl_lead_only") else "not a QTL table: not needed"})
        else:
            st.append({"name": "processed", "status": "pending"})
        out.append({"name": s.name, "path": rel, "url": url_of.get(rel, ""), "size": s.stat().st_size,
                    "downloaded": True, "assessment": a or None, "stages": st, "processed": made, **_summary(st)})
    orphans = [proc_entries[q] for q in processed if owner.get(q) is None]
    for e in download_errors:
        out.append({"name": Path(str(e.get("url") or "")).name or str(e.get("url")), "path": "", "url": e.get("url", ""),
                    "size": None, "downloaded": False, "assessment": None, "processed": [],
                    "stages": [{"name": "downloaded", "status": "failed", "reason": str(e.get("error"))[:300]}],
                    **_summary([{"name": "downloaded", "status": "failed", "reason": str(e.get("error"))[:300]}])})
    for pattern, why in skipped.items():  # tables that were never downloaded (e.g. over the download limit)
        if not any(pattern in (f["path"], f["name"]) or fnmatch.fnmatch(f["path"], pattern) for f in out) \
                and not glob.glob(str(root / fetch.safe_rel(pattern))):
            st = [{"name": "downloaded", "status": "skipped", "reason": why[:300]}]
            out.append({"name": Path(pattern).name or pattern, "path": pattern, "url": "", "size": None,
                        "downloaded": False, "assessment": None, "processed": [], "stages": st, **_summary(st)})
    counts = {"files": len(out), "downloaded": sum(f["downloaded"] for f in out),
              "processed_tables": len(processed),
              "in_manifest": sum(1 for p in proc_entries.values() if p["stages"][-1]["status"] == "complete"),
              "failed": sum(1 for f in out if f.get("failed")) + sum(1 for p in proc_entries.values() if p.get("failed"))}
    return {"folder": folder_name, "files": out, "processed_unmatched": orphans, "counts": counts}


def _summary(stages: list[dict[str, Any]]) -> dict[str, Any]:
    """The furthest stage reached, and the first failure."""
    done = [s["name"] for s in stages if s["status"] == "complete"]
    failed = next((s for s in stages if s["status"] == "failed"), None)
    return {"stage": done[-1] if done else "not started",
            "failed": {"stage": failed["name"], "reason": failed.get("reason", "")} if failed else None}
