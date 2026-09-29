"""The written criteria for the AI check: one source for the prompt, the scoring and the page.

The LLM never picks a verdict or a confidence number. It answers three factual QUESTIONS
("yes" / "no" / "unclear", each backed by a verbatim quote from the paper). The program adds
two facts it knows itself (was the full text read? was a download location for QTL results
identified?). :func:`score` then derives the verdict and the confidence from fixed rules and
weights, so the same answers always give the same result.

Change RUBRIC_VERSION whenever a question, a rule or a weight changes; it is stored with
every verdict so old and new scores can be told apart.
"""

from __future__ import annotations

import re
from typing import Any

RUBRIC_VERSION = "2026-09-28"

# What a picked location must hold to count as "a download location for QTL results".
RESULT_CONTENT = ("qtl_summary_statistics", "qtl_results_browser", "supplementary_table")

ANSWERS = ("yes", "no", "unclear")

CONTENT_TYPES = (*RESULT_CONTENT, "raw_or_individual_data", "other")
# Models sometimes describe a location in words instead of naming a content type; these
# rules (checked in this order: browser, supplement, summary statistics, raw data) map the
# description onto one, so the score does not depend on
# the model's wording.
CONTENT_RULES: list[tuple[str, str]] = [
    (
        "qtl_results_browser",
        r"browser|browsing|portal|shiny|web ?(?:site|app|server)|interactive|database",
    ),
    ("supplementary_table", r"supplement|supp\.? ?table|additional file|\btable s?\d"),
    (
        "qtl_summary_statistics",
        r"summary[ _-]?stat|sumstat|full (?:association )?results|nominal|all (?:tested )?(?:snp|variant)-gene"
        r"|association results|qtl (?:results|associations|data)",
    ),
    (
        "raw_or_individual_data",
        r"raw|fastq|\bbam\b|individual|genotype|sequencing data|controlled|ega|dbgap|expression matrix|count",
    ),
]

# (key, question, definition) — sent verbatim to the LLM and shown on How it works.
QUESTIONS: list[tuple[str, str, str]] = [
    (
        "mapped_qtls",
        "Did the authors themselves perform QTL association mapping?",
        '"yes" if the methods or results describe the authors testing genetic variants for '
        "association with molecular phenotypes (gene expression, splicing, protein, chromatin "
        "accessibility, methylation, metabolites, …) in genotyped samples and reporting the "
        "resulting QTLs — in their own cohort, or by newly analysing public genotype + "
        'phenotype data. "no" if the authors did not map QTLs. "unclear" if the text given '
        "does not say.",
    ),
    (
        "reuse_only",
        "Does the paper only reuse QTL results published by others?",
        '"yes" if every QTL association the paper uses comes from existing resources or '
        "published summary statistics (GTEx, eQTLGen, eQTL Catalogue, UKB-PPP, deCODE, …) — "
        "e.g. Mendelian randomization, SMR, colocalization, TWAS, or fine-mapping of published "
        'QTLs. "no" if the paper also produces QTL associations of its own. "unclear" '
        "otherwise.",
    ),
    (
        "results_released",
        "Does the paper say where its own QTL results can be obtained?",
        '"yes" if the paper states where its OWN QTL association results (summary statistics, '
        "full result tables, supplementary tables of QTLs, or a results browser) can be "
        'obtained — including controlled access or "available on request". "no" if it says '
        "they are not available, or its data availability statement mentions no QTL results. "
        '"unclear" if the text given contains no data availability information.',
    ),
]

# (key, label, weight, how it is decided). Weights sum to 1.0; with the default auto-save
# threshold of 0.90, a paper is auto-saved only when ALL five criteria are met.
CRITERIA: list[tuple[str, str, float, str]] = [
    (
        "mapped_qtls",
        "Authors mapped QTLs themselves",
        0.35,
        'LLM answers "yes" to question 1, with a supporting quote',
    ),
    ("not_reuse_only", "Not only reusing published QTLs", 0.15, 'LLM answers "no" to question 2'),
    (
        "results_released",
        "Paper says where its QTL results are",
        0.20,
        'LLM answers "yes" to question 3, with a supporting quote',
    ),
    (
        "location_found",
        "Download location for QTL results identified",
        0.15,
        "at least one picked link holds QTL summary statistics, a results browser or a "
        "supplementary table (checked by the program)",
    ),
    (
        "full_text_read",
        "Judged from the full text",
        0.15,
        "the full text was read — PMC / Europe PMC, the publisher page, or AI web search "
        "(checked by the program)",
    ),
]

