---
name: liftover
when: the table coordinates are GRCh37 / hg19, or the build is unclear
required_before: [liftover]
---

# GRCh37 → GRCh38 (liftover)

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
