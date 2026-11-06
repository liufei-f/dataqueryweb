---
name: manifest-columns
when: naming the columns of a row: before validate and before submit
required_before: [validate, submit]
---

# The manifest files and the 27 columns of a row

| File | Role | What you do with it |
|---|---|---|
| Reference manifest: `reference/qtl_manifest.tsv`, this project's copy of LocusCompare's `scripts/qtl_manifest.tsv` | What LocusCompare already loads. Read only, never written | The first message says whether this paper is already in it, under which dataset name and with which rows (qtl_type + biocontext). The instructions list every dataset it holds and its level_1_context vocabulary |
| `manifest/qtl_manifest_candidates.tsv` | Every row you submit, with status, issues and evidence | Written by the program from your submit; a person reviews it on the /manifest page |
| `manifest/qtl_manifest.tsv` | The rows a person approved, in the reference manifest's 27 columns, ready to append to it | Written by the program after approval |

You never write these files: no tool can, and submit is the only way rows get in. GWAS
summary statistics belong to LocusCompare's separate `gwas_manifest.tsv` and are not
submitted here; list a paper's GWAS tables in skipped_tables as "GWAS, not QTL".

Reference checks:
- A paper already in the reference manifest (same DOI) is not processed at all: the queue skips it.
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
value. The values in those columns must follow the required formats in step 4; the importer
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
(see step 6, submit, in the core procedure).
