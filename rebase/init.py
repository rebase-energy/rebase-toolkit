"""Connect a repository to a workspace you already belong to.

`rebase setup` is machine-scoped: it authenticates, writes credentials to
`~/.rebase/config.json`, and may create a workspace. This is repo-scoped and does
none of that — it writes the committed `.rebase/config.json` marker and nothing
else, so every clone of a repo resolves to the same workspace without each person
running an identity flow, and CI can pin a checkout without a browser.

The split matters in practice: connecting one repo through `rebase setup` also
re-points the machine's active workspace, which is more than the person asking for
it wanted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rebase.client import Client, RebaseWorkflowError
from rebase.config import find_local_config, local_workspace_id, write_local_config

# Presentation and git-root helpers are shared with `rebase setup` on purpose: this
# command is the same conversation about the same marker, so it should look identical.
from rebase.setup import (
    _choose,
    _current_git_root,
    _directory_handle_suggestion,
    _hint,
    _normalize_handle,
    _success,
    _workspace_display_name,
)

NOT_A_MEMBER_HINT = (
    "You are not a member of workspace {workspace_id}. "
    "Run `rebase setup --workspace {workspace_id}` to create it, or ask for an invite."
)
NO_WORKSPACES_HINT = "No workspaces found for these credentials. Run `rebase setup` to create or join one."


def target_directory(directory: str | Path | None = None) -> Path:
    """Where the marker belongs: the git root, else the directory itself.

    Anchoring at the root means running this from `deploy/rebase/` marks the repo
    once, and every subdirectory resolves to it — the same rule `rebase setup` uses.
    """
    if directory is not None:
        return Path(directory).expanduser().resolve()
    return _current_git_root() or Path.cwd().resolve()


def existing_marker_workspace(directory: Path) -> str | None:
    """Workspace named by a marker that would apply here, if there is one.

    Resolved by the same upward walk the client uses, so a marker at the repo root
    counts when this runs in a subdirectory, and `rebase init` does not quietly add
    a second one below it.
    """
    return local_workspace_id(directory)


def choose_workspace(
    workspaces: list[dict[str, Any]],
    *,
    requested: str | None,
    directory: Path,
) -> dict[str, Any]:
    """The workspace to pin: the one asked for, or the one the user picks.

    Never creates: this command exists because the setup flow pushed people who
    already owned a workspace through "Create a new workspace" to reach it.
    """
    if not workspaces:
        raise RebaseWorkflowError(NO_WORKSPACES_HINT)

    by_id = {str(workspace["id"]): workspace for workspace in workspaces}
    if requested is not None:
        workspace_id = _normalize_handle(requested, label="Workspace handle")
        if workspace_id not in by_id:
            raise RebaseWorkflowError(NOT_A_MEMBER_HINT.format(workspace_id=workspace_id))
        return by_id[workspace_id]

    if len(workspaces) == 1:
        return workspaces[0]

    labels = {_workspace_display_name(workspace): workspace for workspace in workspaces}
    suggestion = _directory_handle_suggestion(directory)
    default = next(
        (label for label, workspace in labels.items() if str(workspace["id"]) == suggestion),
        None,
    )
    selected = _choose(
        "workspace",
        list(labels),
        default=default,
        title=f"Connect {directory.name} to which workspace?",
    )
    return labels[selected]


def init_repository(
    *,
    workspace: str | None = None,
    directory: str | Path | None = None,
    force: bool = False,
    client: Client | None = None,
) -> Path | None:
    """Write the marker connecting this repository to a workspace.

    Returns the marker path, or None when one already named that workspace — running
    this twice is a no-op rather than an error, so it is safe in a bootstrap script.
    """
    resolved_dir = target_directory(directory)
    pinned = existing_marker_workspace(resolved_dir)

    # Nothing to ask the API when the answer is already on disk and unambiguous.
    if (
        pinned is not None
        and workspace is not None
        and pinned == _normalize_handle(workspace, label="Workspace handle")
    ):
        _success(f"{resolved_dir.name} is already connected to workspace {pinned}")
        return None

    selected = choose_workspace(
        (client or Client()).list_my_workspaces(),
        requested=workspace,
        directory=resolved_dir,
    )
    workspace_id = str(selected["id"])

    if pinned is not None and pinned != workspace_id and not force:
        existing = find_local_config(resolved_dir)
        raise RebaseWorkflowError(
            f"{existing} already connects this directory to workspace {pinned}. "
            f"Pass --force to change it to {workspace_id}."
        )
    if pinned == workspace_id:
        _success(f"{resolved_dir.name} is already connected to workspace {workspace_id}")
        return None

    name = selected.get("name")
    marker = write_local_config(
        resolved_dir,
        workspace_id=workspace_id,
        workspace_name=name if isinstance(name, str) and name else None,
    )
    _success(f"Connected {resolved_dir.name} to workspace {workspace_id}")
    _hint(f"Wrote {marker} — commit it so the repo always resolves to this workspace")
    return marker
