---
name: build-manifest
description: The fixed procedure the manifest agent (src/backend/dataqueryweb/agent.py) follows to turn one saved record into LocusCompare qtl_manifest rows — read the paper, inventory every data location, choose the full summary-statistics tables, download, process, validate, submit. agent.py puts this file into the system prompt of every build; edit it here to change how the LLM works.
metadata:
  version: "2026-11-04"
---

# Build manifest: one paper → manifest rows

Work through the phases in order. Each phase ends with a check; do not start the next phase
until it holds. The step budget is about 80 tool calls; a typical paper needs 20–40. Rough
allocation: phases 1–2 ≤ 15 calls, phase 3 ≤ 15, phase 3b (only when needed) ≤ 15, phases 4–6
the rest, and always keep 3 calls for phase 7.

## Every step: a reason, and evidence for every judgement

Nothing is decided from nothing. Every tool call carries **`why`**: what you already saw that
leads to this step (name the tool result, file or paper text) and what you expect to learn or
change. "next", "continue" or a few words are refused. The judgements `assess_file`,
`note_location` and `give_up` also carry **`evidence`**: one or more quotes copied word for word
from what you were shown (a file head, a tool result, the paper, a page), each with its source.
The program checks every quote against those texts; a judgement with no verified quote is
refused and nothing is recorded. Row facts in `submit` keep their own quoted evidence.

Every call — accepted or refused — is written to the build log as `Decision: <tool> (<outcome>)`
and to `<run_id>.decisions.jsonl`: step, why, evidence (verified or not), outcome and result.
The program's own decisions (refusals, rebuild deletions and keeps) are recorded the same way,
with their reasons.

## Situation → what to do (the decision table)

The phases below explain each rule; this table is the short form. "Enforced" means the
program refuses or flags it whatever you do.

**The table's contents**

| Situation | Do | Enforced |
|---|---|---|
| Every tested variant–phenotype pair (nominal / allpairs) | keep; process if needed | — |
| Only significant / lead / eGene / permutation results | skipped_tables; in phase 3b keep the most complete one as `scope: non_full` only when no full table exists anywhere | submit asks for `non_full_reason` + `scope_note` with numbers |
| Fine-mapping, colocalisation, conditional, enrichment, posterior outputs | skipped_tables (not association statistics) | — |
| QTL type not eQTL / sQTL / caQTL / pQTL (mQTL, isoQTL, apaQTL, …) | skipped_tables "qtl_type not in the manifest"; never relabel it | submit refuses other types |
| Results of a reused resource (GTEx, eQTLGen, …), non-human cohort | skipped_tables | — |
| Genotypes, expression matrices, counts, peaks, covariates | not a table; note_location `not_data` | — |
| Paper already in the reference manifest (same DOI) | not built at all: LocusCompare has it (you will not see such a paper) | the queue skips it |
| A person marked the record `not_qtl` | do not build it | the queue skips it |

**Columns: what is missing and what supplies it**

