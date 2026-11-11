"""Human review (FR-07): the queue, one item with everything a reviewer needs, the decision.

The reviewer sees real names, because they need them to judge the report. What is stored
goes back to tokens. History is append-only: a decision adds a row, nothing is deleted.
"""

import json
import sqlite3
from dataclasses import dataclass, field

from defect_automation.db import audit, set_status, utc_now
from defect_automation.domain import ReportStatus
from defect_automation.extraction import prepare_report_text
from defect_automation.logging_setup import get_logger
from defect_automation.masking import remask, unmask
from defect_automation.parsing import parse_report
from defect_automation.schemas import FIELD_NAMES, ExtractedFields
from defect_automation.validation import CheckResult

APPROVE, REJECT = "approve", "reject"


class ReviewError(RuntimeError):
    """The decision cannot be saved, for a reason the reviewer can act on."""


@dataclass
class ReviewItem:
    report_id: int
    file_name: str
    received_at: str
    original_text: str
    fields: ExtractedFields | None  # None when the model's answer failed the schema twice
    confidence: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)


def review_queue(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Reports waiting for a person, oldest first, critical ones on top."""
    return conn.execute(
        """
        SELECT r.id, r.file_name, r.received_at,
               json_extract(e.fields_json, '$.severity') AS severity
        FROM reports r
        LEFT JOIN extractions e ON e.id = (
            SELECT MAX(id) FROM extractions WHERE report_id = r.id)
        WHERE r.status = ?
        ORDER BY (severity = 'critical') DESC, r.received_at, r.id
        """,
        (str(ReportStatus.IN_REVIEW),),
    ).fetchall()


def _reasons(conn: sqlite3.Connection, report_id: int) -> list[str]:
    row = conn.execute(
        "SELECT event, detail_json FROM audit_log WHERE report_id = ? AND event IN "
        "('routed', 'extraction_schema_failed', 'post_blocked') ORDER BY id DESC LIMIT 1",
        (report_id,),
    ).fetchone()
    if row is None:
        return []
    detail = json.loads(row["detail_json"])
    if row["event"] == "routed":
        return list(detail.get("reasons", []))
    if row["event"] == "post_blocked":
        return [f"posting was blocked: {detail.get('reason', '')}"]
    return ["the model's answer failed the schema check twice; fill the fields by hand"]


def load_review_item(conn: sqlite3.Connection, report_id: int) -> ReviewItem:
    report = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    if report is None:
        raise ReviewError(f"report {report_id} does not exist")
    item = ReviewItem(
        report_id=report_id,
        file_name=report["file_name"],
        received_at=report["received_at"],
        original_text=parse_report(
            report["source_type"], report["raw_text"], clean=False
        ).as_text(),
        fields=None,
        reasons=_reasons(conn, report_id),
    )
    extraction = conn.execute(
        "SELECT * FROM extractions WHERE report_id = ? ORDER BY id DESC LIMIT 1", (report_id,)
    ).fetchone()
    if extraction is None:
        return item

    mapping = prepare_report_text(report["source_type"], report["raw_text"]).mapping
    fields = ExtractedFields.model_validate_json(extraction["fields_json"])
    item.fields = fields.model_copy(
        update={
            "reporter": unmask(fields.reporter, mapping),
            "defect_description": unmask(fields.defect_description, mapping),
        }
    )
    item.confidence = json.loads(extraction["confidence_json"]).get("fields", {})
    item.checks = [
        CheckResult(row["check_name"], bool(row["passed"]), row["message"])
        for row in conn.execute(
            "SELECT * FROM validations WHERE extraction_id = ? ORDER BY id", (extraction["id"],)
        )
    ]
    return item


def submit_review(
    conn: sqlite3.Connection,
    run_id: str,
    report_id: int,
    reviewer: str,
    action: str,
    fields: ExtractedFields | None = None,
    note: str = "",
) -> None:
    """Save a decision. approve -> 'approved' (ready to post), reject -> 'rejected'."""
    if action not in (APPROVE, REJECT):
        raise ReviewError(f"unknown action {action!r}")
    if not reviewer.strip():
        raise ReviewError("a reviewer name is required")
    report = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    if report is None or report["status"] != ReportStatus.IN_REVIEW:
        raise ReviewError("this report is no longer waiting for review")

    stored_json: str | None = None
    changed: list[str] = []
    if action == APPROVE:
        if fields is None:
            raise ReviewError("approving needs the ticket fields")
        if fields.severity is None or fields.defect_type is None or not fields.defect_description:
            raise ReviewError("severity, defect type and description are needed for a ticket")
        mapping = prepare_report_text(report["source_type"], report["raw_text"]).mapping
        stored = fields.model_copy(
            update={
                "reporter": remask(fields.reporter, mapping),
                "defect_description": remask(fields.defect_description, mapping),
            }
        )
        stored_json = stored.model_dump_json()
        extraction = conn.execute(
            "SELECT fields_json FROM extractions WHERE report_id = ? ORDER BY id DESC LIMIT 1",
            (report_id,),
        ).fetchone()
        before = json.loads(extraction["fields_json"]) if extraction else {}
        after = json.loads(stored_json)
        changed = [name for name in FIELD_NAMES if before.get(name) != after.get(name)]

    conn.execute(
        "INSERT INTO reviews (report_id, reviewer, action, edited_fields_json, reviewed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (report_id, reviewer.strip(), action, stored_json, utc_now()),
    )
    audit(
        conn,
        run_id,
        "reviewed",
        report_id,
        {"action": action, "reviewer": reviewer.strip(), "changed_fields": changed, "note": note},
    )
    status = ReportStatus.APPROVED if action == APPROVE else ReportStatus.REJECTED
    set_status(conn, run_id, report_id, status)
    conn.commit()
    # The log names the decision, not the person: no per-person tracking outside the audit trail.
    get_logger().info(
        "reviewed",
        extra={"report_id": report_id, "action": action, "changed_fields": len(changed)},
    )
