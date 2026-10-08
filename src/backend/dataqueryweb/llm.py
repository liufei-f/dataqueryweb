"""LLM judgement: does a paper release *new* QTL data, and which found link downloads it?

The rule-based extractor (extract.py) finds every candidate link; the LLM only decides.
It sees the title, abstract, data-availability statements and the numbered candidate list,
and answers with candidate ids (``S3``) — never free-text URLs — so it cannot invent a
download address. Any OpenAI-compatible chat endpoint works; free options, in the order
they are picked up from the environment:

- ``OPENROUTER_API_KEY``: OpenRouter's ``:free`` models (free account at openrouter.ai).
- ``GROQ_API_KEY``: Groq's free tier (console.groq.com).
- nothing set: Pollinations (text.pollinations.ai), keyless and rate-limited.

``LLM_BASE_URL`` / ``LLM_API_KEY`` / ``LLM_MODEL`` override any of these, and
``LLM_FALLBACK=pollinations`` adds the keyless endpoint as a second choice for when every
configured model fails (quota-limited proxies run out mid-search).

Full text: when the paper's full text was read, the prompt carries an excerpt of it (data,
QTL and accession paragraphs first) up to ``max_text`` characters (``LLM_MAX_TEXT_CHARS``).

Web research: with ``LLM_WEB_SEARCH=true`` and an endpoint that serves the OpenAI Responses
API with the ``web_search`` tool, papers whose full text could not be read — or that the LLM
judged "new data" without a download location — get a second pass in which the model
searches the web and reads the article itself. URLs from that pass are not in our candidate
list, so each is checked with an HTTP request and dropped if it does not exist.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from dataqueryweb import rubric

if TYPE_CHECKING:
    from dataqueryweb.finder import Paper

MAX_CANDIDATES = 30
MAX_AVAILABILITY_CHARS = 3500
MAX_ABSTRACT_CHARS = 2500


@dataclass
class LLMConfig:
    name: str
    base_url: str
    api_key: str
    models: list[str]  # tried in order; free models come and go
    concurrency: int
    max_text: int = 12000  # characters of full text sent with each paper
    web_search: bool = False  # endpoint supports /responses with the web_search tool

    def public(self) -> dict[str, Any]:
        model = "gpt-oss-20b" if self.name == "Pollinations" and self.models == ["openai"] else ""
        info: dict[str, Any] = {"provider": self.name, "model": model or ", ".join(self.models)}
        info["max_text"] = self.max_text
        info["web_search"] = self.web_search
        info["key"] = mask_key(self.api_key) or "none (keyless)"
        info["key_id"] = key_id(self.api_key)
        return info


def pollinations(models: list[str] | None = None) -> LLMConfig:
    # Pollinations' keyless "openai" alias is served by gpt-oss-20b.
    return LLMConfig(
        "Pollinations", "https://text.pollinations.ai/openai", "", models or ["openai"], 1
    )


def configs_from_env() -> list[LLMConfig]:
    """The primary config, then the fallback when ``LLM_FALLBACK=pollinations``."""
    primary = config_from_env()
    wants = os.environ.get("LLM_FALLBACK", "").strip().lower() == "pollinations"
    return [primary, pollinations()] if wants and primary.name != "Pollinations" else [primary]


def describe() -> dict[str, Any]:
    configs = configs_from_env()
    info = configs[0].public()
    if len(configs) > 1:
        info["fallback"] = configs[1].public()
    return info


DOTENV = Path(__file__).resolve().parents[3] / ".env"
_from_dotenv: dict[str, str] = {}  # values this process took from .env (not the shell)


def _load_dotenv(path: Path | None = None) -> None:
    """Apply the project's .env, re-read on every call so a changed key needs no restart.

    Variables set in the shell before start-up win over .env; variables that came from .env
    follow it — updated when it changes, removed when deleted from it.
    """
    path = path or Path(os.environ.get("DATAQUERY_ENV_FILE") or DOTENV)
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
    for key in list(_from_dotenv):
        if key not in values and os.environ.get(key) == _from_dotenv[key]:
            del os.environ[key]
            del _from_dotenv[key]
    for key, value in values.items():
        if key not in os.environ or os.environ[key] == _from_dotenv.get(key):
            os.environ[key] = value
            _from_dotenv[key] = value


def mask_key(key: str) -> str:
    """Enough of an API key to recognise it on screen, never the whole secret."""
    if not key:
        return ""
    return f"{key[:6]}…{key[-4:]}" if len(key) > 14 else f"{key[:2]}…"


def key_id(key: str) -> str:
    """A short fingerprint that changes whenever the key changes."""
    return hashlib.sha256(key.encode()).hexdigest()[:10] if key else ""


def config_from_env() -> LLMConfig:
    _load_dotenv()
    env = os.environ.get
    model_override = [m.strip() for m in (env("LLM_MODEL") or "").split(",") if m.strip()]
    if env("LLM_BASE_URL"):
        base = env("LLM_BASE_URL", "")
        return LLMConfig(
            urlsplit(base).netloc or "custom",
            base,
            env("LLM_API_KEY", ""),
            model_override,
            4,
            max_text=int(env("LLM_MAX_TEXT_CHARS") or 60000),
            web_search=(env("LLM_WEB_SEARCH") or "").lower() in ("1", "true", "yes"),
        )
    if env("OPENROUTER_API_KEY"):
        return LLMConfig(
            "OpenRouter",
            "https://openrouter.ai/api/v1",
            env("OPENROUTER_API_KEY", ""),
            model_override
            or [
                "qwen/qwen3.8-27b:free",
                "google/gemma-4-31b-it:free",
                "nvidia/nemotron-3-super-120b-a12b:free",
            ],
            3,
            max_text=int(env("LLM_MAX_TEXT_CHARS") or 40000),
        )
    if env("GROQ_API_KEY"):
        return LLMConfig(
            "Groq",
            "https://api.groq.com/openai/v1",
            env("GROQ_API_KEY", ""),
            model_override or ["llama-3.3-70b-versatile", "openai/gpt-oss-20b"],
            3,
        )
    return pollinations(model_override)


INTRO = """You are a data curator building a catalogue of NEWLY GENERATED QTL (quantitative \
trait locus) association results: eQTL, sQTL, pQTL, caQTL, mQTL/meQTL, hQTL, apaQTL, \
single-cell/cell-type/context QTL, and similar molecular QTL.