| Situation | Do | Enforced |
|---|---|---|
| **No nominal p-value**, and no −log10 p or 1-df chi-square to compute it from | **not processable**: assess_file `processable: no`, skipped_tables | assess_file refuses ready / yes without a p-value column the format check accepts |
| Only FDR / q-value / adjusted / permutation p | not processable (not a nominal p) | the format check does not count those columns |
| −log10 p or 1-df chi-square instead of p | compute p in processing (`10^-x`; `erfc(sqrt(x/2))`) | — |
| A z score instead of p | compute the two-sided p: `erfc(|z| / sqrt(2))`; name the z column as `pval_column` in assess_file | the format check lists z-score columns; assess_file accepts them |
| Only a t statistic, or only beta + se | not processable (t needs its degrees of freedom; beta / se would assume a normal) | assess_file refuses ready / yes |
| **No rs id**, or rs ids partly empty | **add_rsid** fills them from local dbSNP by chrom/pos/ref/alt; empty only where dbSNP has none | validate fails while a sampled variant dbSNP holds has an empty rs id |
| A column named rsid / snp holds `1_123_A_G`-style ids, not rs numbers | use it as the source of the variant id (split it); do not name it `rsid_col`; fill `rsid` with add_rsid | validate: rs ids must be `rs<digits>` or empty |
| Only the effect allele (no other allele) | write it as `alt`, leave `ref` empty, call **fill_alleles**: with the study's SNP map first, otherwise dbSNP | run_python that names the map is refused |
| Multiallelic / ambiguous site | leave `ref` and `variant_id` empty, keep the row; never drop the table | validate allows empty ref with empty variant id |
| Only rs ids, no chromosome / position | with the study's own SNP info / variant map (README-documented, assessed variant_map): **fill_positions** fills chrom / pos by rs id; ids the map lacks or places at several positions are removed and counted. Without such a map: not processable (skipped_tables, say why) | run_python that names the map is refused; fill_positions accepts only an assessed variant_map |
| Packed variant id (`1:123:A:G`, `chr1_123_A_G_b38`) | split into chrom / pos / ref / alt; rebuild `variant_id` | validate |
| Chromosome written `chr1` | chrom = `1`; variant id keeps `chr` (`chr1_123_A_G`) | validate |
| caQTL peak `chr1-100-200` / `chr1:100-200` | `chr1_100_200`; keep the original as `phenotype_id_original` | validate |
| Peak given only as a name (`peak_123`) | needs the study's peak coordinate file; never guess | validate |
| AF / EAF instead of MAF | check it is a frequency in [0, 1], add `maf = min(af, 1 − af)` | — |
| No beta / se / maf | leave the column out (stored as 0) | — |
| Gene symbols, ENST / ENSE ids instead of ENSG | keep the ids; map only with a documented study mapping; mention it in the note | validate warns; row flagged `phenotype_id_not_ensembl` |
| Effect allele not stated (A1 / A2) | A1 = alt, say so in the note | — |
| No header | name the columns from the paper / README; none → skipped_tables "no header and no column description" | — |
| X / Y / MT rows, indels | may stay; the importer keeps autosomal single-base SNVs only | — |

**Coordinates and files**

| Situation | Do | Enforced |
|---|---|---|
| GRCh37 / hg19 coordinates | **liftover** once, before add_rsid | submit refuses GRCh37 rows |
| Build unknown | find it in the file name, header, README or Methods; never assume | row flagged `genome_build_unknown` |
| One file per chromosome | process each; one row with a `chr*` glob | — |
| Several methods for one context | the paper's primary analysis; the rest to skipped_tables | submit flags duplicate qtl_type + biocontext |
| Archive (zip / tar) | read members in place; one processed file per member | — |
| Spreadsheet (xlsx) | convert the sheet to a tab-separated table | — |
| Files over the download limit | the main contexts first; the rest to skipped_tables "over the download limit" | download refuses over the limit / free space |

**Access, failures and rebuilds**

| Situation | Do | Enforced |
|---|---|---|
| Login, application, "on request", browser-only portal | note_location `manual` with what a person must do and the page | — |
| Timeout, connection reset, truncated download | at most two retries; then note_location `error` with the failure (not "controlled access") | — |
| HTTP 403 on a publisher page | one retry; then the published version, PMC / Europe PMC, the repository API | — |
| Rebuild: processed file truncated or not in the target format | the program deleted it: process its source again | REBUILD CHECK |
| Rebuild: processed file right but rs ids missing | add_rsid; do not reprocess | REBUILD CHECK, validate |
| Rebuild: downloaded file unchanged since an earlier assessment | the assessment is reused; re-assess only if the skill changes affect it | REBUILD CHECK |
| Any step | give `why`; for assess_file, note_location and give_up also `evidence` (verbatim quotes) | refused otherwise |

## Phase 0: Resume, provenance and curator context

### Rebuild check (done by the program, read it first)

When the paper's folder already holds files, the initial message starts with a REBUILD CHECK
that the program ran before your first step. Act on it in this order:

