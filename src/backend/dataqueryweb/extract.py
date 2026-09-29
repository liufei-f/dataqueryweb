"""Pure text/markup parsing: find where a paper says its QTL data can be downloaded.

Nothing here touches the network, so every rule can be unit-tested against a snippet. The
inputs are Europe PMC full-text JATS XML, or a publisher's HTML page when no full text exists;
the output is a list of :class:`Source` — a link or accession with the sentence it came from
and a judgement of how likely it is to hold the paper's QTL results.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from typing import Any, ClassVar
from urllib.parse import urlsplit

XLINK = "{http://www.w3.org/1999/xlink}href"

# ── vocabulary ────────────────────────────────────────────────────────────────

QTL_TYPE_RE = re.compile(
    r"\b((?:sc|sn|cis-|trans-)?(?:e|s|p|ca|m|me|h|ha|hm|apa|ribo|mi|iso|tu|circ|x|ct|ed|re|u)?QTL)s?\b"
)
# Words that make a sentence about a link likely to be about the paper's own QTL results.
QTL_CONTEXT_RE = re.compile(
    r"QTL|summary[ -]statistic|sumstat|full (?:association )?results|association results"
    r"|nominal (?:p|association)|fine[- ]mapping|credible set|colocali[sz]ation",
    re.IGNORECASE,
)
# Words that make it a statement about *sharing* data, not merely using a resource.
SHARE_CONTEXT_RE = re.compile(
    r"available|availability|download|deposited|accessible|can be (?:found|accessed|obtained|browsed)"
    r"|released|hosted|we (?:provide|make|share)|are provided|browser|portal",
    re.IGNORECASE,
)
SUMSTAT_RE = re.compile(
    r"summary[ -]statistic|sumstat|full (?:association )?results|association results|QTL (?:results|data)",
    re.IGNORECASE,
)
OWN_RE = re.compile(
    r"\b(?:our|we|this study|the present study|generated (?:in|by|during) this|of this (?:study|work|paper))\b",
    re.IGNORECASE,
)
# Phrases that say the paper *used* someone else's data rather than sharing its own.
REUSE_RE = re.compile(
    r"(?<!be )(?<!are )(?<!is )\b(?:downloaded|obtained|retrieved)\b|were accessed"
    r"|(?:we|was|were) used|accessed (?:on|via|in)|previous(?:ly)? (?:published|reported)"
    r"|from previous|publicly available (?:GWAS|data from)|(?<!can be )(?<!may be )\bused (?:for|in|to)\b"
    r"|originat\w* from|derived from|sourced from|taken from|\busing (?:the )?[A-Z][\w-]*\b",
    re.IGNORECASE,
)
# A paragraph that is itself an availability statement: "Data and materials availability: …".
AVAILABILITY_LEAD_RE = re.compile(
    r"^\W*(?:data|code|materials|software)(?:,? (?:and )?(?:data|code|materials|software))* "
    r"(?:availability|access)\b",
    re.IGNORECASE,
)
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.;!?])\s+(?=[A-Z(\[])")
CODE_HOSTS = ("GitHub", "GitLab")
CODE_CONTEXT_RE = re.compile(
    r"\b(?:pipelines?|scripts?|source code|code|software|packages?|tools?|workflows?)\b",
    re.IGNORECASE,
)
AVAILABILITY_TITLE_RE = re.compile(
    r"data (?:and (?:code|software|materials) )?(?:availability|access|sharing|deposition)"
    r"|availability of (?:data|supporting)|accession (?:codes|numbers)|code availability"
    r"|data records|resource availability|summary statistics",
    re.IGNORECASE,
)
BARE_DOI_RE = re.compile(r"(?<![/\w.])10\.\d{4,9}/[^\s\"<>()\[\],;]+[^\s\"<>()\[\],;.]")
URL_RE = re.compile(r"(?:https?://|ftp://|www\.)[^\s<>\"'()\[\]{},;]+[^\s<>\"'()\[\]{},;.:]")

# (pattern, repository, landing-page template, access)
ACCESSIONS: list[tuple[re.Pattern[str], str, str, str]] = [
    (
        re.compile(r"\bGSE\d{4,}\b"),
        "GEO",
        "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={}",
        "open",
    ),
    (re.compile(r"\bEGA[SD]\d{11}\b"), "EGA", "https://ega-archive.org/{kind}/{}", "controlled"),
    (
        re.compile(r"\bphs\d{6}(?:\.v\d+\.p\d+)?\b"),
        "dbGaP",
        "https://www.ncbi.nlm.nih.gov/projects/gap/cgi-bin/study.cgi?study_id={}",
        "controlled",
    ),
    (
        re.compile(r"\bE-[A-Z]{4}-\d+\b"),
        "ArrayExpress",
        "https://www.ebi.ac.uk/biostudies/arrayexpress/studies/{}",
        "open",
    ),
    (
        re.compile(r"\bGCST\d{6,}\b"),
        "GWAS Catalog",
        "https://www.ebi.ac.uk/gwas/studies/{}",
        "open",
    ),
    (
        re.compile(r"\bQT[DS]\d{6}\b"),
        "eQTL Catalogue",
        "https://www.ebi.ac.uk/eqtl/Studies/",
        "open",
    ),
    (
        re.compile(r"\bPRJ(?:NA|EB|DB)\d+\b"),
        "BioProject",
        "https://www.ncbi.nlm.nih.gov/bioproject/{}",
        "open",
    ),
    (
        re.compile(r"\b(?:SRP|ERP|DRP)\d{6,}\b"),
        "SRA/ENA",
        "https://www.ebi.ac.uk/ena/browser/view/{}",
        "open",
    ),
    (re.compile(r"\bsyn\d{7,}\b"), "Synapse", "https://www.synapse.org/#!Synapse:{}", "request"),
    (
        re.compile(r"\bHRA\d{6}\b"),
        "GSA-Human",
        "https://ngdc.cncb.ac.cn/gsa-human/browse/{}",
        "controlled",
    ),
    (re.compile(r"\b10\.5281/zenodo\.\d+\b", re.I), "Zenodo", "https://doi.org/{}", "open"),
    (
        re.compile(r"\b10\.6084/m9\.figshare\.[\w.]+\b", re.I),
        "figshare",
        "https://doi.org/{}",
        "open",
    ),
    (re.compile(r"\b10\.5061/dryad\.\w+\b", re.I), "Dryad", "https://doi.org/{}", "open"),
]

# (host fragment, repository, access). Order matters: first match wins.
REPOSITORIES: list[tuple[str, str, str]] = [
    ("zenodo.org", "Zenodo", "open"),
    ("10.5281/zenodo", "Zenodo", "open"),
    ("figshare.com", "figshare", "open"),
    ("10.6084/m9.figshare", "figshare", "open"),
    ("datadryad.org", "Dryad", "open"),
    ("10.5061/dryad", "Dryad", "open"),
    ("ebi.ac.uk/eqtl", "eQTL Catalogue", "open"),
    ("ftp.ebi.ac.uk/pub/databases/spot/eqtl", "eQTL Catalogue", "open"),
    ("ebi.ac.uk/gwas", "GWAS Catalog", "open"),
    ("ftp.ebi.ac.uk", "EBI FTP", "open"),
    ("gtexportal.org", "GTEx Portal", "open"),
    ("eqtlgen.org", "eQTLGen", "open"),
    ("onek1k.org", "OneK1K", "open"),
    ("ega-archive.org", "EGA", "controlled"),
    ("ncbi.nlm.nih.gov/geo", "GEO", "open"),
    ("ncbi.nlm.nih.gov/gap", "dbGaP", "controlled"),
    ("ncbi.nlm.nih.gov/projects/gap", "dbGaP", "controlled"),
    ("ncbi.nlm.nih.gov/bioproject", "BioProject", "open"),
    ("synapse.org", "Synapse", "request"),
    ("biostudies", "BioStudies", "open"),
    ("osf.io", "OSF", "open"),
    ("data.mendeley.com", "Mendeley Data", "open"),
    ("dataverse", "Dataverse", "open"),
    ("cellxgene", "CELLxGENE", "open"),
    ("humancellatlas.org", "HCA", "open"),
    ("ngdc.cncb.ac.cn", "NGDC", "controlled"),
    ("drive.google.com", "Google Drive", "open"),
    ("dropbox.com", "Dropbox", "open"),
    ("box.com", "Box", "open"),
    ("huggingface.co", "Hugging Face", "open"),
    ("github.com", "GitHub", "open"),
    ("gitlab.com", "GitLab", "open"),
]
# Links that are almost never the data: journal plumbing, identifiers, software homepages.
NOISE_HOSTS = (
    "creativecommons.org",
    "orcid.org",
    "crossref.org",
    "europepmc.org",
    "pubmed.ncbi",
    "ncbi.nlm.nih.gov/pmc",
    "pmc.ncbi",
    "scholar.google",
    "twitter.com",
    "cran.r-project.org",
    "bioconductor.org",
    "pypi.org",
)
DOWNLOAD_EXT_RE = re.compile(
    r"\.(?:tsv|txt|csv|gz|bgz|zip|tar|parquet|h5|rds|rda|xlsx?)(?:\?|$)", re.I
)


@dataclass
class DataFile:
    name: str
    url: str
    size: int | None = None


@dataclass
class Source:
    url: str
    label: str
    repository: str
    kind: str  # "repository" | "accession" | "supplement" | "file" | "link"
    access: str  # "open" | "controlled" | "request" | "unknown"
    context: str = ""
    in_availability: bool = False
    qtl_context: bool = False
    files: list[DataFile] = field(default_factory=list)
    mined: bool = False  # from Europe PMC's text-mined accessions, which carry no sentence
    ai_pick: str = ""  # set by llm.apply: what the LLM says this location holds
    ai_note: str = ""

    @property
    def relevance(self) -> str:
        """high: very likely the paper's own QTL results; medium: worth a look; low: incidental."""
        ctx = self.context
        share = bool(SHARE_CONTEXT_RE.search(ctx))
        sumstat = bool(SUMSTAT_RE.search(ctx))
        own = bool(OWN_RE.search(ctx))
        reuse = bool(REUSE_RE.search(ctx))
        about_qtl = self.qtl_context or sumstat
        code = self.repository in CODE_HOSTS or bool(CODE_CONTEXT_RE.search(ctx))
        if code:  # usually software, unless the sentence says it holds results
            if sumstat and not reuse and (self.in_availability or (share and own)):
                return "high"
            return "medium" if self.in_availability else "low"
        if about_qtl and not reuse and (self.in_availability or (share and own)):
            return "high"
        if (
            (self.in_availability and not reuse)
            or self.mined
            or (about_qtl and share and not reuse)
        ):
            return "medium"
        return "low"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"relevance": self.relevance}


