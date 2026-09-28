from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .config import load_institution_config
from .logging_utils import setup_logging
from .match.matcher import match_applicant
from .match.paper_backtrace import run_paper_backtrace_match
from .normalize.institution import institution_from_config
from .pipeline.audit import audit
from .pipeline.audit_pi_quality import run_pi_quality_audit
from .pipeline.audit_region import build_regional_audit, write_regional_audit
from .pipeline.build_research_vectors import process_vector_queue
from .pipeline.carry_forward import carry_forward_records
from .pipeline.export import export_outputs
from .pipeline.ingest_institution import import_institutions_csv, ingest_institution
from .pipeline.ingest_qs_batch import ingest_batch
from .pipeline.merge_shards import merge_database_shards
from .pipeline.record_corrections import apply_record_corrections
from .pipeline.repair_failed_cache import repair_failed_cache
from .pipeline.refresh_official_publications import refresh_official_publications
from .pipeline.reviewed_identity_merge import apply_reviewed_identity_merges
from .pipeline.snapshot import create_institution_snapshot
from .pipeline.sync_openalex_publications import sync_openalex_publications
from .sources.openalex_client import OpenAlexConfigurationError
from .storage import PIIndexStorage


DEFAULT_DB = "outputs/sample_run/pi_index.db"


def _load_person_id_file(path_value: str | Path) -> list[str]:
    path = Path(path_value)
    if not path.is_file():
        raise FileNotFoundError(f"Person ID file does not exist: {path.resolve()}")
    text = path.read_text(encoding="utf-8-sig")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        values = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    else:
        if isinstance(parsed, dict):
            parsed = parsed.get("person_ids")
        if not isinstance(parsed, list):
            raise ValueError(
                "Person ID JSON must be an array or an object with person_ids"
            )
        values = [str(value).strip() for value in parsed]
    if not values or any(not value for value in values):
        raise ValueError("Person ID file must contain at least one nonblank ID")
    return list(dict.fromkeys(values))


def _combined_person_ids(
    direct_ids: list[str] | None,
    file_path: str | Path | None,
) -> list[str] | None:
    values = list(direct_ids or [])
    if file_path is not None:
        values.extend(_load_person_id_file(file_path))
    return list(dict.fromkeys(values)) if values else None


def _print_json(value) -> None:
    """Print JSON without making a successful command fail on a legacy console."""
    try:
        print(json.dumps(value, indent=2, ensure_ascii=False))
    except UnicodeEncodeError:
        print(json.dumps(value, indent=2, ensure_ascii=True))


