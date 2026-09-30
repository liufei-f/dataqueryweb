"""Network side: search Europe PMC, resolve a DOI/URL/PMID, and inspect one paper.

Inspection reads, in order, whatever evidence is reachable:

1. Europe PMC ``fullTextXML`` (open-access PMC papers) — the Data availability section,
   every repository link and accession, and QTL-captioned supplementary files.
2. Europe PMC's text-mined ``Accession Numbers`` annotations, which also cover papers whose
   full text Europe PMC cannot redistribute.
3. For bioRxiv/medRxiv preprints, the bioRxiv API (metadata and the journal DOI once the
   preprint is published) — the published version is then inspected too, and its sources
   merged. bioRxiv itself refuses automated full-text downloads, so preprint full text comes
   from Europe PMC when it has it.
4. The publisher page (via doi.org) when there is no full text; many publishers refuse
   automated requests, and that is reported rather than hidden.
5. Zenodo and figshare record APIs, to turn a record link into direct file download URLs.
"""

from __future__ import annotations

import asyncio
import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

from dataqueryweb import extract
from dataqueryweb.extract import DataFile, Source

EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
BIORXIV_API = "https://api.biorxiv.org"
ANNOTATIONS = "https://www.ebi.ac.uk/europepmc/annotations_api/annotationsByArticleIds"
UA = "dataqueryweb/0.1 (QTL data-source finder; https://github.com/liufei-f/dataqueryweb)"
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
PREPRINT_FILTER = 'SRC:PPR AND (PUBLISHER:"bioRxiv" OR PUBLISHER:"medRxiv")'
SOURCE_FILTERS = {"all": "", "journal": "NOT SRC:PPR", "preprint": PREPRINT_FILTER}
# bioRxiv/medRxiv DOIs: 10.1101 until late 2025, 10.64898 since.
PREPRINT_DOI_RE = re.compile(r"^10\.(?:1101|64898)/")
QTL_FILTER = '(QTL OR eQTL OR sQTL OR pQTL OR caQTL OR mQTL OR meQTL OR "quantitative trait loci")'

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>?#]+")
PMCID_RE = re.compile(r"\bPMC\d+\b", re.I)
PMID_URL_RE = re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)")
NATURE_RE = re.compile(r"nature\.com/articles/([a-z0-9.\-]+)", re.I)


@dataclass
class Paper:
    title: str = ""
    authors: str = ""
    journal: str = ""
    year: str = ""
    doi: str = ""
    pmid: str = ""
    pmcid: str = ""
    url: str = ""
    open_access: bool = False
    abstract: str = ""
    epmc_id: str = ""  # Europe PMC id; "PPR…" for preprints
    preprint_server: str = ""  # "bioRxiv" / "medRxiv" / other preprint server
    published_doi: str = ""  # journal version of a preprint
    published_journal: str = ""
    qtl_types: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)  # which routes were read
    availability: list[str] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    note: str = ""
    ai: dict[str, Any] | None = None  # llm.Judge verdict, when requested
    # Readable text for the LLM ("[Section] paragraph"), and whether it is the full article.
    paragraphs: list[str] = field(default_factory=list)
    full_text: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "authors": self.authors,
            "journal": self.journal,
            "year": self.year,
            "doi": self.doi,
            "pmid": self.pmid,
            "pmcid": self.pmcid,
            "url": self.url,
            "open_access": self.open_access,
            "preprint_server": self.preprint_server,
            "published_doi": self.published_doi,
            "published_journal": self.published_journal,
            "qtl_types": self.qtl_types,
            "evidence": self.evidence,
            "availability": self.availability[:4],
            "sources": [s.to_dict() for s in self.sources],
            "note": self.note,
            "ai": self.ai,
            "full_text": self.full_text,
            "text_chars": sum(len(p) for p in self.paragraphs),
        }


