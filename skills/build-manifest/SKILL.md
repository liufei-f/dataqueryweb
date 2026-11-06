---
name: build-manifest
description: The core procedure the manifest agent (src/backend/dataqueryweb/agent.py) follows to turn one saved record into LocusCompare qtl_manifest rows — read the paper, inventory every data location, choose the full summary-statistics tables, download, process, validate, submit. agent.py puts this file into the system prompt of every build; the modules in modules/ are given to the LLM when it needs them (and are required before the actions they cover), and papers/<record_id>.md notes go to that paper only.
metadata:
  version: "2026-11-05b"
---

# Process data: one paper → manifest rows, in six steps

Process a paper in these six steps, in order:

1. **Sources and files on disk** (modules `resume-rebuild`, `access`, `full-vs-nonfull`) — which data locations exist, which tables are needed, whether
   they can be downloaded, and what is already in the paper's folder (downloaded or processed).
   A processed file already in the target format is not processed again: after its facts (step 3)
   and the assessment of its source (step 4), it goes straight to step 6.
2. **Download** (module `access` when a download fails) — only what is needed and not yet on disk.
3. **Facts for the manifest** (modules `manifest-columns`, `facts`) — read the paper for what the rows need: QTL types, contexts,
   sample sizes, ancestry, genome build, dataset name.
4. **Formats and plan** (modules `table-identity`, `variant-maps`, `liftover`, `processing-cookbook`, `add-rsid`) — inspect every downloaded file: its format and columns, whether it can
   be converted to the standard format, and the plan (which fixed programs and module, what
   code). When no table can be converted, stop and report.
5. **Process** (modules `processing-cookbook`, `variant-maps`, `liftover`, `add-rsid`) — convert to the standard format with the planned script and fixed programs.
6. **Check and write the manifest** (modules `manifest-columns`, `facts`, `full-vs-nonfull`) — validate every processed file; when it passes, submit the
   rows. When it fails, process again; after two failed attempts for a file, report the error.

Each step starts with the modules it may need and ends with a check; do not start the next step
until the check holds. The step budget is
about 80 tool calls; a typical paper needs 20–40. Always keep 3 calls for step 6's submit.

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

## Procedure modules: which to read, and when

This file is the core procedure. The exact procedure for special situations is in modules, so
that it is in front of you when you need it. **Never improvise a procedure a module covers.**
When a situation points to a module you have not read, call `read_skill` with its name first.
The program enforces this: each action below is refused until its module has been read, and
the refusal carries the module's text — read it, then make the call again. A module you have
read stays in the conversation; you do not need to read it twice.

| Module | Read it when | Required before (refused until read) |
|---|---|---|
| `resume-rebuild` | step 1, when the paper was processed before (REBUILD CHECK or history in the first message) | given in the first message |
| `table-identity` | judging what a downloaded file holds and which QTL type it is | `assess_file` |
| `processing-cookbook` | writing a script that produces `_processed` tables | `run_python` that writes `_processed` |
| `variant-maps` | the other allele or the positions are missing, or the study has a SNP info / variant map | `fill_alleles`, `fill_positions`, `assess_file` with contents `variant_map` |
| `liftover` | coordinates are GRCh37 / hg19, or the build is unclear | `liftover` |
| `add-rsid` | a processed table lacks rs ids, or rs ids dbSNP holds are empty | `add_rsid` |
| `manifest-columns` | naming a row's columns | `validate`, `submit` |
| `facts` | sample size, ancestry, genome build, context labels | `submit` |
| `full-vs-nonfull` | no full summary-statistics table is kept for a QTL type the paper mapped | `submit` with a `non_full` row or with no rows, `give_up` |
| `access` | a location needs a person, or a download / page failed | `note_location` with status `manual` or `error`, `give_up` |

The modules `access`, `facts`, `table-identity` and `variant-maps` include curated checks from
the curator workbook (`processed&needsyou.xlsx`, Sheet1, reviewed 2026-11-02): treat them as
leads to verify against the actual sources, not evidence to quote. When this paper has its own
notes (`papers/<record_id>.md`), the first message carries them: lessons from earlier builds of
THIS paper, also leads to verify.

## Situation → what to do (the decision table)

The steps below explain each rule; this table is the short form. "Enforced" means the
program refuses or flags it whatever you do.

**The table's contents** (details: `table-identity`, `full-vs-nonfull`)

