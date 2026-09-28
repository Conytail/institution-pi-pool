from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


REGISTRY_SCHEMA_VERSION = 1
DISCOVERY_MANIFEST_SCHEMA_VERSION = 1


class DiscoveryContractError(ValueError):
    pass


def _load_yaml(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise DiscoveryContractError(f"Invalid YAML root in {source}: expected a mapping")
    return data


def _nonempty_strings(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, str) and bool(item.strip()) for item in value
    )


def _is_official_url(url: str, domains: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == domain.lower() or host.endswith(f".{domain.lower()}") for domain in domains)


def validate_institution_registry(
    registry: dict[str, Any],
    source: str = "<registry>",
) -> dict[str, Any]:
    errors: list[str] = []
    if registry.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        errors.append(f"schema_version must be {REGISTRY_SCHEMA_VERSION}")
    if not isinstance(registry.get("registry_version"), int) or registry["registry_version"] < 1:
        errors.append("registry_version must be a positive integer")
    if not isinstance(registry.get("registry_id"), str) or not registry["registry_id"].strip():
        errors.append("registry_id is required")
    if not isinstance(registry.get("created_at"), str) or not registry["created_at"].strip():
        errors.append("created_at must be an ISO date string")

    selection = registry.get("selection")
    if not isinstance(selection, dict):
        errors.append("selection must be a mapping")
        selection = {}
    institutions = registry.get("institutions")
    if not isinstance(institutions, list) or not institutions:
        errors.append("institutions must be a non-empty list")
        institutions = []
    if selection.get("included_count") != len(institutions):
        errors.append("selection.included_count must equal the number of institutions")

    ids: set[str] = set()
    manifests: set[str] = set()
    for index, institution in enumerate(institutions):
        if not isinstance(institution, dict):
            errors.append(f"institutions[{index}] must be a mapping")
            continue
        institution_id = institution.get("institution_id")
        if not isinstance(institution_id, str) or not institution_id:
            errors.append(f"institutions[{index}].institution_id is required")
        elif institution_id in ids:
            errors.append(f"duplicate institution_id: {institution_id}")
        else:
            ids.add(institution_id)
        manifest = institution.get("discovery_manifest")
        if not isinstance(manifest, str) or not manifest:
            errors.append(f"institutions[{index}].discovery_manifest is required")
        elif manifest in manifests:
            errors.append(f"duplicate discovery_manifest: {manifest}")
        else:
            manifests.add(manifest)
        if institution.get("discovery_status") != "complete":
            errors.append(f"institutions[{index}].discovery_status must be complete")
        if not _nonempty_strings(institution.get("official_domains")):
            errors.append(f"institutions[{index}].official_domains must be non-empty")

    if errors:
        raise DiscoveryContractError(f"Invalid institution registry {source}: " + "; ".join(errors))
    return registry


def load_institution_registry(path: str | Path) -> dict[str, Any]:
    return validate_institution_registry(_load_yaml(path), str(path))


def validate_discovery_manifest(
    manifest: dict[str, Any],
    source: str = "<manifest>",
) -> dict[str, Any]:
    errors: list[str] = []
    if manifest.get("schema_version") != DISCOVERY_MANIFEST_SCHEMA_VERSION:
        errors.append(f"schema_version must be {DISCOVERY_MANIFEST_SCHEMA_VERSION}")
    if not isinstance(manifest.get("manifest_version"), int) or manifest["manifest_version"] < 1:
        errors.append("manifest_version must be a positive integer")
    if not isinstance(manifest.get("institution_id"), str) or not manifest["institution_id"]:
        errors.append("institution_id is required")
    if not isinstance(manifest.get("checked_at"), str) or not manifest["checked_at"].strip():
        errors.append("checked_at must be an ISO date string")

    identity = manifest.get("identity")
    if not isinstance(identity, dict):
        errors.append("identity must be a mapping")
        identity = {}
    domains = identity.get("official_domains")
    if not _nonempty_strings(domains):
        errors.append("identity.official_domains must be non-empty")
        domains = []

    sources = manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        errors.append("sources must be a non-empty list")
        sources = []
    source_ids: set[str] = set()
    source_roles: set[str] = set()
    for index, item in enumerate(sources):
        if not isinstance(item, dict):
            errors.append(f"sources[{index}] must be a mapping")
            continue
        source_id = item.get("source_id")
        if not isinstance(source_id, str) or not source_id:
            errors.append(f"sources[{index}].source_id is required")
        elif source_id in source_ids:
            errors.append(f"duplicate source_id: {source_id}")
        else:
            source_ids.add(source_id)
        role = item.get("role")
        if isinstance(role, str):
            source_roles.add(role)
        url = item.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            errors.append(f"sources[{index}].url must use https")
        elif domains and not _is_official_url(url, domains):
            errors.append(f"sources[{index}].url is outside official_domains: {url}")
        if item.get("official") is not True:
            errors.append(f"sources[{index}].official must be true")

    if "organizational_scope" not in source_roles:
        errors.append("an organizational_scope source is required")
    if not source_roles.intersection({"people_directory", "research_profile"}):
        errors.append("a people_directory or research_profile source is required")
    if "research_degree" not in source_roles:
        errors.append("a research_degree source is required")

    units = manifest.get("academic_units")
    if not isinstance(units, list) or not units:
        errors.append("academic_units must be a non-empty list")
        units = []
    unit_ids: set[str] = set()
    for index, unit in enumerate(units):
        if not isinstance(unit, dict):
            errors.append(f"academic_units[{index}] must be a mapping")
            continue
        unit_id = unit.get("unit_id")
        if not isinstance(unit_id, str) or not unit_id:
            errors.append(f"academic_units[{index}].unit_id is required")
        elif unit_id in unit_ids:
            errors.append(f"duplicate unit_id: {unit_id}")
        else:
            unit_ids.add(unit_id)
        if unit.get("evidence_source_id") not in source_ids:
            errors.append(f"academic_units[{index}].evidence_source_id is unknown")

    coverage = manifest.get("coverage")
    if not isinstance(coverage, dict):
        errors.append("coverage must be a mapping")
        coverage = {}
    if coverage.get("top_level_unit_count") != len(units):
        errors.append("coverage.top_level_unit_count must equal academic_units length")
    for key in (
        "all_top_level_units_accounted_for",
        "people_source_present",
        "research_degree_evidence_present",
    ):
        if coverage.get(key) is not True:
            errors.append(f"coverage.{key} must be true")
    if coverage.get("discovery_status") != "complete":
        errors.append("coverage.discovery_status must be complete")

    assessment = manifest.get("adapter_assessment")
    if not isinstance(assessment, dict) or not assessment.get("template_hypotheses"):
        errors.append("adapter_assessment.template_hypotheses must be non-empty")
    else:
        for index, hypothesis in enumerate(assessment["template_hypotheses"]):
            if hypothesis.get("source_id") not in source_ids:
                errors.append(f"template_hypotheses[{index}].source_id is unknown")

    if errors:
        raise DiscoveryContractError(f"Invalid discovery manifest {source}: " + "; ".join(errors))
    return manifest


def load_discovery_manifest(path: str | Path) -> dict[str, Any]:
    return validate_discovery_manifest(_load_yaml(path), str(path))
