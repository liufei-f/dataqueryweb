"""The dataqueryweb FastAPI app: one page plus two JSON endpoints.

``GET /api/search?q=…`` searches Europe PMC and streams one NDJSON line per inspected paper as
soon as it is done, so the page fills in while slow publishers are still being read.
``GET /api/inspect?ref=…`` inspects a single paper given by DOI, URL, PMID or PMCID.
With ``ai=true`` each paper is also judged by a free LLM (llm.py): does it release new QTL
data, and which of the found links downloads it. ``GET /api/llm`` reports the model in use.
``/api/records`` stores the rows a curator confirmed (records.py); ``/records`` lists them.
``/api/manifest`` holds candidate qtl_manifest rows built from saved records (manifest.py),
with the evidence for each field; ``/manifest`` is where a curator reviews and exports them.
With ``autosave=true`` (and ``ai=true``), papers the LLM is very sure about are saved
automatically (``records.autosave``); ``paper.ai.autosave`` says what was saved, or why not.
Papers already in Saved records are not sent to the LLM (``paper.ai.skipped``) unless
``recheck=true``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator, Coroutine
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, TypeVar

import anyio
import httpx
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from dataqueryweb import __version__, checks, fetch, finder, llm, manifest, records, rubric
from dataqueryweb import manifest_rules as rules

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
CONCURRENCY = 5
# Upper bound for one search's paper count (DATAQUERY_MAX_LIMIT); each paper costs time and,
# with the AI check on, LLM tokens.
MAX_SEARCH_LIMIT = int(os.environ.get("DATAQUERY_MAX_LIMIT") or 1000)
JsonBody = Annotated[dict[str, Any], Body()]
T = TypeVar("T")


def autosave_settings() -> dict[str, Any]:
    """AUTO_SAVE_MIN_CONFIDENCE (default 0.9) and AUTO_SAVE_FALLBACK (default false)."""
    llm.config_from_env()  # loads .env
    return {
        "min_confidence": float(os.environ.get("AUTO_SAVE_MIN_CONFIDENCE") or 0.9),
        "allow_fallback": os.environ.get("AUTO_SAVE_FALLBACK", "").lower() in ("1", "true", "yes"),
    }


async def until_disconnected(request: Request, coro: Coroutine[Any, Any, T]) -> T:
    """Run ``coro``, cancelling it if the browser goes away (the page's Stop button)."""
    task = asyncio.create_task(coro)
    while not task.done():
        await asyncio.wait({task}, timeout=1)
        if not task.done() and await request.is_disconnected():
            task.cancel()
            raise HTTPException(499, "Client closed request")
    return task.result()


def _line(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def _locations(record: dict[str, Any]) -> list[dict[str, Any]]:
    """The download locations a saved record lists (stored as JSON text)."""
    raw = record.get("locations")
    try:
        found = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        found = None
    return [x for x in found if isinstance(x, dict)] if isinstance(found, list) else []


def n_locations(record: dict[str, Any]) -> int:
    return len(_locations(record)) or 1


def data_kind(record: dict[str, Any]) -> str:
    """What the saved locations offer, as the search recorded it: "open_sumstats" (full
    summary statistics, open), "closed_sumstats" (summary statistics behind a request or
    controlled access), "browser" (a results browser only), "supplement" (supplementary
    tables only, usually significant hits) or "" (not recorded)."""
    locs = _locations(record) or [{"content": record.get("content"), "access_route": record.get("access_route")}]
    sums = [x for x in locs if x.get("content") == "qtl_summary_statistics"]
    if any(x.get("access_route") in ("open", "unknown", "") for x in sums):
        return "open_sumstats"
    if sums:
        return "closed_sumstats"
    kinds = {x.get("content") for x in locs}
    return "browser" if "qtl_results_browser" in kinds else "supplement" if "supplementary_table" in kinds else ""


def create_app(store: records.RecordStore | None = None) -> FastAPI:
    app = FastAPI(title="dataqueryweb", version=__version__)
    saved = store or records.RecordStore()
    checked = checks.CheckStore(saved)
    manifests = manifest.ManifestStore(saved.path)
    data_root = fetch.data_dir(saved.path)  # downloaded and processed files
    app.mount("/static", StaticFiles(directory=_FRONTEND / "static"), name="static")
    templates = Jinja2Templates(directory=_FRONTEND / "displays")
    # `?v=` on every script and stylesheet. The package version never changes between
    # edits, so browsers kept serving a stale app.js; the newest static file's mtime,
    # read at render, changes with every edit and needs no restart.
    templates.env.globals["asset_version"] = lambda: str(max(
        (f.stat().st_mtime_ns for f in (_FRONTEND / "static").rglob("*") if f.is_file()),
        default=0))

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "index.html", {"version": __version__, "active": "search"}
        )

    @app.get("/about", response_class=HTMLResponse)
    async def about(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "about.html",
            {
                "version": __version__,
                "active": "about",
                "llm": llm.describe(),
                "autosave": autosave_settings(),
                "max_limit": MAX_SEARCH_LIMIT,
                "db_path": str(saved.path),
                "rubric": rubric,
            },
        )

    @app.get("/checked", response_class=HTMLResponse)
    async def checked_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "checked.html", {"version": __version__, "active": "checked"}
        )

    @app.get("/api/checks")
    async def list_checks() -> list[dict[str, Any]]:
        """Every paper the AI has read — its latest check — for curators to review and save."""
        return checked.latest()

    @app.get("/records", response_class=HTMLResponse)
    async def records_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "records.html",
            {"version": __version__, "active": "records", "db_path": str(saved.path)},
        )

    @app.get("/api/records")
    async def list_records() -> list[dict[str, Any]]:
        return saved.all()

    @app.post("/api/records")
    async def save_record(data: JsonBody) -> dict[str, Any]:
        try:
            return saved.save(data)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.patch("/api/records/{record_id}")
    async def edit_record(record_id: int, data: JsonBody) -> dict[str, Any]:
        note = data.get("curator_note")
        row = saved.update(
            record_id,
            curator_note=None if note is None else str(note),
            confirm=data.get("saved_by") == "human",
        )
        if row is None:
            raise HTTPException(404, "No such record")
        return row

    @app.delete("/api/records/{record_id}")
    async def delete_record(record_id: int, url: str = "") -> dict[str, Any]:
        """Delete the record, or with ``url`` only that one download location of it."""
        if url:
            row = saved.remove_location(record_id, url)
            if row is None:
                raise HTTPException(404, "No such record or location")
            return row
        if not saved.delete(record_id):
            raise HTTPException(404, "No such record")
        return {"deleted": True}

    @app.get("/api/records/export")
    async def export_records(fmt: str = Query("tsv", pattern="^(tsv|csv)$")) -> PlainTextResponse:
        stamp = datetime.now().strftime("%Y-%m-%d")
        return PlainTextResponse(
            saved.export(fmt),
            media_type="text/tab-separated-values" if fmt == "tsv" else "text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="qtl-saved-records-{stamp}.{fmt}"'
            },
        )

    @app.get("/manifest", response_class=HTMLResponse)
    async def manifest_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request, "manifest.html",
            {"version": __version__, "active": "manifest", "db_path": str(saved.path),
             "reference": rules.load_target().path, "rules_version": rules.RULES_VERSION,
             "manifest_file": str(manifests.out_dir / manifest.MANIFEST_FILE), "data_root": str(data_root),
             "max_gb": fetch.MAX_PAPER_GB,
             "candidates_file": str(manifests.out_dir / manifest.CANDIDATES_FILE)},
        )

    @app.get("/api/manifest")
    async def list_manifest() -> dict[str, Any]:
        """Candidate manifest rows and per-location reports, with what explains them."""
        reference = rules.load_target().publications
        papers = {r["record_id"]: {**{k: r[k] for k in ("publication_title", "doi", "first_author", "year", "journal",
                                                         "qtl_type", "qtl_context", "publication_url", "species", "saved_by",
                                                         "record_code")},
                                   "n_locations": n_locations(r), "data_kind": data_kind(r),
                                   "already_exists": reference.get((r.get("doi") or "").strip().lower(), ""),
                                   "not_eligible": manifest.eligible(r)}
                  for r in saved.all()}
        rows, locations = manifests.rows(), manifests.locations()
        hand = {rid: manifest.needs_you([r for r in rows if r["record_id"] == rid],
                                        [loc for loc in locations if loc["record_id"] == rid])
                for rid in {loc["record_id"] for loc in locations}}
        return {"rows": rows, "locations": locations, "records": papers,
                "built": sorted(manifests.processed()), "needs_you": {k: v for k, v in hand.items() if v},
                "runs": manifests.runs(), "blocking": sorted(rules.BLOCKING), "processing": sorted(rules.PROCESSING),
                "files": manifests.write_files(),
                "issues": rules.ISSUES, "columns": rules.MANIFEST_COLUMNS,
                "role_columns": list(rules.ROLE_COLUMN.values())}

    @app.patch("/api/manifest/{row_id}")
    async def edit_manifest_row(row_id: int, data: JsonBody) -> dict[str, Any]:
        try:
            row = manifests.update(
                row_id, review=data.get("review"), reviewer_note=data.get("reviewer_note"),
                fields=data.get("fields"))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if row is None:
            raise HTTPException(404, "No such manifest row")
        return row

    @app.get("/api/manifest/export")
    async def export_manifest(
        scope: str = Query("approved", pattern="^(approved|ready|all)$"),
        fmt: str = Query("manifest", pattern="^(manifest|review)$"),
    ) -> PlainTextResponse:
        stamp = datetime.now().strftime("%Y-%m-%d")
        return PlainTextResponse(
            manifests.export(scope, fmt), media_type="text/tab-separated-values",
            headers={"Content-Disposition": f'attachment; filename="qtl-manifest-{fmt}-{scope}-{stamp}.tsv"'},
        )

    def run_status() -> dict[str, Any]:
        job: manifest.Batch | None = getattr(app.state, "manifest_job", None)
        everything, done = saved.all(), manifests.processed()
        human = [r for r in everything if not manifest.eligible(r)]
        return {"job": job.snapshot() if job else None, "records": len(everything), "human": len(human),
                "processed": len(done), "waiting": sum(r["record_id"] not in done for r in human),
                "waiting_all_species": sum(r["record_id"] not in done for r in everything)}

    @app.get("/api/manifest/run")
    async def manifest_run_status() -> dict[str, Any]:
        """Progress of the background run, and how many saved records still wait."""
        return run_status()

    @app.post("/api/manifest/run")
    async def start_manifest_run(data: JsonBody) -> dict[str, Any]:
        """Start building candidate rows in the background. One run at a time; it keeps
        going when the page is closed, and each finished record is stored at once."""
        job: manifest.Batch | None = getattr(app.state, "manifest_job", None)
        try:
            ids = [int(x) for x in data.get("record_ids") or []]
            limit = max(0, int(data.get("limit") or 0))
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "record_ids and limit must be numbers") from exc
        todo = manifest.select(
            saved.all(), manifests.processed(), record_ids=ids, limit=limit,
            redo=bool(data.get("redo")), all_species=bool(data.get("all_species")))
        if not todo:
            raise HTTPException(400, "No such saved record" if ids else "No saved record is waiting")
        if job is not None and job.running:  # a Build click while a run goes: join its queue
            job.add(todo)
            return run_status()
        try:
            max_gb = float(data.get("max_gb") or fetch.MAX_PAPER_GB)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "max_gb must be a number") from exc
        options = fetch.Options(data_root, download=data.get("download", True) is not False,
                                max_bytes=max(0.0, max_gb) * 1e9)
        job = manifest.Batch(todo, manifests, no_llm=bool(data.get("no_llm")),
                             include_supplements=bool(data.get("include_supplements")), data=options)
        job.task = asyncio.create_task(job.run())
        app.state.manifest_job = job
        return run_status()

    @app.post("/api/manifest/run/stop")
    async def stop_manifest_run() -> dict[str, Any]:
        job: manifest.Batch | None = getattr(app.state, "manifest_job", None)
        if job is not None and job.running:
            job.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await job.task  # type: ignore[misc]
        return run_status()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    async def judge(
        j: llm.Judge,
        p: finder.Paper,
        *,
        autosave: bool = False,
        search_terms: str = "",
        recheck: bool = False,
    ) -> None:
        """The paper's AI verdict, spending tokens only on a paper never checked before.

        1. Checked before (``llm_checks``): reuse that verdict and its picks; no LLM call.
        2. Already in Saved records (from before checks were kept): skip, as curated.
        3. Otherwise ask the LLM, and record the check — verdict, picks and tokens — whether
           or not it is auto-saved. ``recheck`` skips 1 and 2 and records a new check.
        """
        known = [] if recheck else saved.find_paper(p)
        previous = None if recheck else checked.find(p)
        if previous is not None:
            checks.reuse(p, previous)
            if known:
                p.ai["saved_records"] = [r["record_id"] for r in known]
        elif known:
            p.ai = {
                "skipped": True,
                "reason": "Already in Saved records — AI check skipped to save tokens",
                "saved_records": [r["record_id"] for r in known],
                "saved_by": sorted({r["saved_by"] for r in known}),
            }
            return
        else:
            try:
                # A paper may need both a text check and a web-research pass. The
                # latter alone may use the full 300-second provider timeout.
                verdict = await asyncio.wait_for(j.judge(p), timeout=600)
            except TimeoutError:
                verdict = {"error": "LLM timed out"}
            llm.apply(p, verdict)
            # A paper refused before any model answered (every model out of quota) is not
            # a check: it spent nothing, would drag the per-paper average down, and must be
            # asked again once quota is back.
            if p.ai is not None and ("error" not in p.ai or (p.ai.get("usage") or {}).get("calls")):
                checked.record(p, p.ai, search_terms=search_terms)
        if autosave and p.ai is not None and "error" not in p.ai:
            settings = autosave_settings()
            p.ai["autosave"] = records.autosave(
                saved,
                p,
                search_terms=search_terms,
                min_confidence=settings["min_confidence"],
                allow_fallback=settings["allow_fallback"],
            )

    @app.get("/api/usage")
    async def usage(recent: int = Query(50, ge=0, le=500)) -> dict[str, Any]:
        """Tokens spent on LLM checks: totals, average per checked paper, latest checks."""
        return checked.summary(recent)

    @app.get("/api/llm")
    async def llm_info() -> dict[str, Any]:
        return llm.describe() | {"autosave": autosave_settings(), "max_limit": MAX_SEARCH_LIMIT}

    @app.get("/api/llm/check")
    async def llm_check() -> dict[str, Any]:
        """Is the current key accepted? Lists the endpoint's models (no tokens are used)."""
        config = llm.config_from_env()
        info = config.public()
        if config.name == "Pollinations":
            return info | {"ok": True, "detail": "keyless endpoint"}
        base = config.base_url.rstrip("/")
        async with httpx.AsyncClient(timeout=20) as http:
            try:
                r = await http.get(
                    f"{base}/models", headers={"Authorization": f"Bearer {config.api_key}"}
                )
            except httpx.HTTPError as exc:
                return info | {
                    "ok": False,
                    "detail": f"cannot reach endpoint ({type(exc).__name__})",
                }
        if r.status_code != 200:
            return info | {"ok": False, "detail": f"HTTP {r.status_code}: {r.text[:160]}"}
        try:
            models = [m.get("id") for m in r.json().get("data", [])]
        except ValueError:
            models = []
        missing = [m for m in config.models if models and m not in models]
        detail = f"key accepted, {len(models)} models available"
        if missing:
            detail += f"; not offered: {', '.join(missing)}"
        return info | {"ok": True, "detail": detail, "models": models}

    @app.get("/api/search")
    async def search(
        q: str = Query(..., min_length=1, max_length=500),
        limit: int = Query(10, ge=1, le=MAX_SEARCH_LIMIT),
        qtl_only: bool = True,
        source: str = Query("all", pattern="^(all|journal|preprint)$"),
        year_from: int | None = Query(None, ge=1800, le=3000),
        year_to: int | None = Query(None, ge=1800, le=3000),
        sort: str = Query("relevance", pattern="^(relevance|newest)$"),
        ai: bool = False,
        autosave: bool = False,
        recheck: bool = False,
        cursor: str = Query("*", max_length=500),
        offset: int = Query(0, ge=0, le=MAX_SEARCH_LIMIT),
    ) -> StreamingResponse:
        async def stream() -> AsyncIterator[bytes]:
            j = llm.Judge() if ai else None
            tasks: list[asyncio.Task[tuple[int, finder.Paper]]] = []
            lines = run(j, tasks)
            try:
                async for line in lines:
                    yield line
            finally:
                # Stop search / closed tab: Starlette cancels this generator when the client
                # disconnects. Cancel papers still being read, so they stop using LLM quota
                # and auto-saving, then close the HTTP clients.
                for t in tasks:
                    t.cancel()
                # Shielded: this generator may itself be cancelled (anyio re-raises the
                # cancellation at every await otherwise, skipping the cleanup).
                with anyio.CancelScope(shield=True):
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await lines.aclose()
                    if j:
                        await j.aclose()

        async def run(
            j: llm.Judge | None, tasks: list[asyncio.Task[tuple[int, finder.Paper]]]
        ) -> AsyncIterator[bytes]:
            async with finder.client() as http:
                try:
                    hits, papers, next_cursor = await finder.epmc_search(
                        http,
                        q,
                        limit=limit,
                        qtl_only=qtl_only,
                        source=source,
                        year_from=year_from,
                        year_to=year_to,
                        sort=sort,
                        cursor=cursor,
                    )
                except httpx.HTTPError as exc:
                    code = getattr(getattr(exc, "response", None), "status_code", "")
                    yield _line(
                        {
                            "type": "error",
                            "message": f"Europe PMC is temporarily unavailable ({code or type(exc).__name__}). "
                            "Search progress is kept. Please press Resume in a minute.",
                        }
                    )
                    return
                yield _line(
                    {
                        "type": "meta",
                        "hits": hits,
                        "count": len(papers),
                        # Where the next batch starts; "" when this was the last one.
                        "next_cursor": next_cursor,
                        "llm": j.public() if j else None,
                    }
                )
                # Ordered checks make the checkpoint exact: no later paper spends tokens
                # after an earlier paper has exhausted the provider's quota.
                for i, p in enumerate(papers):
                    if i < offset:
                        continue
                    previous = None if recheck else checked.find(p)
                    if previous is None:
                        try:
                            await asyncio.wait_for(finder.inspect(http, p), timeout=90)
                        except Exception as exc:
                            p.note = f"Inspection failed: {type(exc).__name__}"
                    if j:
                        await judge(j, p, autosave=autosave, search_terms=q, recheck=recheck)
                    if j and p.ai and "error" in p.ai:
                        yield _line({"type": "paused", "offset": i,
                                     "message": p.ai["error"]})
                        return
                    yield _line({"type": "paper", "rank": i, "paper": p.to_dict()})
                yield _line({"type": "done"})

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    @app.get("/api/inspect")
    async def inspect(
        request: Request,
        ref: str = Query(..., min_length=3, max_length=2000),
        ai: bool = False,
        autosave: bool = False,
        recheck: bool = False,
    ) -> dict[str, Any]:
        async def work() -> dict[str, Any]:
            async with finder.client() as http:
                try:
                    paper = await asyncio.wait_for(finder.inspect_ref(http, ref), timeout=120)
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
                except (httpx.HTTPError, TimeoutError) as exc:
                    raise HTTPException(502, f"Lookup failed: {type(exc).__name__}") from exc
            if ai:
                j = llm.Judge()
                try:
                    await judge(j, paper, autosave=autosave, search_terms=ref, recheck=recheck)
                finally:
                    await j.aclose()
            return paper.to_dict()

        return await until_disconnected(request, work())

    return app


app = create_app()


def main() -> None:  # pragma: no cover
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser(description="Run the dataqueryweb server.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8010)
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args()
    uvicorn.run("dataqueryweb.web:app", host=args.host, port=args.port, reload=args.reload)
