"""Offline tests for the parsing rules in dataqueryweb.extract and finder.parse_ref."""

from __future__ import annotations

from dataqueryweb import extract, finder

JATS = """<article xmlns:xlink="http://www.w3.org/1999/xlink"><body>
<sec><title>Methods</title>
  <p>We formatted our cis-eQTLs using SMR (<ext-link xlink:href="https://github.com/x/smr">https://github.com/x/smr</ext-link>).</p>
  <p>GTEx lung summary statistics were downloaded from http://ftp.ebi.ac.uk/pub/databases/spot/eQTL/sumstats/.</p>
</sec>
</body><back>
<sec sec-type="data-availability"><title>Data availability</title>
  <p>Raw data are deposited in EGA, accession number EGAD00001008197.
     Full eQTL summary statistics are available at https://zenodo.org/records/1234567.
     Processed data are deposited in OWEY (10.48802/owey.e4qn-9190).</p>
</sec>
<fn-group><fn><p>Data and Materials Availability</p>
  <p>QTL summary statistics are available on the GTEx Portal (<ext-link xlink:href="https://gtexportal.org">portal</ext-link>).</p>
</fn></fn-group>
<sec><supplementary-material><label>Supplementary Table 3</label>
  <caption><p>Full sQTL summary statistics.</p></caption><media xlink:href="table3.xlsx"/>
</supplementary-material></sec>
</back></article>"""


def by_url(sources: list[extract.Source]) -> dict[str, extract.Source]:
    return {s.url: s for s in sources}


def test_jats_availability_links_rank_high() -> None:
    ex = extract.parse_jats(JATS, "PMC1")
    got = by_url(ex.sources)
    assert got["https://zenodo.org/records/1234567"].relevance == "high"
    assert got["https://gtexportal.org"].relevance == "high"
    assert got["https://ega-archive.org/datasets/EGAD00001008197"].access == "controlled"
    assert "https://doi.org/10.48802/owey.e4qn-9190" in got
    assert ex.availability and "Zenodo" not in ex.availability[0]


def test_reused_and_software_links_rank_below() -> None:
    got = by_url(extract.parse_jats(JATS, "PMC1").sources)
    assert got["https://github.com/x/smr"].relevance == "low"
    reused = got["http://ftp.ebi.ac.uk/pub/databases/spot/eQTL/sumstats/"]
    assert reused.relevance != "high"


def test_supplement_with_qtl_caption() -> None:
    got = by_url(extract.parse_jats(JATS, "PMC1").sources)
    supp = got["https://pmc.ncbi.nlm.nih.gov/articles/PMC1/bin/table3.xlsx"]
    assert supp.kind == "supplement"


def test_qtl_types() -> None:
    assert extract.qtl_types("cis-eQTLs, sQTL and caQTL; pQTLs") == [
        "caQTL",
        "eQTL",
        "pQTL",
        "sQTL",
    ]


def test_html_availability_window() -> None:
    html = """<html><head><meta name="citation_doi" content="10.1000/xyz"></head><body>
      <h2>Methods</h2><p>We used <a href="https://github.com/a/b">tool</a>.</p>
      <h2>Data availability</h2>
      <p>Our eQTL summary statistics are available at <a href="https://doi.org/10.5281/zenodo.42">Zenodo</a>.</p>
    </body></html>"""
    ex = extract.parse_html(html, "https://example.org/article")
    got = by_url(ex.sources)
    assert got["https://doi.org/10.5281/zenodo.42"].relevance == "high"
    assert extract.page_meta_doi(html) == "10.1000/xyz"


def test_parse_ref() -> None:
    assert finder.parse_ref("https://doi.org/10.1038/s41588-021-00913-z").doi == (
        "10.1038/s41588-021-00913-z"
    )
    assert finder.parse_ref("https://www.nature.com/articles/s41586-023-06422-9").doi == (
        "10.1038/s41586-023-06422-9"
    )
    assert finder.parse_ref(
        "https://www.biorxiv.org/content/10.1101/2023.03.03.531033v2.full"
    ).doi == ("10.1101/2023.03.03.531033")
    assert finder.parse_ref("https://pmc.ncbi.nlm.nih.gov/articles/PMC8432599/").pmcid == (
        "PMC8432599"
    )
    assert finder.parse_ref("PMID: 34475573").pmid == "34475573"
    assert finder.parse_ref("https://zenodo.org/records/1").url == "https://zenodo.org/records/1"
    assert finder.parse_ref("hello") == finder.Ref()


def test_parse_ref_new_biorxiv_prefix() -> None:
    ref = finder.parse_ref(
        "https://www.medrxiv.org/content/10.64898/2026.08.13.26360300v2.full-text"
    )
    assert ref.doi == "10.64898/2026.08.13.26360300"
    assert finder.PREPRINT_DOI_RE.match(ref.doi)


def test_build_query_years_and_sources() -> None:
    q = finder.build_query("brain sQTL", source="preprint", year_from=2024, year_to=2026)
    assert q.startswith("(((TITLE_ABS:(brain sQTL)) AND (QTL OR")
    assert 'PUBLISHER:"bioRxiv"' in q and q.endswith("AND PUB_YEAR:[2024 TO 2026]")
    assert finder.build_query("x", qtl_only=False, year_from=2025) == (
        "(TITLE_ABS:(x)) AND PUB_YEAR:[2025 TO 3000]"
    )
    assert "PUB_YEAR" not in finder.build_query("x")
