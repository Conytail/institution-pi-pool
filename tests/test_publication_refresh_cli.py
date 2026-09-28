from __future__ import annotations

from pi_index import cli


def test_refresh_official_publications_parser_maps_all_options(tmp_path):
    database = tmp_path / "pool.db"
    args = cli.build_parser().parse_args(
        [
            "refresh-official-publications",
            "--config",
            "configs/institutions/example.yaml",
            "--db",
            str(database),
            "--archive-root",
            str(tmp_path / "archive"),
            "--crawl-policy",
            "configs/test-crawl-policy.yaml",
            "--person-id",
            "pi_one",
            "--person-id",
            "pi_two",
            "--department",
            "Faculty of Engineering",
            "--limit",
            "17",
            "--due-only",
            "--dry-run",
            "--offline",
            "--missing-confirmations",
            "3",
            "--workers",
            "4",
        ]
    )

    assert args.func is cli.cmd_refresh_official_publications
    assert args.config == "configs/institutions/example.yaml"
    assert args.db == str(database)
    assert args.archive_root == str(tmp_path / "archive")
    assert args.crawl_policy == "configs/test-crawl-policy.yaml"
    assert args.person_id == ["pi_one", "pi_two"]
    assert args.department == ["Faculty of Engineering"]
    assert args.limit == 17
    assert args.due_only is True
    assert args.dry_run is True
    assert args.offline is True
    assert args.missing_confirmations == 3
    assert args.workers == 4


def test_refresh_official_publications_command_forwards_arguments_and_closes_storage(
    tmp_path,
    monkeypatch,
):
    class FakeStorage:
        closed = False

        def close(self):
            self.closed = True

    storage = FakeStorage()
    captured = {}

    def fake_refresh(config, actual_storage, **kwargs):
        captured.update(
            {
                "config": config,
                "storage": actual_storage,
                **kwargs,
            }
        )
        return {"status": "success"}

    printed = []
    monkeypatch.setattr(cli, "_storage", lambda _args: storage)
    monkeypatch.setattr(cli, "refresh_official_publications", fake_refresh)
    monkeypatch.setattr(cli, "_print_json", printed.append)
    monkeypatch.setattr(cli, "setup_logging", lambda _verbose: None)

    exit_code = cli.main(
        [
            "refresh-official-publications",
            "--config",
            "example.yaml",
            "--db",
            str(tmp_path / "pool.db"),
            "--archive-root",
            str(tmp_path / "archive"),
            "--crawl-policy",
            "policy.yaml",
            "--person-id",
            "pi_one",
            "--person-id",
            "pi_two",
            "--department",
            "Faculty of Engineering",
            "--limit",
            "9",
            "--due-only",
            "--dry-run",
            "--offline",
            "--missing-confirmations",
            "4",
            "--workers",
            "3",
        ]
    )

    assert exit_code == 0
    assert captured == {
        "config": "example.yaml",
        "storage": storage,
        "archive_root": str(tmp_path / "archive"),
        "crawl_policy_path": "policy.yaml",
        "person_ids": ["pi_one", "pi_two"],
        "department_patterns": ["Faculty of Engineering"],
        "limit": 9,
        "due_only": True,
        "dry_run": True,
        "offline": True,
        "missing_confirmations": 4,
        "allow_single_confirmation_removal": False,
        "workers": 3,
    }
    assert storage.closed is True
    assert printed == [{"status": "success"}]


def test_refresh_official_publications_command_returns_nonzero_for_partial(monkeypatch):
    class FakeStorage:
        def close(self):
            pass

    monkeypatch.setattr(cli, "_storage", lambda _args: FakeStorage())
    monkeypatch.setattr(
        cli,
        "refresh_official_publications",
        lambda *_args, **_kwargs: {"status": "partial", "sources_usable": 1},
    )
    monkeypatch.setattr(cli, "_print_json", lambda _value: None)
    monkeypatch.setattr(cli, "setup_logging", lambda _verbose: None)

    assert cli.main(
        [
            "refresh-official-publications",
            "--config",
            "example.yaml",
            "--person-id",
            "pi_one",
        ]
    ) == 2


def test_refresh_command_rejects_unscoped_request_before_opening_database(monkeypatch):
    opened = []
    printed = []
    monkeypatch.setattr(cli, "_storage", lambda _args: opened.append(True))
    monkeypatch.setattr(cli, "_print_json", printed.append)
    monkeypatch.setattr(cli, "setup_logging", lambda _verbose: None)

    assert cli.main(
        ["refresh-official-publications", "--config", "example.yaml"]
    ) == 2
    assert opened == []
    assert printed[0]["status"] == "failed"
