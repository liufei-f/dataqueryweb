"""Retrieve what a download location holds, without downloading it.

Two steps, both read-only and bounded:

1. :func:`list_location` — the files behind a saved download location: repository APIs
   (Zenodo, figshare, GitHub, Hugging Face, OSF), a direct file link, or the file links on a
   plain web page. A location with no machine-readable listing (Dropbox, Synapse, dbGaP, a
   results browser) comes back as ``manual`` with the reason, for a person to open.
2. :func:`probe_file` — the header and first rows of one file, from its first 256 KB:
   plain or gzip/bgzip/bz2/xz text, zip members and spreadsheets by HTTP range requests,
   tar archives from the head of the stream, and the column names of a parquet footer.

Synchronous on purpose (zipfile and tarfile want file objects); manifest.py runs it in a
thread. Nothing here interprets the columns — that is manifest_rules.py.
"""

from __future__ import annotations

import bz2
import html
import io
import lzma
import os
import re
import tarfile
import zipfile
import zlib
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx

from dataqueryweb import manifest_rules as rules

HEAD_BYTES = 256 * 1024
ARCHIVE_BUDGET = 48 * 1024 * 1024  # most bytes read from one archive
WHOLE_FILE_LIMIT = 64 * 1024 * 1024  # an archive this small is fetched whole if ranges fail
MAX_LISTED = 6000
SAMPLE_ROWS = 5
UA = "dataqueryweb/0.1 (QTL data-source finder; https://github.com/liufei-f/dataqueryweb)"
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


@dataclass
class RemoteFile:
    name: str
    url: str
    size: int | None = None
    path: str = ""  # path inside the location, when it has folders

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "url": self.url, "size": self.size, "path": self.path or self.name}


@dataclass
class Listing:
    status: str  # "listed" | "direct" | "manual" | "error"
    resolver: str = ""
    landing: str = ""
    note: str = ""
    files: list[RemoteFile] = field(default_factory=list)
    texts: dict[str, str] = field(default_factory=dict)  # repository description, README


@dataclass
class Table:
    name: str  # "" for the file itself; a zip/tar member or a sheet otherwise
    columns: list[str]
    rows: list[list[str]]
    has_header: bool = True
    delimiter: str = "\t"
    fmt: str = "text"  # "text" | "xlsx" | "parquet"
    preamble: str = ""  # title lines above the header (spreadsheets)
    n_rows: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "columns": self.columns, "rows": self.rows,
                "has_header": self.has_header, "delimiter": self.delimiter, "fmt": self.fmt,
                "preamble": self.preamble, "n_rows": self.n_rows}


@dataclass
class Probe:
    kind: str  # "table" | "archive" | "unreadable" | "error"
    tables: list[Table] = field(default_factory=list)
    members: list[str] = field(default_factory=list)
    note: str = ""
    compression: str = ""


