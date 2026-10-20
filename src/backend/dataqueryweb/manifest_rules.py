"""The written rules that turn a remote QTL file into a candidate ``qtl_manifest.tsv`` row.

The format is that of LocusCompare's ``scripts/qtl_manifest.tsv`` (lc2_newfeature_backend,
read only as a reference; our rows go to ``manifest/qtl_manifest.tsv`` here): one row
per (dataset, QTL type, context), pointing at one table — or one ``chr*`` glob of tables — of
full nominal summary statistics, plus the names of the columns its importer reads.

Everything here is deterministic and reads no network: which header is which manifest
column, what a table holds, how file names become units and context labels, when a quote
counts as evidence, and when a row is ready. The LLM (manifest.py) only supplies facts the
files cannot — tissue or cell, sample size, ancestry, genome build — each with a verbatim
quote that :func:`verify_quote` must find in the text the model was given.

MANIFEST_RULES.md describes the same rules for curators; change RULES_VERSION with them.
"""

from __future__ import annotations

import csv
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RULES_VERSION = "2026-10-12"

MANIFEST_COLUMNS = [
    "dataset", "source_project_id", "use_in_lc2", "qtl_type", "biocontext",
    "level_1_context", "level_2_context", "population", "sample_size", "file_path",
    "delimiter", "chrom_col", "position_col", "ref_col", "alt_col", "beta_col", "se_col",
    "pval_col", "rsid_col", "maf_col", "variant_id_col", "phenotype_id_col", "gene_id",
    "gene_id_mode", "median_tpm", "url", "publication",
]
# What the importer cannot run without (load_gwas_qtl_catalog.py: QTL_REQUIRED_COLS).
REQUIRED_ROLES = ("chrom", "position", "ref", "alt", "pval", "phenotype_id")
OPTIONAL_ROLES = ("beta", "se", "rsid", "maf", "variant_id")
ROLE_COLUMN = {
    "chrom": "chrom_col", "position": "position_col", "ref": "ref_col", "alt": "alt_col",
    "beta": "beta_col", "se": "se_col", "pval": "pval_col", "rsid": "rsid_col",
    "maf": "maf_col", "variant_id": "variant_id_col", "phenotype_id": "phenotype_id_col",
    "median_tpm": "median_tpm",
}
# The importer derives gene_id only for these (QTL_TYPES_WITH_GENE_ID).
GENE_QTL_TYPES = ("eQTL", "sQTL", "pQTL")
MANIFEST_QTL_TYPES = ("eQTL", "sQTL", "caQTL", "pQTL")  # types the manifest holds today
POPULATIONS = ("EUR", "EAS", "AFR", "AMR", "SAS", "ALL")
# Broad display groups used as level_1_context by the non-GTEx rows of the manifest.
BROAD_CONTEXTS = [
    "Blood", "Brain", "Neural", "Adipose", "Muscle", "Skin", "Pancreas", "Placenta",
    "Cartilage", "Synovium",
]


def norm(name: str) -> str:
    """A header reduced to lowercase words joined by "_" ("P-value (nominal)" → "p_value_nominal")."""
    return re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")


# ── which header is which manifest column ────────────────────────────────────

