from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

import yaml

from ..pipeline.ingest_institution import ingest_institution, load_yaml
from ..storage import PIIndexStorage
from .research_profile_experiment import utc_now_iso


def generated_institution_config(entry: dict[str, Any]) -> dict[str, Any]:
    domains = list(dict.fromkeys(entry["official_domains"]))
    return {
        "schema_version": 2,
        "config_version": 2,
        "institution": {
            "name": entry["name"],
            "country": entry.get("country"),
            "region": entry.get("region"),
            "ror_id": entry["ror_id"],
            "homepage_url": entry["homepage_url"],
            "official_domains": domains,
            "allowed_email_domains": domains,
        },
        "pool_scope": {
            "type": "department",
            "name": entry["pool_scope"],
            "population": "academic_and_research_personnel",
            "units": [{"name": entry["pool_scope"], "seed_urls": [entry["seed_url"]]}],
        },
        "site": {"template_family": "generic_faculty_directory_v1"},
        "crawl": {
            "seed_urls": [entry["seed_url"]],
            "max_depth": 0,
            "max_pages": 1,
            "crawl_delay_seconds": 0.5,
            "use_homepage_discovery": False,
            "allow_serp": False,
        },
        "parsing": {
            "extract_publication_fingerprints": True,
            "preferred_adapters": [
                "jsonld_person",
                "faculty_directory",
                "mailto_profile",
                "generic_html",
            ]
        },
        "refresh": {
            "directory_interval_days": 30,
            "profile_interval_days": 90,
        },
        "capture": {
            "archive_enabled": True,
            "compression": "gzip",
            "conditional_requests": True,
            "missing_runs_before_inactive": 2,
        },
        "quality_gate": {
            "minimum_people": 1,
            "maximum_duplicate_rate": 0.1,
            "minimum_profile_url_coverage": 0.5,
            "minimum_seed_url_coverage": 1.0,
            "minimum_unit_coverage": 1.0,
            "minimum_profile_fetch_coverage": 0.9,
            "minimum_profile_parse_coverage": 0.0,
            "require_pagination_complete": True,
        },
        "evaluation": {
            "domain": entry["evaluation_domain"],
            "pool_scope": entry["pool_scope"],
        },
    }


def validate_cohort(manifest: dict[str, Any]) -> None:
    entries = manifest.get("institutions") or []
    if len(entries) < 25:
        raise ValueError("Stage-two cohort requires at least 25 institution entries")
    inline = [entry for entry in entries if not entry.get("config_path")]
    rors = [entry.get("ror_id") for entry in inline]
    if any(not ror for ror in rors) or len(rors) != len(set(rors)):
        raise ValueError("Inline institution entries require unique ROR IDs")
    domains = {entry.get("evaluation_domain") for entry in entries}
    required = {"Physical Sciences", "Life Sciences", "Health Sciences", "Social Sciences"}
    if not required.issubset(domains):
        raise ValueError("Stage-two cohort must cover all four OpenAlex domains")