You do not decide a verdict or a confidence: you answer factual questions, and the program \
scores them with fixed rules. Base your answers on the full-text excerpt when one is given \
(methods and data availability are more reliable than the abstract).

"""

CONTENT_VALUES = (
    'content values: "qtl_summary_statistics", "qtl_results_browser", "supplementary_table", '
    '"raw_or_individual_data", "other".'
)
DOWNLOAD_STATUS = (
    'download_status (for the paper\'s own QTL results): "open" (public download), '
    '"controlled" (EGA/dbGaP/… application), "on_request" (contact authors), "not_found".'
)

SYSTEM = (
    INTRO
    + rubric.prompt_block()
    + """

For data_sources choose ONLY from the numbered candidates (S1, S2, …) and ONLY those that \
hold THIS paper's own QTL results or the data needed to reproduce them. Never invent URLs. \
Skip software/code repositories unless the text says they contain the results, and skip \
resources the paper merely downloaded.

"""
    + CONTENT_VALUES
    + "\n"
    + DOWNLOAD_STATUS
    + """

Reply with a single JSON object and nothing else:
{"""
    + rubric.ANSWERS_SCHEMA
    + """, "qtl_types": ["eQTL", …], "species": "human|mouse|…", \
"tissues_or_cells": "short text", "reason": "one sentence", \
"download_status": "open|controlled|on_request|not_found", \
"data_sources": [{"id": "S1", "content": "…", "note": "short: what exactly is there"}]}"""
)

SYSTEM_WEB = (
    INTRO
    + rubric.prompt_block()
    + """

You have a web_search tool. Our program could not read this paper's full text, or found no \
download location in it. Use web search to find and READ the article itself (publisher page, \
PubMed Central, Europe PMC, bioRxiv/medRxiv, author or lab pages) — especially its Data \
availability / Code availability sections and supplementary information — and the data \
repository pages it points to (Zenodo, figshare, GEO, EGA, eQTL Catalogue, lab portals…). \
Quotes must come from pages you opened.