# role → name patterns in priority order, matched against the whole normalised header.
ROLE_PATTERNS: dict[str, list[str]] = {
    "chrom": [
        r"chr", r"chrom", r"chromosome", r"chr_?(name|id)", r"(snp|variant|var)_?chr(om(osome)?)?",
        r"seqnames?", r"contig", r"(hg|grch|b)\d+_?chr(om)?", r"chr(om)?_?(hg|grch|b)\d+",
    ],
    "position": [
        r"pos", r"position", r"bp", r"(snp|variant|var)_?(pos(ition)?|bp)",
        r"pos(ition)?_?(hg|grch|b)\d+", r"(hg|grch|b)\d+_?pos(ition)?", r"base_?pair_?location",
        r"ps", r"variant_?start",
    ],
    "ref": [
        r"ref", r"ref_?allele", r"reference(_?allele)?", r"allele_?ref", r"other_?allele",
        r"non_?effect_?allele", r"nea", r"oa",
    ],
    "alt": [
        r"alt", r"alt_?allele", r"alternat(e|ive)(_?allele)?", r"allele_?alt", r"effect_?allele",
        r"ea", r"tested_?allele", r"assessed_?allele", r"coded_?allele",
    ],
    "beta": [
        r"slope", r"beta", r"effect_?size", r"effect", r"b", r"bhat", r"es", r"nes",
        r"beta_?(g|snp|fixed|meta|hat)", r"log_?or",
    ],
    "se": [
        r"slope_?se", r"se", r"beta_?se", r"se_?beta", r"std_?err(or)?", r"stderr",
        r"standard_?error", r"sebeta", r"se_?(g|fixed|meta)", r"std",
    ],
    "pval": [
        r"pval_?nominal", r"pvalue", r"pval", r"p_?value", r"p", r"nominal_?p(val(ue)?)?",
        r"p_?val", r"p_?nominal", r"p_?value_?nominal", r"pval(ue)?_?(g|fixed|meta)",
        r"p_?(wald|lrt|score|bolt\w*)",
    ],
    "rsid": [r"rs_?id\w*", r"rsid", r"rs", r"rs_?number", r"dbsnp\w*", r"snp_?rs(id)?"],
    "maf": [r"maf", r"minor_?allele_?freq(uency)?"],
    "af": [
        r"af", r"eaf", r"a1_?freq", r"freq(uency)?", r"allele_?freq(uency)?",
        r"(effect|alt|ref|tested)_?(allele_?)?freq(uency)?", r"af_?alt", r"alt_?af",
    ],
    "variant_id": [
        r"variant_?id", r"variant", r"var_?id", r"snp_?id", r"snp", r"sid", r"snpid",
        r"marker(_?name|_?id)?", r"variant_?name", r"id",
    ],
    "phenotype_id": [
        r"molecular_?trait_?id", r"phenotype_?id", r"pid", r"gene_?id", r"ensembl(_?gene)?(_?id)?",
        r"ensg", r"peak_?region", r"peak(_?id|_?name)?", r"feature(_?id)?", r"intron(_?id)?",
        r"cluster(_?id)?", r"probe(_?id)?", r"cpg(_?id)?", r"protein(_?id)?", r"uniprot(_?id)?",
        r"somamer(_?id)?", r"seq_?id", r"transcript(_?id)?", r"exon(_?id)?", r"phenotype",
        r"trait(_?id)?", r"gene", r"gene_?(name|symbol)", r"symbol", r"metabolite(_?id)?",
        r"molecular_?trait_?object_?id",
    ],
    "median_tpm": [r"median_?tpm"],
    "n": [r"n", r"n_?samples?", r"sample_?size", r"num_?(samples|indiv\w*)", r"nobs",
          r"n_?(indiv\w*|obs)"],
    "an": [r"an"],
    # "chr16:85721809" in one column: chromosome and position can be split out of it.
    "locus": [r"(snp|variant|var)_?(location|locus|coord\w*)", r"chr_?pos", r"location", r"locus", r"coord\w*"],
    "neglog10p": [r"(neg_?|m|minus_?|n)?log_?10_?p(val(ue)?)?", r"logp", r"lp", r"mlogp"],
    "chisq": [r"chi_?sq(uared?)?(_?stat(istic)?)?", r"chi2(_?stat)?", r"chisq"],
    # Alleles named by number: which one the effect refers to differs between tools.
    "allele_1": [r"a1", r"allele_?1"],
    "allele_2": [r"a2", r"allele_?2", r"a0", r"allele_?0"],
}
# A p-value column that is not the nominal one.
ADJUSTED_RE = re.compile(r"fdr|q_?val|adj|perm|bonf|beta_?approx|emp|(^|_)bh(_|$)|threshold|df")

CHROM_VALUE = re.compile(r"^(chr)?(\d{1,2}|x|y|m|mt)$", re.I)
ALLELE_VALUE = re.compile(r"^([acgtn*-]+|<[\w:]+>)$", re.I)
RSID_VALUE = re.compile(r"^rs\d+$", re.I)
# chr1_12345_A_G, 1:12345:A:G, chr1:12345_A/G, optionally with a build suffix (GTEx: _b38).
VARIANT_VALUE = re.compile(
    r"^(chr)?(\d{1,2}|x|y|m|mt)[:_-](\d+)[:_-]([acgtn*]+)[:_/>-]([acgtn*]+)(?:[:_-](b3[78]|hg(?:19|38)))?$",
    re.I,
)
ENSG_VALUE = re.compile(r"^ENSG\d{11}(\.\d+)?$")
ENSG_LAST = re.compile(r":ENSG\d{11}(\.\d+)?$")
PEAK_VALUE = re.compile(r"^chr(\d{1,2}|X|Y|MT?)_\d+_\d+$", re.I)  # importer: CAQTL_PHENOTYPE_RE
PEAK_LOOSE = re.compile(r"^(chr)?(\d{1,2}|X|Y|MT?)[:_-]\d+[:_-]\d+$", re.I)
NA = {"", "na", "nan", ".", "null", "none", "-"}


def _num(v: str) -> float | None:
    try:
        return float(v)
    except ValueError:
        return None


def _all(values: list[str], test: Any) -> bool:
    """True when every non-missing sample value passes; True as well when none is given."""
    return all(test(v) for v in values if v.strip().lower() not in NA)


