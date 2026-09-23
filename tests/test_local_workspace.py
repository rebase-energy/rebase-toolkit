from __future__ import annotations

import json
from pathlib import Path

from rebase.config import (
    find_local_config,
    local_workspace_id,
    local_workspace_mismatch,
    profile_for_workspace,
    read_local_config,
    selected_profile_name,
    write_local_config,
)
from rebase.setup import _directory_handle_suggestion, _prompt


def _write_global(config_path: Path, data: dict) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(data), encoding="utf-8")


def test_write_local_config_creates_marker(tmp_path: Path) -> None:
    marker = write_local_config(tmp_path, workspace_id="agent-work", workspace_name="Agent Work")

    assert marker == tmp_path / ".rebase" / "config.json"
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "workspace": "agent-work",
        "workspace_name": "Agent Work",
    }


def test_write_local_config_preserves_unknown_keys(tmp_path: Path) -> None:
    marker_dir = tmp_path / ".rebase"
    marker_dir.mkdir()
    (marker_dir / "config.json").write_text(json.dumps({"custom": "keep me"}), encoding="utf-8")

    write_local_config(tmp_path, workspace_id="agent-work")

    data = json.loads((marker_dir / "config.json").read_text(encoding="utf-8"))
    assert data["custom"] == "keep me"
    assert data["workspace"] == "agent-work"


def test_find_local_config_walks_up_to_repo_root(tmp_path: Path, monkeypatch) -> None:
    write_local_config(tmp_path, workspace_id="agent-work")
    nested = tmp_path / "agent_work" / "accounting"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    assert find_local_config() == tmp_path / ".rebase" / "config.json"
    assert local_workspace_id() == "agent-work"


def test_find_local_config_returns_none_when_unmarked(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    assert find_local_config() is None
    assert local_workspace_id() is None


def test_find_local_config_ignores_the_global_config(tmp_path: Path, monkeypatch) -> None:
    """`~/.rebase/config.json` sits on the parent walk of every repo under home.

    It holds credentials, not a workspace pin, so treating it as a marker would make
    every unmarked directory resolve to whatever is in it.
    """
    home = tmp_path / "home"
    _write_global(home / ".rebase" / "config.json", {"default_profile": "default"})
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(home / ".rebase" / "config.json"))
    workdir = home / "repos" / "unmarked"
    workdir.mkdir(parents=True)
    monkeypatch.chdir(workdir)

    assert find_local_config() is None


def test_malformed_marker_degrades_to_empty(tmp_path: Path, monkeypatch) -> None:
    marker_dir = tmp_path / ".rebase"
    marker_dir.mkdir()
    (marker_dir / "config.json").write_text("{not json", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert read_local_config() == {}
    assert local_workspace_id() is None


def test_marker_selects_the_profile_holding_that_workspace(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "global.json"
    _write_global(
        config_path,
        {
            "default_profile": "default",
            "profiles": {
                "default": {"workspace_id": "rebase-grid"},
                "agent": {"workspace_id": "agent-work"},
            },
        },
    )
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))
    repo = tmp_path / "agent-work"
    repo.mkdir()
    write_local_config(repo, workspace_id="agent-work")
    monkeypatch.chdir(repo)

    assert selected_profile_name() == "agent"


def test_explicit_profile_beats_the_marker(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "global.json"
    _write_global(
        config_path,
        {
            "default_profile": "default",
            "profiles": {
                "default": {"workspace_id": "rebase-grid"},
                "agent": {"workspace_id": "agent-work"},
            },
        },
    )
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))
    write_local_config(tmp_path, workspace_id="agent-work")
    monkeypatch.chdir(tmp_path)

    assert selected_profile_name("default") == "default"


def test_unmarked_directory_keeps_the_global_default(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "global.json"
    _write_global(
        config_path,
        {"default_profile": "default", "profiles": {"default": {"workspace_id": "rebase-grid"}}},
    )
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))
    workdir = tmp_path / "elsewhere"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    assert selected_profile_name() == "default"


def test_unreachable_marker_falls_back_and_reports(tmp_path: Path, monkeypatch) -> None:
    """A marker naming a workspace with no local credentials must not break commands."""
    config_path = tmp_path / "global.json"
    _write_global(
        config_path,
        {"default_profile": "default", "profiles": {"default": {"workspace_id": "rebase-grid"}}},
    )
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))
    write_local_config(tmp_path, workspace_id="nobody-has-this")
    monkeypatch.chdir(tmp_path)

    assert selected_profile_name() == "default"
    assert local_workspace_mismatch() == "nobody-has-this"


def test_profile_for_workspace_prefers_the_configured_default(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "global.json"
    _write_global(
        config_path,
        {
            "default_profile": "second",
            "profiles": {
                "first": {"workspace_id": "shared"},
                "second": {"workspace_id": "shared"},
            },
        },
    )
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))

    assert profile_for_workspace("shared") == "second"


def test_directory_handle_suggestion_uses_the_folder_name(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "agent-work"
    repo.mkdir()
    monkeypatch.chdir(repo)

    assert _directory_handle_suggestion() == "agent-work"


def test_directory_handle_suggestion_normalizes(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "My Repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    assert _directory_handle_suggestion() == "my-repo"


def test_directory_handle_suggestion_rejects_invalid_names(tmp_path: Path, monkeypatch) -> None:
    """Too short to be a handle, so the caller's fallback has to stay in play."""
    repo = tmp_path / "ab"
    repo.mkdir()
    monkeypatch.chdir(repo)

    assert _directory_handle_suggestion() is None


def test_prompt_spells_out_the_default(monkeypatch, capsys) -> None:
    monkeypatch.setattr("rebase.setup._read_input", lambda message: print(message, end="") or "")

    assert _prompt(None, "Workspace handle to create", default="agent-work") == "agent-work"
    assert capsys.readouterr().out == 'Workspace handle to create (enter to use "agent-work"): '


def test_prompt_hint_is_uniform_across_prompts(monkeypatch, capsys) -> None:
    """Every prompt carrying a default states it the same way, not just the new one."""
    monkeypatch.setattr("rebase.setup._read_input", lambda message: print(message, end="") or "")

    _prompt(None, "GitHub repository full name", default="rebase-energy/agent-work")

    assert capsys.readouterr().out == 'GitHub repository full name (enter to use "rebase-energy/agent-work"): '


def test_prompt_without_a_default_shows_no_hint(monkeypatch, capsys) -> None:
    monkeypatch.setattr("rebase.setup._read_input", lambda message: print(message, end="") or "value")

    assert _prompt(None, "Workspace handle to join") == "value"
    assert capsys.readouterr().out == "Workspace handle to join: "


def test_prompt_typed_value_beats_the_default(monkeypatch) -> None:
    monkeypatch.setattr("rebase.setup._read_input", lambda message: "typed-handle")

    assert _prompt(None, "Workspace handle to create", default="agent-work") == "typed-handle"
