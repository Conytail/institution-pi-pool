from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path
from typing import Any

from .authorship_weight_experiment import (
    build_authorship_cases,
    generate_weight_configs,
    utc_now_iso,
)


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def verify_authorship_experiment(dataset_path: Path, output_dir: Path) -> dict[str, Any]:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    manifest = json.loads((output_dir / "experiment_manifest.json").read_text(encoding="utf-8"))
    cases, profiles, heldout_ids = build_authorship_cases(dataset.get("pis") or [])
    checks: dict[str, dict[str, Any]] = {}

    def check(name: str, actual: Any, expected: Any) -> None:
        checks[name] = {"passed": actual == expected, "actual": actual, "expected": expected}

    check("phase_two_ready", manifest["phase_two_readiness"]["ready"], True)
    expected_status = (
        "production_candidate"
        if all(
            result["acceptance_gate"]["passed"]
            for result in manifest["robust_encoder_results"].values()
        )
        else "phase2_no_change"
    )
    check("phase_two_status", manifest["status"], expected_status)
    check(
        "robust_acceptance_uses_nested_folds",
        sorted(
            {
                result.get("acceptance_fold_method")
                for result in manifest["robust_encoder_results"].values()
            }
        ),
        ["institution_grouped_nested_selection"],
    )
    check("dataset_pi_count", manifest["dataset_pi_count"], len(dataset.get("pis") or []))
    check("eligible_profile_pi_count", manifest["eligible_profile_pi_count"], len(profiles))
    check("institution_count", manifest["institution_count"], len({pi["institution_id"] for pi in dataset["pis"]}))
    check("case_count", manifest["case_count"], len(cases))
    check("heldout_work_count", manifest["heldout_work_count"], len(heldout_ids))
    check("unique_case_ids", len({case["case_id"] for case in cases}), len(cases))
    check("unique_proposal_work_ids", len({case["proposal_work_id"] for case in cases}), len(cases))
    training_work_ids = {
        work["id"] for works in profiles.values() for work in works
    }
    check("heldout_profile_overlap", len(heldout_ids.intersection(training_work_ids)), 0)

    config_rows = _csv_rows(output_dir / "config_results.csv")
    expected_configs = {config.config_id for config in generate_weight_configs()}
    expected_config_rows = len(expected_configs) * len(manifest["encoders"])
    check("config_result_rows", len(config_rows), expected_config_rows)
    for encoder in manifest["encoders"]:
        encoder_ids = {row["config_id"] for row in config_rows if row["encoder"] == encoder}
        check(
            f"{encoder}_complete_config_grid",
            sorted(encoder_ids),
            sorted(expected_configs),
        )

    case_rows = _csv_rows(output_dir / "case_results.csv")
    detailed_groups: defaultdict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in case_rows:
        detailed_groups[(row["encoder"], row["config_id"])].append(row)
    check(
        "detailed_case_groups_complete",
        all(len(rows) == len(cases) for rows in detailed_groups.values()),
        True,
    )
    check(
        "detailed_case_rows_unique",
        len({(row["encoder"], row["config_id"], row["case_id"]) for row in case_rows}),
        len(case_rows),
    )

    institution_folds = _csv_rows(output_dir / "institution_fold_results.csv")
    robust_folds = _csv_rows(output_dir / "robust_fold_results.csv")
    field_folds = _csv_rows(output_dir / "field_fold_results.csv")
    for encoder in manifest["encoders"]:
        check(
            f"{encoder}_nested_fold_count",
            sum(row["encoder"] == encoder for row in institution_folds),
            5,
        )
        check(
            f"{encoder}_fixed_robust_diagnostic_fold_count",
            sum(row["encoder"] == encoder for row in robust_folds),
            5,
        )
        check(
            f"{encoder}_field_fold_count",
            sum(row["encoder"] == encoder for row in field_folds),
            5,
        )

    eligible_fields = sum(bool(row["field_aware_eligible"]) for row in manifest["field_support"])
    field_rows = _csv_rows(output_dir / "field_override_results.csv")
    check("field_override_rows", len(field_rows), eligible_fields * len(manifest["encoders"]))
    scheme_rows = _csv_rows(output_dir / "scheme_comparison.csv")
    check("scheme_comparison_rows", len(scheme_rows), 4 * len(manifest["encoders"]))
    check("error_log_empty", (output_dir / "run.err.log").stat().st_size, 0)

    passed = all(item["passed"] for item in checks.values())
    report = {
        "generated_at": utc_now_iso(),
        "passed": passed,
        "dataset": str(dataset_path.resolve()),
        "output_dir": str(output_dir.resolve()),
        "checks": checks,
        "summary": {
            "pi_count": len(dataset.get("pis") or []),
            "institution_count": len({pi["institution_id"] for pi in dataset["pis"]}),
            "case_count": len(cases),
            "heldout_work_count": len(heldout_ids),
            "config_result_rows": len(config_rows),
            "detailed_case_rows": len(case_rows),
            "eligible_field_count": eligible_fields,
            "case_role_counts": dict(Counter(case["author_role"] for case in cases)),
        },
    }
    (output_dir / "verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if not passed:
        failed = [name for name, item in checks.items() if not item["passed"]]
        raise RuntimeError(f"Authorship experiment verification failed: {failed}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify authorship experiment artifacts")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    report = verify_authorship_experiment(Path(args.dataset), Path(args.output_dir))
    print(json.dumps(report["summary"], ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