| Situation | Do | Enforced |
|---|---|---|
| Every tested variant–phenotype pair (nominal / allpairs) | keep; process if needed | — |
| Only significant / lead / eGene / permutation results | skipped_tables; in step 1 keep the most complete one as `scope: non_full` only when no full table exists anywhere | submit asks for `non_full_reason` + `scope_note` with numbers |
| Fine-mapping, colocalisation, conditional, enrichment, posterior outputs | skipped_tables (not association statistics) | — |
| QTL type not eQTL / sQTL / caQTL / pQTL (mQTL, isoQTL, apaQTL, …) | skipped_tables "qtl_type not in the manifest"; never relabel it | submit refuses other types |
| Results of a reused resource (GTEx, eQTLGen, …), non-human cohort | skipped_tables | — |
| Genotypes, expression matrices, counts, peaks, covariates | not a table; note_location `not_data` | — |
| Paper already in the reference manifest (same DOI) | not built at all: LocusCompare has it (you will not see such a paper) | the queue skips it |
| A person marked the record `not_qtl` | do not build it | the queue skips it |

**Columns: what is missing and what supplies it** (details: `processing-cookbook`, `variant-maps`)

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

**Coordinates and files** (details: `liftover`, `processing-cookbook`)

| Situation | Do | Enforced |
|---|---|---|
| GRCh37 / hg19 coordinates | **liftover** once, before add_rsid | submit refuses GRCh37 rows |
| Build unknown | find it in the file name, header, README or Methods; never assume | row flagged `genome_build_unknown` |
| One file per chromosome | process each; one row with a `chr*` glob | — |
| Several methods for one context | the paper's primary analysis; the rest to skipped_tables | submit flags duplicate qtl_type + biocontext |
| Archive (zip / tar) | read members in place; one processed file per member | — |
| Spreadsheet (xlsx) | convert the sheet to a tab-separated table | — |
| Files over the download limit | the main contexts first; the rest to skipped_tables "over the download limit" | download refuses over the limit / free space |

**Access, failures and rebuilds** (details: `access`, `resume-rebuild`)

