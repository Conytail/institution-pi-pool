from __future__ import annotations

import pytest

from pi_index import cli


def test_openalex_sync_parser_requires_institution_id():
    with pytest.raises(SystemExit) as exc_info:
        cli.build_parser().parse_args(
            ["sync-openalex-publications", "--person-id", "pi_one"]
        )

    assert exc_info.value.code == 2


def test_openalex_sync_rejects_unscoped_request_before_opening_database(monkeypatch):
    opened = []
    printed = []
    monkeypatch.setattr(cli, "_storage", lambda _args: opened.append(True))
    monkeypatch.setattr(cli, "_print_json", printed.append)
    monkeypatch.setattr(cli, "setup_logging", lambda _verbose: None)

    assert (
        cli.main(
            [
                "sync-openalex-publications",
                "--institution-id",
                "inst_hku",
            ]
        )
        == 2
    )
    assert opened == []
    assert printed == [
        {
            "status": "scope_error",
            "error": "Supply at least one --person-id or --department selector",
        }
    ]


def test_openalex_sync_forwards_explicit_scope_and_closes_storage(monkeypatch, tmp_path):
    class FakeStorage:
        closed = False

        def close(self):
            self.closed = True

    storage = FakeStorage()
    captured = {}
    printed = []

    def fake_sync(actual_storage, **kwargs):
        captured.update({"storage": actual_storage, **kwargs})
        return {"status": "success"}

    monkeypatch.setattr(cli, "_storage", lambda _args: storage)
    monkeypatch.setattr(cli, "sync_openalex_publications", fake_sync)
    monkeypatch.setattr(cli, "_print_json", printed.append)
    monkeypatch.setattr(cli, "setup_logging", lambda _verbose: None)
    person_file = tmp_path / "people.txt"
    person_file.write_text("# exact cohort\npi_two\npi_one\n", encoding="utf-8")

    assert (
        cli.main(
            [
                "sync-openalex-publications",
                "--db",
                "pool.db",
                "--institution-id",
                "inst_hku",
                "--department",
                "Faculty of Engineering",
                "--person-id",
                "pi_one",
                "--person-id-file",
                str(person_file),
                "--limit",
                "1",
                "--full",
                "--dry-run",
                "--premium-updated-filter",
                "--missing-confirmations",
                "3",
                "--max-author-works",
                "1500",
                "--reviewed-identity-manifest",
                "reviewed-identities.json",
                "--revalidate-identities",
                "--out",
                str(tmp_path / "sync-result.json"),
            ]
        )
        == 0
    )
    assert captured == {
        "storage": storage,
        "person_ids": ["pi_one", "pi_two"],
        "institution_id": "inst_hku",
        "department_patterns": ["Faculty of Engineering"],
        "limit": 1,
        "full": True,
        "dry_run": True,
        "premium_updated_filter": True,
        "missing_runs_before_tombstone": 3,
        "max_author_works": 1500,
        "reviewed_identity_manifest": "reviewed-identities.json",
        "revalidate_identities": True,
    }
    assert storage.closed is True
    assert printed == [{"status": "success"}]
    assert (tmp_path / "sync-result.json").read_text(encoding="utf-8") == (
        '{\n  "status": "success"\n}\n'
    )


def test_openalex_sync_rejects_invalid_author_work_guard_before_database(monkeypatch):
    opened = []
    printed = []
    monkeypatch.setattr(cli, "_storage", lambda _args: opened.append(True))
    monkeypatch.setattr(cli, "_print_json", printed.append)
    monkeypatch.setattr(cli, "setup_logging", lambda _verbose: None)

    assert (
        cli.main(
            [
                "sync-openalex-publications",
                "--institution-id",
                "inst_hku",
                "--person-id",
                "pi_one",
                "--max-author-works",
                "0",
            ]
        )
        == 2
    )
    assert opened == []
    assert printed == [
        {"status": "scope_error", "error": "--max-author-works must be at least 1"}
    ]


def test_vector_cli_forwards_exact_person_file_scope(monkeypatch, tmp_path):
    class FakeStorage:
        closed = False

        def close(self):
            self.closed = True

    storage = FakeStorage()
    captured = {}
    printed = []
    people = tmp_path / "cohort.json"
    people.write_text('{"person_ids":["pi_1","pi_2","pi_1"]}', encoding="utf-8")

    def fake_process(actual_storage, **kwargs):
        captured.update({"storage": actual_storage, **kwargs})
        return {"status": "success"}

    monkeypatch.setattr(cli, "_storage", lambda _args: storage)
    monkeypatch.setattr(cli, "process_vector_queue", fake_process)
    monkeypatch.setattr(cli, "_print_json", printed.append)
    monkeypatch.setattr(cli, "setup_logging", lambda _verbose: None)

    assert cli.main(
        [
            "build-research-vectors",
            "--person-id-file",
            str(people),
            "--batch-size",
            "7",
            "--owner",
            "cohort-worker",
            "--lease-seconds",
            "60",
            "--max-attempts",
            "4",
            "--out",
            str(tmp_path / "vector-result.json"),
        ]
    ) == 0
    assert captured == {
        "storage": storage,
        "limit": None,
        "batch_size": 7,
        "owner": "cohort-worker",
        "lease_seconds": 60.0,
        "max_attempts": 4,
        "person_ids": ["pi_1", "pi_2"],
    }
    assert storage.closed is True
    assert printed == [{"status": "success"}]
    assert (tmp_path / "vector-result.json").read_text(encoding="utf-8") == (
        '{\n  "status": "success"\n}\n'
    )