VALUE_TESTS: dict[str, Any] = {
    "chrom": lambda v: bool(CHROM_VALUE.match(v)),
    "position": lambda v: v.isdigit(),
    "ref": lambda v: bool(ALLELE_VALUE.match(v)),
    "alt": lambda v: bool(ALLELE_VALUE.match(v)),
    "allele_1": lambda v: bool(ALLELE_VALUE.match(v)),
    "allele_2": lambda v: bool(ALLELE_VALUE.match(v)),
    "beta": lambda v: _num(v) is not None,
    "se": lambda v: (n := _num(v)) is not None and n >= 0,
    "pval": lambda v: (n := _num(v)) is not None and 0 <= n <= 1,
    "maf": lambda v: (n := _num(v)) is not None and 0 <= n <= 0.5,
    "af": lambda v: (n := _num(v)) is not None and 0 <= n <= 1,
    "rsid": lambda v: bool(RSID_VALUE.match(v)),
    "median_tpm": lambda v: (n := _num(v)) is not None and n >= 0,
    "n": lambda v: (n := _num(v)) is not None and n >= 1,
    "an": lambda v: (n := _num(v)) is not None and n >= 2,
    "neglog10p": lambda v: (n := _num(v)) is not None and n >= 0,
    "locus": lambda v: bool(re.match(r"^(chr)?(\d{1,2}|x|y|m|mt)[:_-]\d+$", v, re.I)),
}


@dataclass
class Roles:
    """Which header plays which role, and what the rules noticed while deciding."""

    columns: dict[str, str] = field(default_factory=dict)  # role → header as written
    how: dict[str, str] = field(default_factory=dict)  # role → why (shown as evidence)
    issues: list[str] = field(default_factory=list)
    derivable: list[str] = field(default_factory=list)  # required roles a conversion can make
    build_hint: str = ""  # genome build read off the variant ids
    file_n: int | None = None  # sample size read off an N column


def detect_roles(columns: list[str], rows: list[list[str]], qtl_type: str = "") -> Roles:
    """Map headers to manifest roles: by name, then confirmed against the sample rows."""
    out = Roles()
    names = [norm(c) for c in columns]

    def values(i: int) -> list[str]:
        return [r[i].strip() for r in rows if i < len(r)]

    taken: set[int] = set()
    for role, patterns in ROLE_PATTERNS.items():
        for pattern in patterns:
            hit = next(
                (i for i, n in enumerate(names)
                 if i not in taken and re.fullmatch(pattern, n)
                 and not (role in ("pval", "neglog10p") and ADJUSTED_RE.search(n))
                 and _all(values(i), VALUE_TESTS.get(role, lambda v: True))),
                None,
            )
            if hit is not None:
                # A variant-id header whose values are rsIDs is the rsid column.
                if role == "variant_id" and values(hit) and _all(values(hit), VALUE_TESTS["rsid"]):
                    if "rsid" in out.columns:
                        continue
                    role_as = "rsid"
                else:
                    role_as = role
                out.columns[role_as] = columns[hit]
                sample = ", ".join(values(hit)[:3])
                out.how[role_as] = f'header "{columns[hit]}"' + (f" (values: {sample})" if sample else "")
                taken.add(hit)
                break

    c = out.columns
    # Numbered alleles stand in for ref/alt only when nothing better is there, and are flagged.
    if "alt" not in c and "ref" not in c and "allele_1" in c and "allele_2" in c:
        c["alt"], c["ref"] = c.pop("allele_1"), c.pop("allele_2")
        out.how["alt"], out.how["ref"] = out.how.pop("allele_1"), out.how.pop("allele_2")
        out.issues.append("allele_order_ambiguous")
    if "maf" not in c and "af" in c:
        c["maf"] = c["af"]
        out.how["maf"] = out.how["af"] + " — an allele frequency, not a minor allele frequency"
        out.issues.append("maf_col_is_allele_frequency")

    # chrom/pos/ref/alt packed into one id can be split by a conversion step.
    vid = c.get("variant_id")
    if vid:
        vals = [v for v in values(columns.index(vid)) if v.lower() not in NA]
        matches = [VARIANT_VALUE.match(v) for v in vals]
        if vals and all(matches):
            out.derivable = [r for r in ("chrom", "position", "ref", "alt") if r not in c]
            build = {(m.group(6) or "").lower() for m in matches if m} - {""}
            if len(build) == 1:
                out.build_hint = "GRCh38" if build.pop() in ("b38", "hg38") else "GRCh37"
    if "locus" in c and not out.derivable:
        out.derivable = [r for r in ("chrom", "position") if r not in c]
    if "pval" not in c and ("neglog10p" in c or "chisq" in c):
        out.derivable.append("pval")
    if out.derivable:
        out.issues.append("columns_need_deriving")

    beta = c.get("beta")
    if beta:
        nums = [n for v in values(columns.index(beta)) if (n := _num(v)) is not None]
        if len(nums) >= 3 and all(0 < n < 1 for n in nums):
            out.issues.append("effect_scale_check")  # e.g. RASQUAL's π: 0.5 means no effect

    for role, scale in (("n", 1), ("an", 2)):
        if role in c and out.file_n is None:
            nums = {int(n) for v in values(columns.index(c[role])) if (n := _num(v)) is not None}
            if len(nums) == 1:
                out.file_n = nums.pop() // scale
                out.how["file_n"] = f'column "{c[role]}" is constant in the sampled rows'
    return out


