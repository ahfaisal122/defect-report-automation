"""The extraction step: parse, clean, mask, one model call, strict schema check, store.

The model only fills the schema. What happens to the report afterwards is decided by
the validation and routing code, never here and never in the prompt.
"""

import json
import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from pydantic import ValidationError

from defect_automation.config import Settings
from defect_automation.cost import call_cost_eur, check_cost_cap
from defect_automation.db import audit, set_status, utc_now
from defect_automation.domain import ReportStatus
from defect_automation.llm import LLMClient
from defect_automation.logging_setup import get_logger
from defect_automation.masking import MaskResult, mask_text
from defect_automation.parsing import parse_report
from defect_automation.prompting import load_template, render_prompt
from defect_automation.schemas import ExtractionOutput

MAX_SCHEMA_ATTEMPTS = 2  # the first answer, then one retry that includes the error


class Outcome(StrEnum):
    EXTRACTED = "extracted"
    SCHEMA_FAILED = "schema_failed"  # two malformed answers: a person takes over
    LLM_ERROR = "llm_error"  # the provider kept failing: error queue


@dataclass
class ExtractionResult:
    report_id: int
    outcome: Outcome
    extraction_id: int | None = None
    output: ExtractionOutput | None = None
    cost_eur: float = 0.0
    cached: bool = False
    error: str = ""


def prepare_report_text(source_type: str, raw_text: str) -> MaskResult:
    """Parse, clean and mask. The result is the only form of the report the model sees."""
    return mask_text(parse_report(source_type, raw_text).as_text())


def load_stations(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT station_code, name FROM master_stations ORDER BY rowid")
    return {row["station_code"]: row["name"] for row in rows}


def _store_extraction(
    conn: sqlite3.Connection,
    report_id: int,
    output: ExtractionOutput,
    model: str,
    prompt_version: str,
    cost_eur: float,
) -> int:
    confidence = {
        "fields": output.confidence.model_dump(),
        "report": output.report_confidence(),
    }
    cursor = conn.execute(
        "INSERT INTO extractions (report_id, fields_json, confidence_json, model, "
        "prompt_version, cost_eur, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            report_id,
            output.fields().model_dump_json(),
            json.dumps(confidence),
            model,
            prompt_version,
            cost_eur,
            utc_now(),
        ),
    )
    return int(cursor.lastrowid or 0)


def extract_report(
    conn: sqlite3.Connection,
    llm: LLMClient,
    settings: Settings,
    run_id: str,
    report: sqlite3.Row,
) -> ExtractionResult:
    """Process one report in status 'received' or 'parsed'. Commits its own changes.

    Raises CostCapExceeded before calling the model if today's budget is used up.
    """
    log = get_logger()
    report_id = int(report["id"])
    result = ExtractionResult(report_id=report_id, outcome=Outcome.SCHEMA_FAILED)

    masked = prepare_report_text(report["source_type"], report["raw_text"])
    if report["status"] == ReportStatus.RECEIVED:
        set_status(conn, run_id, report_id, ReportStatus.PARSED)
        conn.commit()

    template = load_template(settings.prompts_dir, settings.prompt_version)
    base_prompt = render_prompt(template, load_stations(conn), masked.text)
    prompt = base_prompt

    for attempt in range(1, MAX_SCHEMA_ATTEMPTS + 1):
        check_cost_cap(conn, settings)
        try:
            response = llm.generate_json(prompt, ExtractionOutput)
        except Exception as exc:  # provider retries are already exhausted at this point
            result.outcome, result.error = Outcome.LLM_ERROR, f"{type(exc).__name__}: {exc}"
            audit(conn, run_id, "model_call_failed", report_id, {"error": result.error[:500]})
            set_status(conn, run_id, report_id, ReportStatus.FAILED)
            conn.commit()
            log.error("model_call_failed", extra={"report_id": report_id}, exc_info=exc)
            return result

        cost = (
            0.0
            if response.cached
            else call_cost_eur(settings, response.input_tokens, response.output_tokens)
        )
        result.cost_eur += cost
        result.cached = response.cached
        call_detail = {
            "purpose": "extraction",
            "model": response.model,
            "prompt_version": settings.prompt_version,
            "attempt": attempt,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "cost_eur": cost,
            "cached": response.cached,
        }
        try:
            output = ExtractionOutput.model_validate_json(response.text)
        except ValidationError as exc:
            result.error = str(exc)
            audit(conn, run_id, "model_call", report_id, {**call_detail, "valid": False})
            conn.commit()
            log.warning("schema_error", extra={"report_id": report_id, "attempt": attempt})
            prompt = (
                f"{base_prompt}\n\nYour previous answer was rejected by the schema check:\n"
                f"{exc}\n\nReturn corrected JSON only."
            )
            continue

        audit(conn, run_id, "model_call", report_id, {**call_detail, "valid": True})
        result.extraction_id = _store_extraction(
            conn, report_id, output, response.model, settings.prompt_version, result.cost_eur
        )
        set_status(conn, run_id, report_id, ReportStatus.EXTRACTED)
        conn.commit()
        result.outcome, result.output, result.error = Outcome.EXTRACTED, output, ""
        log.info(
            "extracted",
            extra={
                "report_id": report_id,
                "extraction_id": result.extraction_id,
                "report_confidence": output.report_confidence(),
                "cost_eur": result.cost_eur,
                "cached": response.cached,
            },
        )
        return result

    audit(conn, run_id, "extraction_schema_failed", report_id, {"error": result.error[:500]})
    set_status(conn, run_id, report_id, ReportStatus.IN_REVIEW)
    conn.commit()
    log.error("extraction_schema_failed", extra={"report_id": report_id})
    return result