For data_sources give ONLY URLs you actually saw on a page you opened, which hold THIS \
paper's own QTL results (or the data to reproduce them). Prefer direct repository or file \
URLs over landing pages. Never guess or construct a URL. If the paper says the data are \
available on request or under controlled access, say so and give the application page if \
one is named.

"""
    + CONTENT_VALUES
    + ' access values: "open", "controlled", "on_request", "unknown".\n'
    + DOWNLOAD_STATUS
    + """

When you are done, reply with a single JSON object and nothing else:
{"full_text_found": true|false, "pages_read": ["url", …], """
    + rubric.ANSWERS_SCHEMA
    + """, "qtl_types": ["eQTL", …], "species": "…", "tissues_or_cells": "…", \
"reason": "one sentence", "download_status": "open|controlled|on_request|not_found", \
"data_sources": [{"url": "…", "content": "…", "access": "…", \
"quote": "the sentence that points to it", "found_on": "url of the page with that sentence"}]}"""
)


def build_web_prompt(paper: Paper) -> str:
    known = "\n".join(f"- {s.url} ({s.repository}): {s.context[:200]}" for s in paper.sources[:15])
    return (
        f"Title: {paper.title}\n"
        f"DOI: {paper.doi or '(none)'}   PMID: {paper.pmid or '-'}   PMCID: {paper.pmcid or '-'}\n"
        f"Article URL: {paper.url or '-'}\n"
        f"Journal/server: {paper.journal} {paper.year}"
        f"{' (preprint)' if paper.preprint_server else ''}"
        f"{f'; published as {paper.published_doi}' if paper.published_doi else ''}\n\n"
        f"Abstract: {_strip_tags(paper.abstract)[:MAX_ABSTRACT_CHARS] or '(not available)'}\n\n"
        f"Links our program already found (may be incomplete or irrelevant):\n{known or '(none)'}\n"
    )


def _strip_tags(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


DATA_PARAGRAPH_RE = re.compile(
    r"QTL|summary[ -]statistic|availab|deposited|download|accession|zenodo|figshare|github"
    r"|\bGEO\b|\bEGA\b|dbGaP|synapse|supplementary (?:data|table)|genotyp|donors|individuals",
    re.IGNORECASE,
)


def text_excerpt(paragraphs: list[str], budget: int) -> str:
    """Up to ``budget`` characters: data/QTL paragraphs first, then the rest, in order."""
    if budget <= 0 or not paragraphs:
        return ""
    ranked = [p for p in paragraphs if DATA_PARAGRAPH_RE.search(p)]
    ranked += [p for p in paragraphs if not DATA_PARAGRAPH_RE.search(p)]
    chosen: set[int] = set()
    used = 0
    index = {id(p): i for i, p in enumerate(paragraphs)}
    for p in ranked:
        if used + len(p) > budget:
            continue
        chosen.add(index[id(p)])
        used += len(p) + 1
    return "\n".join(paragraphs[i] for i in sorted(chosen))


def build_prompt(paper: Paper, max_text: int = 0) -> tuple[str, dict[str, int]]:
    """The user message, and the id → index map for ``paper.sources``."""
    ids: dict[str, int] = {}
    lines = []
    for i, s in enumerate(paper.sources[:MAX_CANDIDATES]):
        sid = f"S{i + 1}"
        ids[sid] = i
        where = "data availability" if s.in_availability else s.kind
        lines.append(
            f"{sid} | {s.repository} | {s.access} | {where} | {s.url}\n"
            f"    context: {s.context[:300]}"
        )
    availability = "\n".join(f"- {a}" for a in paper.availability)[:MAX_AVAILABILITY_CHARS]
    text = (
        f"Title: {paper.title}\n"
        f"Journal/server: {paper.journal} {paper.year}"
        f"{' (preprint)' if paper.preprint_server else ''}\n\n"
        f"Abstract: {_strip_tags(paper.abstract)[:MAX_ABSTRACT_CHARS] or '(not available)'}\n\n"
        f"Data availability statements:\n{availability or '(none found)'}\n\n"
        f"Candidate data locations:\n{chr(10).join(lines) or '(none found)'}\n"
    )
    excerpt = text_excerpt(paper.paragraphs, max_text)
    if excerpt:
        label = "Full text" if paper.full_text else "Article page text"
        text += f"\n{label} (excerpt, data-related paragraphs first):\n{excerpt}\n"
    return text, ids


def parse_json(text: str) -> dict[str, Any]:
    """The first JSON object in a model reply (models like to add fences or prose)."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in reply")
    return dict(json.loads(text[start : end + 1]))


