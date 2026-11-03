---
name: build-manifest
description: The fixed procedure the manifest agent (src/backend/dataqueryweb/agent.py) follows to turn one saved record into LocusCompare qtl_manifest rows — read the paper, inventory every data location, choose the full summary-statistics tables, download, process, validate, submit. agent.py puts this file into the system prompt of every build; edit it here to change how the LLM works.
metadata:
  version: "2026-11-03"
---

# Build manifest: one paper → manifest rows

Work through the phases in order. Each phase ends with a check; do not start the next phase
until it holds. The step budget is about 80 tool calls; a typical paper needs 20–40. Rough
allocation: phases 1–2 ≤ 15 calls, phase 3 ≤ 15, phase 3b (only when needed) ≤ 15, phases 4–6
the rest, and always keep 3 calls for phase 7.

## The manifest files

| File | Role | What you do with it |
|---|---|---|
| Reference manifest: `reference/qtl_manifest.tsv`, this project's copy of LocusCompare's `scripts/qtl_manifest.tsv` | What LocusCompare already loads. Read only, never written | The first message says whether this paper is already in it, under which dataset name and with which rows (qtl_type + biocontext). The instructions list every dataset it holds and its level_1_context vocabulary |
| `manifest/qtl_manifest_candidates.tsv` | Every row you submit, with status, issues and evidence | Written by the program from your submit; a person reviews it on the /manifest page |
| `manifest/qtl_manifest.tsv` | The rows a person approved, in the reference manifest's 27 columns, ready to append to it | Written by the program after approval |

You never write these files: no tool can, and submit is the only way rows get in. GWAS
summary statistics belong to LocusCompare's separate `gwas_manifest.tsv` and are not
submitted here; list a paper's GWAS tables in skipped_tables as "GWAS, not QTL".

Reference checks:
- A paper already in the reference manifest keeps its dataset name there; submit only the
  QTL types or contexts it does not have yet, and skip the rest as "already in the manifest".
- A resource the reference manifest already holds (GTEx_v10, eQTL Catalogue datasets, …)
  is never downloaded again, even when this paper ships a copy.
- level_1_context comes from the reference vocabulary; a new value is allowed only when none
  fits, and it will be flagged for a person.

The 27 columns of a row, in file order. One row describes one table of QTL summary
statistics (one QTL type in one context); LocusCompare's importer
(`load_gwas_qtl_catalog.py`) reads the file it points at with these settings. Examples are
real rows of the reference manifest.

**Who and what**

| Column | What it means | How to fill it | Example |
|---|---|---|---|
| `dataset` | The collection the row belongs to. LocusCompare groups tables by `dataset` + `qtl_type`, and that pair carries one population | The resource or cohort name the paper uses; else `FirstAuthor_Year`. If the reference manifest has the paper, its name | `GTEx_v10`, `Tenk10k`, `eQTL-Catalogue` |
| `source_project_id` | The study inside the dataset. It matters for collections of many studies (eQTL-Catalogue → `INTERVAL`, `Schmiedel_2018`); together with `dataset`, `qtl_type` and `biocontext` it identifies a table, so reloading a row updates that table | The paper's project id if it names one, else the same as `dataset` | `Tenk10k` (dataset Tenk10k) |
| `use_in_lc2` | Whether LocusCompare 2 shows the table to users | Always `FALSE`; a person switches it on after review | `FALSE` |
| `qtl_type` | The molecular phenotype tested | `eQTL` (expression), `sQTL` (splicing), `caQTL` (chromatin accessibility) or `pQTL` (protein) | `caQTL` |

**Context**

| Column | What it means | How to fill it | Example |
|---|---|---|---|
| `biocontext` | The name of this table's context, unique within dataset + source_project_id + qtl_type | The label the files use for it, or the cell type / tissue; underscores, no spaces | `CD14_Mono`, `macrophage_Salmonella`, `Whole_Blood` |
| `level_1_context` | Broad tissue group used to browse and filter. Required: the importer refuses a row without it | A value from the reference vocabulary in the instructions ("Blood" covers whole blood, PBMC, immune cells, plasma, LCLs) | `Blood`, `Brain` |
| `level_2_context` | Finer context below level 1 | Cell type, sub-region, or cell type + condition; empty for a bulk tissue with nothing finer | `CD14_Mono`, `macrophage_IFNg`, empty |
| `population` | Genetic ancestry of the donors | `EUR`, `EAS`, `AFR`, `AMR`, `SAS`, or `ALL` for a multi-ancestry cohort | `EUR` |
| `sample_size` | Number of genotyped donors whose data went into this context's QTL mapping | That number for this context, from the paper; not cells, not samples before QC, not the GWAS cohort | `922` |

**The file**

| Column | What it means | How to fill it | Example |
|---|---|---|---|
| `file_path` | The table the importer loads; a `chr*` glob loads every per-chromosome file of one context | The path, relative to the data folder, that passed validate; it starts with the record code's folder | `Chen_2025_blood/CD14_Mono/TenK10K.cis_qtl_pairs.chr*.tsv.gz` |
| `delimiter` | Column separator, passed to `pandas.read_csv` | The delimiter validate used, written `\t` for tab; processed files are always `\t` | `\t` |

