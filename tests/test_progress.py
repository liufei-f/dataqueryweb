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
    assert s["state"] == "interrupted" and s["phase"] == {"num": "5", "name": "Process to the target format"}
    assert s["last"]["tool"] == "run_python" and not s["last"]["done"] and s["last"]["output"] == "file 2 done"
    assert s["last"]["why"] == "assessed: needs chrom split"
    assert s["problem"] is None  # the 403 was followed by steps that went through
    assert "resumes from Phase 5" in s["next"] and "had not finished" in s["next"]
    assert [p["num"] for p in s["phases_reached"]] == ["2", "4", "5"]


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