def normalize(raw: dict[str, Any], ids: dict[str, int]) -> dict[str, Any]:
    """The model's answers and picks. Verdict and confidence are added by :func:`finish`."""
    picks = []
    for item in raw.get("data_sources") or []:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("id", "")).strip().upper()
        if sid in ids:  # drop anything that is not one of our candidates
            picks.append(
                {
                    "index": ids[sid],
                    "content": rubric.normalize_content(item.get("content")),
                    "note": str(item.get("note") or "")[:300],
                }
            )
    return {
        "answers": rubric.parse_answers(raw.get("answers")),
        "qtl_types": [str(t) for t in (raw.get("qtl_types") or [])][:10],
        "species": str(raw.get("species") or ""),
        "tissues_or_cells": str(raw.get("tissues_or_cells") or "")[:200],
        "reason": str(raw.get("reason") or "")[:400],
        "download_status": str(raw.get("download_status") or "not_found"),
        "data_sources": picks,
    }


def finish(result: dict[str, Any], *, location_found: bool, full_text_read: bool) -> dict[str, Any]:
    """Add the rubric's verdict, confidence and checklist to a normalized result."""
    result.update(
        rubric.score(
            result["answers"], location_found=location_found, full_text_read=full_text_read
        )
    )
    return result


ACCESS = {"open": "open", "controlled": "controlled", "on_request": "request"}
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
QUOTA_RE = re.compile(r"usage_limit|quota|cooldown|insufficient_quota|limit has been reached", re.I)


class QuotaExhausted(Exception):
    """The provider refused because an account quota is used up."""


def new_usage() -> dict[str, int]:
    """Tokens one paper's check spent, summed over every model call it made."""
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}


def add_usage(usage: dict[str, int] | None, reported: Any) -> None:
    """Add a provider's ``usage`` block: chat (prompt/completion) or Responses (input/output)."""
    if usage is None or not isinstance(reported, dict):
        return
    prompt = int(reported.get("prompt_tokens") or reported.get("input_tokens") or 0)
    completion = int(reported.get("completion_tokens") or reported.get("output_tokens") or 0)
    usage["prompt_tokens"] += prompt
    usage["completion_tokens"] += completion
    usage["total_tokens"] += int(reported.get("total_tokens") or prompt + completion)
    usage["calls"] += 1


