"""Command line entry point: python -m defect_automation <command>."""

import argparse
import json
import sqlite3

from defect_automation.config import get_settings
from defect_automation.cost import CostCapExceeded, spent_today_eur
from defect_automation.db import audit, connect, init_db, new_run_id
from defect_automation.domain import ReportStatus
from defect_automation.evaluation import (
    format_history,
    format_report,
    load_results,
    run_eval,
    save_result,
)
from defect_automation.extraction import Outcome, extract_report
from defect_automation.ingest import ingest_folder
from defect_automation.llm import get_llm_client
from defect_automation.logging_setup import configure_logging
from defect_automation.pipeline import requeue_failed, run_pipeline
from defect_automation.posting import make_http_client, post_approved, qm_available
from defect_automation.seed import seed_master_data


def _open_db() -> sqlite3.Connection:
    conn = connect(get_settings().db_path)
    init_db(conn)
    return conn


def cmd_seed(_: argparse.Namespace) -> None:
    settings = get_settings()
    conn = _open_db()
    counts = seed_master_data(conn, settings.master_dir)
    print(f"Database: {settings.db_path}")
    for table, count in counts.items():
        print(f"  {table:<18} {count:>4} rows")


def cmd_ingest(_: argparse.Namespace) -> None:
    settings = get_settings()
    conn = _open_db()
    run_id = new_run_id()
    result = ingest_folder(conn, settings.input_dir, run_id)
    total = conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    print(f"Run {run_id}: {len(result.new)} new, {len(result.duplicates)} duplicates skipped")
    print(f"  reports table now holds {total} rows")


def cmd_extract(args: argparse.Namespace) -> None:
    settings = get_settings()
    conn = _open_db()
    run_id = new_run_id()
    log = configure_logging(settings.log_path, run_id)
    llm = get_llm_client(settings, use_cache=True)

    query = "SELECT * FROM reports WHERE status IN (?, ?)"
    params: list[object] = [ReportStatus.RECEIVED, ReportStatus.PARSED]
    if args.files:
        query += f" AND file_name IN ({', '.join('?' for _ in args.files)})"
        params.extend(args.files)
    query += " ORDER BY id"
    if args.limit:
        query += " LIMIT ?"
        params.append(args.limit)
    reports = conn.execute(query, params).fetchall()

    print(f"Run {run_id}: extracting {len(reports)} reports with {llm.model}")
    counts = {outcome: 0 for outcome in Outcome}
    for report in reports:
        try:
            result = extract_report(conn, llm, settings, run_id, report)
        except CostCapExceeded as exc:
            audit(conn, run_id, "cost_cap_reached", None, {"message": str(exc)})
            conn.commit()
            log.error("cost_cap_reached", extra={"spent_eur": spent_today_eur(conn)})
            print(f"STOPPED: {exc}. Remaining reports stay queued.")
            break
        counts[result.outcome] += 1
        note = "cached" if result.cached else f"{result.cost_eur:.6f} EUR"
        if result.output is None:
            print(f"  {report['file_name']}  {result.outcome}  {result.error[:120]}")
            continue
        print(
            f"  {report['file_name']}  {result.outcome}  "
            f"confidence {result.output.report_confidence():.2f}  ({note})"
        )
        if args.show:
            print(json.dumps(json.loads(result.output.model_dump_json()), indent=2))
    summary = ", ".join(f"{count} {outcome}" for outcome, count in counts.items())
    print(f"Done: {summary}. Spent today: {spent_today_eur(conn):.6f} EUR")


def _print_posted(posted: dict[str, str | None]) -> None:
    for file_name, ticket_id in posted.items():
        print(f"  {file_name}  {'posted as ' + ticket_id if ticket_id else 'NOT posted'}")


def _print_status_counts(conn: sqlite3.Connection) -> None:
    print("Reports by status:")
    for row in conn.execute("SELECT status, COUNT(*) FROM reports GROUP BY status ORDER BY 1"):
        print(f"  {row[0]:<10} {row[1]:>3}")
    tickets = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    print(f"Tickets stored: {tickets}")


def cmd_post(args: argparse.Namespace) -> None:
    settings = get_settings()
    conn = _open_db()
    run_id = new_run_id()
    configure_logging(settings.log_path, run_id)
    if args.retry_failed:
        print(f"Requeued {requeue_failed(conn, run_id)} failed reports")
    http = make_http_client(settings)
    if not qm_available(http):
        print(f"QM API not reachable at {settings.qm_api_url}. Nothing posted.")
        return
    print(f"Run {run_id}: posting approved reports")
    _print_posted(post_approved(conn, http, settings, run_id))
    _print_status_counts(conn)


def cmd_qm_api(_: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run(
        "mock_qm.app:app_from_settings",
        factory=True,
        host="127.0.0.1",
        port=get_settings().mock_qm_port,
        log_level="warning",
    )


def cmd_run(args: argparse.Namespace) -> None:
    settings = get_settings()
    conn = _open_db()
    run_id = new_run_id()
    configure_logging(settings.log_path, run_id)
    if args.retry_failed:
        print(f"Requeued {requeue_failed(conn, run_id)} failed reports")
    http = make_http_client(settings)
    summary = run_pipeline(conn, get_llm_client(settings, use_cache=True), settings, run_id, http)

    print(f"Run {run_id}: {summary.new_reports} new, {summary.duplicates} duplicates skipped")
    for file_name, decision in summary.decisions.items():
        print(f"  {file_name}  {decision.route}")
        for reason in decision.reasons:
            print(f"      - {reason}")
    if summary.stopped_by_cost_cap:
        print("STOPPED: daily cost cap reached. Unprocessed reports stay queued.")
    _print_posted(summary.posted)
    if summary.qm_unavailable:
        print(f"QM API not reachable at {settings.qm_api_url}. Approved reports stay queued.")
        print("  Start it in a second terminal: python -m defect_automation qm-api")
    _print_status_counts(conn)
    print(f"Spent today: {spent_today_eur(conn):.6f} EUR")


def cmd_eval(args: argparse.Namespace) -> None:
    settings = get_settings()
    if args.history:
        print(format_history(load_results(settings.eval_results_dir)))
        return
    configure_logging(settings.log_path, new_run_id())
    result = run_eval(settings, get_llm_client(settings, use_cache=True))
    print(format_report(result))
    print(f"\nSaved to {save_result(result, settings.eval_results_dir)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="defect_automation", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed", help="load master data CSVs into SQLite").set_defaults(func=cmd_seed)
    sub.add_parser("ingest", help="read report files from the input folder").set_defaults(
        func=cmd_ingest
    )
    extract = sub.add_parser("extract", help="extract fields from queued reports")
    extract.add_argument("--limit", type=int, help="process at most this many reports")
    extract.add_argument("--files", nargs="+", help="only these file names")
    extract.add_argument("--show", action="store_true", help="print the extracted JSON")
    extract.set_defaults(func=cmd_extract)
    run = sub.add_parser("run", help="process the input folder: ingest, extract, validate, route")
    run.add_argument("--retry-failed", action="store_true", help="requeue the error queue first")
    run.set_defaults(func=cmd_run)
    post = sub.add_parser("post", help="post approved reports to the QM API")
    post.add_argument("--retry-failed", action="store_true", help="requeue the error queue first")
    post.set_defaults(func=cmd_post)
    evaluate = sub.add_parser("eval", help="score the pipeline on the labelled reports")
    evaluate.add_argument("--history", action="store_true", help="compare all saved runs")
    evaluate.set_defaults(func=cmd_eval)
    sub.add_parser("qm-api", help="start the mock QM API").set_defaults(func=cmd_qm_api)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)