**Column names inside the file.** Each value is the name of a header in the file, not a
value. The values in those columns must follow the required formats in phase 5; the importer
keeps only single-base ref/alt on autosomes.

| Column | The file's column that holds | Required | Example |
|---|---|---|---|
| `chrom_col` | Chromosome of the variant, digits only (`1`, not `chr1`) | yes | `chrom` |
| `position_col` | Base-pair position of the variant in the file's genome build, digits only | yes | `pos` |
| `ref_col` | Reference (non-effect) allele, upper case | yes | `ref` |
| `alt_col` | Alternative allele, the one the effect refers to, upper case | yes | `alt` |
| `variant_id_col` | Variant id `chr<chrom>_<pos>_<ref>_<alt>` joined by underscores (`chr1_12345_A_G`), equal to the four columns above | yes | `variant_id` |
| `pval_col` | Nominal p-value of the association, not FDR, q-value or permutation p | yes | `pval_nominal`, `pvalue` |
| `phenotype_id_col` | Molecular phenotype tested: gene, splice cluster, peak or protein id | yes | `phenotype_id`, `molecular_trait_id`, `peak_region` |
| `beta_col` | Effect size of the alt allele; stored as 0 when empty | no | `slope`, `beta` |
| `se_col` | Standard error of the effect size; stored as 0 when empty | no | `slope_se`, `se` |
| `maf_col` | Minor allele frequency; stored as 0 when empty | no | `maf` |
| `rsid_col` | dbSNP rs id (`rs<digits>`), empty for a variant dbSNP does not hold; filled by add_rsid when the file has none | yes | `rsid`, `rs_id_dbSNP155_GRCh38p13` |

**Genes and provenance**

| Column | What it means | How to fill it | Example |
|---|---|---|---|
| `gene_id` | Not read by the importer (it derives the gene from `phenotype_id`) | Always empty | empty |
| `gene_id_mode` | How the importer gets the Ensembl gene from `phenotype_id`: `self` (the id is the gene, `ENSG…`, version allowed) or `phenotype_last_segment` (the gene is the last `:`-separated part, e.g. leafcutter `chr1:100:200:clu_1:ENSG…`) | One of the two for eQTL/sQTL/pQTL; empty for caQTL, whose phenotype ids must be `chr_start_end` | `self` |
| `median_tpm` | Name of a column with each gene's median expression (TPM), stored per phenotype | The header name if the file has such a column, else empty | `median_tpm`, empty |
| `url` | Where a person finds the data | The data landing page (repository record, portal page) | `https://huggingface.co/datasets/anglixue/TenK10K_multiome` |
| `publication` | The paper the data come from | `https://doi.org/<doi>` | `https://doi.org/10.1126/science.aaz1776` |

Besides the 27 columns each row carries `source_url`, `genome_build`, `evidence` and `note`
(see submit).

## Curated failure checks (apply during the phases below)

These checks come from the curator workbook `processed&needsyou.xlsx`, Sheet1, rows 2–61
(reviewed 2026-11-02). Treat its diagnoses and proposed fixes as leads to verify against
actual sources, not evidence to quote. They change future builds, not existing records.

### Separate software names, phenotype types and downloadable outputs (#634)

A string ending in QTL is not by itself a QTL-type definition. Determine its role from the
Methods, software availability, repository README and actual result-file columns:

- Record separately the **software / method**, **tested molecular phenotype**, and
  **available association table**. TensorQTL, SAIGE-QTL and sc-pcQTL are software names;
  never assign a manifest QTL type by stripping their prefixes or matching title tokens.
- Inspect each phenotype definition: gene expression, splicing, chromatin accessibility,
  protein abundance, or another measured/derived trait. A paper introducing software may
  also release new real-cohort QTL results; do not reject it simply for being a methods paper.
- Distinguish new real-data associations, reanalysed reference data, simulated/example
  inputs, cluster counts, enrichment, fine-mapping and colocalisation tables. A software
  name or a repository containing "summary results" proves neither presence nor absence
  of a usable nominal variant–phenotype association release. Inventory the actual files.
- For sc-pcQTL (10.64898/2026.08.18.745314), **sc-pcQTL is the framework** and the authors
  also use **pcQTL for principal-component phenotypes of gene clusters**. These are distinct
  statements, both of which can be true. Do not infer ordinary single-gene eQTL rows from
  cluster-PC results, or reject all eQTL-labelled analyses without inspecting them.
  The author repositories are https://github.com/ZhouLabGenetics/sc-pcQTL and
  https://github.com/ZhouLabGenetics/sc-pcQTL_code; the older joookerz URL may be stale.
  The source article is https://www.biorxiv.org/content/10.64898/2026.08.18.745314v1.full.
  If that page is blocked, state it; do not say you read the full text. The official bioRxiv
  API and the authors' site can support the abstract/method identity, not missing full text.
