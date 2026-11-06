---
name: resume-rebuild
when: the paper was processed before: the first message has REBUILD CHECK or history files (step 1)
required_before: [start_with_history]
---

# Resume and rebuild: start from what is on disk

## The REBUILD CHECK (done by the program before your first step)

When the paper's folder already holds files, the initial message starts with a REBUILD CHECK
that the program ran before your first step. It is the answer to step 1's question "what is
already on disk"; act on it:

1. **DOWNLOADED FILES** — every downloaded file was already format-checked (inspect_local is
   optional for them). They are not downloaded again (step 2 only fetches what is missing).
   Assessments of unchanged files from earlier builds are reused; assess the rest in step 4;
   `assess_file` takes a glob for a group of files with the same header and contents
   (e.g. `folder/x.eQTL.*.cis_nominal.tsv.gz`).
2. **PROCESSED FILES** — every `*_processed` table was read to the end and its first rows tested
   against the required formats (core step 4 table: chrom `1`–`22` without `chr`, variant id
   `chr<chrom>_<pos>_<ref>_<alt>`, caQTL peak `chr<chrom>_<start>_<end>`, rs ids filled where
   dbSNP holds them):
   - **KEPT, in the target format** — not processed again: after step 3 (its facts) and step 4
     (the assessment of its source; a reused one counts) go straight to step 6. add_rsid skips it
     by itself.
   - **KEPT, rs ids missing** — the format is right: steps 3 and 4 as above, step 5 only for
     add_rsid (a glob works), then step 6. Do not reprocess.
   - **DELETED** — truncated (an interrupted write) or not in the target format. The program
     removed them: take their sources through steps 4–6 again. Your own checkpoint or stats
     files that list them as done are stale: do not skip them because of such a file.
   Never reuse a processed file the check deleted, and never write a processing script that
   skips a source only because its `_processed` file exists.

## Resume from the previous run

Before repeating a build, inspect the previous run outcome, existing local files and the
history paths supplied in the initial message. Use `read_build_history(path, offset)` to read
previous Markdown logs and Python scripts; follow `next_offset` when `more` is true. A tail
excerpt may omit earlier successful work, so read relevant preceding sections before deciding
to repeat a costly step.

- Identify the last completed checkpoint, the interrupted or failed operation, and the next
  unfinished step. A saved script proves what was attempted, not that it finished successfully.
- Check reusable outputs, their expected sizes, headers, context and validation results. Inspect
  existing inventories and variant maps before repeating full scans of large compressed archives.
  Revalidate an output whose completeness or compatibility is uncertain; do not infer completion
  solely from its filename. Missing or invalid intermediates may require rerunning their producer.
- Record the resume plan using `note_location` with status `listed`: completed work, reusable
  files, uncertainties, the next step and why it is needed. Resume from that step; revisit earlier
  phases only where evidence is missing, contradicted or invalidated.
- For legacy runs without Markdown logs, inspect saved reports, Python scripts and actual files.
  Explicitly distinguish inferred progress from confirmed successful execution.
- Preserve existing source data and valid outputs. Reuse registered downloads and resumable partial
  downloads through the download tool. Do not overwrite curator uploads or delete files to restart;
  invalid processed files are removed by the program's rebuild check, not by you.
- Treat the paper's saved conversation as curator context. Read supplied access emails, README
  text and local paths; verify their relevance to this paper, release and QTL type (a GWAS
  manifest of the same consortium is not its QTL release).

**Check:** a restart has an evidence-based resume plan before expensive download or processing.

## Build Markdown audit

The host records each new run in `data/.build_logs/<record_id>/<run_id>.md`, incrementally.
It logs tool arguments and results, Python source and invocation, stdout/stderr, validation and
submission results, and checkpoints. Use available tools for all operations so they enter this
history. For any supported command execution, retain the actual command, working directory,
exit status and result; do not describe an unexecuted Bash command as completed work. Python
scripts have separate run-specific paths and must remain traceable from the Markdown.

State substantive decisions and reasons in tool notes or the final summary, including source
selection, variant-map joins, conversion requirements, failed checks and recovery. Do not include
API keys, passwords, authorization headers or signed access credentials in notes or commands.
Record a safe landing-page URL instead. Logging does not require copying entire source datasets
into Markdown: keep full data files on the server, with references and inspection results in the log.
The UI preview retains up to five rows; never truncate the actual source or processed dataset to
five rows. Direct streaming from an archive is valid and does not require extracting all members.
