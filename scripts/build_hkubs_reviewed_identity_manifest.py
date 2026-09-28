from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import re
import sqlite3
import unicodedata
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "outputs/pilots/hku_business_school/pi_index.db"
COHORT_PATH = ROOT / "configs/pilots/hku_business_school_publication_cohort_124.txt"
SINGLE_REVIEW_PATH = (
    ROOT
    / "outputs/pilots/hku_business_school/audit/openalex_single_profile_review.json"
)
SPLIT_REVIEW_PATH = (
    ROOT
    / "outputs/pilots/hku_business_school/audit/openalex_split_profile_review.json"
)
MANIFEST_PATH = (
    ROOT / "configs/pilots/hku_business_school_openalex_reviewed_identities.json"
)
AUDIT_PATH = (
    ROOT
    / "outputs/pilots/hku_business_school/audit/"
    "openalex_reviewed_identity_manifest_v1_synthesis.json"
)
RETRY_MANIFEST_PATH = (
    ROOT / "configs/pilots/hku_business_school_openalex_retry_4_reviewed_identities.json"
)
RESOLUTION_MANIFEST_PATH = (
    ROOT
    / "configs/pilots/hku_business_school_openalex_resolution_3_reviewed_identities.json"
)

INSTITUTION_ID = "inst_77b83f05042f0881"
BUSINESS_YANG_LIU_ID = "pi_ab915b2c31380c0e"
RETRY_PERSON_IDS = (
    "pi_5456a92908d85448",  # Ivy Chu DANG
    "pi_c4db6d2580057d6c",  # Anson Yile JIANG
    "pi_d29e0e699937b31e",  # Zhenhui Jack JIANG
    "pi_d904b94b4ffd6a9a",  # Tak Zhongqiang HUANG
)
RESOLUTION_PERSON_IDS = (
    "pi_0c47fd78b5b78c62",  # Ye LUO
    "pi_2a0429f880011014",  # Jian ZHANG
    BUSINESS_YANG_LIU_ID,
)


# These are reviewed, person-scoped OpenAlex authorship aliases.  They are not
# written to canonical PI aliases and therefore cannot affect global dedupe or
# automatic identity resolution.
AUTHORSHIP_NAME_ALIAS_OVERRIDES: dict[str, list[str]] = {
    "pi_5456a92908d85448": ["Chu Dang"],
    "pi_c4db6d2580057d6c": ["Yile Jiang"],
    "pi_d29e0e699937b31e": ["Zhenhui Jiang"],
    "pi_d904b94b4ffd6a9a": ["Zhongqiang Huang"],
}


# Profiles for Anson and Zhenhui contain same-name contamination even after a
# field gate.  Retain only exact, individually reviewed Work IDs while keeping
# the already reviewed Author link and per-Work authorship provenance.
EXACT_WORK_POLICY_OVERRIDES: dict[str, list[str]] = {
    "pi_c4db6d2580057d6c": [
        "W3113253771",
        "W4389227124",
        "W4403475006",
        "W4410192247",
    ],
    "pi_d29e0e699937b31e": [
        "W1497112516",
        "W1605448959",
        "W2073428267",
        "W2082877471",
        "W2114430050",
        "W2346506637",
        "W2559970365",
        "W2611012316",
        "W2964309803",
        "W2964744639",
        "W3124679280",
        "W4296230421",
        "W4313367648",
        "W4362456338",
        "W4387530451",
        "W4389195255",
        "W4404513897",
        "W4406212158",
        "W4414437244",
        # Manually reviewed title variants from the same official HKUBS list.
        "W2199542973",
        "W2017469186",
        "W2092845900",
        "W2166820194",
        "W2118226919",
        "W1972785869",
        "W2422283038",
        "W2157503882",
    ],
}


# Tak's 23-Work profile has two unrelated Computer Science records.  The
# remaining 21 Works form the reviewed consumer-behaviour corpus.
FIELD_WORK_POLICY_OVERRIDES: dict[str, dict[str, list[str]]] = {
    "pi_d904b94b4ffd6a9a": {
        "fields": [
            "Psychology",
            "Business, Management and Accounting",
            "Energy",
            "Decision Sciences",
        ],
        "always_include_work_ids": [
            "W4385691042",
            "W4225278817",
            "W4306756417",
            "W4210679017",
            "W2906967410",
            "W2771264452",
            "W2746527007",
            "W2272362060",
            "W2465356868",
            "W2022460787",
        ],
    }
}


# These people have no safe whole Author profile.  Their manifest decisions are
# authoritative, explicit PI-Work snapshots with no confirmed Author ID.
OFFICIAL_ONLY_CASE_IDS = {
    "pi_2a0429f880011014",  # Jian ZHANG
    "pi_cef5f6414ce6b0cb",  # Michael B. WONG
    "pi_a21466932ad41696",  # Wei ZHANG
    "pi_a06f8f1556d38968",  # Xiang FANG
    "pi_53109ae5935b51cc",  # Yan PAN
}


