---
name: access
when: a location needs a person (login, application, browser-only), or a download / page failed
required_before: [note_manual, give_up]
---

# Access, failures and human bottlenecks

## When a person must act (note_location manual)

Use `manual` only when a concrete human action is established. Each such location note must say:
what blocks progress, the evidence, the exact action required, the verified application/login/download
page URL, and what instructions or server file paths the curator should supply to resume. If the
URL is unknown, explicitly say it is unverified; do not invent one. Network failures, conversion
errors and unsupported QTL types alone do not establish a need for a human access application.

## Distinguish the actual obstacle (curated checks)

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
  documented file/download endpoints within step 1's link-depth and call budgets. A landing
  page is not the complete inventory. Do not guess private endpoints.
- For GEO, inspect the public supplementary directory and descriptions. Series Matrix,
  gene-by-individual expression matrices and raw counts are not QTL association statistics;
  download them only if their actual contents are the required association table. A filename
  containing "eQTL_Mapping" is insufficient.
- Read the GEO README before deciding whether a companion file is unrelated. If an association
  file references rsIDs or lacks coordinate/allele columns, look for a README-documented,
  same-study variant map (module `variant-maps`), inspect it, and test the mapping
  before skipping the association data.
- Keep these explanations distinct in note_location and the final summary: (1) confirmed
  application / author request, quoting its instructions; (2) manual browser/session download
  (e.g. Dryad), which need not require an application; (3) network or publisher blocking;
  (4) raw/source data, annotation, GWAS or other non-QTL output; (5) no download route found,
  access requirements unconfirmed. Use `manual` for a genuine person/browser action and
  `not_data` for unrelated material. Do not describe an empty JS page as an empty data file.
