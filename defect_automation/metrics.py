"""Numbers for the metrics page (FR-12). Read-only.

Everything here is counted per report or per defect. Nothing is counted per person:
no reviewer speed, no reviewer volume. See the works council note in the README.
"""

import json
import sqlite3
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Metrics:
    total_reports: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    received_per_day: dict[str, int] = field(default_factory=dict)
    tickets_posted: int = 0
    decided_reports: int = 0  # reports the pipeline has routed
    sent_to_review: int = 0
    review_rate: float | None = None
    review_reasons: dict[str, int] = field(default_factory=dict)
    human_decisions: dict[str, int] = field(default_factory=dict)
    defect_types: dict[str, int] = field(default_factory=dict)
    severities: dict[str, int] = field(default_factory=dict)
    median_processing_seconds: float | None = None
    max_processing_seconds: float | None = None
    cost_total_eur: float = 0.0
    cost_per_report_eur: float | None = None


def reason_category(reason: str) -> str:
    """Group the routing reasons into a few kinds a manager can read at a glance."""
    if reason.startswith("severity is critical"):
        return "critical severity"
    if reason.endswith("is missing") or reason.startswith("severity is unknown"):
        return "missing field"
    if " confidence " in reason:
        return "low confidence"
    names = {
        "order_exists": "order not in master data",
        "part_exists": "part not in master data",
        "supplier_exists": "supplier not in master data",
        "supplier_matches_part": "supplier does not match part",
        "station_known": "unknown station",
        "date_plausible": "implausible date",
        "single_defect": "more than one defect",
        "critical_language": "critical wording, lower rating",
    }
    return names.get(reason.split(":", 1)[0], "other")


def _counts(conn: sqlite3.Connection, query: str) -> dict[str, int]:
    return {str(row[0]): int(row[1]) for row in conn.execute(query)}


def _final_fields(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Per report: the reviewer's approved fields if there are any, else the extraction."""
    rows = conn.execute(
        """
        SELECT COALESCE(
            (SELECT edited_fields_json FROM reviews
              WHERE report_id = r.id AND action = 'approve' AND edited_fields_json IS NOT NULL
              ORDER BY id DESC LIMIT 1),
            (SELECT fields_json FROM extractions WHERE report_id = r.id ORDER BY id DESC LIMIT 1)
        ) FROM reports r
        """
    )
    return [json.loads(row[0]) for row in rows if row[0]]


def compute_metrics(conn: sqlite3.Connection) -> Metrics:
    m = Metrics()
    m.by_status = _counts(conn, "SELECT status, COUNT(*) FROM reports GROUP BY status")
    m.total_reports = sum(m.by_status.values())
    m.received_per_day = _counts(
        conn, "SELECT substr(received_at, 1, 10), COUNT(*) FROM reports GROUP BY 1 ORDER BY 1"
    )
    m.tickets_posted = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    m.human_decisions = _counts(conn, "SELECT action, COUNT(*) FROM reviews GROUP BY action")

    # The first routing decision per report decides whether it counted as "sent to review".
    first_routes = conn.execute(
        """
        SELECT a.report_id, a.event, a.detail_json, a.created_at,
               (SELECT MIN(created_at) FROM audit_log
                 WHERE report_id = a.report_id AND event = 'status_changed') AS started_at
        FROM audit_log a
        WHERE a.id IN (
            SELECT MIN(id) FROM audit_log
            WHERE event IN ('routed', 'extraction_schema_failed') GROUP BY report_id)
        """
    ).fetchall()
    reasons: Counter[str] = Counter()
    seconds: list[float] = []
    for row in first_routes:
        detail = json.loads(row["detail_json"])
        to_review = row["event"] != "routed" or detail.get("route") == "review"
        m.sent_to_review += int(to_review)
        if row["event"] != "routed":
            reasons["model answer failed schema"] += 1
        # One report can have several reasons; count each kind once per report.
        reasons.update({reason_category(r) for r in detail.get("reasons", [])})
        if row["started_at"]:  # processing start to routing decision; queue time excluded
            started = datetime.fromisoformat(row["started_at"])
            seconds.append((datetime.fromisoformat(row["created_at"]) - started).total_seconds())
    m.decided_reports = len(first_routes)
    if first_routes:
        m.review_rate = m.sent_to_review / m.decided_reports
    if seconds:
        m.median_processing_seconds = statistics.median(seconds)
        m.max_processing_seconds = max(seconds)
    m.review_reasons = dict(reasons.most_common())

    finals = _final_fields(conn)
    m.defect_types = dict(
        Counter(str(f.get("defect_type") or "unknown") for f in finals).most_common()
    )
    m.severities = dict(Counter(str(f.get("severity") or "unknown") for f in finals).most_common())

    m.cost_total_eur = float(
        conn.execute(
            "SELECT COALESCE(SUM(json_extract(detail_json, '$.cost_eur')), 0) FROM audit_log "
            "WHERE event = 'model_call'"
        ).fetchone()[0]
    )
    extracted = conn.execute("SELECT COUNT(DISTINCT report_id) FROM extractions").fetchone()[0]
    if extracted:
        m.cost_per_report_eur = m.cost_total_eur / extracted
    return m
