"""progress.py: where the latest build is or stopped, from its Markdown log."""

from __future__ import annotations

import json
from pathlib import Path

from dataqueryweb import progress


def log(root: Path, rid: int, run_id: str, events: list[tuple[str, object]]) -> None:
    d = root / ".build_logs" / str(rid)
    d.mkdir(parents=True, exist_ok=True)
    text = "".join(f"\n## 2026-11-04T0{i // 10}:{i % 10}0:00+00:00 — {e}\n\n```text\n"
                   f"{v if isinstance(v, str) else json.dumps(v)}\n```\n" for i, (e, v) in enumerate(events))
    (d / f"{run_id}.md").write_text(text)


START = [("Build started", {"record_id": 1}), ("AI response", {"usage": {"total_tokens": 500}})]


def test_a_log_that_just_ends_is_an_interrupted_build_in_its_phase(tmp_path: Path) -> None:
    log(tmp_path, 1, "20261104T000000Z-a", [*START,
        ("Tool call: open_url", {"url": "https://x", "why": "the paper names a portal"}),
        ("Tool result: open_url", {"error": "HTTP 403"}),
        ("Tool call: download", {"urls": ["https://y"], "why": "the listing shows the nominal tables"}),
        ("Tool result: download", {"files": []}),
        ("Tool call: run_python", {"purpose": "normalize eQTL tables", "why": "assessed: needs chrom split"}),
        ("Python stdout / stderr", "file 1 done\nfile 2 done"),
    ])
    s = progress.summary(tmp_path, 1)
    assert s["state"] == "interrupted" and s["phase"] == {"num": "5", "name": "Process"}
    assert s["last"]["tool"] == "run_python" and not s["last"]["done"] and s["last"]["output"] == "file 2 done"
    assert s["last"]["why"] == "assessed: needs chrom split"
    assert s["problem"] is None  # the 403 was followed by steps that went through
    assert "resumes from step 5" in s["next"] and "had not finished" in s["next"]
    assert [p["num"] for p in s["phases_reached"]] == ["1", "2", "5"]


def test_finished_stopped_and_a_last_refusal(tmp_path: Path) -> None:
    log(tmp_path, 2, "20261104T000000Z-b", [*START,
        ("Tool call: validate", {"file_path": "a_processed.tsv.gz", "why": "processing finished for every file"}),
        ("Tool result: validate", {"refused": "validate waits until every downloaded file is checked"}),
    ])
    s = progress.summary(tmp_path, 2, {"outcome": "stopped", "note": "stopped by a person", "at": "2026-11-04T09:00:00+00:00"})
    assert s["state"] == "stopped" and s["problem"]["outcome"] == "refused" and "waits until" in s["problem"]["why"]
    log(tmp_path, 3, "20261104T000000Z-c", [*START,
        ("Tool call: submit", {"rows": [], "why": "every table checked"}), ("Tool result: submit", {"accepted": True}),
        ("Build investigation finished", {"rows": 0})])
    assert progress.summary(tmp_path, 3)["state"] == "finished"
    assert progress.summary(tmp_path, 4) is None


def test_the_stop_record_says_where_and_why_a_run_ended(tmp_path: Path) -> None:
    log(tmp_path, 5, "20261105T000000Z-e", [*START,
        ("Tool call: run_python", {"why": "convert the tables", "code": "x"}),
        ("Tool result: run_python", {"exit_code": 0}),
        ("Build investigation finished", {"note": "the AI did not submit within the step budget"}),
        ("Build stopped", {"outcome": "no_submission", "step": "5", "step_name": "Process", "last_tool": "run_python",
                           "reason": "the AI did not submit within the step budget"})])
    s = progress.summary(tmp_path, 5)
    assert s["state"] == "failed" and s["phase"] == {"num": "5", "name": "Process"}
    assert s["stop_reason"] == "the AI did not submit within the step budget"
    assert s["stop_outcome"] == "Ended without a submission"