def gene_id_mode(values: list[str], qtl_type: str) -> tuple[str, str]:
    """(gene_id_mode, issue) for the phenotype column, as the importer will read it."""
    vals = [v for v in values if v.strip().lower() not in NA]
    if qtl_type == "caQTL" and not vals:
        return "", "phenotype_values_not_seen"
    if qtl_type == "caQTL":
        if vals and all(PEAK_VALUE.match(v) for v in vals):
            return "", ""
        loose = vals and all(PEAK_LOOSE.match(v) for v in vals)
        return "", "peak_id_needs_reformat" if loose else "peak_id_not_chr_start_end"
    if qtl_type not in GENE_QTL_TYPES:
        return "", ""
    if not vals:
        return "self", "phenotype_values_not_seen"
    if all(ENSG_VALUE.match(v) for v in vals):
        return "self", ""
    if all(ENSG_LAST.search(v) for v in vals):
        return "phenotype_last_segment", ""
    return "self", "phenotype_id_not_ensembl"


# ── what a file holds ────────────────────────────────────────────────────────

QTL_TYPE_WORDS = [
    ("caQTL", r"ca[-_ ]?qtl|chromatin[-_ ]accessib|atac[-_ ]?qtl"),
    ("sQTL", r"s[-_ ]?qtl|splic|leafcutter|intron"),
    ("pQTL", r"p[-_ ]?qtl|protein(?!_coding)|somascan|olink"),
    ("mQTL", r"me?[-_ ]?qtl|methyl|cpg"),
    ("eQTL", r"e[-_ ]?qtl|expression"),
]
QTL_TYPE_ALIASES = {"meqtl": "mQTL", "mqtl": "mQTL", "eqtl": "eQTL", "sqtl": "sQTL",
                    "pqtl": "pQTL", "caqtl": "caQTL"}
ALL_PAIRS_RE = re.compile(
    r"all[-_. ]?pairs|allpairs|nominal|all[-_. ]?tests?|sum[-_. ]?stats?|summary|cis_qtl_pairs"
    r"|(?<![a-z])(all|full)(?![a-z])", re.I)
SUBSET_RE = re.compile(
    r"signif|(?<![a-z])sig(?![a-z])|sig[-_ ]?(?:e|s|p|ca|m)?qtl|e[-_]?genes?|fdr|q[-_]?val|lead|(?<![a-z])top|perm|indep"
    r"|(?<!un)conditional"
    r"|fine[-_]?map|susie|credible|coloc|clump|threshold", re.I)
QTL_NAME_RE = re.compile(r"qtl|sum[-_]?stat|summary|nominal|all[-_]?pairs|(?<![a-z])(cis|trans)(?![a-z])", re.I)
NOT_QTL_NAME_RE = re.compile(
    r"covariate|expression|counts?(?![a-z])|tpm|genotype|vcf|peer|pca|(?<![a-z])bam|narrowpeak"
    r"|gwas|(?<![a-z])ld(?![a-z])|plink|metadata|sample[-_]?info|annotation|script|code"
    r"|(?<![a-z])smr(?![a-z])|coloc|twas|trait[-_ ]assoc|(?<![a-z])mr(?![a-z])|wgcna|fasta|gtf", re.I)
DOC_NAME_RE = re.compile(r"readme|description|legend|column|data[-_ ]?dict|license|notes?(?![a-z])", re.I)
# Extensions a probe can read the header of; everything else is listed but not opened.
TABLE_EXT_RE = re.compile(
    r"\.(tsv|txt|csv|tab|assoc|sumstats?|tbl|out|results?|parquet|xlsx|zip|tar|tgz|bed)"
    r"(\.(gz|bgz|bz2|xz))?$|\.(gz|bgz)$", re.I)
DOC_EXT_RE = re.compile(r"\.(md|rtf|txt|rst)$|(^|/)readme[^/]*$", re.I)
BIG_FILE = 20 * 1024 * 1024  # below this a "QTL results" file is probably a significant subset


