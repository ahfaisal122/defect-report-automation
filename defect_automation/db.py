"""SQLite connection, schema creation and the audit-log helper."""

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def connect(db_path: Path | str) -> sqlite3.Connection:
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
    conn.commit()


def set_status(conn: sqlite3.Connection, run_id: str, report_id: int, status: str) -> None:
    """Move a report to a new status and record the change. The caller commits."""
    conn.execute(
        "UPDATE reports SET status = ?, updated_at = ? WHERE id = ?",
        (str(status), utc_now(), report_id),
    )
    audit(conn, run_id, "status_changed", report_id, {"status": str(status)})


def audit(
    conn: sqlite3.Connection,
    run_id: str,
    event: str,
    report_id: int | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append one audit row. The caller commits, so the row lands with the change it describes."""
    conn.execute(
        "INSERT INTO audit_log (run_id, report_id, event, detail_json, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (run_id, report_id, event, json.dumps(detail or {}, default=str), utc_now()),
    )