def paper_from_epmc(raw: dict[str, Any]) -> Paper:
    doi = str(raw.get("doi") or "")
    pmcid = str(raw.get("pmcid") or "")
    pmid = str(raw.get("pmid") or "")
    url = (
        f"https://doi.org/{doi}"
        if doi
        else f"https://europepmc.org/article/PMC/{pmcid}"
        if pmcid
        else f"https://europepmc.org/article/MED/{pmid}"
        if pmid
        else ""
    )
    publisher = str((raw.get("bookOrReportDetails") or {}).get("publisher") or "")
    return Paper(
        epmc_id=str(raw.get("id") or ""),
        preprint_server=publisher if raw.get("source") == "PPR" else "",
        title=re.sub(r"<[^>]+>", "", html.unescape(raw.get("title") or "")).strip().rstrip("."),
        authors=str(raw.get("authorString") or ""),
        journal=str(
            raw.get("journalTitle")
            or ((raw.get("journalInfo") or {}).get("journal") or {}).get("title")
            or publisher
        ),
        year=str(raw.get("pubYear") or ""),
        doi=doi,
        pmid=pmid,
        pmcid=pmcid,
        url=url,
        open_access=str(raw.get("isOpenAccess") or "N") == "Y",
        abstract=str(raw.get("abstractText") or ""),
    )


# ── reference parsing ─────────────────────────────────────────────────────────


@dataclass
class Ref:
    doi: str = ""
    pmid: str = ""
    pmcid: str = ""
    url: str = ""


def parse_ref(text: str) -> Ref:
    """Turn whatever the user pasted — DOI, doi.org/publisher/PMC/PubMed URL, PMID — into IDs."""
    text = unquote(text.strip())
    if m := PMCID_RE.search(text):
        return Ref(pmcid=m.group(0).upper(), url=text if "://" in text else "")
    if m := PMID_URL_RE.search(text):
        return Ref(pmid=m.group(1), url=text)
    if re.fullmatch(r"(?:PMID:?\s*)?\d{6,9}", text, re.I):
        return Ref(pmid=re.sub(r"\D", "", text))
    if m := DOI_RE.search(text):
        doi = m.group(0).rstrip(".,;)")
        # bioRxiv/medRxiv URLs append a version and a view: 10.1101/2024.01.01.123v2.full
        doi = re.sub(r"(10\.(?:1101|64898)/[\d.]+)v\d+.*$", r"\1", doi)
        doi = re.sub(r"\.(?:full|abstract|pdf)(?:-text)?$", "", doi)
        return Ref(doi=doi, url=text if "://" in text else "")
    if m := NATURE_RE.search(text):
        return Ref(doi=f"10.1038/{m.group(1)}", url=text)
    if "://" in text or text.startswith("www."):
        return Ref(url=extract.normalize_url(text))
    return Ref()


# ── HTTP ─────────────────────────────────────────────────────────────────────


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": UA},
        timeout=httpx.Timeout(40.0, connect=15.0),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=16),
    )


async def epmc_get(http: httpx.AsyncClient, params: dict[str, Any]) -> dict[str, Any]:
    """Europe PMC search, retried on its occasional 429/5xx (it recovers within seconds)."""
    for attempt in range(4):
        r = await http.get(f"{EPMC}/search", params=params)
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            await asyncio.sleep(2 * (attempt + 1))
            continue
        r.raise_for_status()
        return dict(r.json())
    raise httpx.HTTPError("unreachable")


EPMC_PAGE = 1000  # Europe PMC's maximum pageSize
SORTS = {"relevance": "", "newest": "FIRST_PDATE_D desc"}


def build_query(
    query: str,
    *,
    qtl_only: bool = True,
    source: str = "all",
    year_from: int | None = None,
    year_to: int | None = None,
) -> str:
    """The Europe PMC query string for the page's search options."""
    # Plain keywords match title/abstract, which ranks primary QTL studies above papers that
    # merely reuse QTL data; a query with its own field syntax (TITLE:, AUTH:, …) is left as is.
    q = query if ":" in query else f"TITLE_ABS:({query})"
    q = f"({q}) AND {QTL_FILTER}" if qtl_only else q
    if extra := SOURCE_FILTERS.get(source, ""):
        q = f"({q}) AND {extra}" if not extra.startswith("NOT") else f"({q}) {extra}"
    if year_from or year_to:
        q = f"({q}) AND PUB_YEAR:[{year_from or 1800} TO {year_to or 3000}]"
    return q


