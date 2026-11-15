import json
from pathlib import Path

import pytest

from defect_automation.config import Settings
from defect_automation.evaluation import (
    field_matches,
    format_history,
    format_report,
    load_results,
    run_eval,
    save_result,
)
from tests.conftest import RECENT_DATE, SAMPLE_TXT, VALID_ANSWER, FakeLLM

LABEL = {
    "order_number": "SG-2026-0003",
    "part_number": "BL-M10-0060",
    "supplier": "Wexmoor Fasteners Ltd",
    "defect_type": "surface_damage",
    "defect_description": "anything, not scored",
    "quantity_affected": 12,
    "severity": "major",
    "detected_at": "incoming_inspection",
    "detected_on": RECENT_DATE,
    "reporter": "Anna Keller",
    "case_type": "clean",
}


def answer(**changes: object) -> str:
    return json.dumps({**VALID_ANSWER, **changes})


@pytest.fixture
def eval_settings(settings: Settings, tmp_path: Path) -> Settings:
    """Three labelled reports: a clean one, a critical one and one without an order number."""
    reports = tmp_path / "reports"
    reports.mkdir()
    labels = {
        "a_clean.txt": LABEL,
        "b_critical.txt": {**LABEL, "severity": "critical"},
        "c_no_order.txt": {**LABEL, "order_number": None, "case_type": "missing_order"},
    }
    for name in labels:
        (reports / name).write_text(f"{SAMPLE_TXT}\n{name}", encoding="utf-8")
    (reports / "unlabelled.txt").write_text("not part of the eval set", encoding="utf-8")
    labels_path = tmp_path / "labels.json"
    labels_path.write_text(json.dumps(labels), encoding="utf-8")
    return settings.model_copy(
        update={
            "input_dir": reports,
            "labels_path": labels_path,
            "eval_work_dir": tmp_path / "work",
            "eval_results_dir": tmp_path / "results",
        }
    )


def test_perfect_answers_score_100_percent(eval_settings: Settings) -> None:
    llm = FakeLLM([answer(), answer(severity="critical"), answer(order_number=None)])
    result = run_eval(eval_settings, llm)

    assert result.reports == 3  # the unlabelled file is left out
    assert result.field_accuracy == 1.0
    assert result.mismatches == []
    assert (result.critical_total, result.critical_recall) == (1, 1.0)
    assert (result.must_review_total, result.must_review_caught) == (2, 2)
    assert (result.sent_to_review, result.extra_reviews) == (2, 0)
    assert (result.auto_approved, result.auto_approved_with_errors) == (1, 0)
    assert result.per_field["reporter"] == {"correct": 3, "total": 3, "accuracy": 1.0}
    assert result.cost_per_report_eur == pytest.approx((1000 * 0.10 + 200 * 0.40) / 1_000_000)


def test_missed_critical_shows_up_as_recall_below_100(eval_settings: Settings) -> None:
    # The model rates the critical report as major and nothing else flags it.
    llm = FakeLLM([answer(), answer(severity="major"), answer(order_number=None)])
    result = run_eval(eval_settings, llm)

    assert result.critical_recall == 0.0
    assert result.critical_rated_critical_by_model == 0
    assert result.auto_approved_with_errors == 1
    assert result.mismatches == [
        {
            "file": "b_critical.txt",
            "case_type": "clean",
            "field": "severity",
            "expected": "critical",
            "got": "major",
            "sent_to_review": False,
        }
    ]
    assert result.field_accuracy == pytest.approx(26 / 27)


def test_invented_value_for_a_missing_field_counts_as_wrong(eval_settings: Settings) -> None:
    llm = FakeLLM([answer(), answer(severity="critical"), answer()])  # order number made up
    result = run_eval(eval_settings, llm)
    assert result.per_field["order_number"]["correct"] == 2
    assert result.must_review_caught == 1  # and the missing-order case slipped through


def test_report_whose_extraction_failed_scores_zero_and_counts_as_reviewed(
    eval_settings: Settings,
) -> None:
    llm = FakeLLM(
        ["not json", "still not json", answer(severity="critical"), answer(order_number=None)]
    )
    result = run_eval(eval_settings, llm)
    assert result.per_field["part_number"]["correct"] == 2
    assert result.sent_to_review == 3
    assert result.extra_reviews == 1


def test_results_are_saved_and_listed_for_comparison(eval_settings: Settings) -> None:
    llm = FakeLLM([answer(), answer(severity="critical"), answer(order_number=None)])
    result = run_eval(eval_settings, llm)
    path = save_result(result, eval_settings.eval_results_dir)

    assert path.name.endswith("_extraction_v1_gemini-3.5-flash-lite.json")
    latest = json.loads((eval_settings.eval_results_dir / "latest.json").read_text())
    assert latest["field_accuracy"] == 1.0
    runs = load_results(eval_settings.eval_results_dir)
    assert len(runs) == 1
    assert "extraction_v1" in format_history(runs)
    report = format_report(result)
    assert "ALL SCORED FIELDS" in report and "100.0%" in report


@pytest.mark.parametrize(
    ("name", "expected", "got", "matches"),
    [
        ("supplier", "Halden Seals Sp. z o.o.", "halden seals sp z o.o", True),
        ("reporter", "Lutz Häring", "Lutz Häring", True),
        ("part_number", "DS-72-2700", "DS-72-2000", False),
        ("quantity_affected", 12, 12, True),
        ("detected_on", "2026-10-07", "2026-10-08", False),
        ("order_number", None, None, True),
        ("order_number", None, "SG-2026-0003", False),
        ("order_number", "SG-2026-0003", None, False),
    ],
)
def test_field_matching(name: str, expected: object, got: object, matches: bool) -> None:
    assert field_matches(name, expected, got) is matches
