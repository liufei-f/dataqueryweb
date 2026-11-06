---
name: add-rsid
when: a processed table has no rs id column, or rs ids dbSNP holds are empty (step 5, last of the fixed programs)
required_before: [add_rsid]
---

# Fill the rs ids (add_rsid)

Every submitted table carries an rs id column, and **every variant dbSNP holds must carry its
rs id**. An empty rs id is allowed only where dbSNP has no record for that chrom / pos / ref /
alt. A column that exists but is empty (or only partly filled) is not done: writing an empty
`rsid` column in your own processing script does not count as adding rs ids. When the
processed table has no rs id column, or any of its rs ids are empty, call add_rsid on it —
also when the table was processed in an earlier build (REBUILD CHECK: check the `rsid` values of
reused `_processed` files, never assume they were filled):

- `file_path`: the processed table (`<name>_processed.tsv.gz`; a `chr*` glob for split files)
- `columns`: its `chrom_col`, `position_col`, `ref_col`, `alt_col`
- `rsid_col`: `rsid` (or the table's own rs id column, whose existing ids are kept)

add_rsid looks each variant up in the local copy of NCBI dbSNP for GRCh38 (build 157) by
chromosome, position, ref and alt, also trying the alleles swapped. A variant dbSNP does not
hold keeps an empty rs id: never invent one, never copy one from a nearby position. It works
on GRCh38 coordinates only, so a GRCh37 table goes through liftover (module `liftover`) first. Never
download dbSNP or any other reference yourself. If add_rsid answers that the reference is not in place, leave the column out and say so in the note.

Run add_rsid only on the files that need it. A file the REBUILD CHECK lists as "KEPT, in the
target format" already carries every rs id dbSNP holds: do not run add_rsid on it again (it
rewrites the whole file for nothing); name only the files listed as "KEPT, rs ids missing" or
processed in this run.

The program skips by itself every file whose rs ids are already complete (the REBUILD CHECK
found them complete, or an earlier add_rsid filled them, and the file has not changed since); they
are listed under `skipped_complete`.

Report the share it found (`with_rsid` / `rows`) in the row's note.

**Check:** `with_rsid` is above zero (a GRCh38 QTL table normally has most variants in dbSNP;
a share near 0 means wrong coordinates, wrong build or swapped columns — investigate, do not
submit), and inspect_local shows `rsid` values like `rs346557`.

**Enforced by code:** validate looks up a sample (up to 500 variants per file) of the rows whose
rs id is empty in the local dbSNP copy, the same way add_rsid does. If dbSNP holds any of
them, validate fails with `rsid_col … is empty for N of M sampled variants that dbSNP holds`
and the row cannot be submitted until add_rsid has run and validate passes again. The check
is skipped only when the dbSNP copy is not in place (then say so in the note).
