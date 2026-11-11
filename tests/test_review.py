import json
import sqlite3
from pathlib import Path

import pytest

from defect_automation.config import Settings
from defect_automation.metrics import compute_metrics, reason_category
from defect_automation.pipeline import run_pipeline
from defect_automation.review import (
    APPROVE,
    REJECT,
    ReviewError,
    load_review_item,
    review_queue,
    submit_review,
)
from tests.conftest import SAMPLE_TXT, VALID_ANSWER, FakeLLM


def answer(**changes: object) -> str:
    return json.dumps({**VALID_ANSWER, **changes})


@pytest.fixture
def routed(conn: sqlite3.Connection, settings: Settings, tmp_path: Path) -> sqlite3.Connection:
    """Three reports through the pipeline: one clean, one critical, one with an unknown part."""
    folder = tmp_path / "inbox"
    folder.mkdir()
    for name in ("a_clean", "b_unknown_part", "c_critical"):
        (folder / f"{name}.txt").write_text(f"{SAMPLE_TXT}\n{name}", encoding="utf-8")
    llm = FakeLLM([answer(), answer(part_number="BL-M10-9999"), answer(severity="critical")])
    run_pipeline(conn, llm, settings.model_copy(update={"input_dir": folder}), "run-1")
    return conn


def report_id(conn: sqlite3.Connection, file_name: str) -> int:
    return conn.execute("SELECT id FROM reports WHERE file_name = ?", (file_name,)).fetchone()[0]


def status(conn: sqlite3.Connection, file_name: str) -> str:
    return conn.execute("SELECT status FROM reports WHERE file_name = ?", (file_name,)).fetchone()[
        0
    ]


def test_queue_lists_only_reports_in_review_with_critical_first(routed: sqlite3.Connection) -> None:
    queue = review_queue(routed)
    assert [row["file_name"] for row in queue] == ["c_critical.txt", "b_unknown_part.txt"]


def test_item_shows_original_text_real_name_and_reasons(routed: sqlite3.Connection) -> None:
    item = load_review_item(routed, report_id(routed, "b_unknown_part.txt"))
    assert "Anna Keller <anna.keller@factory.example>" in item.original_text
    assert item.fields is not None and item.fields.reporter == "Anna Keller"
    assert item.reasons == ["part_exists: part number 'BL-M10-9999' not found in master data"]
    assert item.confidence["order_number"] == 0.99
    assert len(item.checks) == 9


def test_approve_stores_edits_masked_and_moves_to_approved(routed: sqlite3.Connection) -> None:
    rid = report_id(routed, "b_unknown_part.txt")
    item = load_review_item(routed, rid)
    edited = item.fields.model_copy(update={"part_number": "BL-M10-0060"})
    submit_review(routed, "run-2", rid, "faisal", APPROVE, edited, note="typo in the email")

    assert status(routed, "b_unknown_part.txt") == "approved"
    review = routed.execute("SELECT * FROM reviews").fetchone()
    stored = json.loads(review["edited_fields_json"])
    assert stored["part_number"] == "BL-M10-0060"
    assert stored["reporter"] == "[PERSON_1]"  # the reviewer saw the name, the table does not
    detail = json.loads(
        routed.execute("SELECT detail_json FROM audit_log WHERE event = 'reviewed'").fetchone()[0]
    )
    assert detail["changed_fields"] == ["part_number"]
    assert detail["note"] == "typo in the email"


def test_reject_creates_no_ticket_fields_and_moves_to_rejected(routed: sqlite3.Connection) -> None:
    rid = report_id(routed, "c_critical.txt")
    submit_review(routed, "run-2", rid, "faisal", REJECT)
    assert status(routed, "c_critical.txt") == "rejected"
    assert routed.execute("SELECT edited_fields_json FROM reviews").fetchone()[0] is None


def test_a_report_can_only_be_decided_once(routed: sqlite3.Connection) -> None:
    rid = report_id(routed, "c_critical.txt")
    item = load_review_item(routed, rid)
    submit_review(routed, "run-2", rid, "faisal", APPROVE, item.fields)
    with pytest.raises(ReviewError, match="no longer waiting"):
        submit_review(routed, "run-2", rid, "faisal", APPROVE, item.fields)
    assert routed.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 1


def test_auto_approved_report_cannot_be_reviewed(routed: sqlite3.Connection) -> None:
    with pytest.raises(ReviewError):
        submit_review(routed, "run-2", report_id(routed, "a_clean.txt"), "faisal", REJECT)


def test_approve_needs_a_name_and_the_core_ticket_fields(routed: sqlite3.Connection) -> None:
    rid = report_id(routed, "c_critical.txt")
    item = load_review_item(routed, rid)
    with pytest.raises(ReviewError, match="reviewer name"):
        submit_review(routed, "run-2", rid, "  ", APPROVE, item.fields)
    with pytest.raises(ReviewError, match="severity"):
        no_severity = item.fields.model_copy(update={"severity": None})
        submit_review(routed, "run-2", rid, "faisal", APPROVE, no_severity)
    assert status(routed, "c_critical.txt") == "in_review"


def test_metrics_count_reports_not_people(routed: sqlite3.Connection) -> None:
    rid = report_id(routed, "c_critical.txt")
    submit_review(routed, "run-2", rid, "faisal", REJECT)
    m = compute_metrics(routed)

    assert m.total_reports == 3
    assert m.by_status == {"approved": 1, "in_review": 1, "rejected": 1}
    assert (m.decided_reports, m.sent_to_review) == (3, 2)
    assert m.review_rate == pytest.approx(2 / 3)
    assert m.review_reasons == {"critical severity": 1, "part not in master data": 1}
    assert m.defect_types == {"surface_damage": 3}
    assert m.severities == {"major": 2, "critical": 1}
    assert m.human_decisions == {"reject": 1}
    assert m.median_processing_seconds is not None and m.median_processing_seconds >= 0
    assert m.cost_per_report_eur == pytest.approx(m.cost_total_eur / 3)
    assert "faisal" not in json.dumps(m.__dict__)


def test_metrics_on_an_empty_database(conn: sqlite3.Connection) -> None:
    m = compute_metrics(conn)
    assert m.total_reports == 0 and m.review_rate is None and m.cost_per_report_eur is None


@pytest.mark.parametrize(
    ("reason", "category"),
    [
        ("severity is critical", "critical severity"),
        ("order_number is missing", "missing field"),
        ("severity confidence 0.70 is below 0.85", "low confidence"),
        ("single_defect: report mentions 2 part numbers", "more than one defect"),
        ("something new", "other"),
    ],
)
def test_reason_categories(reason: str, category: str) -> None:
    assert reason_category(reason) == category