class Judge:
    """Rate-limited LLM caller shared by all papers of one request."""

    def __init__(self, configs: list[LLMConfig] | None = None) -> None:
        self.configs = configs or configs_from_env()
        self.config = self.configs[0]
        self.gates = {c.name: asyncio.Semaphore(c.concurrency) for c in self.configs}
        # Models that hit a usage limit are skipped for the rest of this request.
        self.exhausted: set[tuple[str, str]] = set()
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0))

    def public(self) -> dict[str, Any]:
        info = self.config.public()
        if len(self.configs) > 1:
            info["fallback"] = self.configs[1].public()
        return info

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _chat(
        self, config: LLMConfig, model: str, user: str, usage: dict[str, int] | None = None
    ) -> tuple[str, str]:
        """(reply text, model name the provider reports actually answering); adds to ``usage``."""
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        if config.name == "OpenRouter":
            headers["X-Title"] = "dataqueryweb"
        body = {
            "model": model,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        }
        # GPT-6.1 Sol uses reasoning by default, where temperature is unsupported.
        if not model.startswith("gpt-6.1-"):
            body["temperature"] = 0
        url = config.base_url.rstrip("/")
        url = url if url.endswith("/openai") else f"{url}/chat/completions"
        for attempt in range(4):
            try:
                r = await self.http.post(url, headers=headers, json=body)
            except httpx.TimeoutException:
                # A slow proxy response is transient. Retry once before pausing the
                # search; the per-paper deadline still bounds the total wait.
                if attempt == 0:
                    await asyncio.sleep(2)
                    continue
                raise
            if r.status_code in (402, 403, 429) and QUOTA_RE.search(r.text):
                raise QuotaExhausted(r.text[:200])  # won't recover by waiting a few seconds
            if r.status_code in (429, 502, 503) and attempt < 3:  # rate limit / busy
                wait = float(r.headers.get("retry-after") or 4 * (attempt + 1))
                if wait > 30:
                    raise QuotaExhausted(f"retry-after {wait:.0f}s")
                await asyncio.sleep(wait)
                continue
            r.raise_for_status()
            data = r.json()
            add_usage(usage, data.get("usage"))
            message = data["choices"][0]["message"]
            return str(message.get("content") or ""), str(data.get("model") or model)
        raise httpx.HTTPError("rate limited")

    async def judge(self, paper: Paper) -> dict[str, Any]:
        """Text verdict, then — when needed and supported — a web-research pass.

        ``verdict["usage"]`` is every token both passes spent on this paper.
        """
        usage = new_usage()
        verdict = await self._judge_text(paper, usage)
        if self.needs_research(paper, verdict):
            # Mark the text pass's picks now: research inserts sources, shifting indices.
            mark_picks(paper, verdict)
            web = await self.research(paper, usage)
            if web is not None:
                verdict = web
        verdict["usage"] = usage
        return verdict

    def needs_research(self, paper: Paper, verdict: dict[str, Any]) -> bool:
        if not self.config.web_search or verdict.get("new_qtl_data") == "no":
            return False
        if "error" in verdict:
            return not paper.full_text
        return not paper.full_text or (
            verdict.get("new_qtl_data") == "yes" and not verdict.get("data_sources")
        )

    async def _judge_text(
        self, paper: Paper, usage: dict[str, int] | None = None
    ) -> dict[str, Any]:
        errors = []
        for config in self.configs:
            user, ids = build_prompt(paper, config.max_text)
            async with self.gates[config.name]:
                for model in config.models:
                    if (config.name, model) in self.exhausted:
                        continue
                    try:
                        reply, used = await self._chat(config, model, user, usage)
                        result = normalize(parse_json(reply), ids)
                        finish(
                            result,
                            location_found=any(
                                p["content"] in rubric.RESULT_CONTENT
                                for p in result["data_sources"]
                            ),
                            full_text_read=paper.full_text,
                        )
                        result["model"] = f"{config.name} · {used}"
                        if config is not self.configs[0]:
                            result["model"] += " (fallback)"
                        return result
                    except QuotaExhausted:
                        self.exhausted.add((config.name, model))
                        errors.append(f"{model}: usage limit")
                    except (httpx.HTTPError, ValueError, KeyError) as exc:
                        errors.append(f"{model}: {type(exc).__name__}")
        return {"error": "; ".join(errors) or "no model configured"}

    # ── web research (OpenAI Responses API + web_search tool) ──────────────────

    async def _responses(
        self, config: LLMConfig, model: str, user: str, usage: dict[str, int] | None = None
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {config.api_key}"}
        body = {
            "model": model,
            "instructions": SYSTEM_WEB,
            "input": user,
            "tools": [{"type": "web_search"}],
        }
        r = await self.http.post(
            f"{config.base_url.rstrip('/')}/responses", headers=headers, json=body, timeout=300
        )
        if r.status_code in (402, 403, 429) and QUOTA_RE.search(r.text):
            raise QuotaExhausted(r.text[:200])
        r.raise_for_status()
        data = dict(r.json())
        add_usage(usage, data.get("usage"))
        return data

    async def verify_url(self, url: str) -> str:
        """ "ok", "blocked" (exists but refuses robots), or "missing"."""
        if url.startswith("ftp://"):
            return "unchecked"
        try:
            r = await self.http.get(
                url, headers={"User-Agent": BROWSER_UA}, follow_redirects=True, timeout=25
            )
        except httpx.HTTPError:
            return "missing"
        if r.status_code < 400:
            return "ok"
        return "blocked" if r.status_code in (401, 403, 405, 429, 503) else "missing"

    async def research(
        self, paper: Paper, usage: dict[str, int] | None = None
    ) -> dict[str, Any] | None:
        config = self.config
        user = build_web_prompt(paper)
        async with self.gates[config.name]:
            for model in config.models:
                if (config.name, model) in self.exhausted:
                    continue
                try:
                    data = await self._responses(config, model, user, usage)
                    return await self._apply_research(paper, data, config, model)
                except QuotaExhausted:
                    self.exhausted.add((config.name, model))
                except (httpx.HTTPError, ValueError, KeyError):
                    continue
        return None

    async def _apply_research(
        self, paper: Paper, data: dict[str, Any], config: LLMConfig, model: str
    ) -> dict[str, Any]:
        from dataqueryweb import extract  # local: extract imports nothing from here

        text, queries, opened = "", [], []
        for item in data.get("output") or []:
            if item.get("type") == "web_search_call":
                action = item.get("action") or {}
                if action.get("type") == "search" and action.get("query"):
                    queries.append(str(action["query"]))
                elif action.get("url"):
                    opened.append(str(action["url"]))
            elif item.get("type") == "message":
                text += "".join(c.get("text", "") for c in item.get("content") or [])
        raw = parse_json(text)
        result = normalize(raw, {})  # verdict fields; data_sources are URLs here, handled below

        found = [d for d in raw.get("data_sources") or [] if isinstance(d, dict)]
        urls = [extract.normalize_url(str(d.get("url") or "")) for d in found]
        checks = await asyncio.gather(*(self.verify_url(u) for u in urls))
        existing = {s.url.rstrip("/").lower(): s for s in paper.sources}
        kept, dropped = [], []
        for d, url, status in zip(found, urls, checks, strict=True):
            if not url.lower().startswith(("http", "ftp")) or status == "missing":
                dropped.append(url)
                continue
            content = rubric.normalize_content(d.get("content"))
            note = f"Found by web search on {d.get('found_on') or 'the web'}"
            note += "" if status == "ok" else f" (link {status})"
            source = existing.get(url.rstrip("/").lower())
            if source is None:
                klass = extract.classify_url(url)
                source = extract.Source(
                    url=url,
                    label=url,
                    repository=klass[0] if klass else urlsplit(url).netloc,
                    kind="web",
                    access=ACCESS.get(str(d.get("access")), "unknown"),
                    context=str(d.get("quote") or "")[:500],
                    in_availability=True,
                    qtl_context=True,
                )
                paper.sources.insert(0, source)
            source.ai_pick, source.ai_note = content, note
            kept.append(url)

        finish(
            result,
            # Includes picks from the text pass, marked on paper.sources before research.
            location_found=any(s.ai_pick in rubric.RESULT_CONTENT for s in paper.sources),
            full_text_read=paper.full_text or bool(raw.get("full_text_found")),
        )
        result["model"] = f"{config.name} · {data.get('model') or model} + web search"
        result["web"] = {
            "full_text_found": bool(raw.get("full_text_found")),
            "queries": queries[:8],
            "pages": list(dict.fromkeys([*opened, *map(str, raw.get("pages_read") or [])]))[:10],
            "dropped": dropped,
        }
        paper.evidence.append(f"LLM web search ({len(opened)} pages opened)")
        paper.sources.sort(key=lambda s: not s.ai_pick)
        result["data_sources"] = []  # already applied to paper.sources above
        return result


def mark_picks(paper: Paper, verdict: dict[str, Any]) -> None:
    """Copy the verdict's candidate picks onto ``paper.sources`` (and consume them)."""
    for pick in verdict.get("data_sources") or []:
        if 0 <= pick["index"] < len(paper.sources):
            source = paper.sources[pick["index"]]
            source.ai_pick, source.ai_note = pick["content"], pick["note"]
    verdict["data_sources"] = []


def apply(paper: Paper, verdict: dict[str, Any]) -> None:
    """Attach the verdict and move the LLM-picked sources to the top."""
    paper.ai = verdict
    mark_picks(paper, verdict)
    paper.sources.sort(key=lambda s: not s.ai_pick)  # stable: keeps rule order otherwise
    verdict.pop("data_sources", None)