| Situation | Do | Enforced |
|---|---|---|
| Login, application, "on request", browser-only portal | note_location `manual` with what a person must do and the page | — |
| Timeout, connection reset, truncated download | at most two retries; then note_location `error` with the failure (not "controlled access") | — |
| HTTP 403 on a publisher page | one retry; then the published version, PMC / Europe PMC, the repository API | — |
| Rebuild: processed file truncated or not in the target format | the program deleted it: take its source through steps 4–6 again | REBUILD CHECK |
| Rebuild: processed file right but rs ids missing | steps 3 and 4, step 5 add_rsid only, then step 6; do not reprocess | REBUILD CHECK, validate |
| Rebuild: processed file in the target format | no processing, no add_rsid: steps 3 and 4 (assessment), then step 6 | REBUILD CHECK; add_rsid skips it |
| Rebuild: downloaded file unchanged since an earlier assessment | the assessment is reused; it is not downloaded again | REBUILD CHECK |
| validate fails for a processed file | fix the processing and run it again (step 5), then validate again; after two failed attempts the program stops validating it: list it in skipped_tables with the error. A failure only about empty rs ids is not an attempt (run add_rsid) | validate refuses a third attempt |
| No kept table can be converted (all assessed `no`) | stop: submit with `rows: []` and every table in skipped_tables with its reason | — |
| A fact (e.g. a context's sample size) is only in a supplementary table | read it with read_table and quote its row | submit checks the quote against what read_table returned |
| Any step | give `why`; for assess_file, note_location and give_up also `evidence` (verbatim quotes) | refused otherwise |

## Step 1: Sources and files on disk — what exists, what is needed, what can be downloaded

**Modules for this step** (read with `read_skill`; actions marked *required* are refused until read):
- `resume-rebuild` — the paper was processed before. Given in the first message; follow it first.
- `access` — a location needs a person (login, application, "on request", browser-only) or a page /
  download failed. *Required before* `note_location` with status `manual` or `error`.
- `full-vs-nonfull` — no full summary-statistics table for a QTL type the paper mapped. *Required
  before* submitting a `non_full` row or no rows, and before `give_up`.
- `table-identity` — unsure whether a candidate is a QTL association table, which QTL type, or full
  vs subset (it is *required before* `assess_file` in step 4; read it now when you need it here).

Answer four questions before downloading anything:

1. **What is already in the paper's folder?** The first message lists the folder and, when the
   paper was processed before, the REBUILD CHECK (module `resume-rebuild`):
   - a processed file **in the target format** → no download and no processing: step 3 (its facts),
     step 4 (the assessment of its source — a reused one counts), then straight to step 6;
   - a processed file whose **rs ids are missing** → steps 3 and 4 as above, step 5 for add_rsid
     only, then step 6;
   - a **deleted** processed file, or a downloaded source without a usable processed file →
     steps 4–6 from that source;
   - a **downloaded source** is never downloaded again.
2. **Which data locations exist?** Search the paper's data statements with read_paper
   (`data availab|code availab|accession|deposited|zenodo|figshare|synapse|ega|dbgap|portal`;
   with open_url and `find` when only the abstract is available), add the saved locations from the
   first message, and inventory each:

For each location, in this order of promise: summary-statistics repositories (Zenodo,
figshare, OSF, Hugging Face, FTP) → lab pages and portals → GitHub → GEO and the rest.

- Repository or folder: list_files (use `pattern` to narrow long listings, e.g. `*eqtl*`).
- Landing page: open_url, then follow the links that lead to files ("Download", "Data",
  "Files", FTP/HTTP mirrors). Two levels of links at most.
- Call note_location for every location once you know what it is:
  - `listed`: files found
  - `manual`: a person must act (login, data access application, browser-only portal,
    Synapse, Google Drive, Dropbox, "on request")
  - `error`: unreachable after the bounded retries (module `access`)
  - `not_data`: a paper, a code repository without results, an annotation resource
  - `duplicate`: the same files as another location


3. **Which tables are needed?** Choose them with peek_file before any download:

Open one or two files of each group of sibling files with peek_file; files that differ only by
chromosome or context share their layout. Classify each table:

| The table holds | Decision |
|---|---|
| every tested variant–phenotype pair (nominal, allpairs, full_assoc, cis_qtl_pairs, sumstats) | **keep** |
| significant, lead, eGenes, permutation, conditional, fine-mapped, SuSiE, colocalisation hits | skipped_tables: "subset, not full summary statistics" (a non-full table may come back through the module `full-vs-nonfull` when no full one exists) |
| mQTL, meQTL, metabolite, edQTL, apaQTL or any type other than eQTL/sQTL/caQTL/pQTL | skipped_tables: "qtl_type not in the manifest" |
| results of a resource the paper reused (GTEx, eQTLGen, eQTL Catalogue, …) | skipped_tables: "already in the manifest as …" / "not this paper's results" |
| a non-human cohort | skipped_tables: "not human" |
| genotypes, expression matrices, peaks, counts, covariates | do not list as a table; note the location `not_data` |

**Full or not** is judged here from the names, the README and the header; it is verified with
numbers in step 4, on the downloaded file and against the paper's counts from step 3.

For each kept table write down from the peek: delimiter, which column holds chrom, position,
ref, alt, nominal p, phenotype id (and beta, se, maf, rsid, variant id), and what must change.


4. **Can they be downloaded?** An open link (repository, FTP, direct file) can; a login,
   application, "on request" or browser-only portal cannot: note_location `manual` (module
   `access`). A download that fails for network reasons is retried at most twice, then `error`.

**No full table yet:** when no table of full summary statistics is kept for a QTL type the paper
mapped, read the module `full-vs-nonfull` and follow it: search the paper and everything it points
to, and only then keep the most complete non-full table (with `non_full_reason` and a `scope_note`
with numbers). It is required before submitting a `non_full` row, before submitting no rows, and
before give_up.

**Check:** every location has a note_location; every candidate table is kept (to download, or
already on disk) or has a skipped_tables reason; you know for each kept table which of steps 2–6
it still needs. If the paper says the results are controlled-access or on request only, still
check the saved locations: a summary table is often public even when individual data are not.

## Step 2: Download — only what is needed and not yet on disk

**Modules for this step:**
- `access` — a download fails (timeout, reset, 403) or turns out to need a person. *Required before*
  `note_location` with status `manual` or `error`.

Files already in the paper's folder (step 1) are not downloaded again; a download that was
interrupted resumes where it stopped.

- One download call per group: `listing_url` + `pattern` for many files, `urls` for a few.
- Every file of the paper lands in one folder named by the record code (first message, e.g.
  `Sun_2026_skin/`), whatever `dataset` you pass; each `file_path` starts with that folder.
- Download every chromosome and every context of a kept group, not a sample.
- Before a large download, compare the total size with the paper's download limit in the first
  message. When it does not fit, download whole contexts in order of importance (the
  paper's main tissue or cell type first) and list the rest in skipped_tables with
  "over the download limit".

**Check:** inspect_local on one downloaded file shows the same header as the peek.
## Step 3: Facts for the manifest — what the rows need, from the paper

**Modules for this step:**
- `manifest-columns` — what each of the 27 columns means and how to fill it (*required before*
  `validate` and `submit`; read it here to know what to look for).
- `facts` — how to find sample size, ancestry, genome build and context labels in the right release
  and section (*required before* `submit`).

The rows need, for each kept table (module `manifest-columns` lists all 27 columns): the QTL
type, the context (`biocontext`, `level_1_context`, `level_2_context`), the sample size (donors in
this context's QTL mapping), the population, the genome build of the coordinates, and the dataset
name. Find them in the paper with these read_paper searches (one call each), then in the
Methods, cohort tables, supplementary tables and the repository README:

1. `genotyp|imputation|GRCh3|hg19|hg38|build`
2. `eQTL|sQTL|caQTL|pQTL|QTL mapping|tensorqtl|matrixeqtl|fastqtl|qtltools|limix|nominal`
3. `donors|individuals|participants|samples` (sample size per context)
4. `ancestry|European|African|Asian|Hispanic|population`


Write down, with a verbatim quote and its source for each fact:
- the QTL types the paper mapped itself, and the contexts (tissues, cell types, conditions)
- for each context: donors genotyped and used for QTL mapping, ancestry, genome build
- which resources the paper only reused (they are never downloaded)

Facts in supplementary tables or cohort tables (sample sizes per context, donor counts after QC)
are read with **read_table** (an `.xlsx` sheet or a delimited file, rows filtered with `find`): its
rows count as text you were shown, so quotes copied from them pass the check at submit. Output
printed by your own run_python code never counts as evidence.

The module `facts` has the rules for finding them in the right release and section (it is
required before submit).

**Check:** each fact of each kept table has a quote, or is marked unknown with the sources checked.

## Step 4: Formats and plan — can each file become the standard format, and how

**Modules for this step:**
- `table-identity` — what a file holds and which QTL type it is. *Required before* `assess_file`.
- `variant-maps` — the file lacks the other allele or its positions, or the study ships a SNP info /
  variant map. *Required before* `assess_file` with contents `variant_map`.
- `liftover` — the coordinates are GRCh37 / hg19, or the build is unclear.
- `processing-cookbook` — what a processing script may and must do, to write the plan.
- `add-rsid` — when the plan includes filling rs ids (it is *required before* `add_rsid` in step 5).

After the downloads, and before writing any processing code, check **each** downloaded file
(its format, its columns, and what it holds):

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

**Full or not: verify with numbers before assessing a table `qtl_nominal_all`.** Names like "summary
statistics", "allpairs" or "nominal" do not prove a table is full, and a deposit can hold
only part of what was tested. Compare the table with the paper, with numbers (count with a
read-only run_python on the downloaded file; the paper's counts come from step 3):

| Check | Full summary statistics | Not full |
|---|---|---|
| Phenotypes in the table vs phenotypes the paper tested (Methods: "N genes / proteins / peaks after QC were tested") | about the same | far fewer |
| Same count vs the paper's significant count (eGenes, pGenes, …) | larger | equal → only the significant phenotypes |
| Variants per phenotype vs the cis window (±1 Mb usually gives thousands) | thousands, close to all variants in the window | a few, or a fixed handful |
| p-values | spread over 0–1 (median near 0.4–0.5) | all below a threshold → only significant pairs |
| File names, README, scripts that read the file | — | "demo", "example", "test", "illustrative", "not completely included", a `# demo` comment → an example extract |
| Size | usually > 100 MB compressed per context | small |

Write the numbers down: they decide the `non_full_reason` and go into the `scope_note` if
the table is kept through the module `full-vs-nonfull`.

**The standard format.** Every row of a submitted file must hold exactly these; validate checks
every row it reads:

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

**The plan** (assess_file `plan` for each file assessed `yes`): which column becomes chrom, pos,
ref, alt, variant_id, the nominal p and the phenotype id; what the processing script does (module
`processing-cookbook`); and which fixed programs supply what the file lacks, in step 5's order —
fill_positions, fill_alleles, liftover, add_rsid (modules `variant-maps`, `liftover`, `add-rsid`). A file can be used as it is only when it is one delimited text table (plain or gzip), with a
header, with chrom, position, ref, alt, variant id, nominal p and phenotype id each in its own
column, and all values already in these formats.

**When nothing can be converted, stop and report.** When every kept table is assessed `no` (no
nominal p-value or anything to compute it from, no positions and no study map, an unsupported
QTL type, …), do not process: submit with `rows: []` and every table in skipped_tables with its
reason, so the reasons are kept for the curator.

**Check:** every downloaded file has an assessment; each table assessed `yes` has a plan.

## Step 5: Process — convert to the standard format

**Modules for this step:**
- `processing-cookbook` — script rules and recipes. *Required before* `run_python` code that writes
  `_processed` tables.
- `variant-maps` — *required before* `fill_positions` and `fill_alleles`.
- `liftover` — *required before* `liftover`.
- `add-rsid` — *required before* `add_rsid`.

Write one run_python script per group of files that share a layout; it writes
`<original name>_processed.tsv.gz` next to each input, following the module
`processing-cookbook` (script rules and recipes; required before any code that writes
`_processed` tables). Then run the fixed programs the plan needs.

**Check:** inspect_local on one processed file shows `chrom` like `1` (no `chr`), `pos` digits,
upper-case `ref`/`alt`, `variant_id` like `chr1_32523_A_T` (with `chr`) and, for caQTL,
`phenotype_id` like `chr1_817095_817594`.

### Coordinates and alleles — fixed programs, in this order

Only the steps a table needs, always in this order (each is a fixed program; never do these in
your own Python):

1. **fill_positions** — the table gives rs ids but no chromosome / position, and the study has its
   own SNP info / variant map. Module `variant-maps`.
2. **fill_alleles with the study's map** — the table gives only the effect allele. On the table's
   own build, so before liftover. Module `variant-maps`.
3. **liftover** — the coordinates are GRCh37 / hg19. Module `liftover`.
4. **fill_alleles from dbSNP** — only when the study provides no map; dbSNP is GRCh38, so after
   liftover. Module `variant-maps`.
5. **add_rsid** — module `add-rsid`.

### Fill the rs ids (add_rsid)

Every submitted table needs an rs id for every variant dbSNP holds (empty only where dbSNP has
none); validate checks this against dbSNP. Run add_rsid only on the files that need it; the program
skips by itself every file whose rs ids are already complete (found so by the REBUILD CHECK or by
an earlier add_rsid, and unchanged since) and lists it under `skipped_complete`. How to call it, which files, and
what to report: module `add-rsid` (*required before* `add_rsid`).

## Step 6: Check and write the manifest

**Modules for this step:**
- `manifest-columns` — the 27 columns of a row. *Required before* `validate` and `submit`.
- `facts` — sample size, ancestry, genome build, context labels and their quotes. *Required before*
  `submit`.
- `full-vs-nonfull` — *required before* submitting a `non_full` row or no rows.
- `access` — *required before* `give_up` (with `full-vs-nonfull`).

### Validate every processed file

Call validate for every file_path you will submit (a glob for per-chromosome files), with the
exact delimiter and columns of the row, `rsid_col` included. Processed files are always `\t`. If it fails, read the
problems, fix the script or the column map, and validate again. A path that has not passed
validate cannot be submitted. For a file with gene symbols or other non-Ensembl phenotype ids,
validate warns; keep the row and mention the id type in its note.



**When validate fails, process again — twice at most.** Read the problems, fix the processing
script or the column map (step 5), run it again and validate again. A file gets two attempts:
after its second failed validate the program refuses to validate it again. A failure only about
empty rs ids is not an attempt: run add_rsid and validate again. Then list it in
skipped_tables with the validate problems as the reason and say in the summary that processing
failed and why — never submit it, and never keep retrying.

**Check:** every row you plan has a passing validate with the same columns.

### Write the manifest rows (submit)

One row per (qtl_type, context). Before calling submit, check every row against this list:

- [ ] `file_path` and its columns and delimiter are exactly those that passed validate, so
      chrom and position are digits, alleles upper case and the variant id `chr<chrom>_<pos>_<ref>_<alt>`
- [ ] `qtl_type` is eQTL, sQTL, caQTL or pQTL; `gene_id_mode` is `self` or
      `phenotype_last_segment` for eQTL/sQTL/pQTL, empty for caQTL
- [ ] every row has all 27 columns; `gene_id` is empty; `rsid_col` names the column add_rsid
      filled: an rs id for every variant dbSNP holds, empty only where dbSNP has none (validate
      checks this against dbSNP)
- [ ] `scope` is `full` unless the module `full-vs-nonfull` (step 1) found no full table; a `non_full` row has a `non_full_reason` and a
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

Finishing without rows is allowed only after the module `full-vs-nonfull` (step 1). A non-full table kept in step 1 is
a row, not a reason to finish empty. When tables were found but none can be loaded (only
other QTL types, only data that needs outside reference files, no association table at all),
submit with `rows: []` and every table in skipped_tables, so the reasons are kept. Use
give_up only when no table was found at all (nothing public, every location manual or
unreachable); the reason says which, and what the paper says about the full results.

In the final summary, group repeated missing/unverified facts by field and explain the
cause once, while retaining the program's issue codes. Distinguish build completion, local
files that passed validation, partial statistics and full data still needing manual access.
Do not claim to have fixed UI issue duplication or expanded supported QTL types.