- Inspection of Zenodo 21222687's supplementary archive on 2026-11-03 found cell-type cluster
  counts, enrichment, SuSiE graph/signal groups, loading/effect summaries, sensitivity and
  S-LDSC outputs. eQTL labels inside a colocalisation table are not themselves full eQTL
  associations. Explain missing nominal tables on their content, not on the software name.
  Recheck for a later release before relying on this historical inventory.
- Keep these outcomes separate: **not a QTL association table**, **genuine QTL results of a
  phenotype the current importer does not support**, and **access/content not yet verified**.
  An unsupported type is not proof of "not QTL". Do not write a human curation decision on
  the curator's behalf or silently replace it with an AI verdict. When a human has marked
  a record `not_qtl`, do not rebuild it unless that decision is explicitly cleared.

### Use documented, study-provided variant maps when association rows need them (#637)

- A companion file is not automatically an unrelated annotation resource. On GEO and similar
  repositories, read the landing page's supplementary-file descriptions and the study's
  `README.txt` / `README.md` before classifying an association table as unusable. Follow the
  README's stated relationship between files and look for a study-specific SNP/variant map
  (for example `GSE227136_ILD_eQTL_SNPinfo.tsv.gz`) when association files contain rsIDs or
  lack chromosome, position, or the non-effect/effect allele pair.
- Inspect the map header, representative rows, stated genome build, key uniqueness, and allele
  definitions. Join it only to enrich association rows from the **same study and release**, by
  the exact documented variant key (usually rsID); never join association rows to other
  association results, mix studies/releases, or infer mappings from a filename alone. Keep
  every original association row and its nominal p-value, phenotype, and effect estimate intact.
- Verify map coverage and resolve each key unambiguously before processing. If rsIDs are
  duplicated, use documented allele information to disambiguate; if a key remains ambiguous
  or unmatched, do not guess or silently multiply/drop rows. Report coverage and unresolved
  counts. Confirm the map's coordinate build and preserve it in evidence; liftover only after
  a valid coordinate-and-allele mapping is established.
- Determine which allele the association's effect estimate refers to from the README or method
  documentation (for example, `assessed_allele`). In this manifest, `alt` is the effect allele
  and `ref` is the other/non-effect allele; do not assume the map's `ref`/`alt` names already
  use that convention. Check the assessed allele is exactly one of the mapped pair, assign
  effect direction accordingly, and validate the output. If this cannot be established, leave
  the allele fields unresolved instead of manufacturing them.
- A documented same-study variant map may be downloaded and used as a narrowly scoped companion
  input even though it is not itself an association table. Do not download generic external
  annotation databases or reference resources for this purpose. Never submit the map as a QTL
  row.
- Treat large companion maps as real inputs with a memory plan. Check their compressed size
  against the per-paper download budget, then stream/chunk the map and association tables or
  build a bounded on-disk key index; do not load a near-GB compressed map or multi-GB tables
  fully into RAM on this 3-GB server. Preserve the original downloads and report the number and
  fraction of association rows that mapped.
- For Natri et al. (2024), the previous build inspected GEO's public supplementary directory
  but stopped at the LIMIX files' missing second allele. Before making that conclusion again,
  inspect `GSE227136_README.txt` and `GSE227136_ILD_eQTL_SNPinfo.tsv.gz`, then test the
  documented rsID/allele mapping against each LIMIX context. The prior "annotation prohibited"
  conclusion was too broad; do not repeat it. Verify the map's actual columns and compatibility
  instead of assuming every row can be resolved. GEO lists the README at 2.2 KB and SNPinfo
  map at about 928.5 MB compressed; inspect the README and actual header rather than skipping
  the map or downloading unrelated raw-expression files.

### Recover missing facts from the correct release (#84, #123, #130, #165, #168, #262, #467)

- Before leaving sample size, ancestry or genome build missing, follow the repository's
  publication link, any "Now published in" link, and the publisher / Europe PMC full-text
  route already identified for this paper. Read Methods and the relevant tables, captions,
  supplementary cohort descriptions and Data/Population/Sample description sections.
  An abstract or a matching keyword is not a substitute for reading the section.
- For each file group, establish the release / phase, publication DOI and coordinate build
  together. Check the linked project's README, release notes and processing scripts when
  the paper omits the build. BrainSeq Phase 1 preprint and a later hg38 release are not
  interchangeable; a current repository README does not establish an older file's build.
  A companion paper may support the build only when its methods demonstrably describe
  this same release and analysis. Record that connection and quote the source actually read.
- Take sample size from the context's QTL mapping after QC (including Table 1 and supplementary
  sample tables), not the study total or a single arbitrary association row. If sample size
  varies by variant, report that variation and the documented mapping cohort size; do not
  invent a single count. Re-open a table as text with peek_file / inspect_local so the exact
  supporting row is visible before quoting it.
- Identify ancestry from explicit cohort descriptions or reported ancestry results. A PCA
  reference panel containing CEU/FIN/GBR/IBS/TSI does not alone establish the study donors'
  ancestry. If using a named cohort's description, verify that it applies to these donors.
