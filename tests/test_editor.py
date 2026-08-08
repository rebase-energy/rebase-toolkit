from __future__ import annotations

from pathlib import Path

import pytest

from rebase.client import RebaseWorkflowError
from rebase.editor import (
    EditorCommand,
    build_argv,
    detach_kwargs,
    resolve_editor,
    spawn_detached,
)


def nothing_installed(name: str) -> str | None:
    return None


def no_apps(name: str) -> bool:
    return False


def test_editor_prefers_rebase_editor_over_config_visual_and_editor() -> None:
    command = resolve_editor(
        env={"REBASE_EDITOR": "chosen {path}", "VISUAL": "visual", "EDITOR": "editor"},
        configured="configured",
        which=nothing_installed,
        app_exists=no_apps,
    )

    assert command is not None
    assert command.source == "REBASE_EDITOR"
    assert command.template == "chosen {path}"


def test_editor_falls_back_through_config_visual_and_editor_in_order() -> None:
    common = {"which": nothing_installed, "app_exists": no_apps}

    from_config = resolve_editor(env={"VISUAL": "visual", "EDITOR": "editor"}, configured="cfg", **common)
    from_visual = resolve_editor(env={"VISUAL": "visual", "EDITOR": "editor"}, **common)
    from_editor = resolve_editor(env={"EDITOR": "editor"}, **common)
    from_nothing = resolve_editor(env={}, platform="linux", **common)

    assert from_config is not None and from_config.source == "config"
    assert from_visual is not None and from_visual.source == "VISUAL"
    assert from_editor is not None and from_editor.source == "EDITOR"
    assert from_nothing is None


def test_editor_ignores_a_blank_environment_setting() -> None:
    command = resolve_editor(
        env={"REBASE_EDITOR": "   ", "EDITOR": "nano"},
        which=nothing_installed,
        app_exists=no_apps,
    )

    assert command is not None
    assert command.source == "EDITOR"


def test_editor_detects_the_first_installed_gui_editor_when_nothing_is_configured() -> None:
    command = resolve_editor(
        env={},
        which=lambda name: "/usr/local/bin/cursor" if name == "cursor" else None,
        app_exists=no_apps,
    )

    assert command is not None
    assert command.source == "detected:cursor"
    assert "{line}" in command.template
    assert command.terminal is False


def test_editor_uses_a_macos_app_bundle_when_no_shim_is_on_path() -> None:
    """The `code` shim is absent by default on macOS, so this is the branch that
    makes the feature work out of the box there."""
    command = resolve_editor(
        env={},
        which=nothing_installed,
        app_exists=lambda name: name == "Visual Studio Code",
        platform="darwin",
    )

    assert command is not None
    assert command.source == "macos-app:Visual Studio Code"
    assert build_argv(command, "/x/y.py", line=12) == ("open", "-a", "Visual Studio Code", "/x/y.py")


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_editor_returns_none_rather_than_handing_a_python_file_to_the_os(platform: str) -> None:
    """`.py` is commonly associated with the interpreter, so an OS-level "open" would
    execute the file. There is deliberately no xdg-open/start fallback."""
    assert resolve_editor(env={}, which=nothing_installed, app_exists=no_apps, platform=platform) is None


def test_editor_template_substitutes_path_line_and_column_placeholders() -> None:
    command = EditorCommand(template="ed --line {line} --col {column} {path}", terminal=False, source="test")

    assert build_argv(command, "/x/y.py", line=42) == ("ed", "--line", "42", "--col", "1", "/x/y.py")


def test_editor_template_defaults_the_line_when_none_is_known() -> None:
    command = EditorCommand(template="code -g {path}:{line}", terminal=False, source="test")

    assert build_argv(command, "/x/y.py") == ("code", "-g", "/x/y.py:1")


def test_editor_template_without_a_path_placeholder_gets_the_path_appended() -> None:
    command = EditorCommand(template="nvim", terminal=True, source="EDITOR")

    assert build_argv(command, "/x/y.py", line=3) == ("nvim", "/x/y.py")


def test_editor_template_survives_braces_in_the_resolved_path() -> None:
    """Proves substitution is str.replace and not str.format, which would raise here."""
    command = EditorCommand(template="code {path}", terminal=False, source="test")

    assert build_argv(command, "/tmp/{weird}/y.py") == ("code", "/tmp/{weird}/y.py")


def test_editor_rejects_an_empty_command_template() -> None:
    with pytest.raises(RebaseWorkflowError, match="editor command is empty"):
        build_argv(EditorCommand(template="   ", terminal=False, source="test"), "/x/y.py")


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("nvim {path}", True),
        ("/usr/bin/vim {path}", True),
        ("code -g {path}", False),
        ("emacs {path}", False),
        ("emacs -nw {path}", True),
        ("emacsclient -t {path}", True),
    ],
)
def test_editor_marks_known_terminal_editors(template: str, expected: bool) -> None:
    command = resolve_editor(env={"EDITOR": template}, which=nothing_installed, app_exists=no_apps)

    assert command is not None
    assert command.terminal is expected


def test_editor_honours_an_explicit_terminal_override_from_config() -> None:
    command = resolve_editor(
        env={"EDITOR": "nvim {path}"},
        configured_terminal=False,
        which=nothing_installed,
        app_exists=no_apps,
    )

    assert command is not None
    assert command.terminal is False


def test_editor_detach_kwargs_differ_on_windows() -> None:
    posix = detach_kwargs("posix")
    windows = detach_kwargs("nt")

    assert posix == {"start_new_session": True}
    # start_new_session raises ValueError on Windows, so it must not be passed there.
    assert "start_new_session" not in windows
    assert "creationflags" in windows


def test_editor_reports_a_missing_binary_as_a_rebase_workflow_error(tmp_path: Path) -> None:
    with pytest.raises(RebaseWorkflowError, match="definitely-not-an-editor-xyz"):
        spawn_detached(["definitely-not-an-editor-xyz", str(tmp_path / "f.py")])


def test_editor_template_substitutes_the_folder_placeholder() -> None:
    """VS Code takes the workspace folder as a plain path argument alongside -g."""
    command = EditorCommand(template="code {folder} -g {path}:{line}", terminal=False, source="test")

    assert build_argv(command, "/repo/deploy/x.py", line=7, folder="/repo") == (
        "code",
        "/repo",
        "-g",
        "/repo/deploy/x.py:7",
    )


def test_editor_drops_the_folder_token_when_no_folder_is_known() -> None:
    """An empty argv element would be read by the editor as an argument."""
    command = EditorCommand(template="code {folder} -g {path}:{line}", terminal=False, source="test")

    assert build_argv(command, "/x/y.py", line=7) == ("code", "-g", "/x/y.py:7")


def test_editor_detected_vscode_family_opens_the_workspace_folder() -> None:
    command = resolve_editor(
        env={},
        which=lambda name: "/usr/local/bin/code" if name == "code" else None,
        app_exists=no_apps,
    )

    assert command is not None
    assert "{folder}" in command.template


def test_editor_does_not_inject_a_folder_into_a_user_supplied_template() -> None:
    """An editor we did not choose may not accept a directory argument at all."""
    command = resolve_editor(env={"REBASE_EDITOR": "myeditor {path}"}, which=nothing_installed, app_exists=no_apps)

    assert command is not None
    assert build_argv(command, "/repo/x.py", line=7, folder="/repo") == ("myeditor", "/repo/x.py")