def canonical_qtl_type(text: str) -> str:
    return QTL_TYPE_ALIASES.get(str(text).strip().lower().replace("-", "").replace(" ", ""),
                                str(text).strip())


def qtl_type_from_text(text: str) -> str:
    """The QTL type a path names ("…/caQTL_common/…" → caQTL), or "" when it names none."""
    # The usual spelling, wherever it sits ("GlomsigeQTLs"); longest prefix first.
    spelled = re.search(r"(ca|me|s|p|m|e)QTL", text)
    if spelled:
        return canonical_qtl_type(spelled.group(0))
    for name, pattern in QTL_TYPE_WORDS:
        if re.search(rf"(?<![a-z])(?:{pattern})", text, re.I):
            return name
    return ""


def file_kind(path: str, size: int | None) -> str:
    """"doc" (worth reading as text), "table" (worth probing) or "other" (listed only)."""
    name = path.rsplit("/", 1)[-1]
    if DOC_EXT_RE.search(path) and (DOC_NAME_RE.search(name) or name.lower().endswith((".md", ".rtf", ".rst"))):
        return "doc"
    return "table" if TABLE_EXT_RE.search(name) else "other"


def table_holds(roles: Roles, path: str, size: int | None, *, has_header: bool,
                fmt: str = "") -> tuple[str, str]:
    """(holds, why): "all_pairs", "subset", "unknown_scope" or "not_qtl"."""
    if NOT_QTL_NAME_RE.search(path.rsplit("/", 1)[-1]) and not QTL_NAME_RE.search(path):
        return "not_qtl", f'file name says other study data ("{NOT_QTL_NAME_RE.search(path.rsplit("/", 1)[-1]).group(0)}")'
    if not has_header:
        return "unknown_scope", "the file has no header row"
    c = roles.columns
    variant = any(r in c for r in ("variant_id", "rsid", "position"))
    stat = any(r in c for r in ("pval", "neglog10p", "beta"))
    if not (variant and stat):
        return "not_qtl", "no variant column together with a p-value or effect column"
    name = path.rsplit("/", 1)[-1]
    if SUBSET_RE.search(name):
        return "subset", f'file name says a selected subset ("{SUBSET_RE.search(name).group(0)}")'
    if fmt == "xlsx":
        return "subset", "a spreadsheet cannot hold every tested variant–phenotype pair"
    if ALL_PAIRS_RE.search(path):
        return "all_pairs", f'path says full results ("{ALL_PAIRS_RE.search(path).group(0)}")'
    if size is not None and size >= BIG_FILE:
        return "all_pairs", f"large file ({size / 1e6:.0f} MB) with per-variant statistics"
    return "unknown_scope", "small file whose name does not say whether it is the full results"


# ── file names → units and context labels ────────────────────────────────────

# A label made only of these words names a file kind, not a tissue or cell.
NOT_CONTEXT_RE = re.compile(
    r"(?:[\W_]*(?:(?:ca|me|e|s|p|m)?qtls?|cis|trans|summary|sum|statistics?|stats?|results?|all|pairs"
    r"|nominal|full|data|final|assoc\w*))+[\W_]*", re.I)
CHR_TOKEN_RE = re.compile(r"(?<![A-Za-z])chr(?:\d{1,2}|[XYM]|MT)(?![A-Za-z0-9])", re.I)


def natural_key(text: str) -> list[Any]:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text)]


def chr_glob(path: str) -> str:
    """Per-chromosome files share one manifest row: "x.chr12.tsv.gz" → "x.chr*.tsv.gz"."""
    return CHR_TOKEN_RE.sub("chr*", path)


def ext_chain(name: str) -> str:
    m = re.search(r"((?:\.[A-Za-z0-9]{1,8}){1,2})$", name)
    return m.group(1).lower() if m else ""


@dataclass
class Unit:
    """One candidate manifest row: a file, or the files of one ``chr*`` glob."""

    glob: str  # path inside the location, chromosomes globbed
    files: list[Any]  # probe.RemoteFile, in natural order
    group: str = ""  # units expected to share a header
    token: str = ""  # the part of the path that varies within the group
    uid: str = ""

    @property
    def size(self) -> int | None:
        sizes = [f.size for f in self.files if f.size is not None]
        return sum(sizes) if sizes else None


