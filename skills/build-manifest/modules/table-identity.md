---
name: table-identity
when: judging what a downloaded file holds and which QTL type it is (before assess_file)
required_before: [assess_file]
---

# What a table is: software, phenotype, scope and QTL type

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
- Keep these outcomes separate: **not a QTL association table**, **genuine QTL results of a
  phenotype the current importer does not support**, and **access/content not yet verified**.
  An unsupported type is not proof of "not QTL". Do not write a human curation decision on
  the curator's behalf or silently replace it with an AI verdict. When a human has marked
  a record `not_qtl`, do not rebuild it unless that decision is explicitly cleared.

## Select, normalize and validate without changing what the data mean (curated checks)

- PDF/DOCX papers and documentation may supply evidence and links, but are not delimited
  QTL inputs. An XLSX/ZIP/TSV file is only a candidate: inspect its columns and contents.
  "Most significant variant per tested gene", "same as ... with annotation", fine-mapping,
  colocalisation, enrichment and other downstream analyses are not full nominal statistics
  (#140, #262). Prefer the full association table; apply the explicit non_full fallback of module `full-vs-nonfull`
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
  and liftover confirmed GRCh37 tables using steps 5–6. A proposed conversion is not a
  processed result: require the actual local output and passing validation.