def client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": UA, "Accept": "*/*"},
        timeout=httpx.Timeout(60.0, connect=15.0),
        follow_redirects=True,
    )


# ── listing ──────────────────────────────────────────────────────────────────

MANUAL_HOSTS = [
    (r"^ftp://", "FTP server: open it with an FTP client"),
    (r"dropbox\.com", "Dropbox folder: no public listing API"),
    (r"drive\.google\.com|docs\.google\.com", "Google Drive: no public listing API"),
    (r"synapse\.org", "Synapse: needs a Synapse account"),
    (r"ncbi\.nlm\.nih\.gov/(projects/)?gap|ega-archive\.org", "controlled access (dbGaP/EGA)"),
    (r"(^|\.)box\.com", "Box folder: no public listing API"),
    (r"ebi\.ac\.uk/gwas", "GWAS Catalog accession: open the study page for the harmonised file"),
    (r"datadryad\.org|10\.5061/dryad", "Dryad: downloads need a browser session"),
]
PMC_BIN_RE = re.compile(r"https?://(?:pmc\.ncbi\.nlm\.nih\.gov|www\.ncbi\.nlm\.nih\.gov/pmc)/articles/(PMC\d+)/bin/([^?#]+)")


_pmc_zips: dict[str, zipfile.ZipFile | None] = {}


def pmc_supplement(http: httpx.Client, url: str) -> bytes | None:
    """A PMC supplementary file, taken from Europe PMC's zip of the article's supplements.

    PMC answers scripts with a challenge page. The zip is fetched once per article and kept
    for the process; None when Europe PMC has none (author manuscripts) or it is too large.
    """
    m = PMC_BIN_RE.match(url)
    if not m:
        return None
    pmcid, name = m.group(1), unquote(m.group(2))
    if pmcid not in _pmc_zips:
        _pmc_zips[pmcid] = None
        try:
            with http.stream("GET", f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/supplementaryFiles",
                             timeout=180) as r:
                body = b"".join(_take(r.iter_bytes(), WHOLE_FILE_LIMIT + 1))
            if r.status_code == 200 and body[:2] == b"PK" and len(body) <= WHOLE_FILE_LIMIT:
                _pmc_zips[pmcid] = zipfile.ZipFile(io.BytesIO(body))
        except (httpx.HTTPError, zipfile.BadZipFile):
            pass
    z = _pmc_zips[pmcid]
    member = next((n for n in z.namelist() if n.rsplit("/", 1)[-1] == name), None) if z else None
    return z.read(member) if z and member else None


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def _name_of(url: str) -> str:
    return unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])


def list_location(http: httpx.Client, url: str) -> Listing:
    """Every file behind ``url``, or why they cannot be listed."""
    try:
        return _list(http, url, follow=True)
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        return Listing("error", landing=url, note=f"{type(exc).__name__}: {str(exc)[:160]}")


def _list(http: httpx.Client, url: str, *, follow: bool) -> Listing:
    for pattern, why in MANUAL_HOSTS:
        if re.search(pattern, url, re.I):
            return Listing("manual", landing=url, note=why)
    for pattern, resolver in RESOLVERS:
        m = re.search(pattern, url, re.I)
        if m:
            return resolver(http, url, m)
    name = _name_of(url)
    if rules.TABLE_EXT_RE.search(name) and "." in name:
        return Listing("direct", "direct file", url, files=[RemoteFile(name, url)])
    return _list_page(http, url, follow=follow)


def _zenodo(http: httpx.Client, url: str, m: re.Match[str]) -> Listing:
    rid = m.group(1) or m.group(2)
    r = http.get(f"https://zenodo.org/api/records/{rid}")
    if r.status_code != 200:  # a concept DOI names every version: take the newest
        s = http.get("https://zenodo.org/api/records",
                     params={"q": f"conceptrecid:{rid}", "sort": "mostrecent", "size": 1})
        hits = (s.json().get("hits") or {}).get("hits") or [] if s.status_code == 200 else []
        if not hits:
            return Listing("error", "Zenodo API", url, f"record {rid}: HTTP {r.status_code}")
        data = hits[0]
    else:
        data = r.json()
    meta = data.get("metadata") or {}
    landing = (data.get("links") or {}).get("self_html") or f"https://zenodo.org/records/{data.get('id', rid)}"
    files = [RemoteFile(f.get("key", ""), (f.get("links") or {}).get("self", ""), f.get("size"))
             for f in data.get("files") or []]
    out = Listing("listed", "Zenodo API", landing, files=files)
    text = f"{meta.get('title', '')}. {_strip_html(meta.get('description', ''))}".strip(". ")
    if text:
        out.texts["repository description"] = text[:6000]
    if not files:
        out.status = "manual"
        out.note = f"no files listed (access: {meta.get('access_right', 'unknown')})"
    return out


def _figshare(http: httpx.Client, url: str, m: re.Match[str]) -> Listing:
    fid = m.group(1) or m.group(2)
    r = http.get(f"https://api.figshare.com/v2/articles/{fid}")
    articles = [r.json()] if r.status_code == 200 else []
    if not articles:
        c = http.get(f"https://api.figshare.com/v2/collections/{fid}/articles", params={"page_size": 20})
        for item in c.json() if c.status_code == 200 else []:
            a = http.get(f"https://api.figshare.com/v2/articles/{item.get('id')}")
            if a.status_code == 200:
                articles.append(a.json())
    if not articles:
        return Listing("error", "figshare API", url, f"article {fid}: HTTP {r.status_code}")
    out = Listing("listed", "figshare API", articles[0].get("url_public_html") or url)
    for a in articles:
        prefix = f"{a.get('title', '')}/" if len(articles) > 1 else ""
        for f in a.get("files") or []:
            out.files.append(RemoteFile(f.get("name", ""), f.get("download_url", ""), f.get("size"),
                                        prefix + f.get("name", "")))
    text = " ".join(f"{a.get('title', '')}. {_strip_html(a.get('description', ''))}" for a in articles)
    if text.strip(". "):
        out.texts["repository description"] = text[:6000]
    return out


def _github(http: httpx.Client, url: str, m: re.Match[str]) -> Listing:
    owner, repo, ref, sub = m.group(1), m.group(2), m.group(3) or "HEAD", (m.group(4) or "").strip("/")
    headers = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    r = http.get(f"https://api.github.com/repos/{owner}/{repo}/git/trees/{ref}",
                 params={"recursive": 1}, headers=headers)
    if r.status_code != 200:
        return Listing("error", "GitHub API", url, f"HTTP {r.status_code} (set GITHUB_TOKEN if rate-limited)")
    out = Listing("listed", "GitHub API", f"https://github.com/{owner}/{repo}")
    for item in r.json().get("tree") or []:
        path = item.get("path", "")
        if item.get("type") != "blob" or (sub and not path.startswith(sub + "/") and path != sub):
            continue
        if rules.file_kind(path, item.get("size")) == "other":
            continue  # source code, figures, notebooks
        out.files.append(RemoteFile(path.rsplit("/", 1)[-1],
                                    f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{quote(path)}",
                                    item.get("size"), path))
    out.files = out.files[:MAX_LISTED]
    if not out.files:
        out.status, out.note = "manual", "the repository holds no data tables (code only?)"
    return out


def _huggingface(http: httpx.Client, url: str, m: re.Match[str]) -> Listing:
    repo, rev, sub = m.group(1), m.group(2) or "main", (m.group(3) or "").strip("/")
    out = Listing("listed", "Hugging Face API", f"https://huggingface.co/datasets/{repo}")
    nxt: str | None = f"https://huggingface.co/api/datasets/{repo}/tree/{rev}/{quote(sub)}?recursive=true&limit=1000"
    while nxt and len(out.files) < MAX_LISTED:
        r = http.get(nxt)
        if r.status_code != 200:
            if not out.files:
                return Listing("error", "Hugging Face API", url, f"HTTP {r.status_code}")
            break
        for item in r.json():
            if item.get("type") == "file":
                path = item.get("path", "")
                out.files.append(RemoteFile(
                    path.rsplit("/", 1)[-1],
                    f"https://huggingface.co/datasets/{repo}/resolve/{rev}/{quote(path)}",
                    item.get("size"), path))
        nxt = (r.links.get("next") or {}).get("url")
    readme = http.get(f"https://huggingface.co/datasets/{repo}/resolve/{rev}/README.md")
    if readme.status_code == 200:
        out.texts["repository README"] = readme.text[:8000]
    return out


def _osf(http: httpx.Client, url: str, m: re.Match[str]) -> Listing:
    node = m.group(1).lower()
    out = Listing("listed", "OSF API", f"https://osf.io/{node}/")
    todo = [(f"https://api.osf.io/v2/nodes/{node}/files/osfstorage/?page[size]=100", 0, "")]
    kids = http.get(f"https://api.osf.io/v2/nodes/{node}/children/?page[size]=50")
    for kid in (kids.json().get("data") or []) if kids.status_code == 200 else []:
        title = (kid.get("attributes") or {}).get("title", kid.get("id", ""))
        todo.append((f"https://api.osf.io/v2/nodes/{kid.get('id')}/files/osfstorage/?page[size]=100", 0,
                     f"{title}/"))
    while todo and len(out.files) < 400:
        link, depth, prefix = todo.pop(0)
        r = http.get(link)
        if r.status_code != 200:
            if not out.files:
                return Listing("manual", "OSF API", url, f"HTTP {r.status_code}: not a public project id")
            continue
        body = r.json()
        for item in body.get("data") or []:
            attr = item.get("attributes") or {}
            if attr.get("kind") == "folder" and depth < 3:
                rel = ((item.get("relationships") or {}).get("files") or {}).get("links") or {}
                href = (rel.get("related") or {}).get("href")
                if href:
                    todo.append((href, depth + 1, prefix))
            elif attr.get("kind") == "file":
                out.files.append(RemoteFile(attr.get("name", ""), (item.get("links") or {}).get("download", ""),
                                            attr.get("size"),
                                            prefix + (attr.get("materialized_path") or "").lstrip("/")))
        if (body.get("links") or {}).get("next"):
            todo.append((body["links"]["next"], depth, prefix))
    if not out.files:
        out.status, out.note = "manual", "the OSF project lists no files (components or private storage?)"
    return out


RESOLVERS: list[tuple[str, Any]] = [
    (r"zenodo\.org/(?:api/)?(?:records?|deposit)/(\d+)|zenodo\.(\d{4,})", _zenodo),
    (r"figshare\.com/.*?/(\d{6,})|figshare\.(\d{5,})", _figshare),
    (r"github\.com/([\w.\-]+)/([\w.\-]+?)(?:\.git)?(?:/(?:tree|blob)/([^/]+)(?:/(.*?))?)?/?$", _github),
    (r"huggingface\.co/datasets/([\w.\-]+/[\w.\-]+)(?:/(?:tree|blob|resolve)/([^/]+)(?:/(.*?))?)?/?$", _huggingface),
    (r"osf\.io/([a-z0-9]{5})(?![a-z0-9])", _osf),
]


def _list_page(http: httpx.Client, url: str, *, follow: bool) -> Listing:
    """A DOI or web page: follow it to a known repository, else collect its file links."""
    with http.stream("GET", url, headers={"User-Agent": BROWSER_UA}) as r:
        final = str(r.url)
        ctype = r.headers.get("content-type", "").lower()
        if r.status_code >= 400:
            return Listing("error", "web page", url, f"HTTP {r.status_code}")
        if "html" not in ctype and "xml" not in ctype:
            size = r.headers.get("content-length")
            disp = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', r.headers.get("content-disposition", ""))
            name = unquote(disp.group(1)) if disp else _name_of(final)
            return Listing("direct", "direct file", final,
                           files=[RemoteFile(name, final, int(size) if size and size.isdigit() else None)])
        page = b"".join(_take(r.iter_bytes(), 3_000_000)).decode(r.encoding or "utf-8", "replace")
    if follow and final != url:
        redirected = _list(http, final, follow=False)
        if redirected.status != "manual" or redirected.note != NO_LINKS:
            return redirected
    seen: dict[str, RemoteFile] = {}
    for href in re.findall(r"""href\s*=\s*["']([^"'#]+)["']""", page, re.I):
        link = urljoin(final, html.unescape(href))
        name = _name_of(link)
        if link.startswith("http") and "." in name and rules.file_kind(name, None) != "other":
            seen.setdefault(link, RemoteFile(name, link))
    if not seen:
        return Listing("manual", "web page", final, NO_LINKS)
    return Listing("listed", "links on the web page", final, files=list(seen.values())[:300])


NO_LINKS = "no downloadable file links on the page (results browser or script-built page?)"


def _take(chunks: Any, limit: int) -> Any:
    got = 0
    for chunk in chunks:
        yield chunk
        got += len(chunk)
        if got >= limit:
            return


# ── reading the head of a file ───────────────────────────────────────────────


def fetch_head(http: httpx.Client, url: str, n: int = HEAD_BYTES) -> tuple[bytes, int | None, bool]:
    """(first ``n`` bytes, total size if the server says, whether it honours Range)."""
    with http.stream("GET", url, headers={"Range": f"bytes=0-{n - 1}"}) as r:
        if r.status_code >= 400:
            raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
        data = b"".join(_take(r.iter_bytes(), n))[:n]
        ranged = r.status_code == 206
        total = None
        cr = re.search(r"/(\d+)$", r.headers.get("content-range", ""))
        if cr:
            total = int(cr.group(1))
        elif not ranged and r.headers.get("content-length", "").isdigit():
            total = int(r.headers["content-length"])
        return data, total, ranged


def sniff(data: bytes) -> str:
    if data[:4] == b"PK\x03\x04":
        return "zip"
    if data[:4] == b"PAR1":
        return "parquet"
    if data[:2] == b"\x1f\x8b":
        return "gz"
    if data[:3] == b"BZh":
        return "bz2"
    if data[:6] == b"\xfd7zXZ\x00":
        return "xz"
    if data[257:262] == b"ustar":
        return "tar"
    if data[:200].lstrip().lower().startswith((b"<!doctype html", b"<html")):
        return "html"
    sample = data[:4000]
    odd = sum(b < 9 or 13 < b < 32 for b in sample)
    return "binary" if sample and odd / len(sample) > 0.02 else "text"


def decompress_head(data: bytes, limit: int = 4 * HEAD_BYTES) -> tuple[bytes, str]:
    """The start of the decompressed stream, and the compression found."""
    kind = sniff(data)
    if kind == "gz":  # bgzip is many small gzip members: keep going through them
        out, rest = b"", data
        while rest and len(out) < limit:
            d = zlib.decompressobj(47)
            try:
                out += d.decompress(rest, limit - len(out))
            except zlib.error:
                break
            if not d.eof:
                break
            rest = d.unused_data
        return out, "gzip"
    if kind in ("bz2", "xz"):
        d2: Any = bz2.BZ2Decompressor() if kind == "bz2" else lzma.LZMADecompressor()
        try:
            return d2.decompress(data, limit), kind
        except (OSError, EOFError, lzma.LZMAError):
            return b"", kind
    return data, ""


NUMERIC = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


def _cells(line: str, delimiter: str) -> list[str]:
    if delimiter == " ":
        return line.split()
    if delimiter == ",":
        import csv

        return next(csv.reader([line]), [])
    return line.split(delimiter)


def _delimiter(lines: list[str]) -> str:
    """Tab, comma, semicolon or whitespace — whichever splits the first lines evenly."""
    for d in ("\t", ",", ";"):
        counts = [line.count(d) for line in lines[:8]]
        if counts and min(counts) > 0 and max(counts) - min(counts) <= max(1, counts[0] // 10):
            return d
    return " "


def pick_header(rows: list[list[str]]) -> tuple[int, bool]:
    """(index of the header row, whether it is a real header).

    The header is the first row about as wide as the table with no number in it; title
    lines above it (spreadsheets) are skipped. A table whose first wide row already holds
    numbers has no header.
    """
    widths = [sum(1 for c in r if c.strip()) for r in rows[:12]]
    if not widths or max(widths) == 0:
        return 0, False
    wide = 0.7 * max(widths)
    first_wide = next(i for i, w in enumerate(widths) if w >= wide)
    for i, w in enumerate(widths):
        if w >= wide and not any(NUMERIC.match(c.strip()) for c in rows[i] if c.strip()):
            return (i, True) if i == first_wide else (first_wide, False)
    return first_wide, False


def table_from_rows(rows: list[list[str]], name: str = "", **kw: Any) -> Table | None:
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return None
    at, has_header = pick_header(rows)
    width = max(len(r) for r in rows[at:at + SAMPLE_ROWS + 1])
    header = [c.strip() for c in rows[at]] + [""] * (width - len(rows[at]))
    columns = header if has_header else [f"col{i + 1}" for i in range(width)]
    data = rows[at + 1:] if has_header else rows[at:]
    clip = [[c.strip()[:80] for c in r[:200]] for r in data[:SAMPLE_ROWS]]
    preamble = " | ".join(" ".join(c.strip() for c in r if c.strip()) for r in rows[:at])[:600]
    return Table(name, columns[:200], clip, has_header, preamble=preamble, **kw)


def parse_text_table(data: bytes, name: str = "", *, complete: bool = False) -> Table | None:
    text = data.decode("utf-8", "replace").lstrip("\ufeff")
    lines = text.splitlines()
    if not complete and len(lines) > 1:
        lines = lines[:-1]  # the last line of a partial read is cut mid-row
    lines = [ln for ln in lines if ln.strip() and not ln.startswith("##")][:40]
    if not lines:
        return None
    lines[0] = lines[0].lstrip("#").lstrip() if lines[0].startswith("#") else lines[0]
    d = _delimiter(lines)
    return table_from_rows([_cells(ln, d) for ln in lines], name, delimiter=d)


def read_text(data: bytes, limit: int = 8000) -> str:
    """A README or description file as plain text (RTF control words removed)."""
    raw, _ = decompress_head(data)
    text = raw.decode("utf-8", "replace")
    if text.lstrip().startswith("{\\rtf"):
        text = re.sub(r"\\'[0-9a-f]{2}", " ", text)
        text = re.sub(r"\{\\(?:fonttbl|colortbl|stylesheet|\*)[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", " ", text)
        text = re.sub(r"\\[a-zA-Z]+-?\d* ?|[{}]|\\\n", " ", text)
    return re.sub(r"[ \t]+", " ", text).strip()[:limit]


# ── archives, spreadsheets, parquet ──────────────────────────────────────────


class RangeFile(io.RawIOBase):
    """A remote file read by HTTP range requests, for zipfile to seek in."""

    BLOCK = 1 << 20  # fetch at least this much per request: zipfile reads in small steps

    def __init__(self, http: httpx.Client, url: str, size: int, budget: int = ARCHIVE_BUDGET) -> None:
        self.http, self.url, self.size, self.budget = http, url, size, budget
        self.pos = 0
        self.at, self.block = 0, b""  # the cached block and where it starts

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        self.pos = offset if whence == io.SEEK_SET else (
            self.pos + offset if whence == io.SEEK_CUR else self.size + offset)
        return self.pos

    def readinto(self, b: Any) -> int:
        want = min(len(b), self.size - self.pos)
        if want <= 0:
            return 0
        if not self.at <= self.pos < self.at + len(self.block):
            end = min(self.size, self.pos + max(want, self.BLOCK))
            self.budget -= end - self.pos
            if self.budget < 0:
                raise OSError("archive read budget exceeded")
            r = self.http.get(self.url, headers={"Range": f"bytes={self.pos}-{end - 1}"})
            if r.status_code != 206:
                raise OSError(f"range request refused (HTTP {r.status_code})")
            self.at, self.block = self.pos, r.content
        start = self.pos - self.at
        data = self.block[start:start + want]
        b[: len(data)] = data
        self.pos += len(data)
        return len(data)


class StreamFile(io.RawIOBase):
    """The head of an HTTP response body as a forward-only file, for tarfile."""

    def __init__(self, chunks: Any, budget: int = ARCHIVE_BUDGET) -> None:
        self.chunks, self.budget, self.buf = chunks, budget, b""

    def readable(self) -> bool:
        return True

    def readinto(self, b: Any) -> int:
        while not self.buf and self.budget > 0:
            chunk = next(self.chunks, None)
            if chunk is None:
                break
            self.buf, self.budget = chunk, self.budget - len(chunk)
        data, self.buf = self.buf[: len(b)], self.buf[len(b):]
        b[: len(data)] = data
        return len(data)


def _member_table(head: bytes, name: str, complete: bool) -> Table | None:
    raw, _ = decompress_head(head)
    if sniff(raw) != "text":
        return None
    return parse_text_table(raw, name, complete=complete and raw is head)


def _open_zip(http: httpx.Client, url: str, size: int | None, ranged: bool) -> zipfile.ZipFile:
    if ranged and size:
        return zipfile.ZipFile(io.BufferedReader(RangeFile(http, url, size), buffer_size=1 << 17))
    if size is not None and size > WHOLE_FILE_LIMIT:
        raise OSError(f"{size / 1e6:.0f} MB archive on a server without range requests")
    with http.stream("GET", url) as r:
        body = b"".join(_take(r.iter_bytes(), WHOLE_FILE_LIMIT + 1))
    if len(body) > WHOLE_FILE_LIMIT:
        raise OSError("archive larger than the whole-file limit on a server without range requests")
    return zipfile.ZipFile(io.BytesIO(body))


def _probe_zip(http: httpx.Client, url: str, size: int | None, ranged: bool) -> Probe:
    z = _open_zip(http, url, size, ranged)
    names = z.namelist()
    if "xl/workbook.xml" in names:
        return Probe("table", tables=_xlsx_tables(z))
    out = Probe("archive", members=names[:200])
    wanted = [i for i in z.infolist() if not i.is_dir() and rules.file_kind(i.filename, i.file_size) == "table"]
    # Per-chromosome members are one table; skip members whose name marks a subset.
    by_glob: dict[str, Any] = {}
    for info in sorted(wanted, key=lambda i: rules.natural_key(i.filename)):
        if not rules.subset_by_name(info.filename):
            by_glob.setdefault(rules.chr_glob(info.filename), info)
    wanted = sorted(by_glob.values(), key=lambda i: (-rules.name_score(i.filename), -i.file_size))
    for info in wanted[:6]:
        with z.open(info) as fh:
            head = fh.read(HEAD_BYTES)
        table = _member_table(head, rules.chr_glob(info.filename), complete=len(head) >= info.file_size)
        if table:
            out.tables.append(table)
    return out


def _probe_tar(http: httpx.Client, url: str) -> Probe:
    out = Probe("archive")
    with http.stream("GET", url) as r:
        if r.status_code >= 400:
            return Probe("error", note=f"HTTP {r.status_code}")
        try:
            with tarfile.open(fileobj=io.BufferedReader(StreamFile(r.iter_raw())), mode="r|*") as tf:
                for member in tf:
                    out.members.append(member.name)
                    if (member.isfile() and len(out.tables) < 6
                            and rules.file_kind(member.name, member.size) == "table"):
                        fh = tf.extractfile(member)
                        head = fh.read(HEAD_BYTES) if fh else b""
                        table = _member_table(head, member.name, complete=len(head) >= member.size)
                        if table:
                            out.tables.append(table)
                    if len(out.members) >= 200:
                        break
        except (tarfile.TarError, EOFError, OSError, zlib.error) as exc:
            out.note = f"read the first {len(out.members)} members ({type(exc).__name__})"
    return out


COL_REF = re.compile(r"^([A-Z]+)")


def _xlsx_tables(z: zipfile.ZipFile) -> list[Table]:
    """Header and first rows of each sheet, read from the start of the sheet's XML."""
    def head(name: str, limit: int) -> str:
        try:
            with z.open(name) as fh:
                return fh.read(limit).decode("utf-8", "replace")
        except KeyError:
            return ""

    strings = [html.unescape("".join(re.findall(r"<t[^>]*>(.*?)</t>", si, re.S)))
               for si in re.findall(r"<si>(.*?)</si>", head("xl/sharedStrings.xml", 6_000_000), re.S)]
    sheet_names = [html.unescape(n) for n in re.findall(r'<sheet\b[^>]*?\bname="([^"]*)"', head("xl/workbook.xml", 400_000))]
    sheets = sorted((n for n in z.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
                    key=lambda n: int(re.search(r"\d+", n.rsplit("/", 1)[-1]).group(0)))
    tables = []
    for i, sheet in enumerate(sheets[:12]):
        rows: list[list[str]] = []
        for row_xml in re.findall(r"<row\b[^>]*>(.*?)</row>", head(sheet, 600_000), re.S)[:16]:
            row: list[str] = []
            for attrs, body in re.findall(r"<c\b([^>]*?)(?:/>|>(.*?)</c>)", row_xml, re.S):
                ref = re.search(r'\br="([A-Z]+)\d+"', attrs)
                col = 0
                for ch in ref.group(1) if ref else "":
                    col = col * 26 + ord(ch) - 64
                col = (col or len(row) + 1) - 1
                v = re.search(r"<v>(.*?)</v>", body or "", re.S)
                text = html.unescape("".join(re.findall(r"<t[^>]*>(.*?)</t>", body or "", re.S)))
                if v is not None:
                    text = html.unescape(v.group(1))
                    if 't="s"' in attrs and text.isdigit():
                        text = strings[int(text)] if int(text) < len(strings) else "…"
                if col < 200:
                    row += [""] * (col - len(row) + 1)
                    row[col] = text
            rows.append(row)
        table = table_from_rows(rows, sheet_names[i] if i < len(sheet_names) else f"sheet{i + 1}", fmt="xlsx")
        if table:
            tables.append(table)
    return tables


class _Thrift:
    """Just enough of Thrift's compact protocol to read a parquet footer's schema."""

    def __init__(self, data: bytes) -> None:
        self.d, self.i = data, 0

    def byte(self) -> int:
        self.i += 1
        return self.d[self.i - 1]

    def varint(self) -> int:
        shift = out = 0
        while True:
            b = self.byte()
            out |= (b & 0x7F) << shift
            if not b & 0x80:
                return out
            shift += 7

    def zigzag(self) -> int:
        n = self.varint()
        return (n >> 1) ^ -(n & 1)

    def value(self, t: int, in_list: bool = False) -> Any:
        if t in (1, 2):
            return (self.byte() == 1) if in_list else t == 1
        if t == 3:
            return self.byte()
        if t in (4, 5, 6):
            return self.zigzag()
        if t == 7:
            self.i += 8
            return None
        if t == 8:
            n = self.varint()
            self.i += n
            return self.d[self.i - n:self.i]
        if t in (9, 10):
            h = self.byte()
            n = h >> 4 if h >> 4 != 15 else self.varint()
            return [self.value(h & 15, True) for _ in range(n)]
        if t == 11:
            n = self.varint()
            kv = self.byte() if n else 0
            return [(self.value(kv >> 4, True), self.value(kv & 15, True)) for _ in range(n)]
        if t == 12:
            return self.struct()
        raise ValueError(f"unknown thrift type {t}")

    def struct(self, stop_after: int = 0) -> dict[int, Any]:
        out: dict[int, Any] = {}
        fid = 0
        while True:
            h = self.byte()
            if h == 0:
                return out
            fid = fid + (h >> 4) if h >> 4 else self.zigzag()
            out[fid] = self.value(h & 15)
            if stop_after and fid >= stop_after:
                return out


def parquet_schema(footer: bytes) -> tuple[list[str], int | None]:
    """(leaf column names, number of rows) from a parquet FileMetaData footer."""
    meta = _Thrift(footer).struct(stop_after=3)  # 2 = schema, 3 = num_rows
    columns = [el[4].decode("utf-8", "replace") for el in (meta.get(2) or [])[1:]
               if isinstance(el, dict) and 4 in el and not el.get(5)]
    return columns, meta.get(3)


def _probe_parquet(http: httpx.Client, url: str, size: int | None) -> Probe:
    if not size:
        return Probe("unreadable", note="parquet file of unknown size")
    tail = http.get(url, headers={"Range": f"bytes={size - 8}-{size - 1}"})
    if tail.status_code != 206 or tail.content[-4:] != b"PAR1":
        return Probe("unreadable", note="parquet footer not reachable by range request")
    length = int.from_bytes(tail.content[:4], "little")
    if length > 16 * 1024 * 1024:
        return Probe("unreadable", note="parquet footer too large")
    footer = http.get(url, headers={"Range": f"bytes={size - 8 - length}-{size - 9}"}).content
    columns, n_rows = parquet_schema(footer)
    return Probe("table", tables=[Table("", columns, [], fmt="parquet", n_rows=n_rows)],
                 note="parquet: column names from the footer; no rows sampled")


def probe_file(http: httpx.Client, f: RemoteFile) -> Probe:
    """Header and first rows of ``f`` (or of its members); never raises."""
    name = (f.path or f.name).lower()
    try:
        if re.search(r"\.(tar(\.(gz|bz2|xz))?|tgz)$", name):
            return _probe_tar(http, f.url)
        if PMC_BIN_RE.match(f.url):
            body = pmc_supplement(http, f.url)
            if body is None:
                return Probe("unreadable", note="PMC supplementary file: not served to scripts and "
                                                "not in Europe PMC's supplement archive — open it in a browser")
            f.size = len(body)
            if sniff(body) == "zip":
                z = zipfile.ZipFile(io.BytesIO(body))
                if "xl/workbook.xml" in z.namelist():
                    return Probe("table", tables=_xlsx_tables(z))
            table = _member_table(body[:HEAD_BYTES], "", complete=len(body) <= HEAD_BYTES)
            return Probe("table", tables=[table]) if table else Probe(
                "unreadable", note="supplementary file is not a table (PDF, Word or old .xls?)")
        head, total, ranged = fetch_head(http, f.url)
        if f.size is None:
            f.size = total
        kind = sniff(head)
        if kind == "html":
            return Probe("unreadable", note="the link returns a web page, not the file (login or landing page)")
        if kind == "zip":
            return _probe_zip(http, f.url, f.size, ranged)
        if kind == "parquet" or name.endswith(".parquet"):
            return _probe_parquet(http, f.url, f.size)
        raw, compression = decompress_head(head)
        if sniff(raw) == "tar":
            return _probe_tar(http, f.url)
        if not raw or sniff(raw) != "text":
            return Probe("unreadable", note="not a text table (binary or unsupported compression)",
                         compression=compression)
        whole = not compression and total is not None and len(head) >= total
        table = parse_text_table(raw, complete=whole)
        if table is None:
            return Probe("unreadable", note="no rows in the first 256 KB", compression=compression)
        return Probe("table", tables=[table], compression=compression)
    except (httpx.HTTPError, OSError, ValueError, zipfile.BadZipFile, IndexError, KeyError) as exc:
        return Probe("error", note=f"{type(exc).__name__}: {str(exc)[:160]}")


def fetch_doc(http: httpx.Client, f: RemoteFile) -> str:
    """A README-like file as text, or "" when it cannot be read as such."""
    try:
        head, _, _ = fetch_head(http, f.url, 64 * 1024)
    except httpx.HTTPError:
        return ""
    raw, _ = decompress_head(head)
    return read_text(head) if raw and sniff(raw) == "text" else ""