def make_units(files: list[Any]) -> list[Unit]:
    """Group table files into units, units into header groups, and name what varies."""
    by_glob: dict[str, list[Any]] = {}
    for f in files:
        by_glob.setdefault(chr_glob(f.path or f.name), []).append(f)
    units = [Unit(g, sorted(fs, key=lambda f: natural_key(f.path or f.name)))
             for g, fs in by_glob.items()]
    base_count: dict[str, int] = {}
    for u in units:
        base = u.glob.rsplit("/", 1)[-1]
        base_count[base] = base_count.get(base, 0) + 1
    dir_count: dict[str, int] = {}
    for u in units:
        directory, _, base = u.glob.rpartition("/")
        dir_count[f"{directory}|{ext_chain(base)}"] = dir_count.get(f"{directory}|{ext_chain(base)}", 0) + 1
    groups: dict[str, list[Unit]] = {}
    for u in units:
        directory, _, base = u.glob.rpartition("/")
        # Several tables in one folder are a group (OneK1K_<cell type>.tsv.gz); a table alone
        # in its folder groups with its namesakes next door (<cell type>/x.cis_qtl_pairs.chr*).
        dir_key = f"{directory}|{ext_chain(base)}"
        key = f"name:{base}" if dir_count[dir_key] == 1 and base_count[base] > 1 else f"dir:{dir_key}"
        groups.setdefault(key, []).append(u)
    for gi, members in enumerate(sorted(groups.values(), key=lambda m: natural_key(m[0].glob)), 1):
        members.sort(key=lambda u: natural_key(u.glob))
        tokens = varying_tokens([u.glob for u in members])
        for ui, (u, token) in enumerate(zip(members, tokens, strict=True), 1):
            u.group, u.token, u.uid = f"G{gi}", token, f"G{gi}.{ui}"
    return sorted(units, key=lambda u: natural_key(u.uid))


def varying_tokens(paths: list[str]) -> list[str]:
    """What is left of each path once the prefix and suffix they all share are removed."""
    if len(paths) < 2:
        return [""] * len(paths)
    prefix = os.path.commonprefix(paths)
    suffix = os.path.commonprefix([p[::-1] for p in paths])[::-1]
    # Cut on separators so a shared first letter is not eaten ("B_naive" / "B_memory").
    prefix = prefix[: max(prefix.rfind(s) for s in "/_.- ") + 1]
    cut = min((i for s in "/_.- " if (i := suffix.find(s)) >= 0), default=len(suffix))
    suffix = suffix[cut:]
    out = []
    for p in paths:
        end = len(p) - len(suffix) if suffix else len(p)
        token = p[len(prefix):end].strip("/_.- ")
        # What varies may be the QTL type or the kind of table, not a context.
        out.append("" if NOT_CONTEXT_RE.fullmatch(token) else token)
    return out


def subset_by_name(path: str) -> str:
    """Why a file name alone marks a selected subset ("…sig_cis_qtl_pairs…"), or ""."""
    m = SUBSET_RE.search(path.rsplit("/", 1)[-1])
    return f'file name says a selected subset ("{m.group(0)}")' if m else ""


def name_score(path: str) -> int:
    """How much a path looks like QTL results rather than other study files."""
    return (4 * bool(re.search(r"qtl", path, re.I)) + 2 * bool(QTL_NAME_RE.search(path))
            - 4 * bool(NOT_QTL_NAME_RE.search(path)))


def probe_order(units: list[Unit], budget: int) -> list[Unit]:
    """Which units to open: every one when few; otherwise one per group, then a second."""
    if len(units) <= budget:
        return list(units)

    def priority(u: Unit) -> tuple[int, int]:
        return (-name_score(u.glob), -(u.size or 0))

    groups: dict[str, list[Unit]] = {}
    for u in units:
        groups.setdefault(u.group, []).append(u)
    ordered = sorted(groups.values(), key=lambda m: priority(m[0]))
    picks = [m[0] for m in ordered] + [m[-1] for m in ordered if len(m) > 1]
    return picks[:budget]


# ── labels ───────────────────────────────────────────────────────────────────

LABEL_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_+.\-]*$")
POPULATION_WORDS = [
    ("ALL", r"multi|mixed|diverse|trans[- ]?ancest|cross[- ]?ancest|all\b|admixed(?! american)"),
    ("EUR", r"europ|\beur\b|caucasian|white|finnish|british|icelandic|dutch|estonian|sardinian"),
    ("EAS", r"east asian|\beas\b|chinese|japanese|korean|\bhan\b|taiwan"),
    ("AFR", r"africa|\bafr\b|yoruba|black"),
    ("AMR", r"hispanic|latin|admixed american|\bamr\b|mexican|peruvian"),
    ("SAS", r"south asian|\bsas\b|indian|pakistani|bangladesh"),
]


def label(text: str) -> str:
    """A context label in manifest style: no spaces, words joined by "_" ("CD14 Mono" → "CD14_Mono")."""
    return re.sub(r"_+", "_", re.sub(r"[^\w+.\-]+", "_", str(text).strip())).strip("_")