def _write_json_output(path_value: str | Path | None, value) -> None:
    """Persist a command result when an explicit audit path was requested."""
    if path_value is None:
        return
    path = Path(path_value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _storage(args) -> PIIndexStorage:
    return PIIndexStorage(args.db)


def cmd_import_institutions(args) -> int:
    storage = _storage(args)
    count = import_institutions_csv(args.input, storage)
    _print_json({"imported": count, "db": str(args.db)})
    storage.close()
    return 0


def cmd_ingest_institution(args) -> int:
    storage = _storage(args)
    result = ingest_institution(
        args.config,
        storage,
        snapshot_root=args.snapshot_root,
        archive_root=args.archive_root,
        offline=args.offline,
    )
    _print_json(result)
    storage.close()
    return 0


def cmd_ingest_batch(args) -> int:
    storage = _storage(args)
    result = ingest_batch(
        args.institution_list,
        storage,
        args.limit,
        args.snapshot_root,
        args.archive_root,
        args.offline,
    )
    _print_json(result)
    storage.close()
    return 0


def cmd_snapshot_institution(args) -> int:
    storage = _storage(args)
    config = load_institution_config(args.config)
    institution_id = institution_from_config(config, use_ror=False).institution_id
    snapshot = create_institution_snapshot(
        storage,
        institution_id,
        args.snapshot_root,
        config=config,
        config_path=args.config,
        archive_root=args.archive_root,
    )
    _print_json(snapshot.manifest)
    storage.close()
    return 0


def cmd_merge_shards(args) -> int:
    storage = _storage(args)
    result = merge_database_shards(storage, args.shards)
    _print_json(result)
    storage.close()
    return 0


def cmd_carry_forward_records(args) -> int:
    storage = _storage(args)
    result = carry_forward_records(
        args.source_db,
        storage,
        args.source_url_like,
        reason=args.reason,
        report_path=args.report,
        reactivate_existing=args.reactivate,
    )
    _print_json(result)
    storage.close()
    return 0


def cmd_apply_record_corrections(args) -> int:
    if not Path(args.db).is_file():
        raise FileNotFoundError(
            f"Record corrections require an existing target database: {args.db}"
        )
    storage = _storage(args)
    try:
        result = apply_record_corrections(
            storage,
            args.corrections,
            report_path=args.report,
        )
    finally:
        storage.close()
    _print_json(result)
    return 0


def cmd_apply_reviewed_identity_merges(args) -> int:
    result = apply_reviewed_identity_merges(
        args.db,
        args.review,
        report_path=args.report,
        run_id=args.run_id,
        reason=args.reason,
    )
    _print_json(
        {
            "requested_pair_count": result["requested_pair_count"],
            "merged_count": result["merged_count"],
            "already_merged_count": result["already_merged_count"],
            "integrity_check": result["integrity_check"],
            "report": str(Path(args.report).resolve()),
        }
    )
    return 0


def cmd_repair_failed_cache(args) -> int:
    storage = _storage(args)
    result = repair_failed_cache(
        args.config,
        storage,
        args.failed_run_id,
        archive_root=args.archive_root,
    )
    _print_json(result)
    storage.close()
    return 0 if result["offline_replay_ready"] else 2


def cmd_refresh_official_publications(args) -> int:
    try:
        person_ids = _combined_person_ids(args.person_id, args.person_id_file)
    except (FileNotFoundError, UnicodeError, ValueError) as exc:
        _print_json({"status": "failed", "error": str(exc)})
        return 2
    selectors = [person_ids, args.department]
    if all(values is None for values in selectors):
        _print_json(
            {
                "status": "failed",
                "error": "Supply at least one --person-id or --department selector",
            }
        )
        return 2
    if any(
        values is not None
        and (not values or any(not str(value).strip() for value in values))
        for values in selectors
    ):
        _print_json({"status": "failed", "error": "Selectors must not be empty or blank"})
        return 2
    if args.missing_confirmations < 1:
        _print_json(
            {"status": "failed", "error": "--missing-confirmations must be at least 1"}
        )
        return 2
    if args.missing_confirmations < 2 and not (
        args.offline and args.allow_single_confirmation_removal
    ):
        _print_json(
            {
                "status": "failed",
                "error": (
                    "--missing-confirmations 1 requires --offline and "
                    "--allow-single-confirmation-removal"
                ),
            }
        )
        return 2
    if args.limit is not None and args.limit < 1:
        _print_json({"status": "failed", "error": "--limit must be at least 1"})
        return 2

    storage = _storage(args)
    try:
        try:
            result = refresh_official_publications(
                args.config,
                storage,
                archive_root=args.archive_root,
                crawl_policy_path=args.crawl_policy,
                person_ids=person_ids,
                department_patterns=args.department,
                limit=args.limit,
                due_only=args.due_only,
                dry_run=args.dry_run,
                offline=args.offline,
                missing_confirmations=args.missing_confirmations,
                allow_single_confirmation_removal=(
                    args.allow_single_confirmation_removal
                ),
                workers=args.workers,
            )
        except ValueError as exc:
            result = {"status": "failed", "error": str(exc)}
    finally:
        storage.close()
    _print_json(result)
    return 0 if result.get("status") == "success" else 2


def cmd_sync_openalex_publications(args) -> int:
    try:
        person_ids = _combined_person_ids(args.person_id, args.person_id_file)
    except (FileNotFoundError, UnicodeError, ValueError) as exc:
        _print_json({"status": "scope_error", "error": str(exc)})
        return 2
    selectors = [person_ids, args.department]
    if all(values is None for values in selectors):
        _print_json(
            {
                "status": "scope_error",
                "error": "Supply at least one --person-id or --department selector",
            }
        )
        return 2
    if any(
        values is not None
        and (not values or any(not str(value).strip() for value in values))
        for values in selectors
    ):
        _print_json(
            {"status": "scope_error", "error": "Selectors must not be empty or blank"}
        )
        return 2
    if args.max_author_works < 1:
        _print_json(
            {"status": "scope_error", "error": "--max-author-works must be at least 1"}
        )
        return 2

    storage = _storage(args)
    try:
        try:
            result = sync_openalex_publications(
                storage,
                person_ids=person_ids,
                institution_id=args.institution_id,
                department_patterns=args.department,
                limit=args.limit,
                full=args.full,
                dry_run=args.dry_run,
                premium_updated_filter=args.premium_updated_filter,
                missing_runs_before_tombstone=args.missing_confirmations,
                max_author_works=args.max_author_works,
                reviewed_identity_manifest=args.reviewed_identity_manifest,
                revalidate_identities=args.revalidate_identities,
            )
        except (FileNotFoundError, ValueError) as exc:
            result = {"status": "scope_error", "error": str(exc)}
    except OpenAlexConfigurationError as exc:
        _print_json(
            {
                "status": "configuration_error",
                "error": str(exc),
                "required_environment": "OPENALEX_API_KEY",
            }
        )
        return 2
    finally:
        storage.close()
    _write_json_output(args.out, result)
    _print_json(result)
    return 0 if result.get("status") in {"success", "partial", "partial_success"} else 2


def cmd_build_research_vectors(args) -> int:
    if args.limit is not None and args.limit < 1:
        _print_json({"status": "failed", "error": "--limit must be at least 1"})
        return 2
    if args.batch_size < 1 or args.max_attempts < 1 or args.lease_seconds <= 0:
        _print_json(
            {
                "status": "failed",
                "error": (
                    "--batch-size and --max-attempts must be at least 1; "
                    "--lease-seconds must be positive"
                ),
            }
        )
        return 2
    try:
        person_ids = _combined_person_ids(args.person_id, args.person_id_file)
    except (FileNotFoundError, UnicodeError, ValueError) as exc:
        _print_json({"status": "scope_error", "error": str(exc)})
        return 2
    storage = _storage(args)
    try:
        result = process_vector_queue(
            storage,
            limit=args.limit,
            batch_size=args.batch_size,
            owner=args.owner,
            lease_seconds=args.lease_seconds,
            max_attempts=args.max_attempts,
            person_ids=person_ids,
        )
    finally:
        storage.close()
    _write_json_output(args.out, result)
    _print_json(result)
    return 0 if result.get("status") in {"success", "limited"} else 2


def cmd_audit(args) -> int:
    storage = _storage(args)
    _print_json(audit(storage))
    storage.close()
    return 0


def cmd_audit_region(args) -> int:
    storage = _storage(args)
    report = build_regional_audit(storage, args.registry, args.snapshot_root)
    outputs = write_regional_audit(report, args.out)
    _print_json({"summary": report["summary"], **outputs})
    storage.close()
    return 0


def cmd_audit_sample(args) -> int:
    storage = _storage(args)
    count = storage.write_audit_sample(args.out, args.sample_size)
    _print_json({"sampled": count, "out": str(args.out)})
    storage.close()
    return 0


def cmd_audit_pi_quality(args) -> int:
    report, outputs = run_pi_quality_audit(
        args.db,
        args.out,
        samples=args.samples,
    )
    _print_json({"summary": report["summary"], **outputs})
    return 0


def cmd_export(args) -> int:
    storage = _storage(args)
    export_outputs(storage, args.out)
    _print_json({"exported_to": str(args.out)})
    storage.close()
    return 0


def cmd_match_applicant(args) -> int:
    storage = _storage(args)
    results = match_applicant(args.applicant, storage, args.top_k, args.institution)
    _print_json(results)
    storage.close()
    return 0


def cmd_paper_backtrace_match(args) -> int:
    storage = _storage(args)
    results = run_paper_backtrace_match(
        args.applicant,
        storage,
        args.out,
        args.target_pi,
        args.top_k,
        args.institution,
    )
    _print_json({"matched": len(results), "out": str(args.out)})
    storage.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m pi_index.cli")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import-institutions")
    p.add_argument("--input", required=True)
    p.add_argument("--db", default=DEFAULT_DB)
    p.set_defaults(func=cmd_import_institutions)

    p = sub.add_parser("ingest-institution")
    p.add_argument("--config", required=True)
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--snapshot-root", default="snapshots")
    p.add_argument("--archive-root", default="data/raw_sources")
    p.add_argument("--offline", action="store_true")
    p.set_defaults(func=cmd_ingest_institution)

    p = sub.add_parser("ingest-batch")
    p.add_argument("--institution-list", required=True)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--snapshot-root", default="snapshots")
    p.add_argument("--archive-root", default="data/raw_sources")
    p.add_argument("--offline", action="store_true")
    p.set_defaults(func=cmd_ingest_batch)

    p = sub.add_parser("snapshot-institution")
    p.add_argument("--config", required=True)
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--snapshot-root", default="snapshots")
    p.add_argument("--archive-root", default="data/raw_sources")
    p.set_defaults(func=cmd_snapshot_institution)

    p = sub.add_parser("merge-shards")
    p.add_argument("--shards", nargs="+", required=True)
    p.add_argument("--db", default=DEFAULT_DB)
    p.set_defaults(func=cmd_merge_shards)

    p = sub.add_parser(
        "carry-forward-records",
        help="Carry evidence-selected records from a read-only prior database",
    )
    p.add_argument("--source-db", required=True)
    p.add_argument("--source-url-like", action="append", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--report", required=True)
    p.add_argument(
        "--reactivate",
        action="store_true",
        help="Explicitly restore selected existing missing records to active",
    )
    p.add_argument("--db", default=DEFAULT_DB)
    p.set_defaults(func=cmd_carry_forward_records)

    p = sub.add_parser(
        "apply-record-corrections",
        help="Apply exact official-evidence-backed canonical record corrections",
    )
    p.add_argument("--corrections", required=True)
    p.add_argument("--report", required=True)
    p.add_argument("--db", default=DEFAULT_DB)
    p.set_defaults(func=cmd_apply_record_corrections)

    p = sub.add_parser(
        "apply-reviewed-identity-merges",
        help="Apply only explicit, evidence-backed identity pairs from a review",
    )
    p.add_argument("--review", required=True)
    p.add_argument("--report", required=True)
    p.add_argument("--run-id", default=None)
    p.add_argument("--reason", default="reviewed_identity_merge")
    p.add_argument("--db", default=DEFAULT_DB)
    p.set_defaults(func=cmd_apply_reviewed_identity_merges)

    p = sub.add_parser("repair-failed-cache")
    p.add_argument("--config", required=True)
    p.add_argument("--failed-run-id", required=True)
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--archive-root", default="data/raw_sources")
    p.set_defaults(func=cmd_repair_failed_cache)

    p = sub.add_parser(
        "refresh-official-publications",
        help="Incrementally refresh PI publication claims without a full institution crawl",
    )
    p.add_argument("--config", required=True)
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument(
        "--archive-root",
        default=None,
        help="Content archive root (defaults to <db-directory>/raw_sources)",
    )
    p.add_argument("--crawl-policy", default="configs/crawl_policy.yaml")
    p.add_argument("--person-id", action="append", default=None)
    p.add_argument(
        "--person-id-file",
        default=None,
        help="UTF-8 text/JSON exact person-ID allowlist; # comments are ignored",
    )
    p.add_argument(
        "--department",
        action="append",
        default=None,
        help="Limit to PI department/faculty text (repeatable, case-insensitive substring)",
    )
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--due-only", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--missing-confirmations", type=int, default=2)
    p.add_argument(
        "--allow-single-confirmation-removal",
        action="store_true",
        help=(
            "Permit one-pass claim removal only during an explicit offline replay"
        ),
    )
    p.add_argument("--workers", type=int, default=4)
    p.set_defaults(func=cmd_refresh_official_publications)

    p = sub.add_parser(
        "sync-openalex-publications",
        help="Build or refresh confirmed OpenAlex Work manifests for selected PIs",
    )
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--institution-id", required=True)
    p.add_argument("--department", action="append", default=None)
    p.add_argument("--person-id", action="append", default=None)
    p.add_argument(
        "--person-id-file",
        default=None,
        help="UTF-8 text/JSON exact person-ID allowlist; # comments are ignored",
    )
    p.add_argument("--limit", type=int, default=None)
    p.add_argument(
        "--full",
        action="store_true",
        help=(
            "Force a complete cursor snapshot; otherwise the first/30-day refresh is full "
            "and intervening free-tier runs stop an updated_date-desc scan at the watermark"
        ),
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--premium-updated-filter",
        action="store_true",
        help="Use Premium from_updated_date for delta runs instead of the free sorted scan",
    )
    p.add_argument("--missing-confirmations", type=int, default=2)
    p.add_argument(
        "--max-author-works",
        type=int,
        default=2000,
        help="Fail closed before a confirmed Author profile larger than this Work count",
    )
    p.add_argument(
        "--reviewed-identity-manifest",
        default=None,
        help=(
            "Audited JSON manifest of explicitly reviewed PI-to-OpenAlex Author links; "
            "automatic identity rules remain unchanged"
        ),
    )
    p.add_argument(
        "--revalidate-identities",
        action="store_true",
        help=(
            "Re-run bounded evidence checks for existing non-reviewed confirmed links; "
            "unresolved links become stale instead of being silently reused"
        ),
    )
    p.add_argument(
        "--out",
        default=None,
        help="Optional UTF-8 JSON path for the complete synchronization audit",
    )
    p.set_defaults(func=cmd_sync_openalex_publications)

    p = sub.add_parser(
        "build-research-vectors",
        help="Drain queued OpenAlex paper and PI career vector rebuilds locally",
    )
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum queue attempts; omit to drain all paper/career jobs",
    )
    p.add_argument("--batch-size", type=int, default=100)
    p.add_argument("--owner", default=None)
    p.add_argument("--lease-seconds", type=float, default=900.0)
    p.add_argument("--max-attempts", type=int, default=3)
    p.add_argument("--person-id", action="append", default=None)
    p.add_argument(
        "--person-id-file",
        default=None,
        help="Restrict paper and career vector jobs to this exact PI cohort",
    )
    p.add_argument(
        "--out",
        default=None,
        help="Optional UTF-8 JSON path for the vector queue audit",
    )
    p.set_defaults(func=cmd_build_research_vectors)

    p = sub.add_parser("audit")
    p.add_argument("--db", default=DEFAULT_DB)
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("audit-region")
    p.add_argument("--registry", required=True)
    p.add_argument("--snapshot-root", default="snapshots")
    p.add_argument("--out", required=True)
    p.add_argument("--db", default=DEFAULT_DB)
    p.set_defaults(func=cmd_audit_region)

    p = sub.add_parser("audit-sample")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--out", required=True)
    p.add_argument("--sample-size", type=int, default=60)
    p.set_defaults(func=cmd_audit_sample)

    p = sub.add_parser(
        "audit-pi-quality",
        help="Audit active PI data quality and write JSON plus Markdown reports",
    )
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--out", required=True)
    p.add_argument("--samples", type=int, choices=(2, 3), default=3)
    p.set_defaults(func=cmd_audit_pi_quality)

    p = sub.add_parser("export")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--out", default="outputs/sample_run/")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("match-applicant")
    p.add_argument("--applicant", required=True)
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--top-k", type=int, default=20)
    p.add_argument("--institution", default=None)
    p.set_defaults(func=cmd_match_applicant)

    p = sub.add_parser("paper-backtrace-match")
    p.add_argument("--applicant", required=True)
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--out", default="outputs/paper_backtrace_matches.csv")
    p.add_argument("--target-pi", default=None)
    p.add_argument("--top-k", type=int, default=30)
    p.add_argument("--institution", default=None)
    p.set_defaults(func=cmd_paper_backtrace_match)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
