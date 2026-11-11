"""Cost per model call and the daily cost cap.

Every model call is written to the audit log with its cost. The cap sums today's calls
from there, so it holds across runs and restarts.
"""

import sqlite3
from datetime import UTC, datetime

from defect_automation.config import Settings


class CostCapExceeded(RuntimeError):
    """Raised before a model call when today's spend has reached the configured cap."""


def call_cost_eur(settings: Settings, input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens * settings.price_input_eur_per_mtok
        + output_tokens * settings.price_output_eur_per_mtok
    ) / 1_000_000


def spent_today_eur(conn: sqlite3.Connection) -> float:
    today = datetime.now(UTC).date().isoformat()
    row = conn.execute(
        "SELECT COALESCE(SUM(json_extract(detail_json, '$.cost_eur')), 0) FROM audit_log "
        "WHERE event = 'model_call' AND substr(created_at, 1, 10) = ?",
        (today,),
    ).fetchone()
    return float(row[0])


def check_cost_cap(conn: sqlite3.Connection, settings: Settings) -> None:
    spent = spent_today_eur(conn)
    if spent >= settings.daily_cost_cap_eur:
        raise CostCapExceeded(
            f"Daily cost cap reached: {spent:.4f} EUR spent, "
            f"cap is {settings.daily_cost_cap_eur:.2f} EUR"
        )
