from __future__ import annotations

import json
from pathlib import Path

from ..discovery_contracts import load_institution_registry
from ..storage import PIIndexStorage


def _read_pointer(path: Path) -> dict | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def build_regional_audit(
    storage: PIIndexStorage,
    registry_path: str | Path,
    snapshot_root: str | Path,
) -> dict:
    registry = load_institution_registry(registry_path)
    snapshot_root = Path(snapshot_root)
    institutions: list[dict] = []

    for entry in registry["institutions"]:
        row = storage.conn.execute(
            "SELECT institution_id FROM institutions WHERE ror_id=? OR name=? ORDER BY ror_id IS NULL LIMIT 1",
            (entry.get("ror_id"), entry["official_name"]),
        ).fetchone()
        if row is None:
            institutions.append(
                {
                    "registry_id": entry["institution_id"],
                    "institution_name": entry["official_name"],
                    "status": "not_run",
                    "serving_ready": False,
                }
            )
            continue

        institution_id = str(row["institution_id"])
        latest_run_row = storage.conn.execute(
            """
            SELECT run_id FROM ingestion_runs
            WHERE institution_id=? AND run_id IS NOT NULL
            ORDER BY started_at DESC, id DESC LIMIT 1
            """,
            (institution_id,),
        ).fetchone()
        latest_run = (
            storage.get_ingestion_run(str(latest_run_row["run_id"]))
            if latest_run_row is not None
            else None
        )
        root = snapshot_root / institution_id
        latest_pointer = _read_pointer(root / "latest.json")
        current_pointer = _read_pointer(root / "current.json")
        active_people = int(
            storage.conn.execute(
                """
                SELECT COUNT(*) FROM canonical_pi_records
                WHERE institution_id=? AND COALESCE(membership_status, 'active')!='inactive'
                """,
                (institution_id,),
            ).fetchone()[0]
        )
        metrics = (latest_run or {}).get("metrics") or {}
        run_id = (latest_run or {}).get("run_id")
        failures = []
        if run_id:
            failures = [
                dict(item)
                for item in storage.conn.execute(
                    """
                    SELECT source_url, stage, reason FROM crawl_errors
                    WHERE institution_id=? AND run_id=? ORDER BY id
                    """,
                    (institution_id, run_id),
                ).fetchall()
            ]
        latest_quality = (latest_pointer or {}).get("quality_status")
        institutions.append(
            {
                "registry_id": entry["institution_id"],
                "institution_id": institution_id,
                "institution_name": entry["official_name"],
                "status": "pass" if latest_quality == "pass" else "partial",
                "serving_ready": current_pointer is not None,
                "active_people": active_people,
                "latest_run_id": run_id,
                "latest_run_status": (latest_run or {}).get("status"),
                "latest_crawl_complete": bool((latest_run or {}).get("crawl_complete")),
                "latest_snapshot_quality": latest_quality,
                "serving_snapshot_run_id": (current_pointer or {}).get("run_id"),
                "seed_url_coverage": metrics.get("seed_url_coverage"),
                "unit_coverage": metrics.get("unit_coverage"),
                "profile_url_coverage": metrics.get("observed_profile_url_coverage"),
                "duplicate_rate": metrics.get("run_duplicate_rate"),
                "units_missing": metrics.get("units_missing") or [],
                "failures": failures,
            }
        )

    return {
        "schema_version": 1,
        "registry_id": registry["registry_id"],
        "baseline_tag": registry["baseline_tag"],
        "summary": {
            "institutions_total": len(institutions),
            "latest_pass": sum(item["status"] == "pass" for item in institutions),
            "partial": sum(item["status"] == "partial" for item in institutions),
            "not_run": sum(item["status"] == "not_run" for item in institutions),
            "serving_ready": sum(bool(item["serving_ready"]) for item in institutions),
            "active_people": sum(int(item.get("active_people") or 0) for item in institutions),
        },
        "institutions": institutions,
    }


def write_regional_audit(report: dict, output_dir: str | Path) -> dict:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "regional_audit.json"
    markdown_path = output_dir / "regional_audit.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    summary = report["summary"]
    lines = [
        "# Regional PI Pool Audit",
        "",
        f"- Institutions: {summary['institutions_total']}",
        f"- Latest pass: {summary['latest_pass']}",
        f"- Partial: {summary['partial']}",
        f"- Not run: {summary['not_run']}",
        f"- Serving ready: {summary['serving_ready']}",
        f"- Active people: {summary['active_people']}",
        "",
        "| Institution | Status | Active people | Seed coverage | Unit coverage | Missing units |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for item in report["institutions"]:
        seed = item.get("seed_url_coverage")
        unit = item.get("unit_coverage")
        missing = ", ".join(item.get("units_missing") or [])
        lines.append(
            f"| {item['institution_name']} | {item['status']} | {item.get('active_people', 0)} | "
            f"{seed:.3f} | {unit:.3f} | {missing} |"
            if isinstance(seed, (int, float)) and isinstance(unit, (int, float))
            else f"| {item['institution_name']} | {item['status']} | {item.get('active_people', 0)} | - | - | {missing} |"
        )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json": str(json_path), "markdown": str(markdown_path)}
