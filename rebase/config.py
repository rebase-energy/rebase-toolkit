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
LOCAL_CONFIG_DIRNAME = ".rebase"
LOCAL_CONFIG_FILENAME = "config.json"


def config_path() -> Path:
    override = os.getenv(CONFIG_PATH_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".rebase" / "config.json"


def find_local_config(start: str | Path | None = None) -> Path | None:
    """Nearest `.rebase/config.json` marker, searching `start` then each parent.

    Mirrors how git and uv find their config, so a command run from a subdirectory
    of the repo still resolves to that repo's workspace.

    The global config lives at `~/.rebase/config.json`, which sits directly on this
    walk for any repo under the home directory. It is credentials, not a marker, so
    it is skipped — otherwise every lookup outside a marked repo would "find" it.
    """
    try:
        current = Path(start).expanduser().resolve() if start else Path.cwd().resolve()
    except OSError:
        return None

    skip = set()
    for candidate in (config_path(), Path.home() / LOCAL_CONFIG_DIRNAME / LOCAL_CONFIG_FILENAME):
        with suppress(OSError, RuntimeError):
            skip.add(candidate.expanduser().resolve())

    for directory in (current, *current.parents):
        candidate = directory / LOCAL_CONFIG_DIRNAME / LOCAL_CONFIG_FILENAME
        if candidate.resolve() in skip:
            continue
        if candidate.is_file():
            return candidate
    return None


def read_local_config(start: str | Path | None = None) -> dict[str, Any]:
    """The nearest marker's contents, or {} when there is none or it is unreadable.

    A malformed marker degrades to the global default rather than breaking every
    command in the repo: it is checked in, so a bad merge must not brick the CLI.
    """
    local_path = find_local_config(start)
    if local_path is None:
        return {}
    try:
        with local_path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def local_workspace_id(start: str | Path | None = None) -> str | None:
    """Workspace this directory is pinned to by a checked-in marker."""
    workspace_id = read_local_config(start).get("workspace")
    return workspace_id if isinstance(workspace_id, str) and workspace_id else None


def profile_for_workspace(workspace_id: str, *, path: Path | None = None) -> str | None:
    """A local profile holding credentials for `workspace_id`, if one exists.

    The marker is committed and so names a workspace, not a profile: profile names
    are personal to each checkout. Preference goes to the configured default when it
    already points at the right workspace, so that having several matching profiles
    does not make the choice depend on dict ordering.
    """
    profiles = list_profiles(path=path)
    configured = read_config(path).get("default_profile")
    if isinstance(configured, str) and profiles.get(configured, {}).get("workspace_id") == workspace_id:
        return configured
    for name, profile in profiles.items():
        if profile.get("workspace_id") == workspace_id:
            return name
    return None


def local_workspace_mismatch(*, path: Path | None = None, start: str | Path | None = None) -> str | None:
    """Workspace pinned by a marker that no local profile can reach, else None.

    Commands surface this as a hint: the directory says one thing and the available
    credentials say another, so whatever runs is silently against the wrong
    workspace unless the user is told.
    """
    workspace_id = local_workspace_id(start)
    if workspace_id and profile_for_workspace(workspace_id, path=path) is None:
        return workspace_id
    return None


def write_local_config(
    directory: str | Path,
    *,
    workspace_id: str,
    workspace_name: str | None = None,
) -> Path:
    """Pin `directory` to a workspace by writing `.rebase/config.json`.

    Meant to be committed, so it carries only the workspace identity — never
    credentials, which stay in the global config. Unknown keys already in the file
    are preserved so this can share `.rebase/` with other repo-local content.
    """
    resolved_dir = Path(directory).expanduser().resolve()
    target = resolved_dir / LOCAL_CONFIG_DIRNAME / LOCAL_CONFIG_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)

    data: dict[str, Any] = {}
    if target.is_file():
        with suppress(OSError, json.JSONDecodeError):
            with target.open("r", encoding="utf-8") as file:
                existing = json.load(file)
            if isinstance(existing, dict):
                data = existing

    data["workspace"] = workspace_id
    if workspace_name:
        data["workspace_name"] = workspace_name

    with target.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)
        file.write("\n")
    return target


def read_config(path: Path | None = None) -> dict[str, Any]:
    resolved_path = path or config_path()
    if not resolved_path.exists():
        return {}
    with resolved_path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        return {}
    return data


def selected_profile_name(
    profile: str | None = None,
    *,
    path: Path | None = None,
    start: str | Path | None = None,
) -> str:
    """Resolve the profile to use: explicit argument, then marker, then global default.

    A `.rebase/config.json` marker only wins when some local profile actually holds
    credentials for the workspace it names; otherwise this falls back so that an
    unreachable workspace does not break commands like `rebase profile list`, which
    are how you would diagnose it. `local_workspace_mismatch` reports that case.
    """
    if profile:
        return profile
    workspace_id = local_workspace_id(start)
    if workspace_id:
        matched = profile_for_workspace(workspace_id, path=path)
        if matched:
            return matched
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


def set_profile_workspace(
    workspace_id: str,
    workspace_name: str | None = None,
    *,
    profile: str | None = None,
    path: Path | None = None,
) -> Path:
    """Point a profile at a different workspace, keeping its credentials.

    A profile is an identity on a backend; the workspace is a selection within
    it, carried per request in the `X-Rebase-Workspace` header. Changing it is
    therefore an edit to one field, not a reason for a second profile — which is
    why this exists separately from `write_profile`, which rewrites the whole
    entry and would drop the credentials it was not given.
    """
    resolved_path = path or config_path()
    data = read_config(resolved_path)
    profiles = data.get("profiles")
    name = selected_profile_name(profile, path=resolved_path)
    if not isinstance(profiles, dict) or name not in profiles:
        raise KeyError(name)
    profile_data = profiles[name]
    if not isinstance(profile_data, dict):
        raise KeyError(name)

    profile_data["workspace_id"] = workspace_id
    if workspace_name:
        profile_data["workspace_name"] = workspace_name
    else:
        profile_data.pop("workspace_name", None)
    profiles[name] = profile_data
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


def active_environment(workspace_id: str, *, path: Path | None = None) -> str | None:
    value = workspace_settings(workspace_id, path=path).get("environment")
    return value if isinstance(value, str) and value else None


def set_active_environment(workspace_id: str, environment: str, *, path: Path | None = None) -> None:
    """Persist a personal environment selection in the global user config."""
    resolved_path = path or config_path()
    data = read_config(resolved_path)
    workspaces = data.get("workspaces")
    if not isinstance(workspaces, dict):
        workspaces = {}
    settings = workspaces.get(workspace_id)
    if not isinstance(settings, dict):
        settings = {}
    settings["environment"] = environment
    workspaces[workspace_id] = settings
    data["workspaces"] = workspaces
    _write_config(data, resolved_path)


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