VERDICT_RULES = [
    '"New QTL data" (yes) — question 1 is "yes" and question 2 is not "yes".',
    '"No new QTL data" (no) — question 1 is "no", or question 2 is "yes" (and question 1 is not "yes").',
    '"Unclear" — anything else, including a contradiction (1 "yes" but 2 "yes").',
    'A "yes" without a verbatim quote counts as "unclear".',
]

CONFIDENCE_RULES = [
    "For a yes-verdict: the sum of the weights of the criteria that are met.",
    'For a no-verdict: 0.40 if question 1 is "no" + 0.40 if question 2 is "yes" + 0.20 if the full text was read.',
    "For unclear: 0.",
]


def prompt_block() -> str:
    """The questions as the LLM sees them."""
    lines = [
        'Answer these three questions strictly by the definitions. Each answer is "yes", '
        '"no" or "unclear", with "quote": one short VERBATIM sentence from the given text that '
        'supports it (use "" when unclear). Do not guess: if the text does not say, answer '
        '"unclear".'
    ]
    for i, (key, question, definition) in enumerate(QUESTIONS, 1):
        lines.append(f"{i}. {key} — {question} {definition}")
    return "\n".join(lines)


ANSWERS_SCHEMA = (
    '"answers": {'
    + ", ".join(f'"{k}": {{"answer": "yes|no|unclear", "quote": "…"}}' for k, _, _ in QUESTIONS)
    + "}"
)


def parse_answers(raw: Any) -> dict[str, dict[str, str]]:
    """The LLM's answers, with anything missing or malformed read as "unclear"."""
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for key, _, _ in QUESTIONS:
        item = raw.get(key)
        if isinstance(item, str):
            item = {"answer": item}
        item = item if isinstance(item, dict) else {}
        answer = str(item.get("answer", "unclear")).strip().lower()
        quote = str(item.get("quote") or "").strip()[:400]
        if answer not in ANSWERS:
            answer = "unclear"
        if answer == "yes" and not quote:  # an unsupported "yes" is not evidence
            answer = "unclear"
        out[key] = {"answer": answer, "quote": quote}
    return out


def score(
    answers: dict[str, dict[str, str]], *, location_found: bool, full_text_read: bool
) -> dict[str, Any]:
    """Verdict, confidence and the criteria checklist — fully determined by the inputs."""
    a = {k: v["answer"] for k, v in answers.items()}
    if a["mapped_qtls"] == "yes" and a["reuse_only"] != "yes":
        verdict = "yes"
    elif a["mapped_qtls"] == "no" or (a["reuse_only"] == "yes" and a["mapped_qtls"] != "yes"):
        verdict = "no"
    else:
        verdict = "unclear"

    met = {
        "mapped_qtls": a["mapped_qtls"] == "yes",
        "not_reuse_only": a["reuse_only"] == "no",
        "results_released": a["results_released"] == "yes",
        "location_found": location_found,
        "full_text_read": full_text_read,
    }
    if verdict == "yes":
        confidence = sum(w for key, _, w, _ in CRITERIA if met[key])
    elif verdict == "no":
        confidence = (
            0.40 * (a["mapped_qtls"] == "no")
            + 0.40 * (a["reuse_only"] == "yes")
            + 0.20 * full_text_read
        )
    else:
        confidence = 0.0
    return {
        "new_qtl_data": verdict,
        "confidence": round(confidence, 2),
        "criteria": [
            {"id": key, "label": label, "weight": weight, "met": met[key]}
            for key, label, weight, _ in CRITERIA
        ],
        "answers": answers,
        "rubric": RUBRIC_VERSION,
    }


def normalize_content(value: Any) -> str:
    """A content type from the model's value — the exact name, or its description."""
    text = str(value or "").strip().lower()
    if text in CONTENT_TYPES:
        return text
    for content, pattern in CONTENT_RULES:
        if re.search(pattern, text):
            return content
    return "other"


def unmet_labels(verdict: dict[str, Any]) -> list[str]:
    return [c["label"] for c in verdict.get("criteria") or [] if not c["met"]]