1. **SKILL CHANGES** — the diff between the skill the previous build used and this one (or a
   note that no earlier copy is kept, in which case every rule may be new). Earlier outputs and
   plans were made under the old rules: check them against the changed lines, not from memory.
2. **DOWNLOADED FILES** — every downloaded file was already format-checked (inspect_local is
   optional for them). Assessments of unchanged files from earlier builds are reused; re-assess
   any the skill changes affect. Assess the rest; `assess_file` takes a glob for a group of files
   with the same header and contents (e.g. `folder/x.eQTL.*.cis_nominal.tsv.gz`).
3. **PROCESSED FILES** — every `*_processed` table was read to the end and its first rows tested
   against the required formats (Phase 5 table: chrom `1`–`22` without `chr`, variant id
   `chr<chrom>_<pos>_<ref>_<alt>`, caQTL peak `chr<chrom>_<start>_<end>`, rs ids filled where
   dbSNP holds them):
   - **DELETED** — truncated (an interrupted write) or not in the target format. The program
     removed them; process their sources again into the target format. Your own checkpoint or
     stats files that list them as done are stale: do not skip them because of such a file.
   - **KEPT, rs ids missing** — the format is right; call add_rsid (a glob works), do not reprocess.
   - **KEPT, in the target format** — reuse; validate before submitting.
   Never reuse a processed file the check deleted or flagged, and never write a processing
   script that skips a source only because its `_processed` file exists.

Before repeating a build, inspect the previous run outcome, existing local files and the
history paths supplied in the initial message. Use `read_build_history(path, offset)` to read
previous Markdown logs and Python scripts; follow `next_offset` when `more` is true. A tail
excerpt may omit earlier successful work, so read relevant preceding sections before deciding
to repeat a costly step.

- Identify the last completed checkpoint, the interrupted or failed operation, and the next
  unfinished step. A saved script proves what was attempted, not that it finished successfully.
- Check reusable outputs, their expected sizes, headers, context and validation results. Inspect
  existing inventories and variant maps before repeating full scans of large compressed archives.
  Revalidate an output whose completeness or compatibility is uncertain; do not infer completion
  solely from its filename. Missing or invalid intermediates may require rerunning their producer.
- Record the resume plan using `note_location` with status `listed`: completed work, reusable
  files, uncertainties, the next step and why it is needed. Resume from that step; revisit earlier
  phases only where evidence is missing, contradicted or invalidated.
- For legacy runs without Markdown logs, inspect saved reports, Python scripts and actual files.
  Explicitly distinguish inferred progress from confirmed successful execution.
- Preserve existing source data and valid outputs. Reuse registered downloads and resumable partial
  downloads through the download tool. Do not overwrite curator uploads or delete files to restart;
  invalid processed files are removed by the program's rebuild check, not by you.
- Treat the paper's saved conversation as curator context. Read supplied access emails, README
  text and local paths; verify their relevance to this paper, release and QTL type. In particular,
  FinnGen R13 GWAS manifests are not automatically the single-nucleus multiome QTL data release.
  Follow the relevant multiome resource rather than downloading unrelated GWAS results.

**Check:** a restart has an evidence-based resume plan before expensive download or processing.

### Build Markdown audit

The host records each new run in `data/.build_logs/<record_id>/<run_id>.md`, incrementally.
It logs tool arguments and results, Python source and invocation, stdout/stderr, validation and
submission results, and checkpoints. Use available tools for all operations so they enter this
history. For any supported command execution, retain the actual command, working directory,
exit status and result; do not describe an unexecuted Bash command as completed work. Python
scripts have separate run-specific paths and must remain traceable from the Markdown.

State substantive decisions and reasons in tool notes or the final summary, including source
selection, variant-map joins, conversion requirements, failed checks and recovery. Do not include
API keys, passwords, authorization headers or signed access credentials in notes or commands.
Record a safe landing-page URL instead. Logging does not require copying entire source datasets
into Markdown: keep full data files on the server, with references and inspection results in the log.
The UI preview retains up to five rows; never truncate the actual source or processed dataset to
five rows. Direct streaming from an archive is valid and does not require extracting all members.

