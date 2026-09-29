"""The AI-check rubric: fixed rules and weights turn the LLM's answers into a verdict."""

from __future__ import annotations

from dataqueryweb import rubric


def answers(mapped: str = "yes", reuse: str = "no", released: str = "yes") -> dict:
    q = {"yes": "A verbatim sentence.", "no": "", "unclear": ""}
    return rubric.parse_answers(
        {
            "mapped_qtls": {"answer": mapped, "quote": q[mapped]},
            "reuse_only": {"answer": reuse, "quote": q[reuse]},
            "results_released": {"answer": released, "quote": q[released]},
        }
    )


def test_weights_sum_to_one_and_threshold_needs_all_five() -> None:
    assert abs(sum(w for _, _, w, _ in rubric.CRITERIA) - 1.0) < 1e-9
    # Every single missing criterion drops a yes-verdict below the 0.90 auto-save threshold.
    smallest = min(w for _, _, w, _ in rubric.CRITERIA)
    assert 1.0 - smallest < 0.90


def test_all_met_scores_one() -> None:
    out = rubric.score(answers(), location_found=True, full_text_read=True)
    assert (out["new_qtl_data"], out["confidence"]) == ("yes", 1.0)
    assert all(c["met"] for c in out["criteria"])


def test_each_missing_criterion_costs_its_weight() -> None:
    assert rubric.score(answers(), location_found=True, full_text_read=False)["confidence"] == 0.85
    assert rubric.score(answers(), location_found=False, full_text_read=True)["confidence"] == 0.85
    assert (
        rubric.score(answers(released="unclear"), location_found=True, full_text_read=True)[
            "confidence"
        ]
        == 0.8
    )
    assert (
        rubric.score(answers(reuse="unclear"), location_found=True, full_text_read=True)[
            "confidence"
        ]
        == 0.85
    )
    assert rubric.score(answers(), location_found=False, full_text_read=False)["confidence"] == 0.7


def test_verdict_rules() -> None:
    s = rubric.score
    assert (
        s(answers("no", "yes", "no"), location_found=False, full_text_read=True)["new_qtl_data"]
        == "no"
    )
    assert (
        s(answers("no", "yes", "no"), location_found=False, full_text_read=True)["confidence"]
        == 1.0
    )
    assert (
        s(answers("unclear", "yes", "no"), location_found=False, full_text_read=False)["confidence"]
        == 0.4
    )
    assert s(
        answers("unclear", "unclear", "unclear"), location_found=True, full_text_read=True
    ) == s(answers("unclear", "unclear", "unclear"), location_found=True, full_text_read=True)
    unclear = s(answers("unclear", "unclear", "unclear"), location_found=True, full_text_read=True)
    assert (unclear["new_qtl_data"], unclear["confidence"]) == ("unclear", 0.0)
    contradiction = s(answers("yes", "yes", "yes"), location_found=True, full_text_read=True)
    assert contradiction["new_qtl_data"] == "unclear"


def test_yes_without_quote_counts_as_unclear() -> None:
    parsed = rubric.parse_answers(
        {"mapped_qtls": {"answer": "yes", "quote": ""}, "reuse_only": "maybe"}
    )
    assert parsed["mapped_qtls"]["answer"] == "unclear"
    assert parsed["reuse_only"]["answer"] == "unclear"
    assert parsed["results_released"] == {"answer": "unclear", "quote": ""}


def test_prompt_contains_every_question_verbatim() -> None:
    block = rubric.prompt_block()
    for key, question, definition in rubric.QUESTIONS:
        assert key in block and question in block and definition in block


def test_content_descriptions_map_to_fixed_types() -> None:
    n = rubric.normalize_content
    assert n("qtl_summary_statistics") == "qtl_summary_statistics"
    assert (
        n("Full summary statistics for cis-eQTL, trans-eQTL and eQTS analyses")
        == "qtl_summary_statistics"
    )
    assert n("ShinyApp for browsing the eQTL results") == "qtl_results_browser"
    assert n("Supplementary Table 3 with all significant QTLs") == "supplementary_table"
    assert n("Controlled-access EGA deposition of genotypes") == "raw_or_individual_data"
    assert n("analysis code") == "other" and n(None) == "other"
