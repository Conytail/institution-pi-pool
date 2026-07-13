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
from .pipeline.export import export_outputs
from .pipeline.ingest_institution import import_institutions_csv, ingest_institution
from .pipeline.ingest_qs_batch import ingest_batch
from .pipeline.snapshot import create_institution_snapshot
from .storage import PIIndexStorage


DEFAULT_DB = "outputs/sample_run/pi_index.db"


def _storage(args) -> PIIndexStorage:
    return PIIndexStorage(args.db)


def cmd_import_institutions(args) -> int:
    storage = _storage(args)
    count = import_institutions_csv(args.input, storage)
    print(json.dumps({"imported": count, "db": str(args.db)}, indent=2))
    storage.close()
    return 0


def cmd_ingest_institution(args) -> int:
    storage = _storage(args)
    result = ingest_institution(args.config, storage, snapshot_root=args.snapshot_root)
    print(json.dumps(result, indent=2))
    storage.close()
    return 0


def cmd_ingest_batch(args) -> int:
    storage = _storage(args)
    result = ingest_batch(args.institution_list, storage, args.limit, args.snapshot_root)
    print(json.dumps(result, indent=2))
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
    )
    print(json.dumps(snapshot.manifest, indent=2, ensure_ascii=False))
    storage.close()
    return 0


def cmd_audit(args) -> int:
    storage = _storage(args)
    print(json.dumps(audit(storage), indent=2, ensure_ascii=False))
    storage.close()
    return 0


def cmd_audit_sample(args) -> int:
    storage = _storage(args)
    count = storage.write_audit_sample(args.out, args.sample_size)
    print(json.dumps({"sampled": count, "out": str(args.out)}, indent=2))
    storage.close()
    return 0


def cmd_export(args) -> int:
    storage = _storage(args)
    export_outputs(storage, args.out)
    print(json.dumps({"exported_to": str(args.out)}, indent=2))
    storage.close()
    return 0


def cmd_match_applicant(args) -> int:
    storage = _storage(args)
    results = match_applicant(args.applicant, storage, args.top_k, args.institution)
    print(json.dumps(results, indent=2, ensure_ascii=False))
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
    print(json.dumps({"matched": len(results), "out": str(args.out)}, indent=2, ensure_ascii=False))
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
    p.set_defaults(func=cmd_ingest_institution)

    p = sub.add_parser("ingest-batch")
    p.add_argument("--institution-list", required=True)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--snapshot-root", default="snapshots")
    p.set_defaults(func=cmd_ingest_batch)

    p = sub.add_parser("snapshot-institution")
    p.add_argument("--config", required=True)
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--snapshot-root", default="snapshots")
    p.set_defaults(func=cmd_snapshot_institution)

    p = sub.add_parser("audit")
    p.add_argument("--db", default=DEFAULT_DB)
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("audit-sample")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--out", required=True)
    p.add_argument("--sample-size", type=int, default=60)
    p.set_defaults(func=cmd_audit_sample)

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