### Human bottlenecks and missing metadata

Use `manual` only when a concrete human action is established. Each such location note must say:
what blocks progress, the evidence, the exact action required, the verified application/login/download
page URL, and what instructions or server file paths the curator should supply to resume. If the
URL is unknown, explicitly say it is unverified; do not invent one. Network failures, conversion
errors and unsupported QTL types alone do not establish a need for a human access application.

For missing sample sizes, check full-text Methods, cohort/QC and supplementary tables, repository
README files and data headers. Report genotyped donors used in each context's QTL mapping, not
cells or the overall cohort copied across contexts. Preserve unknown values and explain the sources
checked when no supported count is found. A build should resolve missing journal metadata through
the paper-reading path when available; do not infer a journal or donor count from an unrelated release.
For manifest fields, retain field-specific source quotes and transformation reasons so the reviewer
can see both the proposed value and its justification.

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
  counts.
- **Multiallelic / ambiguous sites do not make a table unusable.** Where the map gives a key
  more than one possible other allele (e.g. rs2921059 at 8:8460377, assessed G, listed as
  G/A, G/C and G/T), leave that row's `ref` (and `variant_id`) empty and keep the row as it
  is; fill `ref` for every site with exactly two alleles. Never skip a whole table because a
  small share of its sites is multiallelic. Report filled / left-empty counts in the note.
- **Alleles are mapped by a fixed program, not your code.** Write the table with the effect
  allele in `alt` and `ref` empty, then call fill_alleles (phase 5a2): with `map_file` = the
  study's SNP info file when the source provides one (assessed `variant_map` / `companion`),
  otherwise without it, from the local dbSNP copy. The study's own map always comes first.
  run_python code that writes `_processed` tables and names the map file is refused.
- **No p-value → not processable.** A table with no nominal p-value (and no -log10 p, 1-df
  chi-square or z score to compute one from) is assessed `processable: no` and goes to
  skipped_tables; assess_file refuses ready / yes without a p-value or z-score column the format
  check accepts. A t statistic or beta / se alone is not enough.
- **Only rs ids, no chromosome / position → fill_positions with the study's map.** Write the
  table with its rs ids and empty `chrom` / `pos`, then call fill_positions (phase 5a1) with the
  study's SNP info file. Without such a map the table is not processable (no other source of
  positions is used). Confirm the map's coordinate build and preserve it in evidence; liftover only after
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

## Phase 4b: Check every downloaded file (inspect_local → assess_file) — enforced

After all downloads, and before writing any processing code, check **each** downloaded file:

1. `inspect_local` it. The program adds a `format_check`: delimiter, header, whether rows have
   the header's width, which manifest columns it found (`roles_found`), which can be derived
   and which required ones are missing. For an archive, inspect its listing and at least one
   member (`archive::member`); members with different layouts are assessed one by one.
2. `assess_file` it with your verdict:
   - `contents`: qtl_nominal_all, qtl_significant_only, qtl_lead_only, variant_map, readme,
     posterior_or_downstream, annotation_or_other, unreadable
   - `processable`: `ready` (already the standard format), `yes` (can be processed to it) or
     `no` (with `reason`; it goes to skipped_tables). Only QTL association tables can be
     ready / yes; a variant map is named in the `plan` of the table it serves.
   - `plan` (for yes): how chrom, pos, ref, alt, variant_id, pval and phenotype are made, and
     which companion file or tool (liftover, fill_alleles, add_rsid) supplies what is missing.

The program refuses run_python code that writes `_processed` tables, validate, liftover,
fill_alleles, add_rsid and submit while any downloaded file has no assessment. A read-only
script (no `_processed` output) may still be used to check a file inspect_local cannot read.
Downloading more files later puts them on the list again.

## Phase 5: Process (only when needed)

**Required value formats.** Every row of a submitted file must hold exactly these, and
validate checks every row it reads:

| Column | Format | Right | Wrong |
|---|---|---|---|
| chromosome | the autosome number only, `1`–`22`, **no `chr`** | `1` | `chr1`, `01`, `X`, `chrX` |
| position | digits only | `12345` | `12345.0`, `1.2e4` |
| ref, alt | upper-case A/C/G/T/N | `A`, `TG` | `a`, `t` |
| variant id | `chr<chrom>_<pos>_<ref>_<alt>` joined by underscores, **with `chr`**, equal to those four columns | `chr1_32523_A_T` | `1_32523_A_T`, `1:32523:A:T`, `chr1:32523:A:T`, `chrchr1_32523_A_T`, `chr1_32523_A_T_b38`, `rs123` |
| caQTL phenotype id (peak) | `chr<chrom>_<start>_<end>` joined by underscores, **with `chr`**, start < end | `chr1_817095_817594` | `chr1-817095-817594`, `chr1:817095-817594`, `1_817095_817594`, `chr1_817594_817095`, `peak_123` |

**The `chr` rule:** the chromosome column holds only the number (`1`); the variant id is
`chr` + that number + `_` + position + `_` + ref allele + `_` + alt allele (`chr1_32523_A_T`).
So the same chromosome is `1` in `chrom` and `chr1` inside `variant_id`. When the source writes
`chr1`, strip the prefix for `chrom` and build `variant_id` from the cleaned columns:
`df["chrom"] = df["chrom"].str.replace(r"^chr", "", regex=True)`, then
`df["variant_id"] = "chr" + df["chrom"] + "_" + df["pos"] + "_" + df["ref"] + "_" + df["alt"]`.
Never copy the source's own variant id when it differs from this.

**caQTL peak ids:** the phenotype id of a caQTL row is its peak, written
`chr<chrom>_<start>_<end>`: `chr`, the chromosome number, `_`, the peak start, `_`, the peak
end (`chr1_817095_817594`). Sources often write `chr1-817095-817594` or `chr1:817095-817594`:
keep that value as `phenotype_id_original` and write the new id in `phenotype_id`, e.g.
`df["phenotype_id"] = df["phenotype_id"].str.replace(r"[:-]", "_", regex=True)`, adding `chr`
when the source lacks it. A peak given only as a name (`peak_123`) needs the study's peak
coordinate file to build the id; never guess coordinates.

**Enforced by code:** validate rejects a `chrom` value that is not `1`–`22` (a `chr` prefix is
named in the error), a `variant_id` without the `chr` prefix, and any `variant_id` that is not
exactly `chr<chrom>_<pos>_<ref>_<alt>` of the same row — in the first 20,000 rows of every file
a glob matches, not only the first ones. For caQTL it also rejects any phenotype id that is not
`chr<chrom>_<start>_<end>` with start < end.

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
- read the association input only; a missing non-effect allele is left empty for fill_alleles
  (phase 5a2), which reads the study's SNP info file or dbSNP itself. Never join association
  rows from different files or analyses
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

**Check:** inspect_local on one processed file shows `chrom` like `1` (no `chr`), `pos` digits,
upper-case `ref`/`alt`, `variant_id` like `chr1_32523_A_T` (with `chr`) and, for caQTL,
`phenotype_id` like `chr1_817095_817594`.

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

## Phase 5a1: Fill chromosome and position from rs ids (fill_positions — a fixed program)

Only for association tables that give rs ids but no chromosome / position, when the study
provides its own SNP info / variant map (assessed `variant_map`). Write the processed table with
the rs ids, the alleles you have, and empty `chrom` / `pos`, then call fill_positions:

- `file_path`: the processed table (`<name>_processed.tsv.gz`; a glob for split files)
- `columns`: `rsid_col`, `chrom_col`, `position_col`, and `ref_col`, `alt_col`, `variant_id_col`
  when the table has them
- `map_file`, `map_columns` (`chrom`, `pos`, `id`, `allele1`, `allele2`; header names, or 0-based
  numbers with `map_header: false`), `map_delimiter`

