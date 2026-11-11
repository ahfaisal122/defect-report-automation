"""Moves reports through the status flow, one step at a time.

    received -> parsed -> extracted -> validated -> in_review | approved -> posted

Every step reads its input from SQLite and commits its result, so a run that stops
anywhere picks up where it left off. A report is selected by its status, so no step
runs twice for the same report.
"""

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from defect_automation.config import Settings
from defect_automation.cost import CostCapExceeded
from defect_automation.db import audit, set_status
from defect_automation.domain import ReportStatus
from defect_automation.extraction import extract_report, prepare_report_text
from defect_automation.ingest import ingest_folder
from defect_automation.llm import LLMClient
from defect_automation.logging_setup import get_logger
from defect_automation.posting import post_approved, qm_available
from defect_automation.routing import Route, RouteDecision, decide_route
from defect_automation.schemas import ExtractedFields
from defect_automation.validation import CheckResult, MasterData, validate


@dataclass
class RunSummary:
    new_reports: int = 0
    duplicates: int = 0
    decisions: dict[str, RouteDecision] = field(default_factory=dict)  # file name -> decision
    stopped_by_cost_cap: bool = False
    posted: dict[str, str | None] = field(default_factory=dict)  # file name -> ticket ID
    qm_unavailable: bool = False


def _reports_in(conn: sqlite3.Connection, *statuses: ReportStatus) -> list[sqlite3.Row]:
    marks = ", ".join("?" for _ in statuses)
    return conn.execute(
        f"SELECT * FROM reports WHERE status IN ({marks}) ORDER BY id", [str(s) for s in statuses]
    ).fetchall()


def _reload(conn: sqlite3.Connection, report: sqlite3.Row) -> sqlite3.Row:
    return conn.execute("SELECT * FROM reports WHERE id = ?", (report["id"],)).fetchone()


def latest_extraction(conn: sqlite3.Connection, report_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM extractions WHERE report_id = ? ORDER BY id DESC LIMIT 1", (report_id,)
    ).fetchone()


def validate_report(
    conn: sqlite3.Connection, settings: Settings, run_id: str, report: sqlite3.Row
) -> list[CheckResult]:
    """extracted -> validated. Stores one row per check."""
    extraction = latest_extraction(conn, report["id"])
    if extraction is None:
        raise RuntimeError(f"report {report['id']} is 'extracted' but has no extraction")
    fields = ExtractedFields.model_validate_json(extraction["fields_json"])
    text = prepare_report_text(report["source_type"], report["raw_text"]).text
    checks = validate(
        fields,
        text,
        MasterData.load(conn),
        datetime.now(UTC).date(),
        tuple(settings.required_fields),
    )
    conn.execute("DELETE FROM validations WHERE extraction_id = ?", (extraction["id"],))
    conn.executemany(
        "INSERT INTO validations (extraction_id, check_name, passed, message) VALUES (?, ?, ?, ?)",
        [(extraction["id"], c.name, int(c.passed), c.message) for c in checks],
    )
    failed = [c.name for c in checks if not c.passed]
    audit(conn, run_id, "validated", report["id"], {"failed_checks": failed})
    set_status(conn, run_id, report["id"], ReportStatus.VALIDATED)
    conn.commit()
    return checks


def route_report(
    conn: sqlite3.Connection, settings: Settings, run_id: str, report: sqlite3.Row
) -> RouteDecision:
    """validated -> in_review or approved."""
    extraction = latest_extraction(conn, report["id"])
    if extraction is None:
        raise RuntimeError(f"report {report['id']} is 'validated' but has no extraction")
    fields = ExtractedFields.model_validate_json(extraction["fields_json"])
    confidence = json.loads(extraction["confidence_json"])["fields"]
    checks = [
        CheckResult(row["check_name"], bool(row["passed"]), row["message"])
        for row in conn.execute(
            "SELECT * FROM validations WHERE extraction_id = ? ORDER BY id", (extraction["id"],)
        )
    ]
    decision = decide_route(
        fields,
        confidence,
        checks,
        settings.confidence_threshold,
        tuple(settings.required_fields),
    )
    audit(
        conn,
        run_id,
        "routed",
        report["id"],
        {"route": decision.route, "reasons": decision.reasons},
    )
    status = (
        ReportStatus.APPROVED if decision.route is Route.AUTO_APPROVE else ReportStatus.IN_REVIEW
    )
    set_status(conn, run_id, report["id"], status)
    conn.commit()
    get_logger().info(
        "routed",
        extra={
            "report_id": report["id"],
            "route": decision.route,
            "reason_count": len(decision.reasons),
        },
    )
    return decision


def run_pipeline(
    conn: sqlite3.Connection,
    llm: LLMClient,
    settings: Settings,
    run_id: str,
    http: httpx.Client | None = None,
) -> RunSummary:
    """Process everything that is waiting. Without an HTTP client the posting step is skipped."""
    summary = RunSummary()
    ingested = ingest_folder(conn, settings.input_dir, run_id)
    summary.new_reports, summary.duplicates = len(ingested.new), len(ingested.duplicates)

    # One report at a time, from wherever it stopped to its routing decision.
    waiting = _reports_in(
        conn,
        ReportStatus.RECEIVED,
        ReportStatus.PARSED,
        ReportStatus.EXTRACTED,
        ReportStatus.VALIDATED,
    )
    for report in waiting:
        if report["status"] in (ReportStatus.RECEIVED, ReportStatus.PARSED):
            if summary.stopped_by_cost_cap:
                continue  # no more model calls today; later steps of other reports still run
            try:
                extract_report(conn, llm, settings, run_id, report)
            except CostCapExceeded as exc:
                audit(conn, run_id, "cost_cap_reached", None, {"message": str(exc)})
                conn.commit()
                get_logger().error("cost_cap_reached")
                summary.stopped_by_cost_cap = True
                continue
            report = _reload(conn, report)
        if report["status"] == ReportStatus.EXTRACTED:
            validate_report(conn, settings, run_id, report)
            report = _reload(conn, report)
        if report["status"] == ReportStatus.VALIDATED:
            summary.decisions[report["file_name"]] = route_report(conn, settings, run_id, report)

    if http is not None:
        if qm_available(http):
            summary.posted = post_approved(conn, http, settings, run_id)
        else:  # approved reports simply wait for the next run
            summary.qm_unavailable = True
            get_logger().error("qm_api_unavailable")

    return summary


def requeue_failed(conn: sqlite3.Connection, run_id: str) -> int:
    """Put reports from the error queue back in line.

    A report that failed while posting was already approved, so it returns to 'approved'.
    Anything else restarts at 'received'. Cached model answers make that cheap.
    """
    failed = _reports_in(conn, ReportStatus.FAILED)
    for report in failed:
        post_failures = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE report_id = ? AND event = 'post_failed'",
            (report["id"],),
        ).fetchone()[0]
        target = ReportStatus.APPROVED if post_failures else ReportStatus.RECEIVED
        audit(conn, run_id, "requeued", report["id"], {"to": target})
        set_status(conn, run_id, report["id"], target)
    conn.commit()
    return len(failed)
