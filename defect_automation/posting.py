"""Post approved tickets to the QM API (FR-08, FR-09).

Three things keep a report from ever becoming two tickets:

1. The Idempotency-Key is derived from the report's file hash. A retry, a rerun or a
   crash between "API answered" and "ticket stored" sends the same key, and the QM
   system answers with the ticket it already has.
2. tickets.report_id is unique, so the local database holds one ticket row per report.
3. Only reports in status 'approved' are posted, and posting moves them to 'posted'.

The client can only create. It has no code path that edits, closes or deletes a ticket.
"""

import random
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx
from pydantic import BaseModel

from defect_automation.config import Settings
from defect_automation.db import audit, set_status, utc_now
from defect_automation.domain import ReportStatus, Severity
from defect_automation.extraction import prepare_report_text
from defect_automation.logging_setup import get_logger
from defect_automation.masking import unmask
from defect_automation.parsing import parse_report
from defect_automation.schemas import ExtractedFields

NOTIFICATIONS_PATH = "/api/v1/notifications"
HEALTH_PATH = "/api/v1/health"
MAX_RETRY_AFTER_SECONDS = 30.0
AUTO_APPROVER = "auto"


class TicketPayload(BaseModel):
    order_number: str | None
    part_number: str | None
    supplier: str | None
    defect_type: str
    defect_description: str
    quantity_affected: int | None
    severity: str
    detected_at: str | None
    detected_on: str | None
    reporter: str | None  # the real name, restored from the raw report
    source_report: str  # file hash, links the ticket back to its source
    approved_by: str


class PostingError(RuntimeError):
    """The QM API did not accept the ticket: retries exhausted or a non-retryable answer."""


class PostingBlocked(RuntimeError):
    """A rule forbids posting this report. It goes back to a person."""


@dataclass(frozen=True)
class PostResult:
    ticket_id: str
    attempts: int
    replayed: bool  # the QM system already had this ticket


def idempotency_key(file_hash: str) -> str:
    return f"report-{file_hash}"


def make_http_client(settings: Settings) -> httpx.Client:
    return httpx.Client(
        base_url=settings.qm_api_url,
        timeout=settings.posting_timeout_seconds,
        headers={"Authorization": f"Bearer {settings.qm_api_token.get_secret_value()}"},
    )


def qm_available(http: httpx.Client) -> bool:
    try:
        return http.get(HEALTH_PATH).status_code == 200
    except httpx.TransportError:
        return False


def _human_approval(conn: sqlite3.Connection, report_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM reviews WHERE report_id = ? AND action = 'approve' ORDER BY id DESC LIMIT 1",
        (report_id,),
    ).fetchone()


def build_payload(conn: sqlite3.Connection, report: sqlite3.Row) -> TicketPayload:
    """The final ticket: the reviewer's edited fields if there are any, else the extraction."""
    approval = _human_approval(conn, report["id"])
    if approval is not None and approval["edited_fields_json"]:
        fields = ExtractedFields.model_validate_json(approval["edited_fields_json"])
    else:
        extraction = conn.execute(
            "SELECT fields_json FROM extractions WHERE report_id = ? ORDER BY id DESC LIMIT 1",
            (report["id"],),
        ).fetchone()
        if extraction is None:
            raise PostingBlocked("no extraction to post")
        fields = ExtractedFields.model_validate_json(extraction["fields_json"])

    if fields.severity == Severity.CRITICAL and approval is None:
        raise PostingBlocked("critical defect without a human approval")
    if fields.severity is None or fields.defect_type is None or not fields.defect_description:
        raise PostingBlocked("severity, defect type or description is missing")

    mapping = prepare_report_text(report["source_type"], report["raw_text"]).mapping
    return TicketPayload(
        **fields.model_dump(mode="json", exclude={"reporter", "defect_description"}),
        defect_description=unmask(fields.defect_description, mapping) or "",
        reporter=unmask(fields.reporter, mapping),
        source_report=report["file_hash"],
        approved_by=approval["reviewer"] if approval is not None else AUTO_APPROVER,
    )


@dataclass(frozen=True)
class PostedTicket:
    ticket_id: str
    posted_at: str
    file_name: str
    report_id: int
    payload: TicketPayload  # what was sent to the QM system, rebuilt from stored data
    original_text: str


def list_tickets(conn: sqlite3.Connection) -> list[PostedTicket]:
    """Every posted ticket, newest first, with the content that was sent. Read-only."""
    rows = conn.execute(
        "SELECT t.qm_ticket_id, t.posted_at, r.* FROM tickets t "
        "JOIN reports r ON r.id = t.report_id ORDER BY t.id DESC"
    ).fetchall()
    return [
        PostedTicket(
            ticket_id=row["qm_ticket_id"],
            posted_at=row["posted_at"],
            file_name=row["file_name"],
            report_id=row["id"],
            payload=build_payload(conn, row),
            original_text=parse_report(row["source_type"], row["raw_text"], clean=False).as_text(),
        )
        for row in rows
    ]