def test_a_log_cut_by_a_restart_gets_its_stop_record_when_the_server_starts(tmp_path: Path) -> None:
    log(tmp_path, 6, "20261105T000000Z-f", [*START,
        ("Tool call: add_rsid", {"why": "fill the rs ids", "file_path": "a_processed.tsv.gz"})])
    log(tmp_path, 7, "20261105T000000Z-g", [*START, ("Build stopped", {"outcome": "submitted", "step": "6"})])
    marked = progress.mark_interrupted(tmp_path)
    assert len(marked) == 1 and marked[0].endswith("20261105T000000Z-f.md")
    s = progress.summary(tmp_path, 6)
    assert s["state"] == "interrupted" and s["phase"]["num"] == "5" and "add_rsid was running" in s["stop_reason"]
    assert progress.mark_interrupted(tmp_path) == []  # written once


def test_a_run_whose_server_process_is_alive_is_not_marked_interrupted(tmp_path: Path) -> None:
    import subprocess
    import sys

    log(tmp_path, 8, "20261105T000000Z-h", [*START, ("Tool call: download", {"why": "get the tables"})])
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])  # another live server
    try:
        (tmp_path / ".build_logs" / "8" / "20261105T000000Z-h.pid").write_text(str(other.pid))
        assert progress.mark_interrupted(tmp_path) == []  # still being written by a live process
    finally:
        other.kill()
        other.wait()
    assert len(progress.mark_interrupted(tmp_path)) == 1  # its process is gone: interrupted
    assert not (tmp_path / ".build_logs" / "8" / "20261105T000000Z-h.pid").exists()


def test_a_stop_record_with_backticks_keeps_the_log_readable(tmp_path: Path) -> None:
    log(tmp_path, 9, "20261105T000000Z-i", [*START, ("Tool call: read_build_history", {"why": "x" * 30}),
                                            ("Tool result: read_build_history", {"text": "```text\nold log\n```"})])
    progress.mark_interrupted(tmp_path)
    s = progress.summary(tmp_path, 9)
    assert s["state"] == "interrupted" and s["stop"]["outcome"] == "interrupted"


def test_the_current_step_of_a_running_paper_is_one_answer_for_row_and_details(tmp_path: Path) -> None:
    from dataqueryweb import details

    # the previous run ended at step 6
    log(tmp_path, 10, "20261105T000000Z-j", [*START, ("Tool call: validate", {"why": "check the tables"}),
                                             ("Build stopped", {"outcome": "stopped", "step": "6"})])
    since = "2026-11-05T09:00:00+00:00"  # the queue started the paper again, after that log
    for text, want in [("reading the paper", "1"), ("rebuild: reading 49 processed file(s) to the end", "1"),
                       ("AI: assess_file {...}", "4"), ("running the AI's Python (convert): {'file'", "5"),
                       ("downloading file 1 of 3 (content): 1.09 of 3.88 GB", "2"),
                       ("AI working (step 3, 1,000 tokens so far)", "1")]:  # no log of this run yet: not step 6
        assert progress.current_step(tmp_path, 10, text, since)["num"] == want, text
    steps = details.steps(progress.summary(tmp_path, 10), "AI: assess_file {...}",
                          progress.current_step(tmp_path, 10, "AI: assess_file {...}", since))
    assert [s["status"] for s in steps] == ["complete", "complete", "complete", "running", "pending", "pending"]


def test_stop_record_followed_by_manifest_saved_note_is_still_read():
    text = ("\n## 2026-11-05T09:19:46+00:00 — Build stopped\n\n```text\n"
            '{\n  "outcome": "submitted",\n  "step": "6"\n}\n```\n\n'
            "## Manifest saved\n\nRecord: 637; manifest rows: 38; tokens: 1.\n")
    (ev,) = progress.events(text)
    assert ev[1] == "Build stopped"
    assert json.loads(ev[2])["outcome"] == "submitted"
