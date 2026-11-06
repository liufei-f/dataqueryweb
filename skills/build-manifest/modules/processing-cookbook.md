---
name: processing-cookbook
when: writing a run_python script that produces _processed tables
required_before: [process]
---

# Processing scripts: rules and recipes

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
  (module `variant-maps`), which reads the study's SNP info file or dbSNP itself. Never join association
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