def _wait_seconds(response: httpx.Response | None, attempt: int, backoff: float) -> float:
    if response is not None and response.status_code == 429:
        try:
            return min(float(response.headers.get("Retry-After", "")), MAX_RETRY_AFTER_SECONDS)
        except ValueError:
            pass
    return backoff * 2 ** (attempt - 1) * (1 + random.random() * 0.25)  # backoff with jitter


def post_with_retries(
    http: httpx.Client,
    payload: TicketPayload,
    key: str,
    max_retries: int,
    backoff_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
    on_attempt: Callable[[int, str], None] | None = None,
) -> PostResult:
    """One call plus up to max_retries retries. Retries 429, 5xx and network errors only."""
    last = "no attempt made"
    for attempt in range(1, max_retries + 2):
        response: httpx.Response | None = None
        try:
            response = http.post(
                NOTIFICATIONS_PATH, json=payload.model_dump(), headers={"Idempotency-Key": key}
            )
            last = f"HTTP {response.status_code}"
        except httpx.TransportError as exc:
            last = type(exc).__name__
        if on_attempt:
            on_attempt(attempt, last)

        if response is not None:
            if response.status_code in (200, 201):
                return PostResult(response.json()["id"], attempt, response.status_code == 200)
            if response.status_code != 429 and response.status_code < 500:
                raise PostingError(f"QM API rejected the ticket: {last} {response.text[:200]}")
        if attempt <= max_retries:
            sleep(_wait_seconds(response, attempt, backoff_seconds))
    raise PostingError(f"QM API failed after {max_retries} retries, last answer: {last}")


def post_report(
    conn: sqlite3.Connection,
    http: httpx.Client,
    settings: Settings,
    run_id: str,
    report: sqlite3.Row,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """approved -> posted. Returns the QM ticket ID, or None if the report was not posted."""
    log = get_logger()
    report_id = int(report["id"])

    existing = conn.execute(
        "SELECT qm_ticket_id FROM tickets WHERE report_id = ?", (report_id,)
    ).fetchone()
    if existing is not None:  # posted before, status update was lost: nothing to send
        set_status(conn, run_id, report_id, ReportStatus.POSTED)
        conn.commit()
        return str(existing["qm_ticket_id"])

    try:
        payload = build_payload(conn, report)
    except PostingBlocked as exc:
        audit(conn, run_id, "post_blocked", report_id, {"reason": str(exc)})
        set_status(conn, run_id, report_id, ReportStatus.IN_REVIEW)
        conn.commit()
        log.error("post_blocked", extra={"report_id": report_id, "reason": str(exc)})
        return None

    def record_attempt(attempt: int, outcome: str) -> None:
        audit(conn, run_id, "post_attempt", report_id, {"attempt": attempt, "outcome": outcome})
        conn.commit()

    try:
        result = post_with_retries(
            http,
            payload,
            idempotency_key(report["file_hash"]),
            settings.posting_max_retries,
            settings.posting_backoff_seconds,
            sleep,
            record_attempt,
        )
    except PostingError as exc:
        audit(conn, run_id, "post_failed", report_id, {"error": str(exc)[:500]})
        set_status(conn, run_id, report_id, ReportStatus.FAILED)
        conn.commit()
        log.error("post_failed", extra={"report_id": report_id})
        return None

    conn.execute(
        "INSERT INTO tickets (report_id, qm_ticket_id, posted_via, posted_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (report_id) DO NOTHING",
        (report_id, result.ticket_id, "api", utc_now()),
    )
    audit(
        conn,
        run_id,
        "posted",
        report_id,
        {
            "qm_ticket_id": result.ticket_id,
            "attempts": result.attempts,
            "replayed": result.replayed,
            "approved_by": payload.approved_by,
        },
    )
    set_status(conn, run_id, report_id, ReportStatus.POSTED)
    conn.commit()
    log.info(
        "posted",
        extra={
            "report_id": report_id,
            "qm_ticket_id": result.ticket_id,
            "attempts": result.attempts,
        },
    )
    return result.ticket_id


def post_approved(
    conn: sqlite3.Connection,
    http: httpx.Client,
    settings: Settings,
    run_id: str,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, str | None]:
    """Post every 'approved' report. Returns file name -> ticket ID (None = not posted)."""
    reports = conn.execute(
        "SELECT * FROM reports WHERE status = ? ORDER BY id", (str(ReportStatus.APPROVED),)
    ).fetchall()
    return {r["file_name"]: post_report(conn, http, settings, run_id, r, sleep) for r in reports}
