"""Work out which editor to open a file with, and launch it.

Everything environmental — the process environment, `PATH` lookups, the platform —
arrives as a keyword argument with a real default, so resolution can be tested
without a real editor installed and without touching the developer's own settings.
Only `spawn_detached` and `run_foreground` actually start a process.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from rebase.client import RebaseWorkflowError

#: GUI editors worth auto-detecting, best-known first. The line-number flag is
#: hard-coded per editor because we picked this list and know their arguments; an
#: editor arriving via $EDITOR gets no such assumption.
#: The `{folder}` placeholder opens the file's project as the editor's workspace, so
#: the sidebar shows the tree rather than a lone file. VS Code-family editors reuse
#: an existing window when that folder is already open, so this does not spawn one
#: window per keypress. JetBrains editors are left without it: they resolve the
#: enclosing project from the file themselves, and their CLI is order-sensitive.
DETECTED_EDITORS: tuple[tuple[str, str], ...] = (
    ("code", "code {folder} -g {path}:{line}"),
    ("cursor", "cursor {folder} -g {path}:{line}"),
    ("windsurf", "windsurf {folder} -g {path}:{line}"),
    ("code-insiders", "code-insiders {folder} -g {path}:{line}"),
    ("codium", "codium {folder} -g {path}:{line}"),
    ("zed", "zed {folder} {path}:{line}"),
    ("subl", "subl {folder} {path}:{line}"),
    ("idea", "idea --line {line} {path}"),
    ("pycharm", "pycharm --line {line} {path}"),
)

#: macOS application bundles to fall back on. The `code` shim is not on PATH by
#: default there, so without this the common setup resolves to nothing. `open -a`
#: cannot carry a line number; opening at the top of the file is accepted.
MACOS_APPS: tuple[str, ...] = (
    "Cursor",
    "Visual Studio Code",
    "Zed",
    "Sublime Text",
    "PyCharm",
)

#: Editors that draw in the terminal and therefore need the TUI suspended.
TERMINAL_EDITORS = frozenset({"vim", "nvim", "vi", "nano", "hx", "helix", "kak", "micro", "pico", "ed", "joe", "ne"})

NO_EDITOR_HINT = (
    "no editor found. Set REBASE_EDITOR to a command containing {path}, "
    "for example: REBASE_EDITOR='code -g {path}:{line}'"
)


@dataclass(frozen=True)
class EditorCommand:
    template: str
    terminal: bool
    #: Where this came from, so callers can explain themselves.
    source: str


def _macos_app_exists(name: str) -> bool:
    return any((Path(prefix) / f"{name}.app").exists() for prefix in ("/Applications", Path.home() / "Applications"))


def _is_terminal_editor(template: str) -> bool:
    try:
        argv = shlex.split(template, posix=os.name != "nt")
    except ValueError:
        return False
    if not argv:
        return False
    stem = Path(argv[0]).stem.lower()
    if stem in TERMINAL_EDITORS:
        return True
    # Bare `emacs` opens a window on macOS; only the explicit no-window flags mean
    # it will draw in this terminal.
    if stem in {"emacs", "emacsclient"}:
        return any(flag in argv[1:] for flag in ("-nw", "-t", "--no-window-system"))
    return False


def resolve_editor(
    *,
    env: Mapping[str, str] | None = None,
    configured: str | None = None,
    configured_terminal: bool | None = None,
    which: Callable[[str], str | None] = shutil.which,
    app_exists: Callable[[str], bool] = _macos_app_exists,
    platform: str = sys.platform,
) -> EditorCommand | None:
    """Pick an editor command template, or None if nothing suitable is available.

    Returning None is a real outcome, not a failure to try harder: handing a `.py`
    file to the OS "open this" handler would execute it on Windows, so there is
    deliberately no such fallback.
    """
    environment = os.environ if env is None else env

    candidates: list[tuple[str | None, str]] = [
        (environment.get("REBASE_EDITOR"), "REBASE_EDITOR"),
        (configured, "config"),
        (environment.get("VISUAL"), "VISUAL"),
        (environment.get("EDITOR"), "EDITOR"),
    ]
    for template, source in candidates:
        if template and template.strip():
            terminal = configured_terminal if configured_terminal is not None else _is_terminal_editor(template)
            return EditorCommand(template=template.strip(), terminal=bool(terminal), source=source)

    for name, template in DETECTED_EDITORS:
        if which(name):
            terminal = bool(configured_terminal) if configured_terminal is not None else False
            return EditorCommand(template=template, terminal=terminal, source=f"detected:{name}")

    if platform == "darwin":
        for name in MACOS_APPS:
            if app_exists(name):
                return EditorCommand(
                    template=f'open -a "{name}" {{path}}',
                    terminal=False,
                    source=f"macos-app:{name}",
                )

    return None


def build_argv(
    command: EditorCommand,
    path: Path | str,
    *,
    line: int | None = None,
    folder: Path | str | None = None,
) -> tuple[str, ...]:
    """Expand a command template into an argv list.

    Placeholders are substituted with `str.replace` rather than `str.format` so a
    brace occurring in a real path cannot raise. A template that never mentions
    `{path}` gets the path appended, which is what makes a bare `EDITOR=nvim` work.

    A token mentioning `{folder}` is dropped entirely when no folder is known,
    rather than expanded to an empty string that the editor would read as an
    argument. `{folder}` is never injected into a template the user wrote: an
    editor we did not choose may not accept a directory argument.
    """
    argv = shlex.split(command.template, posix=os.name != "nt")
    if not argv:
        raise RebaseWorkflowError(f"editor command is empty: {command.template!r}")

    resolved_path = str(path)
    substitutions = {
        "{path}": resolved_path,
        "{line}": str(line if line is not None else 1),
        "{column}": "1",
    }
    if folder is not None:
        substitutions["{folder}"] = str(folder)

    expanded: list[str] = []
    mentions_path = False
    for token in argv:
        if folder is None and "{folder}" in token:
            continue
        for placeholder, value in substitutions.items():
            if placeholder in token:
                if placeholder == "{path}":
                    mentions_path = True
                token = token.replace(placeholder, value)
        expanded.append(token)

    if not mentions_path:
        expanded.append(resolved_path)
    return tuple(expanded)


def detach_kwargs(os_name: str | None = None) -> dict[str, object]:
    """Popen keyword arguments that put the editor in its own process group.

    `start_new_session` is POSIX-only and raises ValueError on Windows, which is why
    this branches rather than passing both.
    """
    if (os_name or os.name) == "nt":
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200) | 0x00000008  # DETACHED_PROCESS
        return {"creationflags": creation_flags}
    return {"start_new_session": True}


def spawn_detached(argv: Sequence[str]) -> None:
    """Start a GUI editor and return immediately.

    All three streams go to devnull: an editor that writes to the inherited stdout
    would draw over the TUI.
    """
    try:
        subprocess.Popen(  # noqa: S603
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **detach_kwargs(),  # type: ignore[arg-type]
        )
    except OSError as exc:
        raise RebaseWorkflowError(f"could not launch editor {argv[0]!r}: {exc}") from exc


def run_foreground(argv: Sequence[str]) -> int:
    """Run a terminal editor to completion, handing it this terminal."""
    try:
        return subprocess.run(list(argv), check=False).returncode  # noqa: S603
    except OSError as exc:
        raise RebaseWorkflowError(f"could not launch editor {argv[0]!r}: {exc}") from exc
