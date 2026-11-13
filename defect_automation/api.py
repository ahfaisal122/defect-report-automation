"""A small HTTP door to the pipeline, so an orchestrator such as UiPath can start a run.

The caller only triggers a run and reads the counts. Every decision (validation, routing,
posting) still happens in the pipeline code. The answer holds counts only: no report text
and no personal data.
"""

import hmac
import sqlite3
import threading
from collections.abc import Callable

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from defect_automation.config import Settings, get_settings
from defect_automation.db import connect, init_db, new_run_id
from defect_automation.domain import ReportStatus
from defect_automation.llm import LLMClient, get_llm_client
from defect_automation.logging_setup import configure_logging
from defect_automation.pipeline import run_pipeline
from defect_automation.posting import make_http_client
from defect_automation.routing import Route


class RunResult(BaseModel):
    run_id: str
    new_reports: int
    duplicates_skipped: int
    auto_approved: int
    sent_to_review: int
    posted: int
    failed: int
    waiting_for_review: int  # the whole review queue, not only this run
    needs_attention: bool  # true when a person has something to do
    stopped_by_cost_cap: bool
    qm_unavailable: bool
    review_url: str


class StatusResult(BaseModel):
    reports_by_status: dict[str, int]
    tickets: int
    waiting_for_review: int
    review_url: str


def _count(conn: sqlite3.Connection, status: ReportStatus) -> int:
    return conn.execute("SELECT COUNT(*) FROM reports WHERE status = ?", (status,)).fetchone()[0]


def _failed_in_run(conn: sqlite3.Connection, run_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(DISTINCT report_id) FROM audit_log WHERE run_id = ? "
        "AND event = 'status_changed' AND json_extract(detail_json, '$.status') = ?",
        (run_id, ReportStatus.FAILED),
    ).fetchone()[0]


def create_app(
    settings: Settings,
    llm_factory: Callable[[], LLMClient] | None = None,
    http_factory: Callable[[], httpx.Client | None] | None = None,
) -> FastAPI:
    app = FastAPI(title="Defect Report Automation: pipeline API", docs_url=None, redoc_url=None)
    busy = threading.Lock()  # one run at a time; a second caller gets 409

    def open_db() -> sqlite3.Connection:
        conn = connect(settings.db_path)
        init_db(conn)
        return conn

    def require_token(authorization: str = Header(default="")) -> None:
        if settings.pipeline_api_token is None:
            raise HTTPException(503, "PIPELINE_API_TOKEN is not set. The API stays closed.")
        expected = f"Bearer {settings.pipeline_api_token.get_secret_value()}"
        if not hmac.compare_digest(authorization.encode(), expected.encode()):
            raise HTTPException(401, "Missing or wrong bearer token.")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/runs", dependencies=[Depends(require_token)])
    def start_run() -> RunResult:
        if not busy.acquire(blocking=False):
            raise HTTPException(409, "A run is already in progress.")
        try:
            run_id = new_run_id()
            configure_logging(settings.log_path, run_id)
            conn = open_db()
            try:
                llm = llm_factory() if llm_factory else get_llm_client(settings, use_cache=True)
                http = http_factory() if http_factory else make_http_client(settings)
                summary = run_pipeline(conn, llm, settings, run_id, http)
                routes = [decision.route for decision in summary.decisions.values()]
                failed = _failed_in_run(conn, run_id)
                waiting = _count(conn, ReportStatus.IN_REVIEW)
            finally:
                conn.close()
        finally:
            busy.release()
        return RunResult(
            run_id=run_id,
            new_reports=summary.new_reports,
            duplicates_skipped=summary.duplicates,
            auto_approved=routes.count(Route.AUTO_APPROVE),
            sent_to_review=len(routes) - routes.count(Route.AUTO_APPROVE),
            posted=sum(1 for ticket_id in summary.posted.values() if ticket_id),
            failed=failed,
            waiting_for_review=waiting,
            needs_attention=bool(
                waiting or failed or summary.stopped_by_cost_cap or summary.qm_unavailable
            ),
            stopped_by_cost_cap=summary.stopped_by_cost_cap,
            qm_unavailable=summary.qm_unavailable,
            review_url=settings.review_url,
        )

    @app.get("/status", dependencies=[Depends(require_token)])
    def status() -> StatusResult:
        conn = open_db()
        try:
            by_status = {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT status, COUNT(*) FROM reports GROUP BY status ORDER BY 1"
                )
            }
            tickets = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        finally:
            conn.close()
        return StatusResult(
            reports_by_status=by_status,
            tickets=tickets,
            waiting_for_review=by_status.get(ReportStatus.IN_REVIEW, 0),
            review_url=settings.review_url,
        )

    return app


def app_from_settings() -> FastAPI:
    return create_app(get_settings())
