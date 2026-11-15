"""Read report files from the input folder into the reports table.

The SHA-256 of the file bytes is the report's identity. A file seen before is skipped,
which is the first layer of duplicate protection (FR-09).
"""

import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from defect_automation.db import audit, utc_now
from defect_automation.domain import ReportStatus

SUPPORTED_SUFFIXES = {".txt": "txt", ".eml": "eml"}


@dataclass
class IngestResult:
    new: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)


def file_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def ingest_folder(conn: sqlite3.Connection, folder: Path, run_id: str) -> IngestResult:
    result = IngestResult()
    files = sorted(p for p in folder.iterdir() if p.suffix.lower() in SUPPORTED_SUFFIXES)
    for path in files:
        content = path.read_bytes()
        digest = file_hash(content)
        now = utc_now()
        cursor = conn.execute(
            "INSERT INTO reports (source_type, file_name, file_hash, raw_text, status, "
            "received_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (file_hash) DO NOTHING",
            (
                SUPPORTED_SUFFIXES[path.suffix.lower()],
                path.name,
                digest,
                content.decode("utf-8", errors="replace"),
                ReportStatus.RECEIVED,
                now,
                now,
            ),
        )
        if cursor.rowcount == 1:
            audit(
                conn,
                run_id,
                "report_received",
                cursor.lastrowid,
                {"file_name": path.name, "file_hash": digest},
            )
            result.new.append(path.name)
        else:
            audit(
                conn,
                run_id,
                "duplicate_skipped",
                None,
                {"file_name": path.name, "file_hash": digest},
            )
            result.duplicates.append(path.name)
        conn.commit()
    return result
