from __future__ import annotations

import json
from pathlib import Path

import pytest

from rebase import auth as auth_module
from rebase.auth import AuthSession, auth_file_path, load_session, refresh_session
from rebase.config import (
    DEFAULT_PLATFORM,
    active_platform,
    config_path,
    find_local_config,
    is_default_platform,
    load_profile,
    write_profile,
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """A fresh home with no pointer, no env override, and no explicit config paths."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for name in ("REBASE_PLATFORM", "REBASE_CONFIG_PATH", "REBASE_AUTH_FILE", "REBASE_WORKFLOWS_AUTH_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return home


def _point(home: Path, name: str) -> None:
    (home / ".rebase").mkdir(exist_ok=True)
    (home / ".rebase" / "platform").write_text(f"{name}\n", encoding="utf-8")


def test_active_platform_is_production_without_pointer_or_env(home: Path) -> None:
    assert active_platform() == DEFAULT_PLATFORM == "production"
    assert is_default_platform()
    assert config_path() == home / ".rebase" / "config.json"
    assert auth_file_path() == home / ".config" / "rebase" / "auth.json"


def test_active_platform_reads_the_pointer_file(home: Path) -> None:
    _point(home, "staging")

    assert active_platform() == "staging"
    assert not is_default_platform()
    assert config_path() == home / ".rebase" / "platforms" / "staging" / "config.json"
    assert auth_file_path() == home / ".config" / "rebase" / "platforms" / "staging" / "auth.json"


def test_env_override_beats_the_pointer_file(home: Path, monkeypatch) -> None:
    _point(home, "staging")
    monkeypatch.setenv("REBASE_PLATFORM", "dev")

    assert active_platform() == "dev"
    assert config_path() == home / ".rebase" / "platforms" / "dev" / "config.json"


def test_pointer_named_production_or_empty_is_the_default(home: Path) -> None:
    _point(home, "production")
    assert is_default_platform()
    assert config_path() == home / ".rebase" / "config.json"

    _point(home, "")
    assert is_default_platform()


@pytest.mark.parametrize("name", ["../x", "Staging", "sta ging", "-x", "a" * 40])
def test_invalid_pointer_name_fails_loudly(home: Path, name: str) -> None:
    """Silently falling back to production is the one thing the pointer must never do."""
    _point(home, name)

    with pytest.raises(RuntimeError, match="invalid platform name"):
        active_platform()
    with pytest.raises(RuntimeError):
        config_path()


def test_explicit_path_overrides_beat_the_platform(home: Path, monkeypatch) -> None:
    _point(home, "staging")
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(home / "elsewhere.json"))
    monkeypatch.setenv("REBASE_AUTH_FILE", str(home / "elsewhere-auth.json"))

    assert config_path() == home / "elsewhere.json"
    assert auth_file_path() == home / "elsewhere-auth.json"


def test_auth_file_honours_xdg_config_home_under_a_platform(home: Path, monkeypatch) -> None:
    _point(home, "staging")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))

    assert auth_file_path() == home / "xdg" / "rebase" / "platforms" / "staging" / "auth.json"


def test_find_local_config_still_skips_the_global_config_under_a_platform(home: Path, monkeypatch) -> None:
    _point(home, "staging")
    (home / ".rebase" / "config.json").write_text(json.dumps({"default_profile": "default"}), encoding="utf-8")
    write_profile(api_key="rbw_staging", workspace={"id": "ws"})
    workdir = home / "repos" / "unmarked"
    workdir.mkdir(parents=True)
    monkeypatch.chdir(workdir)

    assert find_local_config() is None


def test_write_profile_under_a_platform_leaves_the_production_config_untouched(home: Path) -> None:
    production = home / ".rebase" / "config.json"
    production.parent.mkdir()
    before = json.dumps({"default_profile": "work", "profiles": {"work": {"api_key": "rbw_prod"}}})
    production.write_text(before, encoding="utf-8")
    _point(home, "staging")

    write_profile(api_key="rbw_staging", api_url="https://staging.example", workspace={"id": "ws"})

    assert production.read_text(encoding="utf-8") == before
    assert load_profile()["api_key"] == "rbw_staging"
    staged = json.loads((home / ".rebase" / "platforms" / "staging" / "config.json").read_text(encoding="utf-8"))
    assert staged["profiles"]["default"]["api_url"] == "https://staging.example"


def test_refresh_session_saves_into_the_platform_auth_file(home: Path, monkeypatch) -> None:
    _point(home, "staging")

    class Response:
        text = ""

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"access_token": "fresh", "refresh_token": "next", "expires_in": 3600, "token_type": "bearer"}

    monkeypatch.setattr(auth_module.requests, "post", lambda *args, **kwargs: Response())
    stale = AuthSession(
        access_token="stale",
        refresh_token="old",
        expires_at=1,
        token_type="bearer",
        supabase_url="https://staging-project.supabase.co",
        supabase_anon_key="anon",
    )

    refreshed = refresh_session(stale)

    assert refreshed.access_token == "fresh"
    platform_file = home / ".config" / "rebase" / "platforms" / "staging" / "auth.json"
    assert load_session(path=platform_file) is not None
    assert load_session(path=platform_file).access_token == "fresh"
    assert not (home / ".config" / "rebase" / "auth.json").exists()