The map is indexed once (`<map>.alleles.sqlite`, shared with fill_alleles). An rs id gets the
position only when the map places it at exactly one position; ids the map lacks or lists at
several positions cannot be placed, and those rows are removed (the importer could not use them).
The coordinates are on the map's build: run fill_positions BEFORE fill_alleles and liftover.
Report `filled`, `ambiguous`, `not_in_map`, `removed` and `share_with_position` in the row's note.
Never map positions in your own Python, and never use dbSNP or another resource for positions.

## Phase 5a2: Fill the missing non-effect allele (fill_alleles — a fixed program)

For a table whose source gives the effect allele but not the other one. Process it first so
`alt` holds the effect allele and `ref` is empty, then call fill_alleles:

- `file_path`: the processed table (`<name>_processed.tsv.gz`; a `chr*` glob for split files)
- `columns`: its `chrom_col`, `position_col`, `ref_col` (to fill), `alt_col` (effect allele),
  and `rsid_col` / `variant_id_col` when it has them
- with the study's SNP info file (assessed `variant_map`, processable `companion`):
  `map_file`, `map_columns` (`chrom`, `pos`, `allele1`, `allele2`, optional `id`; header names,
  or 0-based numbers with `map_header: false`), `map_delimiter`. The map is on the table's own
  build, so run this BEFORE liftover. The map is indexed once into `<map>.alleles.sqlite`.
- without a study map (only then): no map arguments; it reads the local dbSNP copy (GRCh38,
  build 157), so run it AFTER liftover. Refused while a variant_map is assessed.

Records at the position (narrowed to the row's rs id when listed; `rs1;1_10505_A_T` ids are
split on `;`) must hold exactly two single-base alleles including the effect allele. Then `ref`
is filled; multiallelic sites, alleles not listed and positions missing keep an empty `ref`
and empty `variant_id` and are left as they are (the importer skips them). Existing `ref`
values are kept. Report `filled`, `multiallelic`, the not-listed counts and
`share_with_both_alleles` in the row's note.

## Phase 5b: Fill the rs ids (add_rsid)

Every submitted table carries an rs id column, and **every variant dbSNP holds must carry its
rs id**. An empty rs id is allowed only where dbSNP has no record for that chrom / pos / ref /
alt. A column that exists but is empty (or only partly filled) is not done: writing an empty
`rsid` column in your own processing script does not count as adding rs ids. When the
processed table has no rs id column, or any of its rs ids are empty, call add_rsid on it —
also when the table was processed in an earlier build (phase 0: check the `rsid` values of
reused `_processed` files, never assume they were filled):

- `file_path`: the processed table (`<name>_processed.tsv.gz`; a `chr*` glob for split files)
- `columns`: its `chrom_col`, `position_col`, `ref_col`, `alt_col`
- `rsid_col`: `rsid` (or the table's own rs id column, whose existing ids are kept)

add_rsid looks each variant up in the local copy of NCBI dbSNP for GRCh38 (build 157) by
chromosome, position, ref and alt, also trying the alleles swapped. A variant dbSNP does not
hold keeps an empty rs id: never invent one, never copy one from a nearby position. It works
on GRCh38 coordinates only, so a GRCh37 table goes through liftover (phase 5a) first. Never
download dbSNP or any other reference yourself. If add_rsid answers that the reference is not in place, leave the column out and say so in the note.

Report the share it found (`with_rsid` / `rows`) in the row's note.

**Check:** `with_rsid` is above zero (a GRCh38 QTL table normally has most variants in dbSNP;
a share near 0 means wrong coordinates, wrong build or swapped columns — investigate, do not
submit), and inspect_local shows `rsid` values like `rs346557`.

**Enforced by code:** validate looks up a sample (up to 500 variants per file) of the rows whose
rs id is empty in the local dbSNP copy, the same way add_rsid does. If dbSNP holds any of
them, validate fails with `rsid_col … is empty for N of M sampled variants that dbSNP holds`
and the row cannot be submitted until add_rsid has run and validate passes again. The check
is skipped only when the dbSNP copy is not in place (then say so in the note).

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
      filled: an rs id for every variant dbSNP holds, empty only where dbSNP has none (validate
      checks this against dbSNP)
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