def population_code(text: str) -> str:
    """One of EUR/EAS/AFR/AMR/SAS/ALL from a code or a description; "" when it names none."""
    t = str(text).strip()
    if t.upper() in POPULATIONS:
        return t.upper()
    found = [code for code, pattern in POPULATION_WORDS if re.search(pattern, t, re.I)]
    if len({f for f in found if f != "ALL"}) > 1:
        return "ALL"
    return found[0] if found else ""


def quote_supports_population(code: str, quote: str) -> bool:
    """Does the quote itself speak of this ancestry (or, for ALL, of several or a mixed one)?"""
    found = {c for c, pattern in POPULATION_WORDS if re.search(pattern, quote, re.I)}
    return code in found or (code == "ALL" and len(found) > 1)


def default_dataset(first_author: str, year: str) -> str:
    """eQTL Catalogue style: "Alasoo K" + 2018 → "Alasoo_2018"."""
    surname = re.split(r"[\s,]+", first_author.strip())[0] if first_author.strip() else "Unknown"
    surname = re.sub(r"[^\w\-]", "", surname)
    return f"{surname}_{year}" if year else surname


@dataclass
class Target:
    """What the existing manifest already holds: its vocabulary and its publications."""

    level_1: list[str] = field(default_factory=lambda: list(BROAD_CONTEXTS))
    publications: dict[str, str] = field(default_factory=dict)  # doi → dataset
    datasets: list[str] = field(default_factory=list)  # every dataset name it holds
    path: str = ""


def load_target(path: str | Path | None = None) -> Target:
    """Read $LC2_QTL_MANIFEST, or lc2_newfeature_backend's manifest next to this project."""
    candidates = [path, os.environ.get("LC2_QTL_MANIFEST"),
                  Path(__file__).resolve().parents[4] / "lc2_newfeature_backend/scripts/qtl_manifest.tsv"]
    target = Target()
    for cand in candidates:
        if cand and Path(cand).is_file():
            with Path(cand).open(encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh, delimiter="\t"):
                    l1 = (row.get("level_1_context") or "").strip()
                    if l1 and l1 not in target.level_1:
                        target.level_1.append(l1)
                    name = (row.get("dataset") or "").strip()
                    if name and name not in target.datasets:
                        target.datasets.append(name)
                    doi = doi_of(row.get("publication") or "")
                    if doi:
                        target.publications.setdefault(doi, row.get("dataset") or "")
            target.path = str(cand)
            break
    return target


def doi_of(text: str) -> str:
    m = re.search(r"10\.\d{4,9}/[^\s\"<>?#]+", str(text))
    return m.group(0).rstrip(".").lower() if m else ""


# ── evidence ─────────────────────────────────────────────────────────────────


def _flat(text: str) -> str:
    """Letters and digits only, lowercased: what must match between a quote and its source."""
    # Only real tags: "P < 0.05 … MAF > 0.01" must not swallow the words in between.
    text = unicodedata.normalize("NFKC", re.sub(r"</?[A-Za-z][^<>]{0,80}>", " ", str(text)))
    return re.sub(r"[\W_]+", "", text.lower())


def verify_quote(quote: str, texts: dict[str, str]) -> str:
    """The name of the supplied text that contains ``quote``, or "".

    The words and numbers must appear in the same order in one text; case, spacing,
    punctuation and typographic variants (dashes, quotes, ligatures) are ignored. A quote the
    model shortened with "…" must have all its pieces in one text, in order. Fewer than 12
    letters and digits in total prove nothing.
    """
    pieces = [f for p in re.split(r"…|\.\.\.", str(quote)) if (f := _flat(p))]
    if sum(len(p) for p in pieces) < 12:
        return ""
    for name, text in texts.items():
        flat, at = _flat(text), 0
        for p in pieces:
            at = flat.find(p, at)
            if at < 0:
                break
            at += len(p)
        else:
            return name
    return ""


def number_in(value: int, quote: str) -> bool:
    """Is the sample size itself in the quote ("1,267 individuals" for 1267, not for 126)?"""
    plain = re.sub(r"(?<=\d)[,\u00a0\u202f ](?=\d{3}(?!\d))", "", quote)
    return bool(re.search(rf"(?<!\d){value}(?!\d)", plain))


def n_from_name(path: str) -> int | None:
    """A sample size written into a file name ("…_175samples_…", "n=922")."""
    m = re.search(r"(?<![A-Za-z0-9])(\d{2,6})[-_ ]?(?:samples?|indiv\w*|donors?|subjects?)(?![a-z])"
                  r"|(?<![A-Za-z])n[=_-]?(\d{2,6})(?!\d)", path, re.I)
    return int(m.group(1) or m.group(2)) if m else None


