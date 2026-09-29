# dataqueryweb

A web page for finding where the QTL data behind a publication can be downloaded. Its look
matches [LocusView](../locusview): the same `#015A84` teal header, slate/white surfaces and
system-ui + JetBrains Mono fonts.

- **Search literature**: type keywords. The backend searches Europe PMC (title/abstract,
  restricted to QTL papers by default), reads each result, and streams the papers back as
  they finish.
- **Sources**: search all, journals only, or bioRxiv + medRxiv preprints only.
- **DOI or URL**: paste a DOI, a doi.org / publisher / PMC / PubMed / bioRxiv link, a PMID or
  a PMCID, and that one paper is inspected in the same way.

For each paper the backend reads the Europe PMC or NCBI PMC full text (JATS), Europe PMC's
text-mined accessions, and, when there is no open full text, the publisher page. It extracts
the Data availability statement, repository links, dataset DOIs and accessions (GEO, EGA,
dbGaP, Zenodo, figshare, GWAS Catalog, eQTL Catalogue…), and supplementary files with QTL
captions. For bioRxiv/medRxiv preprints it also calls api.biorxiv.org and, when the
preprint has been published, inspects the journal version too. Each source is rated high, medium or low relevance, using the sentence it was found
in. Zenodo and figshare records are expanded into direct file download links.

## AI check (free LLM)

The search is for **newly published QTL data**. So with `ai=true` (on by default in the page),
an LLM reads each paper's title, abstract, data availability statements and the numbered list
of found links. It decides whether the paper mapped QTLs itself (new data), or only reused
published QTLs (MR, colocalization, TWAS…), and which found links hold the results. It answers
with link numbers only, so it cannot invent URLs. When the full text was read, the prompt
also carries it (data paragraphs first, `LLM_MAX_TEXT_CHARS`). With `LLM_WEB_SEARCH=true` on
an endpoint that serves the Responses API `web_search` tool, papers without readable full
text (or without a found download) get a web-research pass. Every URL from that pass is
checked with an HTTP request, and dead ones are dropped. See `src/backend/dataqueryweb/llm.py`.

Provider, picked from the environment or `.env` (see `.env.example`):

| Set | Provider | Models |
|---|---|---|
| `OPENROUTER_API_KEY` | OpenRouter (free account) | `qwen/qwen3.8-27b:free`, then `gemma-4-31b-it:free`, then `nemotron-3-super:free` |
| `GROQ_API_KEY` | Groq free tier | `llama-3.3-70b-versatile` |
| nothing | Pollinations, keyless | `gpt-oss-20b`, one request at a time |
| `LLM_BASE_URL` (+`LLM_API_KEY`, `LLM_MODEL`) | any OpenAI-compatible endpoint | yours |

## Saved records

Each result row has a **Save** button. Rows confirmed as correct go into a local SQLite table
(`data/records.sqlite3`), listed at `/records` and exported as TSV/CSV. The export uses
locusview's qtl-data-agent review-table columns. API: `GET/POST /api/records`,
`PATCH/DELETE /api/records/{id}`, `GET /api/records/export?fmt=tsv|csv`.

## Auto-save

With `ai=true&autosave=true` (both on by default in the page), papers the LLM is very sure
about are saved automatically as `saved_by=auto`:
- the verdict is "yes", with confidence ≥ `AUTO_SAVE_MIN_CONFIDENCE` (default 0.9);
- the verdict came from the main model, not the fallback;
- there is a picked location holding QTL results.

Everything else is left for a person. The page filters papers by review status: needs human
check / auto-saved / saved by you / no new data. Auto-saved rows can be confirmed or removed.
See `records.autosave`.

## Run

中文使用说明（启动、重启、配置 LLM、API）见 [USAGE.md](USAGE.md).

```bash
uv sync
uv run dataqueryweb --port 8010      # http://127.0.0.1:8010
uv run pytest                        # offline parsing tests
```

## API

- `GET /api/inspect?ref=10.1038/s41588-021-00913-z` returns one paper as JSON.
- `GET /api/search?q=single-cell+eQTL&limit=10&qtl_only=true&source=all|journal|preprint&ai=true` returns NDJSON lines:
  one `meta` line, then one `paper` line per paper, then `done`.

## Layout

```
src/backend/dataqueryweb/   web.py (FastAPI), finder.py (network), extract.py (pure parsing),
                            llm.py (free-LLM verdict), records.py (saved records)
src/frontend/displays/      Jinja templates (layout, index, about)
src/frontend/static/        css/base.css, js/app.js
tests/                      test_extract.py
```
