import json
import sqlite3
from pathlib import Path

import pytest

from defect_automation.config import Settings
from defect_automation.pipeline import requeue_failed, run_pipeline
from defect_automation.routing import Route
from tests.conftest import SAMPLE_TXT, VALID_ANSWER, FakeLLM


def answer(**changes: object) -> str:
    return json.dumps({**VALID_ANSWER, **changes})


@pytest.fixture
def inbox(settings: Settings, tmp_path: Path) -> Settings:
    folder = tmp_path / "inbox"
    folder.mkdir()
    return settings.model_copy(update={"input_dir": folder})


def drop(settings: Settings, name: str, text: str = SAMPLE_TXT) -> None:
    (settings.input_dir / name).write_text(text, encoding="utf-8")


def statuses(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["file_name"]: r["status"] for r in conn.execute("SELECT * FROM reports")}


def test_clean_report_is_approved_and_broken_ones_land_in_review(
    conn: sqlite3.Connection, inbox: Settings
) -> None:
    drop(inbox, "a_clean.txt")
    drop(inbox, "b_critical.txt", SAMPLE_TXT + "\nsecond file")
    drop(inbox, "c_unknown_part.txt", SAMPLE_TXT + "\nthird file")
    drop(inbox, "d_no_order.txt", SAMPLE_TXT + "\nfourth file")
    llm = FakeLLM(
        [
            answer(),
            answer(severity="critical"),
            answer(part_number="BL-M10-9999"),
            answer(order_number=None),
        ]
    )
    summary = run_pipeline(conn, llm, inbox, "run-1")

    assert statuses(conn) == {
        "a_clean.txt": "approved",
        "b_critical.txt": "in_review",
        "c_unknown_part.txt": "in_review",
        "d_no_order.txt": "in_review",
    }
    assert summary.decisions["a_clean.txt"].route is Route.AUTO_APPROVE
    assert summary.decisions["b_critical.txt"].reasons == ["severity is critical"]
    assert any("part_exists" in r for r in summary.decisions["c_unknown_part.txt"].reasons)
    assert summary.decisions["d_no_order.txt"].reasons == ["order_number is missing"]


def test_every_check_and_the_route_are_recorded(conn: sqlite3.Connection, inbox: Settings) -> None:
    drop(inbox, "a.txt")
    run_pipeline(conn, FakeLLM([answer(severity="critical")]), inbox, "run-1")

    assert conn.execute("SELECT COUNT(*) FROM validations").fetchone()[0] == 9
    routed = conn.execute("SELECT * FROM audit_log WHERE event = 'routed'").fetchone()
    assert routed["run_id"] == "run-1"
    assert json.loads(routed["detail_json"])["route"] == "review"
    flow = [
        json.loads(r["detail_json"])["status"]
        for r in conn.execute("SELECT * FROM audit_log WHERE event = 'status_changed' ORDER BY id")
    ]
    assert flow == ["parsed", "extracted", "validated", "in_review"]


def test_second_run_over_the_same_folder_does_nothing(
    conn: sqlite3.Connection, inbox: Settings
) -> None:
    drop(inbox, "a.txt")
    llm = FakeLLM([answer()])
    run_pipeline(conn, llm, inbox, "run-1")
    second = run_pipeline(conn, llm, inbox, "run-2")  # an extra model call would raise

    assert (second.new_reports, second.duplicates) == (0, 1)
    assert second.decisions == {}
    assert conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM extractions").fetchone()[0] == 1


def test_run_resumes_after_a_crash_between_steps(conn: sqlite3.Connection, inbox: Settings) -> None:
    drop(inbox, "a.txt")
    run_pipeline(conn, FakeLLM([answer()]), inbox, "run-1")
    # Simulate a crash right after extraction: the report is back at 'extracted'.
    conn.execute("UPDATE reports SET status = 'extracted'")
    conn.commit()

    run_pipeline(conn, FakeLLM([]), inbox, "run-2")  # no model call needed
    assert statuses(conn) == {"a.txt": "approved"}
    assert conn.execute("SELECT COUNT(*) FROM validations").fetchone()[0] == 9


def test_failed_report_can_be_requeued(conn: sqlite3.Connection, inbox: Settings) -> None:
    drop(inbox, "a.txt")
    run_pipeline(conn, FakeLLM([TimeoutError("slow")]), inbox, "run-1")
    assert statuses(conn) == {"a.txt": "failed"}

    assert requeue_failed(conn, "run-2") == 1
    run_pipeline(conn, FakeLLM([answer()]), inbox, "run-2")
    assert statuses(conn) == {"a.txt": "approved"}