def build_from_text(text: str) -> str:
    """A genome build named in a path or header ("…_b38…", "hg19")."""
    if re.search(r"grch38|hg38|(?<![a-z0-9])b38(?![0-9])", text, re.I):
        return "GRCh38"
    if re.search(r"grch37|hg19|(?<![a-z0-9])b37(?![0-9])", text, re.I):
        return "GRCh37"
    return ""


# ── is the row ready? ────────────────────────────────────────────────────────

# issue → what a curator has to do about it (also shown on the review page).
ISSUES: dict[str, str] = {
    "not_full_summary_statistics": "The file is a selected subset (significant, lead or fine-mapped QTLs); the manifest needs every tested pair.",
    "scope_unknown": "Could not tell whether the file holds every tested pair or a subset — open it and check.",
    "required_column_missing": "A column the importer requires (chrom, position, ref, alt, p-value, phenotype) was not found.",
    "columns_need_deriving": "Required columns must be derived first (split the variant id or locus, or convert −log10 p / chi-square to p).",
    "no_header": "The file has no header row; column names are borrowed from a sibling file or must be added.",
    "header_inferred_from_sibling": "This file was not opened; its columns are assumed to equal a sibling's.",
    "header_not_read": "The header could not be read (format, size or access).",
    "mixed_headers": "Files of this group do not share one header; the unopened ones need checking.",
    "format_needs_conversion": "Not a delimited text file (parquet, spreadsheet, archive member); convert to .tsv.gz first.",
    "allele_order_ambiguous": "Alleles are named by number (A1/A2); confirm which one the effect refers to.",
    "maf_col_is_allele_frequency": "maf_col points at an allele frequency, not a minor allele frequency.",
    "effect_scale_check": "The effect column is not obviously a regression slope (all sampled values lie in 0–1).",
    "phenotype_id_not_ensembl": "Phenotype ids are not Ensembl gene ids; the importer cannot derive gene_id.",
    "phenotype_values_not_seen": "No sample values were read, so the phenotype id format (and gene_id_mode) is unchecked.",
    "peak_id_needs_reformat": "Peak ids must be rewritten as chr_start_end.",
    "peak_id_not_chr_start_end": "Peak ids carry no coordinates; a peak coordinate file is needed.",
    "qtl_type_unknown": "Neither the path nor the paper says which QTL type this file holds.",
    "qtl_type_not_in_manifest": "The manifest holds eQTL, sQTL, caQTL and pQTL so far.",
    "context_unverified": "The tissue or cell context has no quote found in the paper or repository text.",
    "sample_size_missing": "No sample size was found.",
    "sample_size_unverified": "The sample size has no supporting quote that contains the number.",
    "sample_size_study_level": "The sample size is the study's, not this context's.",
    "population_missing": "No ancestry was found.",
    "population_unverified": "The ancestry has no supporting quote.",
    "genome_build_unknown": "Genome build not established; the manifest's data are GRCh38.",
    "needs_liftover": "Coordinates are GRCh37/hg19; lift over to GRCh38.",
    "new_level_1_context": "level_1_context is not one the manifest already uses.",
    "label_format": "biocontext or a context level contains characters the manifest does not use.",
    "duplicate_biocontext": "Another row of this dataset and QTL type has the same biocontext.",
    "paper_full_text_not_read": "The paper's full text could not be read; facts come from the abstract or repository only.",
    "publication_already_in_manifest": "The manifest already has rows for this publication.",
    "access_not_open": "The location is not an open download.",
    "llm_facts_missing": "The LLM step did not run or failed; context, sample size and ancestry are empty.",
    "data_not_downloaded": "The files are not in the data folder yet (downloading was off).",
    "data_too_large": "Not downloaded: over the per-paper size limit or the free disk space; raise the limit or download elsewhere.",
    "download_failed": "Downloading the files failed; see the data section for the error.",
    "processing_failed": "No processing plan produced values the importer accepts; see the plans that were tried.",
}
BLOCKING = {"not_full_summary_statistics", "required_column_missing", "header_not_read"}
# Conversions a curator can do without going back to the authors.
PROCESSING = {"columns_need_deriving", "no_header", "format_needs_conversion",
              "peak_id_needs_reformat", "needs_liftover",
              "peak_id_not_chr_start_end",  # the importer stops on such ids (parse_caqtl_phenotype_loci)
              "data_not_downloaded", "data_too_large", "download_failed", "processing_failed"}


def status_of(issues: list[str]) -> str:
    """"not_loadable", "needs_processing", "needs_review" or "ready" — the worst issue wins."""
    found = set(issues)
    if found & BLOCKING:
        return "not_loadable"
    if found & PROCESSING:
        return "needs_processing"
    return "needs_review" if found else "ready"
