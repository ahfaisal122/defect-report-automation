"""Shared fixtures: an in-memory database with master data, and a scripted fake model."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel

from defect_automation.config import Settings
from defect_automation.db import connect, init_db, utc_now
from defect_automation.llm import LLMResponse
from defect_automation.seed import seed_master_data

ROOT = Path(__file__).resolve().parent.parent

SAMPLE_TXT = """From: Anna Keller <anna.keller@factory.example>
To: quality@factory.example
Sent: Tue, 04 Aug 2026 09:15
Subject: Scratched gaskets on SG-2026-0003

Hello Quality,

At incoming inspection on 03 Aug 2026 we found scratches on 12 gaskets.

Regards,
Anna Keller
"""

# Relative to today, so the date-plausibility check never goes stale.
RECENT_DATE = (datetime.now(UTC).date() - timedelta(days=5)).isoformat()

VALID_ANSWER = {
    "order_number": "SG-2026-0003",
    "part_number": "BL-M10-0060",
    "supplier": "Wexmoor Fasteners Ltd",
    "defect_type": "surface_damage",
    "defect_description": "Scratches on the gaskets.",
    "quantity_affected": 12,
    "severity": "major",
    "detected_at": "incoming_inspection",
    "detected_on": RECENT_DATE,
    "reporter": "[PERSON_1]",
    "confidence": {
        "order_number": 0.99,
        "part_number": 0.99,
        "supplier": 0.98,
        "defect_type": 0.95,
        "defect_description": 0.95,
        "quantity_affected": 0.98,
        "severity": 0.97,
        "detected_at": 0.98,
        "detected_on": 0.98,
        "reporter": 0.97,
    },
}


class FakeLLM:
    """Returns scripted answers in order and records every prompt it was given."""

    model = "fake-model"

    def __init__(self, answers: list[str | Exception]) -> None:
        self._answers = list(answers)
        self.prompts: list[str] = []

    def generate_json(
        self, prompt: str, schema: type[BaseModel], temperature: float = 0.0
    ) -> LLMResponse:
        self.prompts.append(prompt)
        answer = self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return LLMResponse(text=answer, model=self.model, input_tokens=1000, output_tokens=200)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        prompts_dir=ROOT / "prompts",
        master_dir=ROOT / "data" / "master",
        db_path=tmp_path / "test.db",
        llm_cache_dir=tmp_path / "cache",
        log_path=tmp_path / "log.jsonl",
    )


@pytest.fixture
def conn(settings: Settings) -> sqlite3.Connection:
    connection = connect(":memory:")
    init_db(connection)
    seed_master_data(connection, settings.master_dir)
    return connection


@pytest.fixture
def valid_answer() -> str:
    return json.dumps(VALID_ANSWER)


def add_report(
    conn: sqlite3.Connection, raw_text: str = SAMPLE_TXT, file_hash: str = "hash-1"
) -> sqlite3.Row:
    now = utc_now()
    conn.execute(
        "INSERT INTO reports (source_type, file_name, file_hash, raw_text, status, "
        "received_at, updated_at) VALUES ('txt', 'sample.txt', ?, ?, 'received', ?, ?)",
        (file_hash, raw_text, now, now),
    )
    conn.commit()
    return conn.execute("SELECT * FROM reports WHERE file_hash = ?", (file_hash,)).fetchone()
