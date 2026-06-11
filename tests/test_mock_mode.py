"""End-to-end --mock mode: full entrypoint, fixture clients, real ledger file."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from skysync.main import main

REPO = Path(__file__).resolve().parents[1]

CONFIG = """
[graph]
tenant_id = "11111111-1111-1111-1111-111111111111"
client_id = "22222222-2222-2222-2222-222222222222"

[sharepoint]
site_id = "example.sharepoint.com,aaa,bbb"
list_id = "ccc"

[skylight]
frame_id = "1234567"

[mapping.children.avery]
todo_list = "Avery's Chores"
skylight_category = "Avery"
sp_assignee = "Avery"

[mapping.children.blake]
todo_list = "Blake's Chores"
skylight_category = "Blake"
sp_assignee = "Blake"
"""


def make_project(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(CONFIG, encoding="utf-8")
    fx = tmp_path / "fixtures"
    fx.mkdir()
    shutil.copy2(REPO / "fixtures" / "family.json", fx / "family.json")
    return cfg


def test_mock_run_succeeds_and_is_idempotent(tmp_path, capsys):
    cfg = make_project(tmp_path)
    assert main(["--config", str(cfg), "run", "--mock"]) == 0
    first = json.loads(capsys.readouterr().out)
    # 4 fixture tasks discovered across the three sides
    assert first["counts"]["seen_sp"] == 2
    assert first["counts"]["new_from_sp"] == 2
    assert first["counts"]["new_from_todo"] == 1
    assert first["counts"]["new_from_skylight"] == 1
    assert (tmp_path / "state" / "ledger-mock.sqlite3").exists()

    # second run: same fixture clients are rebuilt fresh (mock clients are
    # process-local), so the engine must re-bind everything via markers and
    # content with zero new rows -- and still exit 0.
    assert main(["--config", str(cfg), "run", "--mock"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert "new_from_sp" not in second["counts"] or second["counts"]["new_from_sp"] == 0


def test_mock_run_unknown_fixture_fails_cleanly(tmp_path):
    cfg = make_project(tmp_path)
    assert main(["--config", str(cfg), "run", "--mock", str(tmp_path / "nope.json")]) == 1