- Use the existing context vocabulary first. Preserve actual brain regions, cell types and
  conditions from the file and paper; do not collapse cortex, caudate and cerebellum into
  one generic brain context. If no broad label fits (e.g. iPSC), retain the accurate label
  and flag vocabulary review instead of inventing membership in an unrelated tissue.

### Inventory and access: distinguish the actual obstacle (#619, #616, #612, #609, #596, #591)

- A connection reset, truncated archive, unexpected EOF or timeout is a transport failure,
  not evidence of controlled access. For transient failures, allow at most two retries
  after the first attempt within the remaining call budget; inspect existing local files
  and retry only missing or incomplete inputs using the normal download tools. Do not
  delete valid data, repeatedly rebuild the whole paper, or submit partial archives as
  complete releases. If still broken, note_location `error` with the URL and exact failure.
- For HTTP 403 / client challenges, use one retry and the linked published version, PMC /
  Europe PMC mirror, repository API or documented download route. A blocked bioRxiv v1
  page does not mean that the underlying study requires an access application.
- When a portal has no file links, inspect its linked data collections, project pages and
  documented file/download endpoints within phase 2's link-depth and call budgets. A landing
  page (including HGSVC2) is not the complete inventory. Do not guess private endpoints.
- For GEO, inspect the public supplementary directory and descriptions. Series Matrix,
  gene-by-individual expression matrices and raw counts are not QTL association statistics;
  download them only if their actual contents are the required association table. A filename
  containing "eQTL_Mapping" is insufficient.
