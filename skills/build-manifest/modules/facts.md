---
name: facts
when: filling sample size, ancestry, genome build or context labels of the rows (before submit)
required_before: [submit]
---

# Facts for the rows: sample size, ancestry, genome build, context

## Missing sample sizes and metadata

For missing sample sizes, check full-text Methods, cohort/QC and supplementary tables, repository
README files and data headers. Report genotyped donors used in each context's QTL mapping, not
cells or the overall cohort copied across contexts. Preserve unknown values and explain the sources
checked when no supported count is found. A build should resolve missing journal metadata through
the paper-reading path when available; do not infer a journal or donor count from an unrelated release.
For manifest fields, retain field-specific source quotes and transformation reasons so the reviewer
can see both the proposed value and its justification.

## Read supplementary tables with read_table

Sample sizes per context, donor counts after QC and cohort descriptions are often only in
supplementary tables (`.xlsx`) or repository tables. Read them with read_table (`sheet`, and `find`
to pick the rows): its rows count as evidence, so a quote copied from a row (cells joined by
spaces) passes the check at submit. Do not read them with run_python for evidence: printed output
of your own code never counts.

## Recover missing facts from the correct release (curated checks)

- Before leaving sample size, ancestry or genome build missing, follow the repository's
  publication link, any "Now published in" link, and the publisher / Europe PMC full-text
  route already identified for this paper. Read Methods and the relevant tables, captions,
  supplementary cohort descriptions and Data/Population/Sample description sections.
  An abstract or a matching keyword is not a substitute for reading the section.
- For each file group, establish the release / phase, publication DOI and coordinate build
  together. Check the linked project's README, release notes and processing scripts when
  the paper omits the build. A preprint's release and a later re-release (e.g. on another build)
  are not interchangeable; a current repository README does not establish an older file's build.
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
