"""`rebase init` — connect a repository to a workspace you already belong to.

The command exists because `rebase setup` conflates two scopes: it authenticates and
may create a workspace, and connecting one repo through it also re-points the
machine's active workspace. These tests pin the boundary — the marker is written, and
nothing else is.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rebase.client import RebaseWorkflowError
from rebase.config import write_local_config, write_profile
from rebase.init import choose_workspace, init_repository, target_directory


class FakeClient:
    """Duck-types the bit of Client this module touches, and records that it stops there."""

    def __init__(self, workspaces: list[dict[str, Any]] | None = None) -> None:
        self.workspaces = (
            workspaces
            if workspaces is not None
            else [
                {"id": "agent-work", "name": "agent-work"},
                {"id": "rebase-grid", "name": "rebase-grid"},
            ]
        )
        self.calls = 0

    def list_my_workspaces(self) -> list[dict[str, Any]]:
        self.calls += 1
        return self.workspaces


def _marker(directory: Path) -> dict[str, Any]:
    return json.loads((directory / ".rebase" / "config.json").read_text(encoding="utf-8"))


def _global_config(tmp_path: Path, monkeypatch) -> Path:
    config = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config))
    write_profile(api_key="rbw_key", workspace={"id": "agent-work"}, path=config)
    return config


def test_writes_the_marker_for_a_named_workspace(tmp_path: Path, monkeypatch) -> None:
    _global_config(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()

    marker = init_repository(workspace="rebase-grid", directory=repo, client=FakeClient())

    assert marker == repo / ".rebase" / "config.json"
    assert _marker(repo) == {"workspace": "rebase-grid", "workspace_name": "rebase-grid"}


def test_leaves_the_machine_config_alone(tmp_path: Path, monkeypatch) -> None:
    """The whole point of the split: connecting a repo is not a machine-wide change."""
    config = _global_config(tmp_path, monkeypatch)
    before = config.read_text(encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()

    init_repository(workspace="rebase-grid", directory=repo, client=FakeClient())

    assert config.read_text(encoding="utf-8") == before


def test_refuses_a_workspace_you_do_not_belong_to(tmp_path: Path, monkeypatch) -> None:
    """It never creates: being pushed into creating one you own is the bug it fixes."""
    _global_config(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(RebaseWorkflowError, match="not a member of workspace someone-elses"):
        init_repository(workspace="someone-elses", directory=repo, client=FakeClient())

    assert not (repo / ".rebase").exists()


def test_running_it_twice_is_a_no_op(tmp_path: Path, monkeypatch) -> None:
    """Safe in a bootstrap script: the second run reports and changes nothing."""
    _global_config(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    client = FakeClient()

    assert init_repository(workspace="rebase-grid", directory=repo, client=client) is not None
    assert init_repository(workspace="rebase-grid", directory=repo, client=client) is None
    # The second run answered from disk without asking the API.
    assert client.calls == 1


def test_changing_the_workspace_needs_force(tmp_path: Path, monkeypatch) -> None:
    _global_config(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    write_local_config(repo, workspace_id="agent-work")

    with pytest.raises(RebaseWorkflowError, match="already connects this directory to workspace agent-work"):
        init_repository(workspace="rebase-grid", directory=repo, client=FakeClient())
    assert _marker(repo)["workspace"] == "agent-work"

    init_repository(workspace="rebase-grid", directory=repo, force=True, client=FakeClient())
    assert _marker(repo)["workspace"] == "rebase-grid"


def test_a_marker_above_counts_as_this_repo_being_connected(tmp_path: Path, monkeypatch) -> None:
    """Resolution walks up, so a subdirectory must not sprout a second marker."""
    _global_config(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    nested = repo / "deploy" / "rebase"
    nested.mkdir(parents=True)
    write_local_config(repo, workspace_id="rebase-grid")

    assert init_repository(workspace="rebase-grid", directory=nested, client=FakeClient()) is None
    assert not (nested / ".rebase").exists()


def test_target_directory_prefers_the_git_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("rebase.init._current_git_root", lambda: tmp_path / "root")
    assert target_directory() == tmp_path / "root"

    monkeypatch.setattr("rebase.init._current_git_root", lambda: None)
    monkeypatch.chdir(tmp_path)
    assert target_directory() == tmp_path.resolve()

    # An explicit directory wins over both.
    assert target_directory(tmp_path / "elsewhere") == tmp_path / "elsewhere"


def test_a_single_workspace_needs_no_prompt(tmp_path: Path) -> None:
    only = [{"id": "rebase-grid", "name": "rebase-grid"}]
    assert choose_workspace(only, requested=None, directory=tmp_path)["id"] == "rebase-grid"


def test_the_folder_name_is_the_default_offered(tmp_path: Path, monkeypatch) -> None:
    """A workspace is 1:1 with a repo, so the folder name is the best guess."""
    seen: dict[str, Any] = {}

    def fake_choose(label: str, values: list[str], *, default: str | None = None, title: str | None = None) -> str:
        seen.update(values=values, default=default, title=title)
        return default or values[0]

    monkeypatch.setattr("rebase.init._choose", fake_choose)
    repo = tmp_path / "rebase-grid"
    repo.mkdir()

    selected = choose_workspace(FakeClient().workspaces, requested=None, directory=repo)

    assert selected["id"] == "rebase-grid"
    assert seen["default"] == "rebase-grid"
    assert seen["title"] == "Connect rebase-grid to which workspace?"


def test_no_workspaces_at_all_points_at_setup(tmp_path: Path) -> None:
    with pytest.raises(RebaseWorkflowError, match="Run `rebase setup`"):
        choose_workspace([], requested=None, directory=tmp_path)