async def epmc_search(
    http: httpx.AsyncClient,
    query: str,
    *,
    limit: int,
    qtl_only: bool = True,
    source: str = "all",
    year_from: int | None = None,
    year_to: int | None = None,
    sort: str = "relevance",
    cursor: str = "*",
) -> tuple[int, list[Paper], str]:
    """Up to ``limit`` papers from ``cursor`` on, fetched in pages of up to 1000 with Europe
    PMC's cursorMark. Returns (hits, papers, the cursor where the next batch starts — "" when
    nothing is left), so a search can be continued past its first ``limit`` papers."""
    q = build_query(query, qtl_only=qtl_only, source=source, year_from=year_from, year_to=year_to)
    params: dict[str, Any] = {"query": q, "format": "json", "resultType": "core",
                              "cursorMark": cursor or "*"}
    if SORTS.get(sort):
        params["sort"] = SORTS[sort]
    papers: list[Paper] = []
    hits = 0
    following = ""
    while len(papers) < limit:
        params["pageSize"] = min(EPMC_PAGE, limit - len(papers))
        data = await epmc_get(http, params)
        hits = int(data.get("hitCount") or 0)
        results = (data.get("resultList") or {}).get("result") or []
        papers += [paper_from_epmc(x) for x in results]
        following = str(data.get("nextCursorMark") or "")
        if not results or not following or following == params["cursorMark"]:
            following = ""
            break
        params["cursorMark"] = following
    return hits, papers[:limit], following


async def epmc_lookup(http: httpx.AsyncClient, ref: Ref) -> Paper | None:
    if ref.pmcid:
        q = f"PMCID:{ref.pmcid}"
    elif ref.pmid:
        q = f"EXT_ID:{ref.pmid} AND SRC:MED"
    elif ref.doi:
        q = f'DOI:"{ref.doi}"'
    else:
        return None
    data = await epmc_get(http, {"query": q, "format": "json", "resultType": "core"})
    results = (data.get("resultList") or {}).get("result") or []
    return paper_from_epmc(results[0]) if results else None


async def fetch_text(http: httpx.AsyncClient, url: str, *, browser: bool = False) -> str | None:
    try:
        r = await http.get(url, headers={"User-Agent": BROWSER_UA} if browser else None)
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    return r.text


async def biorxiv_details(
    http: httpx.AsyncClient, doi: str, server_hint: str = ""
) -> dict[str, Any] | None:
    """Latest-version metadata from the bioRxiv/medRxiv API, trying the hinted server first."""
    servers = ["medrxiv", "biorxiv"] if "med" in server_hint.lower() else ["biorxiv", "medrxiv"]
    for server in servers:
        try:
            r = await http.get(f"{BIORXIV_API}/details/{server}/{doi}")
            r.raise_for_status()
            collection = r.json().get("collection") or []
        except (httpx.HTTPError, ValueError):
            continue
        if collection:
            return dict(collection[-1])
    return None


async def biorxiv_published(http: httpx.AsyncClient, doi: str, server: str) -> dict[str, Any]:
    """The journal version of a preprint ({published_doi, published_journal}), if any."""
    try:
        r = await http.get(f"{BIORXIV_API}/pubs/{server.lower()}/{doi}")
        r.raise_for_status()
        collection = r.json().get("collection") or []
    except (httpx.HTTPError, ValueError):
        return {}
    return dict(collection[0]) if collection else {}


async def accession_annotations(http: httpx.AsyncClient, paper: Paper) -> list[Source]:
    ident = f"PMC:{paper.pmcid}" if paper.pmcid else f"MED:{paper.pmid}" if paper.pmid else ""
    if not ident:
        return []
    try:
        r = await http.get(
            ANNOTATIONS,
            params={"articleIds": ident, "type": "Accession Numbers", "format": "JSON"},
        )
        r.raise_for_status()
        payload = r.json()
    except (httpx.HTTPError, ValueError):
        return []
    out = []
    for article in payload or []:
        for ann in article.get("annotations") or []:
            acc = ann.get("exact") or ""
            text = f"{ann.get('prefix') or ''}{acc}{ann.get('postfix') or ''}"
            section = (ann.get("section") or "").lower()
            # Only data-repository accessions; the feed also carries rsIDs and cited DOIs.
            hits = extract.accession_sources(acc, in_availability=False)
            if not hits:
                continue
            s = hits[0]
            s.context = (
                extract.squash(text)
                if text.strip() != acc
                else (
                    "Accession text-mined from the article by Europe PMC; no surrounding sentence."
                )
            )
            s.qtl_context = bool(extract.QTL_CONTEXT_RE.search(text))
            s.in_availability = "availab" in section
            s.mined = True
            out.append(s)
    return out


# ── repository expansion (record page → file URLs) ────────────────────────────

