"""`rebase setup` reuses the profile already signed in on an API instead of minting one per workspace."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rebase import setup as rebase_setup
from rebase.cli import main
from rebase.config import DEFAULT_SERVER_URL, delete_profile, session_profile_for

HOSTED = DEFAULT_SERVER_URL


def _config(tmp_path: Path, monkeypatch, data: dict[str, Any]) -> Path:
    path = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(path))
    monkeypatch.setenv("REBASE_AUTH_FILE", str(tmp_path / "auth.json"))
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _setup_args(**overrides: Any) -> SimpleNamespace:
    args = SimpleNamespace(
        profile=None,
        api_url=None,
        provider=None,
        force_auth=False,
        callback_port=17658,
        auth_timeout=300,
        no_browser=True,
        workspace=None,
        workspace_name=None,
        handle=None,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _fake_setup(monkeypatch, *, workspace: dict[str, Any], api_url: str = HOSTED) -> None:
    class FakeClient:
        def __init__(self, *, api_url: str | None = None, access_token: str | None = None, profile=None) -> None:
            self.api_url = api_url or HOSTED

        def setup_config(self) -> dict[str, Any]:
            return {}

    monkeypatch.setattr(rebase_setup, "Client", FakeClient)
    monkeypatch.setattr(rebase_setup, "_restore_terminal_for_prompts", lambda: None)
    monkeypatch.setattr(rebase_setup, "_intro", lambda: None)
    monkeypatch.setattr(rebase_setup, "_access_token", lambda args, config: "token")
    monkeypatch.setattr(rebase_setup, "load_session", lambda: None)
    monkeypatch.setattr(rebase_setup, "_select_workspace", lambda args, client, *, session: workspace)
    monkeypatch.setattr(rebase_setup, "_create_workspace", lambda args, client, *, session: workspace)
    monkeypatch.setattr(rebase_setup, "_mark_local_workspace", lambda workspace: None)


class TestSessionProfileLookup:
    def test_prefers_the_active_profile_when_it_qualifies(self, tmp_path: Path, monkeypatch) -> None:
        _config(
            tmp_path,
            monkeypatch,
            {
                "default_profile": "work",
                "profiles": {"a": {"api_url": HOSTED}, "work": {"api_url": HOSTED + "/"}},
            },
        )
        assert session_profile_for(None) == "work"

    def test_falls_back_to_the_first_by_name(self, tmp_path: Path, monkeypatch) -> None:
        _config(
            tmp_path,
            monkeypatch,
            {
                "default_profile": "keyed",
                "profiles": {"zed": {}, "beta": {"api_url": HOSTED}, "keyed": {"api_key": "k"}},
            },
        )
        assert session_profile_for(HOSTED) == "beta"

    def test_api_key_profiles_and_other_apis_never_match(self, tmp_path: Path, monkeypatch) -> None:
        _config(
            tmp_path,
            monkeypatch,
            {"profiles": {"keyed": {"api_key": "k", "api_url": HOSTED}, "local": {"api_url": "http://127.0.0.1:8080"}}},
        )
        assert session_profile_for(HOSTED) is None
        assert session_profile_for("http://127.0.0.1:8080/") == "local"


class TestSetupReusesTheProfile:
    def test_moves_the_workspace_on_the_existing_profile(self, tmp_path: Path, monkeypatch, capsys) -> None:
        path = _config(
            tmp_path,
            monkeypatch,
            {"default_profile": "default", "profiles": {"default": {"api_url": HOSTED, "workspace_id": "old"}}},
        )
        _fake_setup(monkeypatch, workspace={"id": "rebase-signals", "name": "Rebase Signals"})

        assert rebase_setup.run_setup(_setup_args()) == 0

        data = _read(path)
        assert set(data["profiles"]) == {"default"}, "no second profile for a second workspace"
        assert data["profiles"]["default"]["workspace_id"] == "rebase-signals"
        assert data["profiles"]["default"]["workspace_name"] == "Rebase Signals"
        assert data["default_profile"] == "default"
        assert "existing Rebase profile 'default'" in capsys.readouterr().out

    def test_reuses_whatever_the_profile_is_called(self, tmp_path: Path, monkeypatch) -> None:
        path = _config(
            tmp_path,
            monkeypatch,
            {"default_profile": "uk-signal", "profiles": {"uk-signal": {"api_url": HOSTED, "workspace_id": "uk"}}},
        )
        _fake_setup(monkeypatch, workspace={"id": "acme", "name": "Acme"})

        assert rebase_setup.run_setup(_setup_args()) == 0

        data = _read(path)
        assert set(data["profiles"]) == {"uk-signal"}
        assert data["profiles"]["uk-signal"]["workspace_id"] == "acme"

    def test_writes_default_when_nothing_is_signed_in_on_this_api(self, tmp_path: Path, monkeypatch) -> None:
        path = _config(tmp_path, monkeypatch, {"profiles": {"keyed": {"api_key": "k", "api_url": HOSTED}}})
        _fake_setup(monkeypatch, workspace={"id": "acme", "name": "Acme"})

        assert rebase_setup.run_setup(_setup_args()) == 0

        data = _read(path)
        assert data["profiles"]["default"] == {"api_url": HOSTED, "workspace_id": "acme", "workspace_name": "Acme"}
        assert data["profiles"]["keyed"]["api_key"] == "k"
        assert data["default_profile"] == "default"

    def test_matches_on_the_api_url_not_the_name(self, tmp_path: Path, monkeypatch) -> None:
        path = _config(
            tmp_path,
            monkeypatch,
            {
                "default_profile": "default",
                "profiles": {"default": {"api_url": HOSTED}, "local": {"api_url": "http://127.0.0.1:8080"}},
            },
        )
        _fake_setup(monkeypatch, workspace={"id": "acme", "name": "Acme"}, api_url="http://127.0.0.1:8080")

        assert rebase_setup.run_setup(_setup_args(api_url="http://127.0.0.1:8080")) == 0

        data = _read(path)
        assert data["profiles"]["default"] == {"api_url": HOSTED}, "the hosted profile is untouched"
        assert data["profiles"]["local"]["workspace_id"] == "acme"
        assert data["default_profile"] == "local"

    def test_explicit_profile_still_creates_one_with_a_hint(self, tmp_path: Path, monkeypatch, capsys) -> None:
        path = _config(
            tmp_path, monkeypatch, {"default_profile": "default", "profiles": {"default": {"api_url": HOSTED}}}
        )
        _fake_setup(monkeypatch, workspace={"id": "acme", "name": "Acme"})

        assert rebase_setup.run_setup(_setup_args(profile="personal")) == 0

        data = _read(path)
        assert set(data["profiles"]) == {"default", "personal"}
        assert data["default_profile"] == "personal"
        out = capsys.readouterr().out
        assert "Profile 'default' already signs in on this API" in out
        assert "Saved Rebase profile 'personal'" in out

    def test_workspace_create_reuses_too(self, tmp_path: Path, monkeypatch) -> None:
        path = _config(
            tmp_path,
            monkeypatch,
            {"default_profile": "default", "profiles": {"default": {"api_url": HOSTED, "workspace_id": "old"}}},
        )
        _fake_setup(monkeypatch, workspace={"id": "fresh", "name": "Fresh"})

        assert rebase_setup.run_workspace_create(_setup_args(workspace="fresh")) == 0

        data = _read(path)
        assert set(data["profiles"]) == {"default"}
        assert data["profiles"]["default"]["workspace_id"] == "fresh"


class TestProfileListAndDelete:
    def test_list_shows_the_api_so_duplicates_look_identical(self, tmp_path: Path, monkeypatch, capsys) -> None:
        _config(
            tmp_path,
            monkeypatch,
            {
                "default_profile": "default",
                "profiles": {
                    "default": {"api_url": HOSTED, "workspace_id": "a"},
                    "uk-signal": {"api_url": HOSTED, "workspace_id": "b"},
                    "local": {"api_url": "http://127.0.0.1:8080", "workspace_id": "c"},
                },
            },
        )
        assert main(["profile", "list"]) == 0
        out = capsys.readouterr().out
        assert out.count("hosted") == 2
        assert "http://127.0.0.1:8080" in out

    def test_delete_removes_an_inactive_profile(self, tmp_path: Path, monkeypatch, capsys) -> None:
        path = _config(
            tmp_path,
            monkeypatch,
            {
                "default_profile": "default",
                "profiles": {"default": {"api_url": HOSTED}, "uk-signal": {"api_url": HOSTED}},
            },
        )
        assert main(["profile", "delete", "uk-signal", "--yes"]) == 0
        assert set(_read(path)["profiles"]) == {"default"}
        assert "Deleted profile" in capsys.readouterr().out

    def test_delete_refuses_the_active_profile(self, tmp_path: Path, monkeypatch, capsys) -> None:
        path = _config(tmp_path, monkeypatch, {"default_profile": "default", "profiles": {"default": {}, "x": {}}})
        assert main(["profile", "delete", "default", "--yes"]) == 1
        assert set(_read(path)["profiles"]) == {"default", "x"}
        assert "rebase profile switch" in capsys.readouterr().err

    def test_delete_unknown_profile(self, tmp_path: Path, monkeypatch, capsys) -> None:
        _config(tmp_path, monkeypatch, {"default_profile": "default", "profiles": {"default": {}}})
        assert main(["profile", "delete", "nope", "--yes"]) == 1
        assert "unknown profile" in capsys.readouterr().err

    def test_delete_profile_helper_guards(self, tmp_path: Path, monkeypatch) -> None:
        path = _config(tmp_path, monkeypatch, {"default_profile": "default", "profiles": {"default": {}}})
        with pytest.raises(ValueError):
            delete_profile("default", path=path)
        with pytest.raises(KeyError):
            delete_profile("missing", path=path)