def _pool_rows(storage: PIIndexStorage, metadata_by_ror: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = storage.conn.execute(
        """
        SELECT i.name, i.ror_id, i.institution_id,
               COUNT(p.person_id) AS extracted_pi_count,
               SUM(CASE WHEN p.title IS NOT NULL AND p.title != '' THEN 1 ELSE 0 END) AS titled_pi_count,
               SUM(CASE WHEN p.profile_url IS NOT NULL AND p.profile_url != '' THEN 1 ELSE 0 END) AS profile_url_count,
               SUM(CASE WHEN p.emails_json != '[]' THEN 1 ELSE 0 END) AS email_count
        FROM institutions i
        LEFT JOIN canonical_pi_records p ON p.institution_id=i.institution_id
            AND COALESCE(p.membership_status, 'active')!='inactive'
        GROUP BY i.institution_id, i.name, i.ror_id
        ORDER BY i.name
        """
    ).fetchall()
    output: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        metadata = metadata_by_ror.get(item["ror_id"] or "", {})
        item["evaluation_domain"] = metadata.get("evaluation_domain")
        item["pool_scope"] = metadata.get("pool_scope")
        output.append(item)
    return output


def collect_stage2_pool(args: argparse.Namespace) -> dict[str, Any]:
    manifest_path = Path(args.manifest)
    manifest = load_yaml(manifest_path)
    validate_cohort(manifest)
    output_dir = Path(args.output_dir)
    generated_dir = output_dir / "generated_configs"
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_dir.mkdir(parents=True, exist_ok=True)
    storage = PIIndexStorage(args.db)
    runs: list[dict[str, Any]] = []
    metadata_by_ror: dict[str, dict[str, Any]] = {}

    for index, entry in enumerate(manifest["institutions"], start=1):
        if entry.get("config_path"):
            source_config_path = Path(entry["config_path"])
            config = load_yaml(source_config_path)
            ror_id = (config.get("institution") or {}).get("ror_id")
            config.setdefault("crawl", {}).update(
                {
                    "max_depth": 0,
                    "max_pages": len((config.get("crawl") or {}).get("seed_urls") or []) or 1,
                    "use_homepage_discovery": False,
                    "allow_serp": False,
                }
            )
            config_path = generated_dir / source_config_path.name
            config_path.write_text(
                yaml.safe_dump(config, sort_keys=False, allow_unicode=False),
                encoding="utf-8",
            )
        else:
            config = generated_institution_config(entry)
            config_path = generated_dir / f"{entry['slug']}.yaml"
            config_path.write_text(
                yaml.safe_dump(config, sort_keys=False, allow_unicode=False),
                encoding="utf-8",
            )
            ror_id = entry["ror_id"]
        metadata_by_ror[ror_id] = {
            "evaluation_domain": entry["evaluation_domain"],
            "pool_scope": entry["pool_scope"],
        }
        try:
            result = ingest_institution(config_path, storage)
            status = "success" if result["canonical_pi_records"] else "empty"
            error = None
        except Exception as exc:
            result = {"pages_parsed": 0, "canonical_pi_records": 0, "emails": 0}
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
        run = {
            "index": index,
            "config_path": str(config_path.resolve()),
            "ror_id": ror_id,
            "evaluation_domain": entry["evaluation_domain"],
            "pool_scope": entry["pool_scope"],
            "status": status,
            "error": error,
            **result,
        }
        runs.append(run)
        print(
            f"[{index}/{len(manifest['institutions'])}] {ror_id} -> "
            f"{result['canonical_pi_records']} records ({status})",
            flush=True,
        )

    pool_rows = _pool_rows(storage, metadata_by_ror)
    storage.close()
    with (output_dir / "pool_counts.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(pool_rows[0]) if pool_rows else [])
        if pool_rows:
            writer.writeheader()
            writer.writerows(pool_rows)
    successful = [row for row in pool_rows if int(row["extracted_pi_count"] or 0) > 0]
    result = {
        "generated_at": utc_now_iso(),
        "cohort_id": (manifest.get("cohort") or {}).get("id"),
        "source_manifest": str(manifest_path.resolve()),
        "db": str(Path(args.db).resolve()),
        "configured_institution_count": len(manifest["institutions"]),
        "successful_institution_count": len(successful),
        "extracted_pi_count": sum(int(row["extracted_pi_count"] or 0) for row in pool_rows),
        "pool_member_count": sum(int(row["extracted_pi_count"] or 0) for row in pool_rows),
        "domain_counts": {
            domain: sum(
                int(row["extracted_pi_count"] or 0)
                for row in pool_rows
                if row["evaluation_domain"] == domain
            )
            for domain in sorted({row["evaluation_domain"] for row in pool_rows if row["evaluation_domain"]})
        },
        "runs": runs,
    }
    (output_dir / "collection_manifest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect the official stage-two PI cohort")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--output-dir", required=True)
    return parser


def main() -> None:
    result = collect_stage2_pool(build_parser().parse_args())
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