# Split/manual cases whose retained profiles require a conservative field gate.
FIELD_CASES: dict[str, list[str]] = {
    "pi_d2b51acd5f3ed816": [
        "Business, Management and Accounting",
        "Economics, Econometrics and Finance",
        "Decision Sciences",
        "Social Sciences",
    ],
    "pi_f41b16319905d8c5": [
        "Economics, Econometrics and Finance",
        "Social Sciences",
        "Arts and Humanities",
        "Business, Management and Accounting",
    ],
    "pi_44907897a27d0f76": [
        "Mathematics",
        "Computer Science",
        "Decision Sciences",
        "Social Sciences",
    ],
    "pi_488684782a761e3a": [
        "Business, Management and Accounting",
        "Decision Sciences",
        "Economics, Econometrics and Finance",
        "Social Sciences",
        "Computer Science",
    ],
    "pi_68efdd79ce411f23": [
        "Economics, Econometrics and Finance",
        "Business, Management and Accounting",
    ],
    "pi_6f3513e0677457d7": [
        "Economics, Econometrics and Finance",
        "Social Sciences",
        "Business, Management and Accounting",
    ],
    "pi_961bee2b1d38220e": [
        "Economics, Econometrics and Finance",
        "Business, Management and Accounting",
        "Decision Sciences",
        "Social Sciences",
    ],
    "pi_e73351eb30ea28d8": [
        "Business, Management and Accounting",
        "Economics, Econometrics and Finance",
        "Decision Sciences",
        "Social Sciences",
    ],
    "pi_e7cf984812cdfba7": [
        "Economics, Econometrics and Finance",
        "Business, Management and Accounting",
    ],
    "pi_feb1ba7a80f65158": [
        "Business, Management and Accounting",
        "Economics, Econometrics and Finance",
        "Decision Sciences",
    ],
}


# The Hong ZOU audit included two demonstrably wrong same-name profiles.  The
# reviewed correction retains only the coherent finance/insurance profile.
CASE_AUTHOR_OVERRIDES: dict[str, list[str]] = {
    "pi_c5d1e5f0b64b45d5": ["A5002252532"],
}

CASE_REASON_OVERRIDES: dict[str, str] = {
    "pi_c5d1e5f0b64b45d5": (
        "Corrected Hong ZOU review: retain only the coherent finance/insurance profile "
        "A5002252532. Exclude A5113756416 (biology) and A5106276864 (astronomy); "
        "A5022896232 is not admitted as a profile and its possible official Work is not "
        "part of this full-profile decision."
    ),
}


EXPLICIT_OFFICIAL_WORK_IDS: dict[str, list[str]] = {
    "pi_0c47fd78b5b78c62": [
        # DOI 10.1257/aer.p20171040; unique AEA/Crossref/OpenAlex match.
        "W2607714261",
    ],
    "pi_2a0429f880011014": [
        "W2980969207",
        "W4388108648",
        "W4391957190",
        "W2811017897",
        "W2793856001",
        "W3123076947",
        "W3091937209",
        "W2615932930",
        "W3035795437",
        "W3108156014",
        "W4409727779",
        # DOI 10.1287/mnsc.2024.04649; unique publisher/OpenAlex match.
        "W4414595748",
    ],
    "pi_d60f303526575ca2": [
        "W4388797309",
        "W4385621981",
        "W4295105933",
        "W3145717908",
        "W3149310895",
        "W4317664627",
    ],
    "pi_cef5f6414ce6b0cb": ["W2965166727", "W4408107566"],
    "pi_a06f8f1556d38968": [
        "W2550513998",
        "W4285165024",
        "W4285209182",
    ],
    "pi_53109ae5935b51cc": ["W7130602836"],
}


