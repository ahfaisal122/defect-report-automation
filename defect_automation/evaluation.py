"""Score the pipeline on the labelled reports (FR-13).

The labelled files run through the real pipeline (ingest, extract, validate, route) in a
separate database, so an eval run never touches live data. Model answers come from the
cache when the model and prompt are unchanged, so repeating a run costs nothing.

The labels are for scoring only. They are never used to write or tune a prompt.
"""

import json
import re
import shutil
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from defect_automation.config import Settings
from defect_automation.cost import call_cost_eur
from defect_automation.db import connect, init_db, new_run_id
from defect_automation.extraction import prepare_report_text
from defect_automation.llm import LLMClient
from defect_automation.masking import unmask
from defect_automation.pipeline import run_pipeline
from defect_automation.seed import seed_master_data

# defect_description is free text: there is no single right wording, so it is not scored.
SCORED_FIELDS: tuple[str, ...] = (
    "order_number",
    "part_number",
    "supplier",
    "defect_type",
    "quantity_affected",
    "severity",
    "detected_at",
    "detected_on",
    "reporter",
)
# Case types the routing rules must send to a person, whatever the severity.
MUST_REVIEW_CASES = {"missing_order", "wrong_part", "two_defects"}


@dataclass
class FieldScore:
    correct: int = 0
    total: int = 0

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


@dataclass
class EvalResult:
    run_at: str
    model: str
    prompt_version: str
    confidence_threshold: float
    reports: int = 0
    field_accuracy: float = 0.0
    per_field: dict[str, dict[str, float]] = field(default_factory=dict)
    critical_total: int = 0
    critical_sent_to_review: int = 0
    critical_rated_critical_by_model: int = 0
    critical_recall: float | None = None
    must_review_total: int = 0
    must_review_caught: int = 0
    sent_to_review: int = 0
    review_rate: float = 0.0
    extra_reviews: int = 0  # reports with nothing wrong by design that still went to a person
    auto_approved: int = 0
    auto_approved_with_errors: int = 0
    cost_per_report_eur: float = 0.0
    mismatches: list[dict[str, object]] = field(default_factory=list)


def _text(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def field_matches(name: str, expected: object, got: object) -> bool:
    """Exact match. Supplier and reporter ignore case, spacing and punctuation."""
    if expected is None or got is None:
        return expected is None and got is None
    if name in ("supplier", "reporter"):
        return _text(expected) == _text(got)
    return str(expected) == str(got)


def _extracted_fields(conn: sqlite3.Connection, report: sqlite3.Row) -> dict[str, object]:
    """The extraction with the reporter token turned back into a name. Empty if none."""
    row = conn.execute(
        "SELECT fields_json FROM extractions WHERE report_id = ? ORDER BY id DESC LIMIT 1",
        (report["id"],),
    ).fetchone()
    if row is None:
        return {}
    fields = json.loads(row["fields_json"])
    mapping = prepare_report_text(report["source_type"], report["raw_text"]).mapping
    fields["reporter"] = unmask(fields.get("reporter"), mapping)
    return fields


def _route(conn: sqlite3.Connection, report_id: int) -> str:
    row = conn.execute(
        "SELECT detail_json FROM audit_log WHERE report_id = ? AND event = 'routed' "
        "ORDER BY id DESC LIMIT 1",
        (report_id,),
    ).fetchone()
    return json.loads(row["detail_json"])["route"] if row else "review"  # no route = a person


def score(
    conn: sqlite3.Connection, labels: dict[str, dict[str, object]], settings: Settings
) -> EvalResult:
    result = EvalResult(
        run_at=datetime.now(UTC).isoformat(timespec="seconds"),
        model=settings.gemini_model,
        prompt_version=settings.prompt_version,
        confidence_threshold=settings.confidence_threshold,
    )
    scores = {name: FieldScore() for name in SCORED_FIELDS}
    tokens_in = tokens_out = 0

    for file_name, label in sorted(labels.items()):
        report = conn.execute("SELECT * FROM reports WHERE file_name = ?", (file_name,)).fetchone()
        if report is None:
            raise RuntimeError(f"labelled report {file_name} was not ingested")
        got = _extracted_fields(conn, report)
        to_review = _route(conn, report["id"]) == "review"
        errors = 0
        for name in SCORED_FIELDS:
            scores[name].total += 1
            if field_matches(name, label[name], got.get(name)):
                scores[name].correct += 1
            else:
                errors += 1
                result.mismatches.append(
                    {
                        "file": file_name,
                        "case_type": label["case_type"],
                        "field": name,
                        "expected": label[name],
                        "got": got.get(name),
                        "sent_to_review": to_review,
                    }
                )

        result.reports += 1
        result.sent_to_review += int(to_review)
        is_critical = label["severity"] == "critical"
        must_review = is_critical or label["case_type"] in MUST_REVIEW_CASES
        if is_critical:
            result.critical_total += 1
            result.critical_sent_to_review += int(to_review)
            result.critical_rated_critical_by_model += int(got.get("severity") == "critical")
        if must_review:
            result.must_review_total += 1
            result.must_review_caught += int(to_review)
        elif to_review:
            result.extra_reviews += 1
        if not to_review:
            result.auto_approved += 1
            result.auto_approved_with_errors += int(errors > 0)

        for call in conn.execute(
            "SELECT detail_json FROM audit_log WHERE report_id = ? AND event = 'model_call'",
            (report["id"],),
        ):
            detail = json.loads(call["detail_json"])
            tokens_in += detail["input_tokens"]
            tokens_out += detail["output_tokens"]

    correct = sum(s.correct for s in scores.values())
    total = sum(s.total for s in scores.values())
    result.field_accuracy = correct / total if total else 0.0
    result.per_field = {
        name: {"correct": s.correct, "total": s.total, "accuracy": s.accuracy}
        for name, s in scores.items()
    }
    if result.critical_total:
        result.critical_recall = result.critical_sent_to_review / result.critical_total
    if result.reports:
        result.review_rate = result.sent_to_review / result.reports
        # Priced from token counts, so a cached run reports the same cost as a live one.
        result.cost_per_report_eur = call_cost_eur(settings, tokens_in, tokens_out) / result.reports
    return result


def run_eval(settings: Settings, llm: LLMClient) -> EvalResult:
    labels: dict[str, dict[str, object]] = json.loads(
        settings.labels_path.read_text(encoding="utf-8")
    )
    work = settings.eval_work_dir
    shutil.rmtree(work, ignore_errors=True)
    inbox = work / "inbox"
    inbox.mkdir(parents=True)
    for file_name in labels:
        shutil.copy(settings.input_dir / file_name, inbox / file_name)

    eval_settings = settings.model_copy(update={"input_dir": inbox, "db_path": work / "eval.db"})
    conn = connect(eval_settings.db_path)
    init_db(conn)
    seed_master_data(conn, settings.master_dir)
    run_pipeline(conn, llm, eval_settings, new_run_id())  # no HTTP client: nothing is posted
    return score(conn, labels, eval_settings)


def save_result(result: EvalResult, results_dir: Path) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    stamp = result.run_at.replace(":", "").replace("-", "")[:15]
    path = results_dir / f"{stamp}_{result.prompt_version}_{result.model}.json"
    payload = json.dumps(asdict(result), indent=2, default=str) + "\n"
    path.write_text(payload, encoding="utf-8")
    (results_dir / "latest.json").write_text(payload, encoding="utf-8")
    return path


def load_results(results_dir: Path) -> list[dict[str, object]]:
    """Every saved run, oldest first."""
    files = sorted(p for p in results_dir.glob("*.json") if p.name != "latest.json")
    return [json.loads(p.read_text(encoding="utf-8")) for p in files]


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:6.1%}"


