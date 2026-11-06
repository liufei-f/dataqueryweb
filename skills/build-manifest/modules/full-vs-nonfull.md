---
name: full-vs-nonfull
when: no full summary-statistics table is kept for a QTL type the paper mapped
required_before: [submit_non_full, submit_empty, give_up]
---

# No full table yet: search the paper, then keep the most complete non-full one

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

Every new location goes through step 1: note_location, then peek_file before any
download. When this module finds the full summary statistics, continue with step 2.

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
3. Download, process and validate it like a full table (steps 2–6), and submit it with
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