# Reviewed Work IDs known from the prior API diagnostics but not necessarily
# materialized in openalex_works yet.  expected_title is always the exact
# current official HKU title.  expected_openalex_title is used only for a
# reviewed metadata-title variant on the OpenAlex singleton.
KNOWN_EXTERNAL_WORKS: dict[str, dict[str, dict[str, str]]] = {
    "pi_0c47fd78b5b78c62": {
        "W2607714261": {
            "expected_title": "L2-Boosting for Economic Applications",
            "expected_openalex_title": (
                "<i>L</i><sub>2</sub>-Boosting for Economic Applications"
            ),
            "provenance": "reviewed_publisher_doi_openalex_singleton",
        },
    },
    "pi_2a0429f880011014": {
        "W2811017897": {
            "expected_title": (
                "Disguised Corruption: Evidence from Consumer Credit in China"
            ),
            "provenance": "reviewed_exact_openalex_search",
        },
        "W2793856001": {
            "expected_title": (
                "Gender Difference and Intra-Household Economic Power in Mortgage "
                "Signing Order"
            ),
            "provenance": "reviewed_exact_openalex_search",
        },
        "W3123076947": {
            "expected_title": (
                "Gender Gap in Personal Bankruptcy Risks: Empirical Evidence from "
                "Singapore"
            ),
            "provenance": "reviewed_exact_openalex_search",
        },
        "W3091937209": {
            "expected_title": (
                "Good Days, Bad Days: Stock Market Fluctuation and Taxi Tipping Decisions"
            ),
            "provenance": "reviewed_exact_openalex_search",
        },
        "W2615932930": {
            "expected_title": (
                "Housing Property Rights, Collateral, and Entrepreneurship: Evidence "
                "from China"
            ),
            "provenance": "reviewed_exact_openalex_search",
        },
        "W3035795437": {
            "expected_title": (
                "Interest Rate Pass-Through and Consumption Response: The Deposit Channel"
            ),
            "provenance": "reviewed_exact_openalex_search",
        },
        "W3108156014": {
            "expected_title": (
                "Investing in Low-Trust Countries: On the Role of Social Trust in the "
                "Global Mutual Fund Industry"
            ),
            "provenance": "reviewed_exact_openalex_search",
        },
        "W4409727779": {
            "expected_title": "Tax Policy Transmission and Household Expenditures",
            "provenance": "reviewed_exact_openalex_search",
        },
        "W4414595748": {
            "expected_title": "Do Households React to Monetary Policy?",
            "provenance": "reviewed_publisher_doi_openalex_singleton",
        },
    },
    "pi_d60f303526575ca2": {
        "W4388797309": {
            "expected_title": (
                "A note on improving variational estimation for multidimensional item "
                "response theory"
            ),
            "provenance": "reviewed_exact_openalex_search",
        },
        "W4385621981": {
            "expected_title": "DIF Statistical Inference without Knowing Anchoring Items",
            "provenance": "reviewed_exact_openalex_search",
        },
        "W4295105933": {
            "expected_title": (
                "High-dimensional Inference for Generalized Linear Models with Hidden "
                "Confounding"
            ),
            "provenance": "reviewed_exact_openalex_search",
        },
        "W3145717908": {
            "expected_title": "Identifiability of latent class models with covariates",
            "provenance": "reviewed_exact_openalex_search",
        },
        "W3149310895": {
            "expected_title": (
                "Learning latent and hierarchical structures in cognitive diagnosis models"
            ),
            "provenance": "reviewed_exact_openalex_search",
        },
        "W4317664627": {
            "expected_title": "Statistical inference for noisy incomplete binary matrix",
            "provenance": "reviewed_exact_openalex_search",
        },
    },
    "pi_53109ae5935b51cc": {
        "W7130602836": {
            "expected_title": (
                "High-Investment Human Resource Practices and Firm Performance in the "
                "Context of National Education Systems and Labor Markets: A Cross-National "
                "Meta-Analysis"
            ),
            "expected_openalex_title": (
                "High-investment human resource practices and firm performance in the "
                "context of national education systems and labor markets: A cross-national "
                "meta-analysis"
            ),
        }
    },
    BUSINESS_YANG_LIU_ID: {
        "W4399970449": {"expected_title": "Dynamic ESG Equilibrium"},
        "W4285165024": {
            "expected_title": (
                "Getting to the Core: Inflation Risks Within and Across Asset Classes"
            )
        },
        "W4317933862": {"expected_title": "Government Debt and Risk Premia"},
        "W3177432426": {
            "expected_title": "Government Policy Approval and Exchange Rate",
            "expected_openalex_title": "Government Policy Approval and Exchange Rates",
        },
        "W4414266988": {
            "expected_title": "Volatility (Dis)Connect in International Markets"
        },
        "W4210399059": {"expected_title": "Volatility Risk Pass-Through"},
        "W3134119437": {
            "expected_title": "Volatility, Intermediaries, and Exchange Rate",
            "expected_openalex_title": (
                "Volatility, intermediaries, and exchange rates"
            ),
        },
        "W4406073117": {
            "expected_title": "Currency Risk Under Capital Controls",
            "provenance": "reviewed_ssrn_doi_openalex_singleton",
        },
        "W4210509393": {
            "expected_title": "Political Announcement Return",
            "expected_openalex_title": "Government Policy Announcement Return",
            "provenance": "reviewed_ssrn_doi_title_history",
        },
    },
}


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _normalize_title(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(character for character in text if not unicodedata.combining(character))
    return " ".join(
        "".join(character if character.isalnum() else " " for character in text).split()
    )


def _normalize_doi(value: Any) -> str:
    text = str(value or "").strip().casefold()
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", text)


def _read_cohort() -> list[str]:
    return [
        line.strip()
        for line in COHORT_PATH.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _ro_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _current_official_fingerprints(
    conn: sqlite3.Connection, cohort: Iterable[str]
) -> list[dict[str, Any]]:
    person_ids = list(cohort)
    placeholders = ",".join("?" for _ in person_ids)
    rows = conn.execute(
        f"""
        SELECT f.person_id, f.fingerprint_id, f.title, f.doi
        FROM official_publication_fingerprints f
        WHERE f.person_id IN ({placeholders})
          AND (
                NOT EXISTS (
                    SELECT 1 FROM official_publication_source_claims c
                    WHERE c.fingerprint_id=f.fingerprint_id
                )
                OR EXISTS (
                    SELECT 1 FROM official_publication_source_claims c
                    WHERE c.fingerprint_id=f.fingerprint_id
                      AND c.claim_status='active'
                )
          )
        ORDER BY f.person_id, f.title, f.fingerprint_id
        """,
        person_ids,
    ).fetchall()
    return [dict(row) for row in rows]


def _match_official_fingerprints(
    fingerprints: list[dict[str, Any]], work: Mapping[str, Any]
) -> list[dict[str, Any]]:
    work_doi = _normalize_doi(work.get("doi"))
    work_title = _normalize_title(work.get("title"))
    return [
        item
        for item in fingerprints
        if (work_doi and _normalize_doi(item.get("doi")) == work_doi)
        or (work_title and _normalize_title(item.get("title")) == work_title)
    ]


def _find_current_title(
    fingerprints: list[dict[str, Any]], expected_title: str
) -> str:
    exact = [item["title"] for item in fingerprints if item.get("title") == expected_title]
    if len(set(exact)) == 1:
        return exact[0]
    normalized = [
        item["title"]
        for item in fingerprints
        if _normalize_title(item.get("title")) == _normalize_title(expected_title)
    ]
    if len(set(normalized)) == 1:
        return normalized[0]
    raise ValueError(f"official title is not uniquely current: {expected_title!r}")


def _reviewed_work_reference(
    *,
    person_id: str,
    work_id: str,
    fingerprints_by_person: Mapping[str, list[dict[str, Any]]],
    works_by_id: Mapping[str, dict[str, Any]],
    hinted_official_title: str | None = None,
) -> tuple[dict[str, str], str]:
    fingerprints = fingerprints_by_person.get(person_id, [])
    known = KNOWN_EXTERNAL_WORKS.get(person_id, {}).get(work_id)
    work = works_by_id.get(work_id)
    provenance = "local_exact_title_or_doi"

    if known is not None:
        provenance = known.get(
            "provenance", "reviewed_prior_api_diagnostic"
        )
        official_title = _find_current_title(fingerprints, known["expected_title"])
        reference = {
            "openalex_work_id": work_id,
            "expected_title": official_title,
        }
        if known.get("expected_openalex_title"):
            reference["expected_openalex_title"] = known["expected_openalex_title"]
        if work is not None and _normalize_title(work.get("title")) != _normalize_title(
            official_title
        ):
            reference["expected_openalex_title"] = str(work.get("title") or "")
            provenance = "reviewed_openalex_title_variant"
        return reference, provenance

    if work is None:
        raise ValueError(f"reviewed Work is absent from the local OpenAlex cache: {work_id}")
    matched = _match_official_fingerprints(fingerprints, work)
    titles = sorted({str(item.get("title") or "") for item in matched if item.get("title")})
    if len(titles) == 1:
        official_title = titles[0]
    elif hinted_official_title:
        official_title = _find_current_title(fingerprints, hinted_official_title)
        decoded_work_title = html.unescape(str(work.get("title") or ""))
        if _normalize_title(decoded_work_title) != _normalize_title(official_title):
            raise ValueError(
                f"reviewed Work {work_id} does not align with current official title "
                f"{official_title!r}"
            )
        provenance = "reviewed_openalex_title_variant"
    else:
        raise ValueError(
            f"reviewed Work {work_id} does not map uniquely to a current official title"
        )
    reference = {
        "openalex_work_id": work_id,
        "expected_title": official_title,
    }
    if _normalize_title(work.get("title")) != _normalize_title(official_title):
        reference["expected_openalex_title"] = str(work.get("title") or "")
        provenance = "reviewed_openalex_title_variant"
    return reference, provenance


def _case_profile_ids(case: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    keep = list(case.get("keep_profile_ids") or [])
    filtered = list(case.get("filter_required_profile_ids") or [])
    if case.get("classification") == "K" and case.get("profile_id"):
        keep.append(str(case["profile_id"]))
    return list(dict.fromkeys(keep)), list(dict.fromkeys(filtered))


def _case_profile_official_work_ids(
    case: Mapping[str, Any], included_author_ids: set[str]
) -> list[str]:
    output: list[str] = []
    for profile in case.get("profiles") or []:
        if profile.get("profile_id") not in included_author_ids:
            continue
        output.extend(str(value) for value in profile.get("official_work_ids") or [])
    return list(dict.fromkeys(output))


def _one_local_reference_per_official_title(
    *,
    person_id: str,
    preferred_work_ids: list[str],
    fingerprints_by_person: Mapping[str, list[dict[str, Any]]],
    works_by_id: Mapping[str, dict[str, Any]],
) -> list[str]:
    fingerprints = fingerprints_by_person.get(person_id, [])
    preferred_rank = {work_id: index for index, work_id in enumerate(preferred_work_ids)}
    candidates: dict[str, list[str]] = defaultdict(list)
    for work_id, work in works_by_id.items():
        for fingerprint in _match_official_fingerprints(fingerprints, work):
            key = _normalize_title(fingerprint.get("title")) or (
                "doi:" + _normalize_doi(fingerprint.get("doi"))
            )
            candidates[key].append(work_id)
    selected: list[str] = []
    for key in sorted(candidates):
        values = sorted(
            set(candidates[key]),
            key=lambda value: (preferred_rank.get(value, 10_000), value),
        )
        selected.append(values[0])
    return selected


class _ReadOnlyPreflightStorage:
    """Small read-only adapter for the sync loader/preflight private contract."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @staticmethod
    def _decoded_record(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        try:
            payload = json.loads(result.get("record_json") or "{}")
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, Mapping):
            for key, value in payload.items():
                result.setdefault(key, value)
        return result

    def iter_pi_records(self, include_inactive: bool = False):
        query = "SELECT * FROM canonical_pi_records"
        params: tuple[Any, ...] = ()
        if not include_inactive:
            query += " WHERE membership_status='active'"
        for row in self.conn.execute(query, params):
            yield self._decoded_record(row)

    def get_openalex_author_link(self, person_id: str):
        row = self.conn.execute(
            "SELECT * FROM openalex_author_links WHERE person_id=?", (person_id,)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            evidence = json.loads(result.get("evidence_json") or "{}")
        except json.JSONDecodeError:
            evidence = {}
        result["evidence"] = evidence if isinstance(evidence, Mapping) else {}
        return result

    def get_official_publication_identity_evidence(self, person_id: str):
        rows = self.conn.execute(
            """
            SELECT f.title, f.doi
            FROM official_publication_fingerprints f
            WHERE f.person_id=?
              AND (
                    NOT EXISTS (
                        SELECT 1 FROM official_publication_source_claims c
                        WHERE c.fingerprint_id=f.fingerprint_id
                    )
                    OR EXISTS (
                        SELECT 1 FROM official_publication_source_claims c
                        WHERE c.fingerprint_id=f.fingerprint_id
                          AND c.claim_status IN ('active', 'no_longer_observed')
                    )
              )
            """,
            (person_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def main() -> int:
    cohort = _read_cohort()
    if len(cohort) != 124 or len(set(cohort)) != 124:
        raise ValueError("HKU Business cohort allowlist must contain exactly 124 unique IDs")

    single_review = _read_json(SINGLE_REVIEW_PATH)
    split_review = _read_json(SPLIT_REVIEW_PATH)
    cases = (
        list(split_review["split_profile_reviews"])
        + list(split_review["unresolved_case_reviews"])
        + list(split_review["failed_http_case_reviews"])
    )

    conn = _ro_connection()
    try:
        placeholders = ",".join("?" for _ in cohort)
        records = {
            row["person_id"]: dict(row)
            for row in conn.execute(
                f"""
                SELECT person_id, display_name, institution_id, membership_status
                FROM canonical_pi_records
                WHERE person_id IN ({placeholders})
                """,
                cohort,
            ).fetchall()
        }
        if set(records) != set(cohort):
            raise ValueError("cohort allowlist does not match the pilot canonical records")
        if any(row["membership_status"] != "active" for row in records.values()):
            raise ValueError("cohort contains a non-active canonical PI")
        if any(row["institution_id"] != INSTITUTION_ID for row in records.values()):
            raise ValueError("cohort contains a PI outside HKU")

        fingerprints = _current_official_fingerprints(conn, cohort)
        fingerprints_by_person: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in fingerprints:
            fingerprints_by_person[item["person_id"]].append(item)
        works_by_id = {
            row["openalex_work_id"]: dict(row)
            for row in conn.execute(
                "SELECT openalex_work_id, title, doi FROM openalex_works"
            ).fetchall()
        }

        links: list[dict[str, Any]] = []
        work_reference_provenance: list[dict[str, str]] = []

        for review in single_review["reviews"]:
            person_id = review["person_id"]
            classification = review["classification"]
            base = {
                "person_id": person_id,
                "institution_id": INSTITUTION_ID,
                "expected_display_name": records[person_id]["display_name"],
                "reason": (
                    f"Singleton audit classification {classification}: {review['reason']}"
                ),
            }
            if classification == "official_evidence_only_required":
                official_works: list[dict[str, str]] = []
                selected_work_ids: set[str] = set()
                for match in review.get("matched_official_titles") or []:
                    selected_work_ids.add(match["selected_work_id"])
                    reference, provenance = _reviewed_work_reference(
                        person_id=person_id,
                        work_id=match["selected_work_id"],
                        fingerprints_by_person=fingerprints_by_person,
                        works_by_id=works_by_id,
                        hinted_official_title=match.get("official_title"),
                    )
                    official_works.append(reference)
                    work_reference_provenance.append(
                        {
                            "person_id": person_id,
                            "openalex_work_id": match["selected_work_id"],
                            "provenance": provenance,
                        }
                    )
                for work_id in EXPLICIT_OFFICIAL_WORK_IDS.get(person_id, []):
                    if work_id in selected_work_ids:
                        continue
                    reference, provenance = _reviewed_work_reference(
                        person_id=person_id,
                        work_id=work_id,
                        fingerprints_by_person=fingerprints_by_person,
                        works_by_id=works_by_id,
                    )
                    official_works.append(reference)
                    work_reference_provenance.append(
                        {
                            "person_id": person_id,
                            "openalex_work_id": work_id,
                            "provenance": provenance,
                        }
                    )
                base.update(
                    {
                        "sync_mode": "official_evidence_only",
                        "primary_openalex_author_id": None,
                        "confirmed_openalex_author_ids": [],
                        "official_works": official_works,
                    }
                )
            else:
                author_id = review["openalex_author_id"]
                base.update(
                    {
                        "sync_mode": "full_profile",
                        "primary_openalex_author_id": author_id,
                        "confirmed_openalex_author_ids": [author_id],
                    }
                )
                if classification == "field_allowlist_required":
                    base["work_policy"] = {
                        "mode": "field_allowlist",
                        "fields": list(review["recommended_fields"]),
                        "always_include_work_ids": list(
                            review.get("always_include_work_ids") or []
                        ),
                    }
            links.append(base)

        for case in cases:
            person_id = case["person_id"]
            keep_ids, filter_ids = _case_profile_ids(case)
            mode = "official_evidence_only" if person_id in OFFICIAL_ONLY_CASE_IDS else (
                "field_allowlist" if person_id in FIELD_CASES else "full_profile"
            )
            base = {
                "person_id": person_id,
                "institution_id": INSTITUTION_ID,
                "expected_display_name": records[person_id]["display_name"],
                "reason": CASE_REASON_OVERRIDES.get(
                    person_id,
                    (
                        f"Split/manual audit reviewed policy {mode}; K={keep_ids}, "
                        f"KF={filter_ids}. "
                        f"{case.get('recommended_person_policy') or case.get('case_reason') or case.get('recommended_policy') or ''}"
                    ).strip(),
                ),
            }
            if mode == "official_evidence_only":
                work_ids = list(EXPLICIT_OFFICIAL_WORK_IDS.get(person_id, []))
                if person_id == "pi_a21466932ad41696":
                    work_ids = _one_local_reference_per_official_title(
                        person_id=person_id,
                        preferred_work_ids=["W3200381836", "W3201670330"],
                        fingerprints_by_person=fingerprints_by_person,
                        works_by_id=works_by_id,
                    )
                official_works = []
                for work_id in work_ids:
                    reference, provenance = _reviewed_work_reference(
                        person_id=person_id,
                        work_id=work_id,
                        fingerprints_by_person=fingerprints_by_person,
                        works_by_id=works_by_id,
                    )
                    official_works.append(reference)
                    work_reference_provenance.append(
                        {
                            "person_id": person_id,
                            "openalex_work_id": work_id,
                            "provenance": provenance,
                        }
                    )
                base.update(
                    {
                        "sync_mode": "official_evidence_only",
                        "primary_openalex_author_id": None,
                        "confirmed_openalex_author_ids": [],
                        "official_works": official_works,
                    }
                )
            else:
                author_ids = list(
                    CASE_AUTHOR_OVERRIDES.get(person_id, keep_ids + filter_ids)
                )
                author_ids = list(dict.fromkeys(author_ids))
                if not author_ids:
                    raise ValueError(f"reviewed full/field case has no Author IDs: {person_id}")
                original_primary = case.get("primary_profile_id_in_original_sync")
                primary = original_primary if original_primary in author_ids else author_ids[0]
                ordered_ids = [primary] + [value for value in author_ids if value != primary]
                base.update(
                    {
                        "sync_mode": "full_profile",
                        "primary_openalex_author_id": primary,
                        "confirmed_openalex_author_ids": ordered_ids,
                    }
                )
                if mode == "field_allowlist":
                    always = _case_profile_official_work_ids(case, set(author_ids))
                    base["work_policy"] = {
                        "mode": "field_allowlist",
                        "fields": FIELD_CASES[person_id],
                        "always_include_work_ids": always,
                    }
            links.append(base)

        yang_works = []
        for work_id in KNOWN_EXTERNAL_WORKS[BUSINESS_YANG_LIU_ID]:
            reference, provenance = _reviewed_work_reference(
                person_id=BUSINESS_YANG_LIU_ID,
                work_id=work_id,
                fingerprints_by_person=fingerprints_by_person,
                works_by_id=works_by_id,
            )
            yang_works.append(reference)
            work_reference_provenance.append(
                {
                    "person_id": BUSINESS_YANG_LIU_ID,
                    "openalex_work_id": work_id,
                    "provenance": provenance,
                }
            )
        links.append(
            {
                "person_id": BUSINESS_YANG_LIU_ID,
                "institution_id": INSTITUTION_ID,
                "expected_display_name": records[BUSINESS_YANG_LIU_ID]["display_name"],
                "sync_mode": "official_evidence_only",
                "primary_openalex_author_id": None,
                "confirmed_openalex_author_ids": [],
                "official_works": yang_works,
                "reason": (
                    "Reviewed Business-school Yang LIU decision after splitting the prior "
                    "same-name biomedical merge; no safe whole OpenAlex Author profile is "
                    "confirmed, so only nine reviewed official Works are authoritative."
                ),
            }
        )

        for link in links:
            person_id = link["person_id"]
            aliases = AUTHORSHIP_NAME_ALIAS_OVERRIDES.get(person_id)
            if aliases:
                link["authorship_name_aliases"] = list(aliases)
            exact_work_ids = EXACT_WORK_POLICY_OVERRIDES.get(person_id)
            if exact_work_ids:
                link["work_policy"] = {
                    "mode": "exact_work_allowlist",
                    "work_ids": list(exact_work_ids),
                }
                link["reason"] += (
                    " The final live safety review restricts this merged profile to an "
                    "exact, individually reviewed Work allowlist."
                )
            field_override = FIELD_WORK_POLICY_OVERRIDES.get(person_id)
            if field_override:
                link["work_policy"] = {
                    "mode": "field_allowlist",
                    "fields": list(field_override["fields"]),
                    "always_include_work_ids": list(
                        field_override["always_include_work_ids"]
                    ),
                }
                link["reason"] += (
                    " The final live safety review excludes two unrelated Computer "
                    "Science records from the otherwise coherent profile."
                )

        links.sort(key=lambda item: item["person_id"])
        link_person_ids = [item["person_id"] for item in links]
        if len(links) != 124 or set(link_person_ids) != set(cohort):
            missing = sorted(set(cohort) - set(link_person_ids))
            extra = sorted(set(link_person_ids) - set(cohort))
            duplicates = sorted(
                person_id
                for person_id, count in Counter(link_person_ids).items()
                if count > 1
            )
            raise ValueError(
                f"manifest cohort mismatch: missing={missing}, extra={extra}, "
                f"duplicates={duplicates}"
            )

        claimed_authors: dict[str, str] = {}
        duplicate_authors: list[dict[str, str]] = []
        for link in links:
            for author_id in link.get("confirmed_openalex_author_ids") or []:
                previous = claimed_authors.get(author_id)
                if previous is not None and previous != link["person_id"]:
                    duplicate_authors.append(
                        {
                            "openalex_author_id": author_id,
                            "first_person_id": previous,
                            "second_person_id": link["person_id"],
                        }
                    )
                claimed_authors[author_id] = link["person_id"]
        if duplicate_authors:
            raise ValueError(f"duplicate reviewed Author assignments: {duplicate_authors}")

        generated_at = _utc_now()
        manifest = {
            "schema_version": 1,
            "audit_type": "reviewed_openalex_identity_manifest",
            "reviewed_at": generated_at,
            "reviewed_by": "codex_hkubs_openalex_review_20260715",
            "links": links,
        }
        MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST_PATH.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        retry_manifest = {
            **{key: value for key, value in manifest.items() if key != "links"},
            "reviewed_by": "codex_hkubs_openalex_retry_review_20260715",
            "links": [
                link for link in links if link["person_id"] in set(RETRY_PERSON_IDS)
            ],
        }
        if len(retry_manifest["links"]) != len(RETRY_PERSON_IDS):
            raise ValueError("retry manifest does not contain the exact four failed PIs")
        RETRY_MANIFEST_PATH.write_text(
            json.dumps(retry_manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        resolution_manifest = {
            **{key: value for key, value in manifest.items() if key != "links"},
            "reviewed_by": "codex_hkubs_openalex_resolution_review_20260715",
            "links": [
                link
                for link in links
                if link["person_id"] in set(RESOLUTION_PERSON_IDS)
            ],
        }
        if len(resolution_manifest["links"]) != len(RESOLUTION_PERSON_IDS):
            raise ValueError(
                "resolution manifest does not contain the exact three affected PIs"
            )
        RESOLUTION_MANIFEST_PATH.write_text(
            json.dumps(resolution_manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        # Validate with the production sync manifest loader and preflight while
        # keeping the pilot SQLite connection strictly read-only.
        from pi_index.pipeline.sync_openalex_publications import (
            _load_reviewed_openalex_identity_manifest,
            _preflight_reviewed_openalex_identities,
            _selected_records,
        )

        readonly_storage = _ReadOnlyPreflightStorage(conn)
        decisions = _load_reviewed_openalex_identity_manifest(MANIFEST_PATH)
        selected_records = _selected_records(
            readonly_storage,
            person_ids=cohort,
            institution_id=INSTITUTION_ID,
            department_patterns=None,
            limit=None,
        )
        _preflight_reviewed_openalex_identities(
            readonly_storage,
            selected_records,
            INSTITUTION_ID,
            decisions,
        )
        retry_decisions = _load_reviewed_openalex_identity_manifest(
            RETRY_MANIFEST_PATH
        )
        retry_records = _selected_records(
            readonly_storage,
            person_ids=list(RETRY_PERSON_IDS),
            institution_id=INSTITUTION_ID,
            department_patterns=None,
            limit=None,
        )
        _preflight_reviewed_openalex_identities(
            readonly_storage,
            retry_records,
            INSTITUTION_ID,
            retry_decisions,
        )
        resolution_decisions = _load_reviewed_openalex_identity_manifest(
            RESOLUTION_MANIFEST_PATH
        )
        resolution_records = _selected_records(
            readonly_storage,
            person_ids=list(RESOLUTION_PERSON_IDS),
            institution_id=INSTITUTION_ID,
            department_patterns=None,
            limit=None,
        )
        _preflight_reviewed_openalex_identities(
            readonly_storage,
            resolution_records,
            INSTITUTION_ID,
            resolution_decisions,
        )

        local_work_titles = {
            _normalize_title(work.get("title"))
            for work in works_by_id.values()
            if _normalize_title(work.get("title"))
        }
        local_work_dois = {
            _normalize_doi(work.get("doi"))
            for work in works_by_id.values()
            if _normalize_doi(work.get("doi"))
        }
        local_matched_fingerprint_ids = {
            item["fingerprint_id"]
            for item in fingerprints
            if (
                _normalize_title(item.get("title")) in local_work_titles
                or (
                    _normalize_doi(item.get("doi"))
                    and _normalize_doi(item.get("doi")) in local_work_dois
                )
            )
        }
        local_unmatched_by_person: dict[str, list[str]] = defaultdict(list)
        for item in fingerprints:
            if item["fingerprint_id"] not in local_matched_fingerprint_ids:
                local_unmatched_by_person[item["person_id"]].append(item["title"])

        official_only_unmatched: list[dict[str, Any]] = []
        for link in links:
            if link["sync_mode"] != "official_evidence_only":
                continue
            approved_titles = {
                _normalize_title(item["expected_title"])
                for item in link["official_works"]
            }
            unmatched_titles = sorted(
                {
                    item["title"]
                    for item in fingerprints_by_person[link["person_id"]]
                    if _normalize_title(item["title"]) not in approved_titles
                },
                key=str.casefold,
            )
            if unmatched_titles:
                official_only_unmatched.append(
                    {
                        "person_id": link["person_id"],
                        "display_name": link["expected_display_name"],
                        "titles": unmatched_titles,
                    }
                )

        policy_counts = Counter(
            "official_evidence_only"
            if link["sync_mode"] == "official_evidence_only"
            else (
                str(link["work_policy"]["mode"])
                if link.get("work_policy") is not None
                else "full_profile"
            )
            for link in links
        )
        provenance_counts = Counter(
            item["provenance"] for item in work_reference_provenance
        )
        audit = {
            "schema_version": 1,
            "audit_type": "hkubs_reviewed_openalex_identity_manifest_synthesis",
            "generated_at": generated_at,
            "manifest": {
                "path": str(MANIFEST_PATH.relative_to(ROOT)).replace("\\", "/"),
                "sha256": _sha256(MANIFEST_PATH),
                "loader_validation": "passed",
                "pilot_read_only_preflight": "passed",
            },
            "retry_manifest": {
                "path": str(RETRY_MANIFEST_PATH.relative_to(ROOT)).replace("\\", "/"),
                "sha256": _sha256(RETRY_MANIFEST_PATH),
                "link_count": len(retry_manifest["links"]),
                "person_ids": list(RETRY_PERSON_IDS),
                "loader_validation": "passed",
                "pilot_read_only_preflight": "passed",
            },
            "resolution_manifest": {
                "path": str(RESOLUTION_MANIFEST_PATH.relative_to(ROOT)).replace(
                    "\\", "/"
                ),
                "sha256": _sha256(RESOLUTION_MANIFEST_PATH),
                "link_count": len(resolution_manifest["links"]),
                "person_ids": list(RESOLUTION_PERSON_IDS),
                "loader_validation": "passed",
                "pilot_read_only_preflight": "passed",
            },
            "inputs": [
                {
                    "path": str(path.relative_to(ROOT)).replace("\\", "/"),
                    "sha256": _sha256(path),
                }
                for path in (COHORT_PATH, SINGLE_REVIEW_PATH, SPLIT_REVIEW_PATH, DB_PATH)
            ],
            "cohort_coverage": {
                "expected": 124,
                "manifest_links": len(links),
                "unique_person_ids": len(set(link_person_ids)),
                "missing_person_ids": sorted(set(cohort) - set(link_person_ids)),
                "extra_person_ids": sorted(set(link_person_ids) - set(cohort)),
                "duplicate_person_ids": sorted(
                    person_id
                    for person_id, count in Counter(link_person_ids).items()
                    if count > 1
                ),
            },
            "policy_counts": dict(sorted(policy_counts.items())),
            "author_id_uniqueness": {
                "confirmed_author_id_count": len(claimed_authors),
                "duplicate_assignments": duplicate_authors,
                "official_evidence_only_has_no_author_ids": all(
                    not link.get("confirmed_openalex_author_ids")
                    and link.get("primary_openalex_author_id") is None
                    for link in links
                    if link["sync_mode"] == "official_evidence_only"
                ),
            },
            "official_work_coverage": {
                "active_official_fingerprint_count": len(fingerprints),
                "official_evidence_only_reference_count": len(
                    work_reference_provenance
                ),
                "reference_provenance_counts": dict(sorted(provenance_counts.items())),
                "locally_exact_matched_fingerprint_count": len(
                    local_matched_fingerprint_ids
                ),
                "locally_unmatched_fingerprint_count": (
                    len(fingerprints) - len(local_matched_fingerprint_ids)
                ),
                "official_evidence_only_unmatched_title_count": sum(
                    len(item["titles"]) for item in official_only_unmatched
                ),
            },
            "official_evidence_only_unmatched_titles": official_only_unmatched,
            "all_cohort_locally_unmatched_official_titles": [
                {
                    "person_id": person_id,
                    "display_name": records[person_id]["display_name"],
                    "titles": sorted(set(titles), key=str.casefold),
                }
                for person_id, titles in sorted(local_unmatched_by_person.items())
            ],
            "reviewed_work_reference_provenance": work_reference_provenance,
            "explicit_corrections": {
                "business_yang_liu_person_id": BUSINESS_YANG_LIU_ID,
                "business_yang_liu_confirmed_author_ids": [],
                "business_yang_liu_unmatched_official_titles": [],
                "business_yang_liu_newly_resolved_official_work_ids": [
                    "W4406073117",
                    "W4210509393",
                ],
                "hong_zou_retained_author_ids": ["A5002252532"],
                "hong_zou_excluded_author_ids": [
                    "A5113756416",
                    "A5106276864",
                    "A5022896232",
                ],
                "reviewed_authorship_name_aliases": (
                    AUTHORSHIP_NAME_ALIAS_OVERRIDES
                ),
                "exact_work_policy_counts": {
                    person_id: len(work_ids)
                    for person_id, work_ids in EXACT_WORK_POLICY_OVERRIDES.items()
                },
            },
        }
        AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        AUDIT_PATH.write_text(
            json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(audit["cohort_coverage"], ensure_ascii=False))
        print(json.dumps(audit["policy_counts"], ensure_ascii=False))
        print(json.dumps(audit["author_id_uniqueness"], ensure_ascii=False))
        print(json.dumps(audit["official_work_coverage"], ensure_ascii=False))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