def format_report(result: EvalResult) -> str:
    lines = [
        f"Evaluation  {result.run_at}",
        f"model {result.model}   prompt {result.prompt_version}   "
        f"threshold {result.confidence_threshold:.2f}   reports {result.reports}",
        "",
        f"{'Field':<20}{'Correct':>9}{'Accuracy':>10}",
    ]
    for name, s in result.per_field.items():
        lines.append(
            f"{name:<20}{int(s['correct']):>5}/{int(s['total']):<3}{_percent(s['accuracy']):>10}"
        )
    lines += [
        f"{'ALL SCORED FIELDS':<20}{'':>9}{_percent(result.field_accuracy):>10}",
        "(defect_description is free text and is not scored)",
        "",
        f"Critical recall         {_percent(result.critical_recall)}   "
        f"{result.critical_sent_to_review}/{result.critical_total} critical reports reached a "
        f"person ({result.critical_rated_critical_by_model} rated critical by the model)",
        f"Must-review cases       {result.must_review_caught}/{result.must_review_total} caught "
        "(critical, missing order, unknown part, two defects)",
        f"Review rate             {_percent(result.review_rate)}   "
        f"{result.sent_to_review}/{result.reports}, of which {result.extra_reviews} had "
        "nothing wrong by design",
        f"Auto-approved           {result.auto_approved} reports, "
        f"{result.auto_approved_with_errors} with a wrong field",
        f"Cost per report         {result.cost_per_report_eur:.6f} EUR",
    ]
    if result.mismatches:
        lines += ["", "Mismatches:"]
        for m in result.mismatches:
            where = "review" if m["sent_to_review"] else "AUTO-APPROVED"
            lines.append(
                f"  {m['file']} ({m['case_type']}, {where})  {m['field']}: "
                f"expected {m['expected']!r}, got {m['got']!r}"
            )
    return "\n".join(lines)


def format_history(runs: list[dict[str, object]]) -> str:
    lines = [f"{'Run':<27}{'Prompt':<16}{'Model':<24}{'Fields':>8}{'Crit.':>8}{'Review':>8}"]
    for run in runs:
        lines.append(
            f"{str(run['run_at']):<27}{str(run['prompt_version']):<16}{str(run['model']):<24}"
            f"{_percent(run['field_accuracy']):>8}{_percent(run['critical_recall']):>8}"
            f"{_percent(run['review_rate']):>8}"
        )
    return "\n".join(lines)
