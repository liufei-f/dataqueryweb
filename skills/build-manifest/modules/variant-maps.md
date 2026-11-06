---
name: variant-maps
when: a table lacks the other allele or its positions, or the study provides a SNP info / variant map
required_before: [fill_alleles, fill_positions, assess_variant_map]
---

# Study variant maps, missing alleles and positions

## Use documented, study-provided variant maps (curated checks)

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
  allele in `alt` and `ref` empty, then call fill_alleles (below): with `map_file` = the
  study's SNP info file when the source provides one (assessed `variant_map` / `companion`),
  otherwise without it, from the local dbSNP copy. The study's own map always comes first.
  run_python code that writes `_processed` tables and names the map file is refused.
- **Only rs ids, no chromosome / position → fill_positions with the study's map.** Write the
  table with its rs ids and empty `chrom` / `pos`, then call fill_positions (below) with the
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

## fill_positions: chromosome and position from rs ids

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

## fill_alleles: the missing non-effect allele

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