ZENODO_RE = re.compile(r"zenodo\.(?:org/(?:records?|deposit)/|)(\d{5,})", re.I)
FIGSHARE_RE = re.compile(r"figshare\.(?:com/.*?/(\d{6,})|(\d{6,}))", re.I)


async def expand_files(http: httpx.AsyncClient, source: Source) -> None:
    """Fill ``source.files`` with direct download URLs for Zenodo/figshare records."""
    if source.files:  # already expanded (e.g. merged in from a published version)
        return
    try:
        if source.repository == "Zenodo" and (m := ZENODO_RE.search(source.url)):
            r = await http.get(f"https://zenodo.org/api/records/{m.group(1)}")
            if r.status_code == 200:
                for f in (r.json().get("files") or [])[:50]:
                    link = (f.get("links") or {}).get("self") or ""
                    source.files.append(DataFile(f.get("key", link), link, f.get("size")))
        elif source.repository == "figshare" and (m := FIGSHARE_RE.search(source.url)):
            fid = m.group(1) or m.group(2)
            r = await http.get(f"https://api.figshare.com/v2/articles/{fid}")
            if r.status_code == 200:
                for f in (r.json().get("files") or [])[:50]:
                    source.files.append(
                        DataFile(f.get("name", ""), f.get("download_url", ""), f.get("size"))
                    )
    except (httpx.HTTPError, ValueError):
        pass


# ── inspection ───────────────────────────────────────────────────────────────


def is_preprint(paper: Paper) -> bool:
    return bool(paper.preprint_server or PREPRINT_DOI_RE.match(paper.doi))


async def fill_preprint(http: httpx.AsyncClient, paper: Paper) -> None:
    """Add bioRxiv/medRxiv metadata and the published journal version, when there is one."""
    if not PREPRINT_DOI_RE.match(paper.doi) or any(e.endswith(" API") for e in paper.evidence):
        return
    info = await biorxiv_details(http, paper.doi, paper.preprint_server)
    if not info:
        return
    paper.evidence.append(f"{info.get('server') or 'bioRxiv'} API")
    paper.preprint_server = str(info.get("server") or paper.preprint_server or "bioRxiv")
    paper.title = paper.title if paper.title and paper.title != paper.doi else str(info["title"])
    paper.authors = paper.authors or str(info.get("authors") or "")
    paper.year = paper.year or str(info.get("date") or "")[:4]
    paper.abstract = paper.abstract or str(info.get("abstract") or "")
    paper.journal = paper.journal or paper.preprint_server
    published = str(info.get("published") or "")
    if published and published != "NA":
        paper.published_doi = published
        pub = await biorxiv_published(http, paper.doi, paper.preprint_server)
        paper.published_journal = str(pub.get("published_journal") or "")