- Read the GEO README before deciding whether a companion file is unrelated. If an association
  file references rsIDs or lacks coordinate/allele columns, look for a README-documented,
  same-study variant map (see the curated #637 procedure), inspect it, and test the mapping
  before skipping the association data.
- Keep these explanations distinct in note_location and the final summary: (1) confirmed
  application / author request, quoting its instructions; (2) manual browser/session download
  (e.g. Dryad), which need not require an application; (3) network or publisher blocking;
  (4) raw/source data, annotation, GWAS or other non-QTL output; (5) no download route found,
  access requirements unconfirmed. Use `manual` for a genuine person/browser action and
  `not_data` for unrelated material. Do not describe an empty JS page as an empty data file.

### Select, normalize and validate without changing what the data mean

- PDF/DOCX papers and documentation may supply evidence and links, but are not delimited
  QTL inputs. An XLSX/ZIP/TSV file is only a candidate: inspect its columns and contents.
  "Most significant variant per tested gene", "same as ... with annotation", fine-mapping,
  colocalisation, enrichment and other downstream analyses are not full nominal statistics
  (#140, #262). Prefer the full association table; apply phase 3b's explicit non_full fallback
  only to eligible association tables. Do not relabel subsets as full or delete local files.
- For AF/EAF columns (#123, #130), first verify that each value is a per-variant frequency
  for a biallelic variant, in [0, 1], not an allele count or percentage. Then derive a new
  `maf = min(af, 1-af)` column, preserving the source frequency. Missing stays empty; invalid
  values are reported. Point maf_col to the derived column, not the original AF/EAF column.
  Do not apply this formula to multiallelic or undocumented frequencies.
- For chromosome shards of the same analysis (#467), normalize siblings and submit one row
  with an exact chr-file glob per QTL type/context, not one row per chromosome. Do not mix
  different releases, analyses or methods into a single table. For multiple methods of the
  same context (#165), select the paper's primary analysis and explain alternatives in
  skipped_tables; distinguish genuine subregions/conditions in biocontext without inventing
  tissue labels just to silence duplicate_biocontext.
- Separate gene-, exon-, transcript-, splicing- and APA-level results (#165, #467). ENSE,
  ENST, splice coordinates and gene symbols are not ENSG gene ids. Use only a documented
  gene mapping already supplied in the permitted study inputs; keep original ids and
  report unmapped/ambiguous cases, without guessed or one-to-many expansions. Respect the
  runtime's ban on downloading external gene annotation or joining unrelated tables. If
  mapping requires such resources, describe the missing resource for a person to resolve.
- isoQTL and apaQTL are not currently supported manifest types: do not relabel them eQTL or
  sQTL merely to pass validation. Record them in skipped_tables with "unsupported QTL type;
  importer support required". A skill change alone cannot add importer support.
- Complete recoverable processing before submitting: unpack/read archive members, convert
  supported spreadsheets to text, split packed variant ids, derive nominal p where justified,
  and liftover confirmed GRCh37 tables using phases 5–6. A proposed conversion is not a
  processed result: require the actual local output and passing validation.

In the final summary, group repeated missing/unverified facts by field and explain the
cause once, while retaining the program's issue codes. Distinguish build completion, local
files that passed validation, partial statistics and full data still needing manual access.
Do not claim to have fixed UI issue duplication or expanded supported QTL types.

## Phase 1: Read the paper (read_paper)

Run these searches, one read_paper call each, and note what you find:

1. `data availab|code availab|accession|deposited|zenodo|figshare|synapse|ega|dbgap|portal`
2. `genotyp|imputation|GRCh3|hg19|hg38|build`
3. `eQTL|sQTL|caQTL|pQTL|QTL mapping|tensorqtl|matrixeqtl|fastqtl|qtltools|limix|nominal`
4. `donors|individuals|participants|samples` (sample size per context)
5. `ancestry|European|African|Asian|Hispanic|population`

If the paper text is only an abstract, open the article with open_url (DOI or publisher URL)
and use `find` with the same patterns.

Write down before moving on:
- the QTL types the paper mapped itself, and the contexts (tissues, cell types, conditions)
- for each context: donors genotyped and used for QTL mapping, ancestry, genome build
- every data location named in the paper, plus the saved locations in the first message
- which resources the paper only reused (they are never downloaded)
- from the first message: whether the reference manifest already has this paper, and which
  of its QTL types and contexts are still missing there

**Check:** you have a list of data locations and a list of contexts. If the paper says the
results are controlled-access or on request only, do not stop here: still check the saved
locations (phases 2–3) and the paper (phase 3b), since a summary table is often public even
when individual data are not.

## Phase 2: Inventory every data location

For each location, in this order of promise: summary-statistics repositories (Zenodo,
figshare, OSF, Hugging Face, FTP) → lab pages and portals → GitHub → GEO and the rest.

- Repository or folder: list_files (use `pattern` to narrow long listings, e.g. `*eqtl*`).
- Landing page: open_url, then follow the links that lead to files ("Download", "Data",
  "Files", FTP/HTTP mirrors). Two levels of links at most.
- Call note_location for every location once you know what it is:
  - `listed`: files found
  - `manual`: a person must act (login, data access application, browser-only portal,
    Synapse, Google Drive, Dropbox, "on request")
  - `error`: unreachable after the bounded retries described in the curated failure checks
  - `not_data`: a paper, a code repository without results, an annotation resource
  - `duplicate`: the same files as another location

**Check:** every location has a note_location, and you have a list of candidate tables
(name, URL, size).

## Phase 3: Choose the tables (peek_file before any download)

Open one or two files of each group of sibling files with peek_file; files that differ only by
chromosome or context share their layout. Classify each table:

| The table holds | Decision |
|---|---|
| every tested variant–phenotype pair (nominal, allpairs, full_assoc, cis_qtl_pairs, sumstats) | **keep** |
| significant, lead, eGenes, permutation, conditional, fine-mapped, SuSiE, colocalisation hits | skipped_tables: "subset, not full summary statistics" (a non-full table may come back in phase 3b when no full one exists) |
| mQTL, meQTL, metabolite, edQTL, apaQTL or any type other than eQTL/sQTL/caQTL/pQTL | skipped_tables: "qtl_type not in the manifest" |
| results of a resource the paper reused (GTEx, eQTLGen, eQTL Catalogue, …) | skipped_tables: "already in the manifest as …" / "not this paper's results" |
| a non-human cohort | skipped_tables: "not human" |
| genotypes, expression matrices, peaks, counts, covariates | do not list as a table; note the location `not_data` |

**Full or not: check every candidate table, whatever its name says.** Names like "summary
statistics", "allpairs" or "nominal" do not prove a table is full, and a deposit can hold
only part of what was tested. Compare the table with the paper, with numbers (count with
run_python on a downloaded file; peek_file only shows the first rows):

| Check | Full summary statistics | Not full |
|---|---|---|
| Phenotypes in the table vs phenotypes the paper tested (Methods: "N genes / proteins / peaks after QC were tested") | about the same | far fewer |
| Same count vs the paper's significant count (eGenes, pGenes, …) | larger | equal → only the significant phenotypes |
| Variants per phenotype vs the cis window (±1 Mb usually gives thousands) | thousands, close to all variants in the window | a few, or a fixed handful |
| p-values | spread over 0–1 (median near 0.4–0.5) | all below a threshold → only significant pairs |
| File names, README, scripts that read the file | — | "demo", "example", "test", "illustrative", "not completely included", a `# demo` comment → an example extract |
| Size | usually > 100 MB compressed per context | small |

Write the numbers down: they decide the `non_full_reason` and go into the `scope_note` if
the table is kept in phase 3b.

For each kept table write down from the peek: delimiter, which column holds chrom, position,
ref, alt, nominal p, phenotype id (and beta, se, maf, rsid, variant id), and what must change.

**Check:** each candidate is either kept with its column map, or has a skipped_tables reason.
If no table of full summary statistics is kept for a QTL type the paper mapped, do phase 3b
before going on.

## Phase 3b: Look in the paper for the full summary statistics

The saved locations are often only part of what the authors released: the significant hits,
example data, or one version of a deposit. Before deciding that the full results are not
public, read the paper again as a curator would and work out where the authors put them.
You have no search engine: follow only what the paper, its other version, the repository
pages and the READMEs name. At most about 15 calls.

**Read, do not just match words.** Use read_paper with a `query` only to find where the
relevant sections start, then read those sections in order with `start` / `count` and think
about what each sentence says. Read in full:
- Data availability and Code availability, including footnotes and the end-matter
- the Methods paragraphs on QTL mapping and its outputs (what was tested, what was written
  out, at which threshold)
- Results sentences that name a resource, portal, browser or deposit
- the descriptions of supplementary data files and source data

Sentences to look for, in any wording: "full / complete / nominal summary statistics are
available at…", "all tested associations can be downloaded from…", "results can be browsed
or downloaded at…", "deposited under accession…", "available on request / under controlled
access". Decide for each candidate what it holds (every tested pair, only significant
results, raw data) from what the paper says about it, not from its name alone. Accession and
host patterns help find the places (`GCST\d+`, `QTD\d+`, `syn\d+`, `EGA[SD]\d+`, `phs\d+`,
zenodo, figshare, osf.io, dryad, huggingface, synapse, github, gitlab, ftp).

Then follow what the paper points to:
1. The article on the publisher's site (open_url on `https://doi.org/<doi>` with `find` set
   to the data availability or supplementary section). It often lists links the full-text
   copy lacks.
2. The other version of the paper: for a preprint its journal article ("Now published in" on
   the preprint page), for a journal article its preprint. Deposits are often added or
   changed between versions.
3. More of the deposits you already know: newer versions of a Zenodo or figshare record (the
   record page lists its versions), a GitHub repository's README, releases and data folders,
   links on lab or portal pages ("Download", "Data", "Resources").
4. The page of every accession the paper names (GWAS Catalog, eQTL Catalogue, Synapse, EGA,
   dbGaP).

Every new location goes through phases 2 and 3: note_location, then peek_file before any
download. When phase 3b finds the full summary statistics, continue with phase 4.

**When no full table is public.** If after this search there is still no full table for a QTL
type and context:
1. Call note_location once with url `(paper)` and status `manual`, quoting what the paper says
   about the full results ("available on request", "controlled access via EGA…", or "no
   statement found").
2. Keep the most complete non-full table instead: one that lacks some tested
   variant–phenotype pairs. One per QTL type and context. Prefer, in
   this order: every tested variant for a subset of phenotypes (e.g. all cis variants of the
   significant genes) → all significant variant–phenotype pairs → lead variants only. It must
   still be a table of associations with chrom, position, ref, alt, p-value and phenotype id
   (or columns they can be derived from); conditional, fine-mapping and colocalisation
   outputs are not kept.
   **One source only.** The kept table is one table from one place: one file, or the sibling
   files of one release that split the same table (per chromosome or per context). Never
   combine rows from different files, deposits, versions or analyses to cover more
   phenotypes or variants — a merged table holds a different selection for each part and its
   reason cannot be stated. Between equally complete candidates, take the one the paper's data
   availability statement names. Every other candidate goes to skipped_tables with its own
   reason.
3. Download, process and validate it like a full table (phases 4–6), and submit it with
   `scope: "non_full"` and say clearly **why it is not full summary statistics**:
   - `non_full_reason`, one of:

     | Reason | The table holds |
     |---|---|
     | `significant_phenotypes_only` | every tested variant, but only for the significant phenotypes (e.g. eGenes) |
     | `significant_pairs_only` | only the variant–phenotype pairs that passed a significance threshold |
     | `lead_variants_only` | only the lead (top) variant of each phenotype |
     | `example_subset` | an example or truncated extract of the results |
     | `some_chromosomes_or_regions` | only some chromosomes or regions |
     | `selected_phenotypes` | only some phenotypes, selected for another reason than significance |

   - `scope_note`, one or two sentences with numbers: what the paper tested, what the table
     holds, how you know, and where the full results would be. For example: "Holds nominal
     results for the 36 significant pGenes only (108,872 pairs); the paper tested 5,192
     proteins (Methods). Full results: available on request (Data availability)."
4. The summary says what a person would have to do to get the full results (contact the
   authors, apply for access, wait for a deposit).

## Phase 4: Download

- One download call per group: `listing_url` + `pattern` for many files, `urls` for a few.
- Every file of the paper lands in one folder named by the record code (first message, e.g.
  `Sun_2026_skin/`), whatever `dataset` you pass; each `file_path` starts with that folder.
- Download every chromosome and every context of a kept group, not a sample.
- Before a large download, compare the total size with the paper's download limit in the first
  message. When it does not fit, download whole contexts in order of importance (the
  paper's main tissue or cell type first) and list the rest in skipped_tables with
  "over the download limit".

**Check:** inspect_local on one downloaded file shows the same header as the peek.

## Phase 5: Process (only when needed)

**Required value formats.** Every row of a submitted file must hold exactly these, and
validate checks every row it reads:

| Column | Format | Right | Wrong |
|---|---|---|---|
| chromosome | digits only; autosomes | `1` | `chr1`, `X`, `chrX` |
| position | digits only | `12345` | `12345.0`, `1.2e4` |
| ref, alt | upper-case A/C/G/T/N | `A`, `TG` | `a`, `t` |
| variant id | `chr<chrom>_<pos>_<ref>_<alt>` joined by underscores, equal to those four columns | `chr1_12345_A_G` | `1_12345_A_G`, `1:12345:A:G`, `chr1:12345:A:G`, `chr1_12345_A_G_b38`, `rs123` |

A file can be used as it is only when it is one delimited text table (plain or gzip), with a
header, with chrom, position, ref, alt, variant id, nominal p and phenotype id each in its own
column, and all values already in these formats. Most files are not (GTEx writes `chr1` and
`chr1_12345_A_G_b38`), so expect to process. Write one run_python script per group that writes
`<original name>_processed.tsv.gz` next to each input. Rules for every script:

- relative paths only; read in chunks of at most 200000 rows; write gzip, tab-separated,
  header once; keep all original columns and add the new ones
- write the standard columns `chrom`, `pos`, `ref`, `alt`, `variant_id` in the formats above;
  if the file already has a column of that name, keep its original values as
  `<name>_original`
- never fill a value that is not in the file; drop only rows that are unparseable or not on
  an autosome (the importer drops those anyway), and print how many were dropped
- a missing value stays empty: never write placeholder words (`not_available`, `none`, `-`,
  `.`) into a cell; validate refuses text in p-value, effect, standard error and MAF columns
- read the association input and, only when needed, a README-documented same-study/same-release
  variant map keyed by a documented identifier; use a validated, allele-resolved lookup to add
  variant metadata without changing association rows. Never join association rows from
  different files or analyses, or use an undocumented or external map
- process large companion maps and association files in bounded chunks; avoid holding the full
  compressed or expanded files in memory
- loop over the files of the group in one script; print one line per file written

Recipes (adapt the column names):

```python
import glob, gzip, math
import pandas as pd

for src in sorted(glob.glob("eqtl/chr*.txt.gz")):
    dst = src.replace(".txt.gz", "_processed.tsv.gz")
    first, dropped = True, 0
    with gzip.open(dst, "wt") as out:
        for df in pd.read_csv(src, sep=r"\s+", dtype=str, chunksize=200000):
            # variant id 1:12345:A:G or chr1_12345_A_G_b38 → chrom, pos, ref, alt
            parts = df["variant_id"].str.replace("_b3[78]$", "", regex=True).str.split(r"[:_]", expand=True)
            df = df.rename(columns={c: f"{c}_original" for c in ("chrom", "pos", "ref", "alt", "variant_id")
                                    if c in df.columns})
            chrom, pos, ref, alt = parts[0], parts[1], parts[2], parts[3]
            # the required formats: digits, upper case, chr<chrom>_<pos>_<ref>_<alt>
            df["chrom"] = chrom.str.replace(r"(?i)^chr", "", regex=True)
            df["pos"] = pos.str.strip()
            df["ref"], df["alt"] = ref.str.strip().str.upper(), alt.str.strip().str.upper()
            ok = (df["chrom"].str.fullmatch(r"[0-9]+") & df["pos"].str.fullmatch(r"[0-9]+")
                  & df["ref"].str.fullmatch(r"[ACGTN]+") & df["alt"].str.fullmatch(r"[ACGTN]+")).fillna(False)
            dropped += int((~ok).sum())
            df = df[ok].copy()
            df["variant_id"] = "chr" + df["chrom"] + "_" + df["pos"] + "_" + df["ref"] + "_" + df["alt"]
            # -log10 p → p
            # df["p"] = (10 ** -pd.to_numeric(df["log10p"], errors="coerce")).astype(str)
            # z → two-sided p
            # df["p"] = pd.to_numeric(df["z"], errors="coerce").abs().map(lambda z: math.erfc(z / math.sqrt(2)))
            # 1-df chi-square → p
            # df["p"] = pd.to_numeric(df["chisq"], errors="coerce").map(lambda x: math.erfc(math.sqrt(x / 2)))
            # caQTL peak chr1:1000-2000 → chr1_1000_2000
            # df["peak"] = df["peak"].str.replace(r"[:-]", "_", regex=True)
            df.to_csv(out, sep="\t", index=False, header=first)
            first = False
    print("wrote", dst, "dropped", dropped, "rows (unparseable or not autosomal)")
```

- No header: give the columns names from the paper's or README's description; if no text
  describes them, the table cannot be used (skipped_tables: "no header and no column
  description").
- Archives: read members with `zipfile` / `tarfile` and write one processed file per member;
  do not unpack everything first.
- A1/A2 alleles: use the paper's or software's definition of the effect allele for alt; if none
  is stated, keep A1 = alt and say so in the row's note.

**Check:** inspect_local on one processed file shows `chrom` like `1`, `pos` digits,
upper-case `ref`/`alt` and `variant_id` like `chr1_12345_A_G`.

## Phase 5a: GRCh37 → GRCh38 (liftover)

Every row is delivered on GRCh38. Establish the build of each table's coordinates first: a
build named in the file name or header (`hg19`, `b37`, `GRCh37`), the README, or the paper's
methods (search `GRCh37|hg19|GRCh38|hg38|imputation|reference panel`). When the build is
GRCh37/hg19, call liftover on the processed table:

- `file_path`: the processed table (`<name>_processed.tsv.gz`; a `chr*` glob for split files)
- `columns`: its `chrom_col`, `position_col`, `ref_col`, `alt_col`, `variant_id_col`

liftover converts each position with UCSC's hg19ToHg38 chain (local copy), complements the
alleles of variants that land on the minus strand, rebuilds `variant_id` on GRCh38, keeps the
GRCh37 coordinates as `chrom_grch37` / `pos_grch37`, and drops variants with no GRCh38
position on the same chromosome. Run it once per file (a second run is refused), before
add_rsid. Never convert coordinates in your own Python and never download a chain file.

Submit the row with `genome_build: GRCh38` and the quote that names the source build
(GRCh37); report the share kept (`lifted` / `rows`) in the row's note. When the build cannot
be established, do not lift over; say so in the note.

**Check:** inspect_local shows `pos` changed, `chrom_grch37` / `pos_grch37` holding the old
coordinates, and `variant_id` matching the new `pos`.

## Phase 5b: Fill the rs ids (add_rsid)

Every submitted table carries an rs id column. When the processed table has none, or some of
its rs ids are empty, call add_rsid on it:

- `file_path`: the processed table (`<name>_processed.tsv.gz`; a `chr*` glob for split files)
- `columns`: its `chrom_col`, `position_col`, `ref_col`, `alt_col`
- `rsid_col`: `rsid` (or the table's own rs id column, whose existing ids are kept)

add_rsid looks each variant up in the local copy of NCBI dbSNP for GRCh38 (build 157) by
chromosome, position, ref and alt, also trying the alleles swapped. A variant dbSNP does not
hold keeps an empty rs id: never invent one, never copy one from a nearby position. It works
on GRCh38 coordinates only, so a GRCh37 table goes through liftover (phase 5a) first. Never
download dbSNP or any other reference yourself. If add_rsid answers that the reference is not in place, leave the column out and say so in the note.

Report the share it found (`with_rsid` / `rows`) in the row's note.

**Check:** inspect_local shows `rsid` values like `rs346557`, or empty.

## Phase 6: Validate

Call validate for every file_path you will submit (a glob for per-chromosome files), with the
exact delimiter and columns of the row, `rsid_col` included. Processed files are always `\t`. If it fails, read the
problems, fix the script or the column map, and validate again. A path that has not passed
validate cannot be submitted. For a file with gene symbols or other non-Ensembl phenotype ids,
validate warns; keep the row and mention the id type in its note.

**Check:** every row you plan has a passing validate with the same columns.

## Phase 7: Submit

One row per (qtl_type, context). Before calling submit, check every row against this list:

- [ ] `file_path` and its columns and delimiter are exactly those that passed validate, so
      chrom and position are digits, alleles upper case and the variant id `chr<chrom>_<pos>_<ref>_<alt>`
- [ ] `qtl_type` is eQTL, sQTL, caQTL or pQTL; `gene_id_mode` is `self` or
      `phenotype_last_segment` for eQTL/sQTL/pQTL, empty for caQTL
- [ ] every row has all 27 columns; `gene_id` is empty; `rsid_col` names the column add_rsid
      filled (rs ids or empty)
- [ ] `scope` is `full` unless phase 3b found no full table; a `non_full` row has a `non_full_reason` and a
      `scope_note` with what it holds and what is missing, and the full table is not
      available anywhere you were able to reach
- [ ] `dataset` is the same in every row of the paper (the reference manifest's name when it
      has the paper); no two rows share qtl_type + biocontext, and no row repeats a
      qtl_type + biocontext the reference manifest already has for this paper
- [ ] `biocontext`, `level_1_context`, `level_2_context` use underscores, no spaces
- [ ] `sample_size` is donors in this context's QTL mapping, and its quote contains that number
- [ ] `population` quote names the ancestry; `genome_build` quote names the build (unless the
      file path or header carries it)
- [ ] every quote is copied character for character from a read_paper, open_url, list_files
      or peek_file result — never rewritten, translated or merged from two places
- [ ] `url` is the data landing page, `source_url` where the files came from,
      `publication` is https://doi.org/<doi>
- [ ] every table found but not used is in skipped_tables with its reason
- [ ] `summary` says in two or three sentences what was loaded, what was skipped and what a
      person must still do (manual locations, non-Ensembl ids)
- [ ] every row is on GRCh38: a GRCh37 table went through liftover

If submit returns problems, fix only those and submit again (at most 3 rounds).

Finishing without rows is allowed only after phase 3b. A non-full table that phase 3b kept is
a row, not a reason to finish empty. When tables were found but none can be loaded (only
other QTL types, only data that needs outside reference files, no association table at all),
submit with `rows: []` and every table in skipped_tables, so the reasons are kept. Use
give_up only when no table was found at all (nothing public, every location manual or
unreachable); the reason says which, and what the paper says about the full results.
