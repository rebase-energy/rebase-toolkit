from __future__ import annotations

import json
import os
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

DEFAULT_SERVER_URL = "https://rebase-toolkit-api-1002868894268.europe-north1.run.app"
DEFAULT_API_URL = DEFAULT_SERVER_URL
DEFAULT_PROFILE = "default"
CONFIG_PATH_ENV = "REBASE_CONFIG_PATH"


def config_path() -> Path:
    override = os.getenv(CONFIG_PATH_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".rebase" / "config.json"


def read_config(path: Path | None = None) -> dict[str, Any]:
    resolved_path = path or config_path()
    if not resolved_path.exists():
        return {}
    with resolved_path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        return {}
    return data


def selected_profile_name(profile: str | None = None, *, path: Path | None = None) -> str:
    if profile:
        return profile
    data = read_config(path)
    configured = data.get("default_profile")
    if isinstance(configured, str) and configured:
        return configured
    return DEFAULT_PROFILE


def load_profile(profile: str | None = None, *, path: Path | None = None) -> dict[str, Any]:
    data = read_config(path)
    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        return {}
    profile_data = profiles.get(selected_profile_name(profile, path=path))
    if not isinstance(profile_data, dict):
        return {}
    return profile_data


def list_profiles(*, path: Path | None = None) -> dict[str, dict[str, Any]]:
    data = read_config(path)
    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        return {}
    return {name: profile for name, profile in profiles.items() if isinstance(name, str) and isinstance(profile, dict)}


def _write_config(data: dict[str, Any], resolved_path: Path) -> Path:
    """Write the whole config atomically, owner-readable only.

    The config holds API keys, so it is written to a sibling temp file and renamed
    rather than truncated in place: a crash mid-write leaves the old file intact.
    """
    resolved_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with suppress(OSError):
        resolved_path.parent.chmod(0o700)

    tmp_path = resolved_path.with_suffix(f"{resolved_path.suffix}.tmp")
    with tmp_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)
        file.write("\n")
    with suppress(OSError):
        tmp_path.chmod(0o600)
    tmp_path.replace(resolved_path)
    with suppress(OSError):
        resolved_path.chmod(0o600)
    return resolved_path


def set_default_profile(profile: str, *, path: Path | None = None) -> Path:
    resolved_path = path or config_path()
    data = read_config(resolved_path)
    profiles = data.get("profiles")
    if not isinstance(profiles, dict) or profile not in profiles:
        raise KeyError(profile)
    data["default_profile"] = profile
    return _write_config(data, resolved_path)


def write_profile(
    *,
    api_key: str | None = None,
    profile: str = DEFAULT_PROFILE,
    api_url: str | None = None,
    workspace: dict[str, Any] | None = None,
    path: Path | None = None,
) -> Path:
    resolved_path = path or config_path()
    data = read_config(resolved_path)
    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}

    profile_data: dict[str, Any] = {}
    if api_key:
        profile_data["api_key"] = api_key
    if api_url:
        profile_data["api_url"] = api_url.rstrip("/")
    if workspace:
        workspace_id = workspace.get("id")
        workspace_name = workspace.get("name")
        if isinstance(workspace_id, str):
            profile_data["workspace_id"] = workspace_id
        if isinstance(workspace_name, str):
            profile_data["workspace_name"] = workspace_name

    profiles[profile] = profile_data
    data["default_profile"] = profile
    data["profiles"] = profiles

    return _write_config(data, resolved_path)


def workspace_key(profile_data: Mapping[str, Any], profile_name: str) -> str:
    """Identify the workspace a profile points at, for per-workspace local settings.

    Two profiles aimed at the same workspace share its settings, which is why this
    keys on the workspace id rather than the profile name. A profile that has not
    been through `rebase setup` yet has no workspace id, so it falls back to its own
    name — namespaced, so it can never collide with a real workspace id.
    """
    workspace_id = profile_data.get("workspace_id")
    if isinstance(workspace_id, str) and workspace_id:
        return workspace_id
    return f"profile:{profile_name}"


def workspace_settings(workspace_id: str, *, path: Path | None = None) -> dict[str, Any]:
    data = read_config(path)
    workspaces = data.get("workspaces")
    if not isinstance(workspaces, dict):
        return {}
    settings = workspaces.get(workspace_id)
    if not isinstance(settings, dict):
        return {}
    return settings


def search_paths(workspace_id: str, *, path: Path | None = None) -> list[str]:
    """Local directories to search for the files declaring this workspace's projects."""
    configured = workspace_settings(workspace_id, path=path).get("search_paths")
    if not isinstance(configured, list):
        return []
    return [entry for entry in configured if isinstance(entry, str) and entry]


def _normalize_search_path(search_path: str | Path) -> str:
    return str(Path(search_path).expanduser().resolve())


def add_search_path(workspace_id: str, search_path: str | Path, *, path: Path | None = None) -> bool:
    """Register a search path. Returns False, without writing, if it was already there.

    The TUI calls this on every launch to record the repo it was started in, so a
    no-op has to stay a no-op: rewriting the config each time would be both wasteful
    and a chance for two running TUIs to clobber each other.
    """
    resolved_path = path or config_path()
    normalized = _normalize_search_path(search_path)

    data = read_config(resolved_path)
    workspaces = data.get("workspaces")
    if not isinstance(workspaces, dict):
        workspaces = {}
    settings = workspaces.get(workspace_id)
    if not isinstance(settings, dict):
        settings = {}
    existing = [entry for entry in settings.get("search_paths", []) if isinstance(entry, str) and entry]
    if normalized in existing:
        return False

    settings["search_paths"] = [*existing, normalized]
    workspaces[workspace_id] = settings
    data["workspaces"] = workspaces
    _write_config(data, resolved_path)
    return True


def remove_search_path(workspace_id: str, search_path: str | Path, *, path: Path | None = None) -> bool:
    """Forget a search path. Returns False, without writing, if it was not registered."""
    resolved_path = path or config_path()
    normalized = _normalize_search_path(search_path)

    data = read_config(resolved_path)
    workspaces = data.get("workspaces")
    if not isinstance(workspaces, dict):
        return False
    settings = workspaces.get(workspace_id)
    if not isinstance(settings, dict):
        return False
    existing = [entry for entry in settings.get("search_paths", []) if isinstance(entry, str) and entry]
    remaining = [entry for entry in existing if entry != normalized]
    if len(remaining) == len(existing):
        return False

    settings["search_paths"] = remaining
    workspaces[workspace_id] = settings
    data["workspaces"] = workspaces
    _write_config(data, resolved_path)
    return True


def editor_settings(*, path: Path | None = None) -> dict[str, Any]:
    """The editor preference, which is global rather than per-workspace."""
    editor = read_config(path).get("editor")
    if not isinstance(editor, dict):
        return {}
    return editor