async def inspect(
    http: httpx.AsyncClient, paper: Paper, *, page_url: str = "", follow_published: bool = True
) -> Paper:
    """Read every reachable piece of the paper and fill ``paper.sources``."""
    sources: list[Source] = []
    if is_preprint(paper):
        await fill_preprint(http, paper)
    text_for_types = f"{paper.title} {paper.abstract}"

    routes = []
    if paper.pmcid:
        routes += [
            ("Europe PMC full text", f"{EPMC}/{paper.pmcid}/fullTextXML"),
            ("PMC full text", f"{EFETCH}?db=pmc&id={paper.pmcid.removeprefix('PMC')}"),
        ]
    if paper.epmc_id.startswith("PPR"):
        routes.append(("Europe PMC preprint full text", f"{EPMC}/{paper.epmc_id}/fullTextXML"))
    if routes:
        for route, url in routes:
            xml = await fetch_text(http, url)
            if not xml or "<body" not in xml:
                continue
            try:
                ex = extract.parse_jats(xml, paper.pmcid)
            except ET.ParseError:  # malformed XML — try the next route
                continue
            if ex:
                paper.evidence.append(route)
                sources += ex.sources
                paper.availability += ex.availability
                paper.qtl_types = ex.qtl_types
                paper.paragraphs = ex.paragraphs
                paper.full_text = True
                break

    ann = await accession_annotations(http, paper)
    if ann:
        paper.evidence.append("Europe PMC accession annotations")
        sources += ann

    # A preprint that became a paper: the journal version usually has the final data statement.
    if follow_published and paper.published_doi:
        pub = await epmc_lookup(http, Ref(doi=paper.published_doi)) or Paper(
            doi=paper.published_doi, url=f"https://doi.org/{paper.published_doi}"
        )
        pub = await inspect(http, pub, follow_published=False)
        paper.published_journal = paper.published_journal or pub.journal
        label = f"published version ({pub.journal or paper.published_doi})"
        paper.evidence += [f"{label}: {e}" for e in pub.evidence]
        for s in pub.sources:
            s.context = f"[{pub.journal or 'Published version'}] {s.context}"
        sources += pub.sources
        paper.availability += pub.availability
        if pub.full_text and not paper.full_text:
            paper.paragraphs = [f"[{label}] {p}" for p in pub.paragraphs]
            paper.full_text = True

    have_full_text = any("full text" in e for e in paper.evidence)
    if not have_full_text and is_preprint(paper) and PREPRINT_DOI_RE.match(paper.doi):
        # biorxiv.org / medrxiv.org answer automated requests with 403; don't pretend otherwise.
        if not sources:
            paper.note = (
                f"{paper.preprint_server or 'bioRxiv'} does not allow automated full-text "
                "downloads and Europe PMC has no full text for this preprint — open it and "
                "check its Data availability section."
            )
    elif not have_full_text:
        url = page_url or (f"https://doi.org/{paper.doi}" if paper.doi else "")
        if url:
            page = await fetch_text(http, url, browser=True)
            if page:
                ex = extract.parse_html(page, url)
                paper.evidence.append(f"publisher page ({urlsplit(url).netloc})")
                sources += ex.sources
                paper.availability += ex.availability
                paper.paragraphs = ex.paragraphs
                # A landing page often holds only the abstract; ~15k chars means an article.
                paper.full_text = sum(len(p) for p in ex.paragraphs) > 15000
                if not paper.qtl_types:
                    paper.qtl_types = ex.qtl_types
            else:
                paper.note = (
                    "No open full text in PMC and the publisher page refused an "
                    "automated request — open the article and check its Data availability section."
                )

    if not paper.qtl_types:
        paper.qtl_types = extract.qtl_types(text_for_types)
    paper.sources = extract.merge(sources)
    await asyncio.gather(*(expand_files(http, s) for s in paper.sources if s.relevance != "low"))
    if not paper.sources and not paper.note:
        paper.note = "No data repository, accession or download link was found in the text read."
    return paper


async def inspect_ref(http: httpx.AsyncClient, text: str) -> Paper:
    """Inspect a paper given as a DOI, URL, PMID or PMCID."""
    ref = parse_ref(text)
    if not (ref.doi or ref.pmid or ref.pmcid or ref.url):
        raise ValueError("Enter a DOI, an article URL, a PMID or a PMCID.")

    # A link straight to a data repository is itself the answer.
    if ref.url and not (ref.doi or ref.pmid or ref.pmcid):
        klass = extract.classify_url(ref.url)
        if klass:
            repo, access, kind = klass
            s = Source(
                url=ref.url,
                label=ref.url,
                repository=repo,
                kind=kind,
                access=access,
                qtl_context=True,
                in_availability=True,
            )
            await expand_files(http, s)
            return Paper(
                title=ref.url, url=ref.url, sources=[s], evidence=["direct repository link"]
            )
        # An arbitrary article page: try to find its DOI to reach Europe PMC.
        page = await fetch_text(http, ref.url, browser=True)
        doi = extract.page_meta_doi(page) if page else ""
        if doi:
            ref.doi = doi

    epmc_down = False
    try:
        paper = await epmc_lookup(http, ref)
    except httpx.HTTPError:  # Europe PMC outage: carry on with the other routes
        paper, epmc_down = None, True
    if paper is None and ref.doi and PREPRINT_DOI_RE.match(ref.doi):
        # Very new preprints reach the bioRxiv API before Europe PMC indexes them.
        paper = Paper(doi=ref.doi, url=f"https://doi.org/{ref.doi}")
        await fill_preprint(http, paper)
        if not paper.preprint_server:
            paper = None
    if paper is None:
        paper = Paper(
            title=ref.doi or ref.url,
            doi=ref.doi,
            pmid=ref.pmid,
            pmcid=ref.pmcid,
            url=ref.url or (f"https://doi.org/{ref.doi}" if ref.doi else ""),
        )
        paper.note = "Not indexed in Europe PMC; read the publisher page only."
    if epmc_down:
        paper.evidence.append("Europe PMC unavailable (server error) — other routes only")
    result = await inspect(http, paper, page_url=ref.url if not ref.pmcid else "")
    return result
