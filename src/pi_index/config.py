from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


INSTITUTION_CONFIG_SCHEMA_VERSION = 1
MATCHING_POLICY_SCHEMA_VERSION = 1


class ConfigValidationError(ValueError):
    pass


def _nonempty_strings(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, str) and bool(item.strip()) for item in value
    )


def _required_mapping(config: dict[str, Any], key: str, errors: list[str]) -> dict[str, Any]:
    value = config.get(key)
    if not isinstance(value, dict):
        errors.append(f"{key} must be a mapping")
        return {}
    return value


def validate_institution_config(
    config: dict[str, Any],
    source: str = "<config>",
) -> dict[str, Any]:
    errors: list[str] = []
    if config.get("schema_version") != INSTITUTION_CONFIG_SCHEMA_VERSION:
        errors.append(f"schema_version must be {INSTITUTION_CONFIG_SCHEMA_VERSION}")
    if not isinstance(config.get("config_version"), int) or config["config_version"] < 1:
        errors.append("config_version must be a positive integer")

    institution = _required_mapping(config, "institution", errors)
    if not isinstance(institution.get("name"), str) or not institution["name"].strip():
        errors.append("institution.name is required")
    if not _nonempty_strings(institution.get("official_domains")):
        errors.append("institution.official_domains must contain at least one domain")
    if not any(
        isinstance(institution.get(key), str) and bool(institution[key].strip())
        for key in ("ror_id", "homepage_url")
    ):
        errors.append("institution requires ror_id or homepage_url as a stable identity anchor")

    pool_scope = _required_mapping(config, "pool_scope", errors)
    if pool_scope.get("type") not in {"institution", "faculty", "school", "department", "multi_unit"}:
        errors.append("pool_scope.type is invalid")
    if not isinstance(pool_scope.get("name"), str) or not pool_scope["name"].strip():
        errors.append("pool_scope.name is required")

    site = _required_mapping(config, "site", errors)
    if not isinstance(site.get("template_family"), str) or not site["template_family"].strip():
        errors.append("site.template_family is required")

    crawl = _required_mapping(config, "crawl", errors)
    if not _nonempty_strings(crawl.get("seed_urls")):
        errors.append("crawl.seed_urls must contain at least one official URL")
    for key in ("max_depth", "max_pages"):
        if not isinstance(crawl.get(key), int) or crawl[key] < 0:
            errors.append(f"crawl.{key} must be a non-negative integer")

    parsing = _required_mapping(config, "parsing", errors)
    if not _nonempty_strings(parsing.get("preferred_adapters")):
        errors.append("parsing.preferred_adapters must contain at least one adapter")

    detection = _required_mapping(config, "pi_detection", errors)
    for key in ("positive_title_patterns", "negative_title_patterns"):
        if not _nonempty_strings(detection.get(key)):
            errors.append(f"pi_detection.{key} must contain at least one pattern")

    refresh = _required_mapping(config, "refresh", errors)
    for key in ("directory_interval_days", "profile_interval_days"):
        if not isinstance(refresh.get(key), int) or refresh[key] < 1:
            errors.append(f"refresh.{key} must be a positive integer")

    quality = _required_mapping(config, "quality_gate", errors)
    if not isinstance(quality.get("minimum_people"), int) or quality["minimum_people"] < 1:
        errors.append("quality_gate.minimum_people must be a positive integer")
    for key in ("maximum_duplicate_rate", "minimum_profile_url_coverage"):
        value = quality.get(key)
        if not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
            errors.append(f"quality_gate.{key} must be between 0 and 1")

    if errors:
        raise ConfigValidationError(f"Invalid institution config {source}: " + "; ".join(errors))
    return config


def load_institution_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(config, dict):
        raise ConfigValidationError(f"Invalid institution config {config_path}: root must be a mapping")
    return validate_institution_config(config, str(config_path))


def validate_matching_policy(
    policy: dict[str, Any],
    source: str = "<matching-policy>",
) -> dict[str, Any]:
    errors: list[str] = []
    if policy.get("schema_version") != MATCHING_POLICY_SCHEMA_VERSION:
        errors.append(f"schema_version must be {MATCHING_POLICY_SCHEMA_VERSION}")
    if not isinstance(policy.get("policy_id"), str) or not policy["policy_id"].strip():
        errors.append("policy_id is required")
    query = _required_mapping(policy, "query", errors)
    proposal_weight = query.get("proposal_weight")
    cv_weight = query.get("cv_weight")
    if not isinstance(proposal_weight, (int, float)) or not isinstance(cv_weight, (int, float)):
        errors.append("query proposal_weight and cv_weight must be numeric")
    elif abs(float(proposal_weight) + float(cv_weight) - 1.0) > 1e-9:
        errors.append("query weights must sum to 1")
    institution = _required_mapping(policy, "institution_fit", errors)
    if institution.get("mode") != "hard_filter":
        errors.append("institution_fit.mode must be hard_filter")
    if institution.get("selected_pool_only") is not True:
        errors.append("institution_fit.selected_pool_only must be true")
    if institution.get("cross_institution_backtrace_override") is not False:
        errors.append("institution_fit.cross_institution_backtrace_override must be false")
    research = _required_mapping(policy, "research_fit", errors)
    if research.get("paper_evidence_role") != "feature_not_filter":
        errors.append("research_fit.paper_evidence_role must be feature_not_filter")
    if research.get("semantic_fallback_when_paper_score_zero") is not True:
        errors.append("research_fit.semantic_fallback_when_paper_score_zero must be true")
    if research.get("zero_only_when_semantic_and_profile_are_zero") is not True:
        errors.append("research_fit.zero_only_when_semantic_and_profile_are_zero must be true")

    full_match = _required_mapping(policy, "full_match", errors)
    research_score = _required_mapping(full_match, "research_score", errors)
    if research_score.get("operation") != "max":
        errors.append("full_match.research_score.operation must be max")
    if research_score.get("components") != ["career_score", "paper_top3_score"]:
        errors.append("full_match.research_score.components must be career_score and paper_top3_score")

    score_output = _required_mapping(policy, "score_output", errors)
    expected_components = [
        "institution_fit_score",
        "research_fit_score",
        "supervisor_validity_score",
    ]
    if score_output.get("independent_components") != expected_components:
        errors.append("score_output.independent_components must use the Matching v1 score decomposition")
    if errors:
        raise ConfigValidationError(f"Invalid matching policy {source}: " + "; ".join(errors))
    return policy


def load_matching_policy(path: str | Path) -> dict[str, Any]:
    policy_path = Path(path)
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
    if not isinstance(policy, dict):
        raise ConfigValidationError(f"Invalid matching policy {policy_path}: root must be a mapping")
    return validate_matching_policy(policy, str(policy_path))