# ── helpers ───────────────────────────────────────────────────────────────────


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def snippet(text: str, needle: str, width: int = 260) -> str:
    """The sentence-ish window of ``text`` around ``needle``."""
    text = squash(text)
    i = text.find(needle)
    if i < 0 or len(text) <= width:
        return text[:width]
    start = max(0, i - width // 2)
    end = min(len(text), i + len(needle) + width // 2)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def sentence(text: str, needle: str, limit: int = 420) -> str:
    """The sentence of ``text`` containing ``needle`` (plus the previous one when short)."""
    text = squash(text)
    parts = SENTENCE_SPLIT_RE.split(text)
    for i, part in enumerate(parts):
        if needle and needle in part:
            if len(part) < 90 and i:
                part = parts[i - 1] + " " + part
            return part if len(part) <= limit else snippet(part, needle, limit)
    return snippet(text, needle, 260)


def qtl_types(text: str) -> list[str]:
    found: dict[str, None] = {}
    for m in QTL_TYPE_RE.finditer(text):
        name = m.group(1)
        name = name.removeprefix("cis-").removeprefix("trans-")
        found.setdefault(name, None)
    return sorted(found)


def normalize_url(url: str) -> str:
    url = url.strip().rstrip(".,;)")
    if url.startswith("www."):
        url = "https://" + url
    return url


def classify_url(url: str) -> tuple[str, str, str] | None:
    """(repository, access, kind) for a data-bearing link, or None for noise."""
    low = url.lower()
    if any(n in low for n in NOISE_HOSTS):
        return None
    for fragment, repo, access in REPOSITORIES:
        if fragment in low:
            kind = "file" if DOWNLOAD_EXT_RE.search(low) else "repository"
            return repo, access, kind
    if low.startswith("ftp://") or DOWNLOAD_EXT_RE.search(low):
        return urlsplit(url).netloc or "FTP", "open", "file"
    return None


def accession_sources(text: str, *, in_availability: bool) -> list[Source]:
    out = []
    for pattern, repo, template, access in ACCESSIONS:
        for m in pattern.finditer(text):
            acc = m.group(0)
            kind = "studies" if acc.startswith("EGAS") else "datasets"
            url = template.replace("{kind}", kind).format(acc)
            ctx = sentence(text, acc)
            out.append(
                Source(
                    url=url,
                    label=acc,
                    repository=repo,
                    kind="accession",
                    access=access,
                    context=ctx,
                    in_availability=in_availability,
                    qtl_context=bool(QTL_CONTEXT_RE.search(ctx)),
                )
            )
    return out


def link_source(url: str, text: str, *, anchor: str = "", in_availability: bool) -> Source | None:
    raw, url = url.strip(), normalize_url(url)
    if not url.lower().startswith(("http", "ftp")):
        return None
    klass = classify_url(url)
    label = squash(anchor)
    ctx = sentence(text, label if label and label in squash(text) else raw)
    qtl = bool(QTL_CONTEXT_RE.search(ctx))
    if klass is None:
        # An unrecognised host is only kept when the paper itself frames it as the data.
        sharing = qtl or SHARE_CONTEXT_RE.search(ctx) or "deposited" in ctx
        if not (in_availability and sharing and not REUSE_RE.search(ctx)):
            return None
        host = urlsplit(url).netloc
        klass = (
            ("Dataset DOI", "unknown", "repository")
            if host == "doi.org"
            else (host, "unknown", "link")
        )
    repo, access, kind = klass
    return Source(
        url=url,
        label=squash(anchor) or url,
        repository=repo,
        kind=kind,
        access=access,
        context=ctx,
        in_availability=in_availability,
        qtl_context=qtl,
    )


def merge(sources: list[Source]) -> list[Source]:
    """Deduplicate by URL, keeping the strongest evidence, strongest first."""
    rank = {"high": 0, "medium": 1, "low": 2}
    best: dict[str, Source] = {}
    for s in sources:
        key = re.sub(r"^\w+://(?:www\.)?", "", s.url).rstrip("/").lower()
        prev = best.get(key)
        if prev is None:
            best[key] = s
            continue
        prev.in_availability |= s.in_availability
        if s.qtl_context and not prev.qtl_context:
            prev.qtl_context, prev.context = True, s.context
        if prev.label == prev.url and s.label != s.url:
            prev.label = s.label
    return sorted(
        best.values(),
        key=lambda s: (
            rank[s.relevance],
            not s.in_availability,
            bool(REUSE_RE.search(s.context)),
            s.kind not in ("repository", "file"),
        ),
    )


# ── JATS full text (Europe PMC fullTextXML) ──────────────────────────────────


@dataclass
class Extraction:
    sources: list[Source]
    availability: list[str]
    qtl_types: list[str]
    # The readable text, one "[Section] paragraph" per item, for the LLM.
    paragraphs: list[str] = field(default_factory=list)


def _title(el: ET.Element) -> str:
    t = el.find("title")
    return squash("".join(t.itertext())) if t is not None else ""


def parse_jats(xml: str | bytes, pmcid: str = "") -> Extraction:
    root = ET.fromstring(xml)
    parent = {c: p for p in root.iter() for c in p}

    def in_availability(el: ET.Element) -> bool:
        node: ET.Element | None = el
        while node is not None:
            if node.tag == "supplementary-material":
                return False
            if node.tag in ("sec", "notes", "fn-group", "app"):
                kind = (node.get("sec-type") or node.get("notes-type") or "").lower()
                if (
                    "availab" in kind
                    or "data" in kind
                    or AVAILABILITY_TITLE_RE.search(_title(node))
                ):
                    return True
            node = parent.get(node)
        return False

    # Some manuscripts mark the statement with a plain paragraph instead of a titled section:
    # <fn><p>Data and Materials Availability</p><p>…</p></fn>. Its following siblings count.
    after_heading: set[ET.Element] = set()
    for node in root.iter():
        opened = False
        for child in node:
            if child.tag != "p":
                continue
            text = squash("".join(child.itertext()))
            if len(text) < 60 and AVAILABILITY_LEAD_RE.match(text):
                opened = True
            elif opened:
                after_heading.add(child)

    sources: list[Source] = []
    availability: list[str] = []

    def section_of(el: ET.Element) -> str:
        node = parent.get(el)
        while node is not None:
            if node.tag == "sec" and _title(node):
                return _title(node)
            if node.tag in ("abstract", "supplementary-material", "ref-list"):
                return node.tag.replace("-", " ").capitalize()
            node = parent.get(node)
        return ""

    paragraphs: list[str] = []
    for p in root.iter("p"):
        text = squash("".join(p.itertext()))
        if not text:
            continue
        section = section_of(p)
        if section != "Ref list":
            paragraphs.append(f"[{section}] {text}" if section else text)
        avail = p in after_heading or in_availability(p) or bool(AVAILABILITY_LEAD_RE.match(text))
        if avail:
            availability.append(text)
        seen = set()
        for link in p.iter("ext-link"):
            href = link.get(XLINK) or "".join(link.itertext())
            if link.get("ext-link-type") == "doi" and not href.startswith("http"):
                href = "https://doi.org/" + href
            seen.add(normalize_url(href))
            s = link_source(href, text, anchor="".join(link.itertext()), in_availability=avail)
            if s:
                sources.append(s)
        for m in URL_RE.finditer(text):
            if normalize_url(m.group(0)) not in seen:
                s = link_source(m.group(0), text, in_availability=avail)
                if s:
                    sources.append(s)
        if avail:
            # Dataset DOIs are often written bare: "deposited in OWEY (10.48802/owey.e4qn-9190)".
            for m in BARE_DOI_RE.finditer(text):
                url = "https://doi.org/" + m.group(0).rstrip(".,;)")
                if url not in seen:
                    seen.add(url)
                    s = link_source(url, text, anchor=m.group(0), in_availability=True)
                    if s:
                        sources.append(s)
        if avail or QTL_CONTEXT_RE.search(text):
            sources.extend(accession_sources(text, in_availability=avail))

    for supp in root.iter("supplementary-material"):
        caption = squash("".join(supp.itertext()))
        media = supp.find(".//media")
        href = (media.get(XLINK) if media is not None else None) or supp.get(XLINK)
        if not href or not QTL_CONTEXT_RE.search(caption):
            continue
        url = (
            href
            if href.startswith("http")
            else (f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/bin/{href}" if pmcid else "")
        )
        if url:
            label = _title(supp) or (supp.findtext("label") or href)
            sources.append(
                Source(
                    url=url,
                    label=squash(label),
                    repository="Supplementary material",
                    kind="supplement",
                    access="open",
                    context=caption[:260],
                    in_availability=False,
                    qtl_context=True,
                )
            )

    body = squash(" ".join(root.itertext()))
    return Extraction(merge(sources), availability, qtl_types(body), paragraphs)


# ── publisher HTML (fallback when there is no open full text) ────────────────


class _PageParser(HTMLParser):
    SKIP: ClassVar[set[str]] = {"script", "style", "noscript", "svg", "nav", "header", "footer"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.size = 0
        self.links: list[tuple[str, int, list[str]]] = []  # href, text offset, anchor parts
        self.meta: dict[str, str] = {}
        self._skip = 0
        self._anchor: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "meta" and a.get("name"):
            self.meta.setdefault(a["name"].lower(), a.get("content", ""))
        elif tag == "a" and a.get("href") and not self._skip:
            self._anchor = []
            self.links.append((a["href"], self.size, self._anchor))
        if tag in ("p", "div", "section", "h1", "h2", "h3", "h4", "li", "br", "tr"):
            self._emit("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "a":
            self._anchor = None

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        self._emit(data)
        if self._anchor is not None:
            self._anchor.append(data)

    def _emit(self, s: str) -> None:
        self.parts.append(s)
        self.size += len(s)


def page_meta_doi(html: str) -> str:
    p = _PageParser()
    p.feed(html)
    for key in ("citation_doi", "dc.identifier", "prism.doi", "dc.identifier.doi"):
        value = p.meta.get(key, "")
        m = re.search(r"10\.\d{4,9}/\S+", value)
        if m:
            return m.group(0)
    return ""


def parse_html(html: str, base_url: str = "") -> Extraction:
    from urllib.parse import urljoin

    p = _PageParser()
    p.feed(html)
    text = "".join(p.parts)
    # Every "Data availability"-like heading opens a ~3000-character window.
    windows = [
        (m.start(), m.start() + 3000)
        for m in re.finditer(r"\n\s*(?:" + AVAILABILITY_TITLE_RE.pattern + r")\s*\n", text, re.I)
    ]

    def avail(pos: int) -> bool:
        return any(a <= pos < b for a, b in windows)

    availability = [squash(text[a:b])[:1200] for a, b in windows[:3]]
    sources: list[Source] = []
    for href, pos, anchor in p.links:
        # The link's own block (paragraph, list item, table row) — block tags emit newlines.
        start = text.rfind("\n", 0, pos) + 1
        end = text.find("\n", pos)
        context = text[start : end if end >= 0 else len(text)][:1500]
        url = urljoin(base_url, href) if base_url else href
        s = link_source(url, context, anchor="".join(anchor), in_availability=avail(pos))
        if s:
            sources.append(s)
    for a, b in windows:
        sources.extend(accession_sources(text[a:b], in_availability=True))
    paragraphs = [squash(b) for b in text.split("\n") if len(squash(b)) > 60]
    return Extraction(merge(sources), availability, qtl_types(text), paragraphs)
