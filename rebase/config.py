from __future__ import annotations

import json
import os
from contextlib import suppress
from pathlib import Path
from typing import Any

DEFAULT_SERVER_URL = "https://rebase-workflow-api-1002868894268.europe-north1.run.app"
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
    return {
        name: profile
        for name, profile in profiles.items()
        if isinstance(name, str) and isinstance(profile, dict)
    }


def set_default_profile(profile: str, *, path: Path | None = None) -> Path:
    resolved_path = path or config_path()
    data = read_config(resolved_path)
    profiles = data.get("profiles")
    if not isinstance(profiles, dict) or profile not in profiles:
        raise KeyError(profile)
    data["default_profile"] = profile
    resolved_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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


def write_profile(
    *,
    api_key: str,
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

    profile_data: dict[str, Any] = {"api_key": api_key}
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
