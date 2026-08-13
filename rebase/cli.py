from __future__ import annotations

import contextlib
import getpass
import importlib
import importlib.util
import json
import os
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Annotated, Any

import click
import typer
import typer.rich_utils as typer_rich
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from rich.tree import Tree
from typer.core import TyperGroup

from rebase.auth import AuthError, auth_file_path, clear_session, load_session
from rebase.brand import (
    BRAND_AMBER,
    BRAND_BRIGHT_GREEN,
    BRAND_CORAL_RED,
    BRAND_MAIN_GREEN,
    BRAND_MEDIUM_GRAY,
    REBASE_THEME,
)
from rebase.client import (
    DEFAULT_API_KEY_PERMISSIONS,
    Agent,
    ASGIApp,
    Bucket,
    Client,
    Cron,
    Environment,
    Function,
    Model,
    OnUpdate,
    OnWorkflow,
    Optimizer,
    Predictor,
    Project,
    RebaseWorkflowError,
    Run,
    Step,
    Volume,
    Workflow,
    _flatten_config,
    _git,
    _parse_github_remote,
    _validate_execution,
    run_failure_summary,
    run_timing_summary,
    set_build_log_consumer,
)
from rebase.config import (
    DEFAULT_PROFILE,
    DEFAULT_SERVER_URL,
    add_search_path,
    config_path,
    editor_settings,
    find_local_config,
    list_profiles,
    load_profile,
    local_workspace_id,
    local_workspace_mismatch,
    remove_search_path,
    search_paths,
    selected_profile_name,
    set_active_environment,
    set_default_profile,
    set_profile_workspace,
    workspace_key,
    write_profile,
)
from rebase.contract import Freshness, validate_frame
from rebase.editor import NO_EDITOR_HINT, build_argv, resolve_editor, run_foreground, spawn_detached
from rebase.locate import describe_failure, find_project_declarations, is_risky_root, project_folder
from rebase.shell import close_message as _shell_close_message
from rebase.shell import run_bridge as _run_shell_bridge

_BANNER_LINES = [
    "██████╗  ███████╗ ██████╗   █████╗  ███████╗ ███████╗",
    "██╔══██╗ ██╔════╝ ██╔══██╗ ██╔══██╗ ██╔════╝ ██╔════╝",
    "██████╔╝ █████╗   ██████╔╝ ███████║ ███████╗ █████╗  ",
    "██╔══██╗ ██╔══╝   ██╔══██╗ ██╔══██║ ╚════██║ ██╔══╝  ",
    "██║  ██║ ███████╗ ██████╔╝ ██║  ██║ ███████║ ███████╗",
    "╚═╝  ╚═╝ ╚══════╝ ╚═════╝  ╚═╝  ╚═╝ ╚══════╝ ╚══════╝",
]

# "REBASE CLI" — REBASE rows extended with 2-space gap then C, L, I in matching ANSI-shadow style.
# C/L/I are each 9 display chars wide; I (narrow) is 3 wide.  Total width ≈ 78 cols.
_BANNER_LINES_CLI = [
    "██████╗  ███████╗ ██████╗   █████╗  ███████╗ ███████╗     ██████╗ ██╗      ██╗",
    "██╔══██╗ ██╔════╝ ██╔══██╗ ██╔══██╗ ██╔════╝ ██╔════╝    ██╔════╝ ██║      ██║",
    "██████╔╝ █████╗   ██████╔╝ ███████║ ███████╗ █████╗      ██║      ██║      ██║",
    "██╔══██╗ ██╔══╝   ██╔══██╗ ██╔══██║ ╚════██║ ██╔══╝      ██║      ██║      ██║",
    "██║  ██║ ███████╗ ██████╔╝ ██║  ██║ ███████║ ███████╗    ╚██████╗ ███████╗ ██║",
    "╚═╝  ╚═╝ ╚══════╝ ╚═════╝  ╚═╝  ╚═╝ ╚══════╝ ╚══════╝     ╚═════╝ ╚══════╝ ╚═╝",
]


def _banner(variant: int = 1) -> str | Text:
    lines = _BANNER_LINES_CLI if variant in {3, 4} else _BANNER_LINES
    if variant in {1, 3}:
        body = "\n".join(lines)
        return f"[bold {BRAND_BRIGHT_GREEN}]\n{body}\n[/bold {BRAND_BRIGHT_GREEN}]"
    # variants 2 and 4: 4 stripes, same total height as solid variants (6 char rows, no extras).
    # Rows 0, 2, 4 replaced with ▀ (upper-half block): top 50% green, bottom 50% = black gap.
    # Gaps fall at natural letter-structure breaks; box-drawing chars produce thin green outlines.
    text = Text("\n")
    for i, line in enumerate(lines):
        modified = line.replace("█", "▀") if i in {0, 2, 4} else line
        text.append(modified + "\n", style=f"bold {BRAND_BRIGHT_GREEN}")
    text.append("\n")
    return text


def _apply_typer_brand_styles() -> None:
    for name, style in {
        "STYLE_ABORTED": BRAND_CORAL_RED,
        "STYLE_COMMANDS_PANEL_BORDER": BRAND_MAIN_GREEN,
        "STYLE_COMMANDS_TABLE_FIRST_COLUMN": f"bold {BRAND_MAIN_GREEN}",
        "STYLE_DEPRECATED": BRAND_CORAL_RED,
        "STYLE_ERRORS_PANEL_BORDER": BRAND_CORAL_RED,
        "STYLE_ERRORS_SUGGESTION": BRAND_MEDIUM_GRAY,
        "STYLE_HELPTEXT": BRAND_MEDIUM_GRAY,
        "STYLE_METAVAR": f"bold {BRAND_BRIGHT_GREEN}",
        "STYLE_NEGATIVE_OPTION": f"bold {BRAND_MEDIUM_GRAY}",
        "STYLE_NEGATIVE_SWITCH": f"bold {BRAND_MEDIUM_GRAY}",
        "STYLE_OPTION": f"bold {BRAND_MAIN_GREEN}",
        "STYLE_OPTION_DEFAULT": BRAND_MEDIUM_GRAY,
        "STYLE_OPTION_ENVVAR": f"dim {BRAND_AMBER}",
        "STYLE_OPTIONS_PANEL_BORDER": BRAND_MEDIUM_GRAY,
        "STYLE_REQUIRED_LONG": BRAND_CORAL_RED,
        "STYLE_REQUIRED_SHORT": f"bold {BRAND_CORAL_RED}",
        "STYLE_SWITCH": f"bold {BRAND_BRIGHT_GREEN}",
        "STYLE_USAGE": BRAND_BRIGHT_GREEN,
        "STYLE_USAGE_COMMAND": f"bold {BRAND_BRIGHT_GREEN}",
    }.items():
        setattr(typer_rich, name, style)


_apply_typer_brand_styles()

console = Console(highlight=False, soft_wrap=True, theme=REBASE_THEME)
error_console = Console(stderr=True, highlight=False, soft_wrap=True, theme=REBASE_THEME)


# Long form first: click uses names[0] in its "Try 'rebase deploy --help' for help." hint.
HELP_OPTION_NAMES = ["--help", "-h"]


def _vendored_click_exception() -> type[BaseException]:
    """The ``ClickException`` base that typer's own parser actually raises.

    typer >= 0.26 vendors a private copy of click under ``typer._click``, and those exception
    classes do not subclass the ones in the installed ``click`` package. So ``except
    click.ClickException`` never fires for anything typer's parser raises: an unknown option
    escaped ``main()`` entirely and reached typer's Rich excepthook, printing a traceback and
    exiting 1 instead of showing "Error: No such option: --bogus" and exiting 2.

    ``typer.BadParameter`` is a public re-export of whichever hierarchy is in play, so its MRO
    is a supported way to reach that base without importing the private module.
    """
    for cls in typer.BadParameter.__mro__:
        if cls.__name__ == "ClickException":
            return cls
    return click.ClickException  # pragma: no cover - typer always exposes a ClickException base


def _distinct(*classes: type[BaseException]) -> tuple[type[BaseException], ...]:
    """Exception classes with duplicates dropped, so ``except`` tuples stay valid if they merge."""
    return tuple(dict.fromkeys(classes))


# Both hierarchies, because typer may or may not be using its vendored click (see above).
CLICK_EXCEPTIONS = _distinct(click.ClickException, _vendored_click_exception())
ABORT_EXCEPTIONS = _distinct(click.Abort, typer.Abort)
EXIT_EXCEPTIONS = _distinct(click.exceptions.Exit, typer.Exit)


class AlphabeticalTyperGroup(TyperGroup):
    """Group that lists subcommands alphabetically and answers to ``-h`` as well as ``--help``.

    Click only registers ``--help`` by default. Setting ``help_option_names`` here covers every
    command in the tree, not just the groups: a click ``Context`` inherits ``help_option_names``
    from its parent, so leaf commands under any of these groups pick ``-h`` up for free.
    """

    def __init__(
        self,
        *args: Any,
        commands: dict[str, click.Command] | None = None,
        context_settings: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        if commands is not None:
            commands = dict(sorted(commands.items(), key=lambda item: item[0]))
        context_settings = {"help_option_names": HELP_OPTION_NAMES, **(context_settings or {})}
        super().__init__(*args, commands=commands, context_settings=context_settings, **kwargs)


app = typer.Typer(
    add_completion=False,
    cls=AlphabeticalTyperGroup,
    help=(
        "Rebase Toolkit lets you develop Python workflows and models that can then be deployed to the Rebase Platform."
    ),
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@app.callback()
def main_callback(
    workspace: Annotated[
        str | None,
        typer.Option("--workspace", "-W", help="Operate on this workspace id instead of the active one."),
    ] = None,
) -> None:
    """Global options applied before any subcommand runs."""
    # Exported rather than threaded through: there are ~100 bare Client()
    # constructions across this module, and the SDK already resolves
    # REBASE_WORKSPACE at the top of its precedence chain. Setting the variable
    # means every one of them picks the override up with no further wiring, and
    # the precedence rule stays defined in exactly one place.
    if workspace:
        os.environ["REBASE_WORKSPACE"] = workspace


workspace_app = typer.Typer(
    add_completion=False,
    cls=AlphabeticalTyperGroup,
    help="List or switch Rebase workspaces.",
    no_args_is_help=False,
    rich_markup_mode="rich",
)
profile_app = typer.Typer(
    add_completion=False,
    cls=AlphabeticalTyperGroup,
    help="Inspect and switch local Rebase CLI profiles.",
    no_args_is_help=False,
    rich_markup_mode="rich",
)
api_key_app = typer.Typer(
    add_completion=False,
    cls=AlphabeticalTyperGroup,
    help="Create, list, and revoke workspace API keys.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
connect_app = typer.Typer(
    add_completion=False,
    cls=AlphabeticalTyperGroup,
    help="Connect external services to the active workspace.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
secret_app = typer.Typer(
    add_completion=False,
    cls=AlphabeticalTyperGroup,
    help="Store workspace secrets to reference from secrets= on functions, models, and apps.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
environment_app = typer.Typer(
    add_completion=False,
    cls=AlphabeticalTyperGroup,
    help="Inspect and manage deployment environment policies.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
endpoint_app = typer.Typer(
    add_completion=False,
    cls=AlphabeticalTyperGroup,
    help="Inspect and invoke Rebase endpoints.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
project_app = typer.Typer(
    add_completion=False,
    cls=AlphabeticalTyperGroup,
    help="Inspect Rebase projects.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
search_path_app = typer.Typer(
    add_completion=False,
    cls=AlphabeticalTyperGroup,
    help="Local directories searched for the files declaring this workspace's projects.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
function_app = typer.Typer(
    add_completion=False,
    cls=AlphabeticalTyperGroup,
    help="Inspect Rebase functions.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
workflow_app = typer.Typer(
    add_completion=False,
    cls=AlphabeticalTyperGroup,
    help="Inspect Rebase workflows.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
model_app = typer.Typer(
    add_completion=False,
    cls=AlphabeticalTyperGroup,
    help="Deploy and operate Rebase models.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
run_app = typer.Typer(
    add_completion=False,
    cls=AlphabeticalTyperGroup,
    help="Run local Rebase targets and inspect submitted runs.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

WORKSPACE_ROLES = ("Viewer", "Developer", "Admin", "Owner")

KNOWN_PERMISSIONS = frozenset(
    {
        # Must match app.permissions.ALL_PERMISSIONS on the server. This is
        # validated client-side only to fail fast; the server validates too, so a
        # list that drifts short does not protect anything — it just makes
        # permissions the server supports impossible to request. That is exactly
        # what happened: seven of these were missing, which blocked minting keys
        # for tasks, artifacts, buckets and datasets entirely.
        # tests/test_permissions_parity.py in the toolkit repo guards the match.
        "workspace:read",
        "workspace:update",
        "members:read",
        "members:write",
        "api_keys:read",
        "api_keys:write",
        "endpoints:read",
        "endpoints:write",
        "endpoints:execute",
        "projects:read",
        "projects:write",
        "functions:read",
        "functions:write",
        "functions:execute",
        "workflows:read",
        "workflows:write",
        "workflows:execute",
        "models:read",
        "models:write",
        "models:promote",
        "models:execute",
        "runs:read",
        "runs:write",
        "tasks:write",
        "artifacts:write",
        "buckets:read",
        "buckets:write",
        "datasets:read",
        "datasets:write",
        "datasets:signal",
        "shell:execute",
        "asgi:source_token",
    }
)


def _print_run_help() -> None:
    console.print("Usage: rebase run [OPTIONS] TARGET_REF")
    console.print("       rebase run COMMAND [ARGS]...")
    console.print()
    console.print("Run a Rebase function, workflow, or model from local source without deploying it.")
    console.print("Inspect submitted runs with the cancel, get, list, logs, and replay subcommands.")
    console.print()

    options = Table(title="Execution Options", box=box.SIMPLE)
    options.add_column("Option", style="rebase.value")
    options.add_column("Description")
    options.add_row("--param, -p", "Target parameter as name=json_value. Can be passed more than once.")
    options.add_row("--parameters-json", "JSON object with target parameters.")
    options.add_row("--mode", "Execution mode: interactive (default) or job.")
    options.add_row("--isolation, -i", "Interactive isolation: shared (default) or dedicated.")
    options.add_row("--run-type, -r", "Deprecated alias: quick, quick_shared, or long.")
    options.add_row("--module, -m", "Interpret the target source as a Python module path instead of a file.")
    options.add_row("--wait / --no-wait, -w", "Wait for the function result before exiting. Defaults to --wait.")
    options.add_row("--timeout, -t", "Maximum seconds to wait for the result. Defaults to 600.")
    options.add_row("--poll-interval", "Seconds between run status polls. Defaults to 1.0.")
    options.add_row("--local, -l", "Execute the target in this process instead of submitting a cloud run.")
    options.add_row("--help, -h", "Show this message and exit.")
    console.print(options)

    commands = Table(title="Inspection Commands", box=box.SIMPLE)
    commands.add_column("Command", style="rebase.value")
    commands.add_column("Description")
    commands.add_row("cancel", "Cancel a submitted or running run.")
    commands.add_row("get", "Show run metadata.")
    commands.add_row("list", "List submitted runs in the active workspace.")
    commands.add_row("logs", "Show persisted run events and workflow step state.")
    commands.add_row("replay", "Replay a run (or a period of workflow runs) with its original knowledge-time bound.")
    console.print(commands)


def _load_module(path: Path) -> ModuleType:
    resolved_path = path.expanduser().resolve()
    if not resolved_path.exists():
        raise RebaseWorkflowError(f"file not found: {path}")
    if not resolved_path.is_file():
        raise RebaseWorkflowError(f"deploy target must be a Python file: {path}")

    module_name = f"_rebase_deploy_{abs(hash(resolved_path))}"
    spec = importlib.util.spec_from_file_location(module_name, resolved_path)
    if spec is None or spec.loader is None:
        raise RebaseWorkflowError(f"could not load Python file: {path}")

    module = importlib.util.module_from_spec(spec)
    parent = str(resolved_path.parent)
    sys.path.insert(0, parent)
    try:
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(parent)
    return module


def _load_python_module(module_name: str) -> ModuleType:
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        raise RebaseWorkflowError(f"module not found: {module_name}") from exc


def _unique_named_objects(module: ModuleType, types: tuple[type, ...]) -> list[tuple[str, Any]]:
    seen: set[int] = set()
    objects: list[tuple[str, Any]] = []
    for name, value in vars(module).items():
        if name.startswith("_") or not isinstance(value, types):
            continue
        object_id = id(value)
        if object_id in seen:
            continue
        seen.add(object_id)
        objects.append((name, value))
    return objects


def _target_ref_parts(target_ref: str) -> tuple[str, str | None]:
    source_ref, separator, object_ref = target_ref.partition("::")
    if not source_ref:
        raise RebaseWorkflowError("run target must be '{file or module}::function_name'")
    if separator and not object_ref:
        raise RebaseWorkflowError("run target is missing the function name after '::'")
    return source_ref, object_ref or None


def _load_target_module(source_ref: str, *, as_module: bool) -> ModuleType:
    if as_module:
        return _load_python_module(source_ref)
    return _load_module(Path(source_ref))


RunnableTarget = Function | Workflow | Model


def _runnable_objects(module: ModuleType) -> list[tuple[str, RunnableTarget]]:
    return [
        (name, target)
        for name, target in _unique_named_objects(module, (Function, Workflow, Model))
        if not isinstance(target, Step)
    ]


def _model_target_type(target: Model) -> str:
    if isinstance(target, Predictor):
        return "predictor"
    if isinstance(target, Optimizer):
        return "optimizer"
    if isinstance(target, Agent):
        return "agent"
    return "model"


def _resolve_object_ref(module: ModuleType, object_ref: str, *, target_ref: str) -> Any:
    target: Any = module
    for part in object_ref.split("."):
        if not part:
            raise RebaseWorkflowError(f"invalid target reference: {target_ref}")
        if not hasattr(target, part):
            raise RebaseWorkflowError(f"target reference not found: {object_ref}")
        target = getattr(target, part)
    return target


def _resolve_run_target(target_ref: str, *, as_module: bool = False) -> RunnableTarget:
    source_ref, object_ref = _target_ref_parts(target_ref)
    module = _load_target_module(source_ref, as_module=as_module)

    if object_ref is not None:
        target = _resolve_object_ref(module, object_ref, target_ref=target_ref)
        if isinstance(target, Step):
            raise RebaseWorkflowError("rebase run cannot run a step directly; run the workflow that uses it")
        if not isinstance(target, (Function, Workflow, Model)):
            raise RebaseWorkflowError(f"target is not a Rebase function, workflow, or model: {object_ref}")
        return target

    targets = _runnable_objects(module)
    if not targets:
        raise RebaseWorkflowError(
            "No runnable Rebase targets found. Define one top-level rb.function(...), rb.workflow(...), "
            "rb.Predictor, rb.Optimizer, or rb.Agent instance target."
        )
    if len(targets) > 1:
        names = ", ".join(name for name, _target in targets)
        raise RebaseWorkflowError(f"Multiple Rebase targets found ({names}); use '{source_ref}::target_name'.")
    return targets[0][1]


def _parse_json_value(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _parse_run_parameters(parameters_json: str | None, parameters: Iterable[str] | None) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    if parameters_json:
        try:
            loaded = json.loads(parameters_json)
        except json.JSONDecodeError as exc:
            raise RebaseWorkflowError(f"--parameters-json must be a JSON object: {exc}") from exc
        if not isinstance(loaded, dict):
            raise RebaseWorkflowError("--parameters-json must be a JSON object")
        parsed.update(loaded)

    for item in parameters or []:
        name, separator, value = item.partition("=")
        if not separator or not name:
            raise RebaseWorkflowError("--param values must be formatted as name=value")
        parsed[name] = _parse_json_value(value)
    return parsed


def _validate_execution_override(
    mode: str | None,
    isolation: str | None,
    run_type: str | None,
    target: RunnableTarget,
) -> tuple[str, str] | None:
    if mode is None and isolation is None and run_type is None:
        return None
    target_type = "workflow" if isinstance(target, Workflow) else "function"
    try:
        return _validate_execution(
            mode if mode is not None else (None if run_type is not None else target.mode),
            isolation if isolation is not None else (None if run_type is not None else target.isolation),
            target_type=target_type,
            run_type=run_type,
        )
    except ValueError as exc:
        raise RebaseWorkflowError(str(exc)) from exc


def _run_table(run_id: str, status: str) -> Table:
    table = Table(
        title="Rebase Run",
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    table.add_column("Run ID", style="rebase.value")
    table.add_column("Status", style="rebase.muted")
    table.add_row(run_id, status)
    return table


def _runs_table(runs: list[dict[str, Any]], *, project_names: dict[str, str]) -> Table:
    table = Table(
        title="Runs",
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    table.add_column("ID", style="rebase.muted")
    table.add_column("Target")
    table.add_column("Status", style="rebase.value")
    table.add_column("Execution")
    table.add_column("Project")
    table.add_column("Created", style="rebase.muted")
    table.add_column("Finished", style="rebase.muted")
    for run in runs:
        project_id = str(run.get("project_id", ""))
        table.add_row(
            str(run.get("id", "-")),
            _format_value(run.get("target_type")),
            _format_value(run.get("status")),
            _execution_label(run),
            project_names.get(project_id, project_id or "-"),
            _format_value(run.get("created_at")),
            _format_value(run.get("finished_at")),
        )
    return table


def _raw_run_logs(run: dict[str, Any], events: list[dict[str, Any]], steps: list[dict[str, Any]]) -> dict[str, Any]:
    return {"run": run, "events": events, "steps": steps}


def _emit_event_to_reporter(
    reporter: _TerminalRunProgressReporter | _LineRunProgressReporter,
    event: dict[str, Any],
) -> None:
    status = str(event.get("status") or "info")
    message = str(event.get("message") or "")
    if not message:
        return
    if status == "running":
        reporter.update(message)
    elif status == "failed":
        reporter.fail(message)
    elif status == "info":
        # An announcement introduces work rather than reporting on it, so it is neither a
        # transient spinner line nor a tick. Ticking it would claim something finished.
        reporter.announce(message)
    else:
        reporter.complete(message)


def _emit_step_to_reporter(
    reporter: _TerminalRunProgressReporter | _LineRunProgressReporter,
    step: dict[str, Any],
) -> None:
    step_name = str(step.get("name") or step.get("node_key") or step.get("id") or "step")
    step_status = str(step.get("status") or "unknown")
    reporter.step(step_name, step_status)


def _render_run_snapshot(
    *,
    run: dict[str, Any],
    events: list[dict[str, Any]],
    steps: list[dict[str, Any]],
    reporter: _TerminalRunProgressReporter | _LineRunProgressReporter,
) -> None:
    for event in events:
        _emit_event_to_reporter(reporter, event)
    for step in steps:
        _emit_step_to_reporter(reporter, step)
    status = str(run.get("status") or "unknown")
    if status in {"succeeded", "failed", "cancelled"}:
        if status == "succeeded":
            reporter.finish("Run completed.")
        elif status == "failed":
            reporter.fail(run_failure_summary(run) or "Run failed.")
        else:
            reporter.fail("Run cancelled.")
    else:
        reporter.update(f"Run is {status}.")


def _progress_prefix(status: str) -> tuple[str, str]:
    if status == "completed":
        return "✓", "rebase.success"
    if status == "failed":
        return "✗", "rebase.error"
    return "•", "rebase.muted"


def _format_duration(seconds: float) -> str:
    return f"{seconds:.2f}"


class _LineRunProgressReporter:
    def __init__(self) -> None:
        self._last_running_message: str | None = None

    def __enter__(self) -> _LineRunProgressReporter:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def update(self, message: str) -> None:
        if message == self._last_running_message:
            return
        self._last_running_message = message
        prefix, style = _progress_prefix("running")
        console.print(f"{prefix} {message}", style=style)

    def announce(self, message: str) -> None:
        prefix, style = _progress_prefix("info")
        console.print(f"{prefix} {message}", style=style)

    def complete(self, message: str) -> None:
        prefix, style = _progress_prefix("completed")
        console.print(f"{prefix} {message}", style=style)

    def fail(self, message: str) -> None:
        prefix, style = _progress_prefix("failed")
        console.print(f"{prefix} {message}", style=style)

    def log(self, timestamp: str, message: str, severity: str = "INFO") -> None:
        style = "rebase.error" if severity in {"ERROR", "CRITICAL", "WARNING"} else None
        console.print(f"[rebase.muted]{timestamp}[/rebase.muted] {message}", style=style, highlight=False)

    def step(self, name: str, status: str) -> None:
        if status == "running":
            self.update(f"Running step {name}.")
            return
        if status == "succeeded":
            self.complete(f"Step {name} completed.")
            return
        if status in {"failed", "cancelled"}:
            self.fail(f"Step {name} {status}.")
            return
        self.update(f"Step {name}: {status}.")

    def finish(self, message: str) -> None:
        self.complete(message)


class _TerminalRunProgressReporter:
    def __init__(self) -> None:
        self._header: Spinner | Text = Spinner("dots", text=Text("Preparing run...", style="rebase.info"))
        self._tree = Tree("[rebase.title]Run[/rebase.title]", guide_style="rebase.border")
        self._steps_tree: Tree | None = None
        self._live = Live(self._renderable(), console=console, refresh_per_second=8, transient=False)

    def _renderable(self) -> Group:
        return Group(self._header, self._tree)

    def _refresh(self) -> None:
        self._live.update(self._renderable(), refresh=True)

    def __enter__(self) -> _TerminalRunProgressReporter:
        self._live.__enter__()
        return self

    def __exit__(self, *args: Any) -> None:
        self._live.__exit__(*args)

    def update(self, message: str) -> None:
        if isinstance(self._header, Spinner):
            self._header.update(text=Text(message, style="rebase.info"))
        else:
            self._header = Spinner("dots", text=Text(message, style="rebase.info"))
        self._refresh()

    def announce(self, message: str) -> None:
        # Kept in the tree rather than shown on the spinner: an announcement is a milestone
        # worth still being able to read once the next stage has replaced the header, and it
        # is not a tick — nothing completed.
        self._tree.add(f"[dim]•[/dim] {message}")
        self._refresh()

    def complete(self, message: str) -> None:
        self._tree.add(f"[green]✓[/green] {message}")
        self._refresh()

    def fail(self, message: str) -> None:
        self._tree.add(f"[red]✗[/red] {message}")
        self._header = Text.from_markup(f"[red]✗[/red] {message}")
        self._refresh()

    def log(self, timestamp: str, message: str, severity: str = "INFO") -> None:
        style = "rebase.error" if severity in {"ERROR", "CRITICAL", "WARNING"} else None
        self._live.console.print(f"[rebase.muted]{timestamp}[/rebase.muted] {message}", style=style, highlight=False)

    def _workflow_steps_tree(self) -> Tree:
        if self._steps_tree is None:
            self._steps_tree = self._tree.add("[rebase.title]Workflow steps[/rebase.title]")
        return self._steps_tree

    def step(self, name: str, status: str) -> None:
        if status == "running":
            self.update(f"Running step {name}...")
            return
        steps_tree = self._workflow_steps_tree()
        if status == "succeeded":
            steps_tree.add(f"[green]✓[/green] {name}")
        elif status in {"failed", "cancelled"}:
            steps_tree.add(f"[red]✗[/red] {name} ({status})")
        else:
            steps_tree.add(f"[dim]•[/dim] {name}: {status}")
        self._refresh()

    def finish(self, message: str) -> None:
        self._header = Text.from_markup(f"[green]✓[/green] {message}")
        self._refresh()


def _run_progress_reporter() -> _TerminalRunProgressReporter | _LineRunProgressReporter:
    if console.is_terminal:
        return _TerminalRunProgressReporter()
    return _LineRunProgressReporter()


class _RunLogFollower:
    """Incrementally fetches run stdout/stderr and prints new lines via the reporter."""

    def __init__(self, run: Run) -> None:
        self._run = run
        self._since: str | None = None
        self._active = True
        self._printed_message = False

    def poll(self, reporter: _TerminalRunProgressReporter | _LineRunProgressReporter) -> None:
        if not self._active:
            return
        try:
            payload = self._run.logs(since=self._since)
        except RebaseWorkflowError:
            self._active = False
            return
        for entry in payload.get("entries") or []:
            reporter.log(
                str(entry.get("timestamp") or ""),
                str(entry.get("message") or ""),
                str(entry.get("severity") or "INFO"),
            )
        next_since = payload.get("next_since")
        if next_since:
            self._since = str(next_since)
        message = payload.get("message")
        if payload.get("source") == "none" and message:
            if not self._printed_message:
                self._printed_message = True
                reporter.log("", str(message), "INFO")
            self._active = False


def _stream_run_result(
    run: Run,
    *,
    target: RunnableTarget | None = None,
    target_type: str | None = None,
    reporter: _TerminalRunProgressReporter | _LineRunProgressReporter,
    started_at: float,
    timeout: int,
    poll_interval: float,
    return_result: bool = True,
    log_follower: _RunLogFollower | None = None,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    seen_event_ids: set[str] = set()
    seen_step_statuses: dict[str, str] = {}
    events_supported = True
    steps_supported = isinstance(target, Workflow) or target_type == "workflow"
    terminal_statuses = {"succeeded", "failed", "cancelled"}
    first_iteration = True

    while True:
        # The submit response already carries the terminal record for synchronous
        # quick runs — in that case every per-iteration fetch below would be a
        # wasted round trip: the run is over, so there is no progress to stream.
        already_terminal = first_iteration and bool(run.data) and str(run.data.get("status") or "") in terminal_statuses

        if events_supported and not already_terminal:
            try:
                for event in run.events():
                    event_id = str(event.get("id", ""))
                    if not event_id or event_id in seen_event_ids:
                        continue
                    seen_event_ids.add(event_id)
                    # Routed through the same helper the snapshot path uses. This loop used
                    # to carry its own copy of the mapping, so a new status had to be taught
                    # to both or the live view and the replayed one disagreed.
                    _emit_event_to_reporter(reporter, event)
            except RebaseWorkflowError:
                events_supported = False

        if steps_supported and not already_terminal:
            try:
                for step in run.steps():
                    step_key = str(step.get("id") or step.get("node_key") or step.get("name") or "")
                    if not step_key:
                        continue
                    step_status = str(step.get("status") or "")
                    previous = seen_step_statuses.get(step_key)
                    if step_status == previous:
                        continue
                    seen_step_statuses[step_key] = step_status
                    step_name = str(step.get("name") or step.get("node_key") or step_key)
                    reporter.step(step_name, step_status)
            except RebaseWorkflowError:
                steps_supported = False

        if log_follower is not None and not already_terminal:
            # Skipping here is safe: the terminal branch below does its own final
            # log_follower.poll, which is the fetch that matters.
            log_follower.poll(reporter)

        data = run.data if first_iteration and run.data else run.refresh()
        first_iteration = False
        status = str(data.get("status") or "queued")
        if status in terminal_statuses:
            if log_follower is not None:
                # Final fetch: log-store ingestion can lag the terminal status.
                log_follower.poll(reporter)
            if status == "succeeded":
                timing = run_timing_summary(data)
                suffix = f" ({timing})" if timing else ""
                if return_result:
                    reporter.finish(
                        f"Run completed in {_format_duration(time.monotonic() - started_at)} seconds{suffix}."
                    )
                else:
                    reporter.finish(f"Run completed{suffix}.")
                return data.get("result") if return_result else None
            error = run_failure_summary(data) or f"run ended with status {status}"
            reporter.fail(error)
            if return_result:
                raise RebaseWorkflowError(error)
            return None
        if status == "queued" and not seen_event_ids:
            reporter.update("Run queued.")
        elif status == "submitted" and not seen_event_ids:
            reporter.update("Run submitted to the backend.")
        elif status == "running" and not seen_event_ids and not seen_step_statuses:
            reporter.update("Run is executing.")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"run {run.id} did not finish within {timeout} seconds")
        time.sleep(poll_interval)


def deploy_file(
    path: str | Path,
    *,
    object_names: Iterable[str] | None = None,
    deploy_source: str | None = None,
    environment: str = "dev",
) -> list[DeployRow]:
    module = _load_module(Path(path))
    selected_names = set(object_names or [])

    all_projects = _unique_named_objects(module, (Project,))
    projects = all_projects
    if selected_names:
        projects = [
            (name, project) for name, project in projects if name in selected_names or project.name in selected_names
        ]

    deployed: list[DeployRow] = []
    if projects:
        # A file with a top-level Project deploys only what is attached to it.
        # Anything declared standalone -- rb.function(project="..."), rb.workflow(...)
        # -- is otherwise dropped in silence, which is how you end up with a
        # deployed workflow calling a function that was never registered.
        attached = {
            id(item)
            for _, candidate in projects
            for item in (*candidate._functions, *candidate._workflows, *candidate._asgi_apps)
        }
        orphans = sorted(
            item_name
            for item_name, item in _unique_named_objects(module, (Workflow, Function, ASGIApp, Model))
            if not isinstance(item, Step) and id(item) not in attached
        )
        if orphans:
            raise RebaseWorkflowError(
                f"{', '.join(orphans)} is not attached to a project in this file, so deploying the "
                "project would skip it. Declare it with the project decorators "
                "(@project.function(...), @project.workflow(...)) or move it to its own file."
            )
        for name, project in projects:
            if deploy_source is None:
                project.deploy(environment=environment)
            else:
                project.deploy(deploy_source=deploy_source, environment=environment)
            deployed.append(("project", project.name or name, project.id))
            for function in project._functions:
                endpoint_url = _deployed_endpoint_url(function)
                if endpoint_url is not None:
                    deployed.append(("function", function.name or "-", function.id, endpoint_url))
            for workflow in project._workflows:
                endpoint_url = _deployed_endpoint_url(workflow)
                if endpoint_url is not None:
                    deployed.append(("workflow", workflow.name or "-", workflow.id, endpoint_url))
            for asgi_app in project._asgi_apps:
                endpoint_url = _deployed_endpoint_url(asgi_app)
                if endpoint_url is not None:
                    deployed.append(("asgi_app", asgi_app.name or "-", asgi_app.id, endpoint_url))
        return deployed
    if all_projects and selected_names:
        raise RebaseWorkflowError(f"No matching Rebase project found for: {', '.join(sorted(selected_names))}")

    deployables = _unique_named_objects(module, (Workflow, Function, ASGIApp, Model))
    deployables = [(name, item) for name, item in deployables if not isinstance(item, Step)]
    if selected_names:
        deployables = [
            (name, item)
            for name, item in deployables
            if name in selected_names or getattr(item, "name", None) in selected_names
        ]
    if not deployables:
        raise RebaseWorkflowError(
            "No deployable Rebase objects found. Define a top-level rb.project(...), "
            "rb.workflow(...), rb.function(...), rb.asgi_app(...), rb.Predictor, rb.Optimizer, or rb.Agent instance."
        )

    for name, deployable in deployables:
        if deploy_source is None:
            deployable.deploy(environment=environment)
        else:
            deployable.deploy(deploy_source=deploy_source, environment=environment)
        if isinstance(deployable, Model):
            target_type = _model_target_type(deployable)
        elif isinstance(deployable, ASGIApp):
            target_type = "asgi_app"
        else:
            target_type = deployable.__class__.__name__.lower()
        endpoint_url = _deployed_endpoint_url(deployable)
        if endpoint_url is not None:
            deployed.append((target_type, deployable.name or name, deployable.id, endpoint_url))
        else:
            deployed.append((target_type, deployable.name or name, deployable.id))
    return deployed


def _environment_policy(client: Client, environment: str) -> dict[str, Any]:
    for policy in client.list_environment_policies():
        if policy.get("environment") == environment:
            return policy
    raise RebaseWorkflowError(f"deployment environment is not configured: {environment}")


def _policy_requires_gitops(policy: dict[str, Any]) -> bool:
    return bool(policy.get("protected")) or policy.get("deploy_mode") == "gitops"


def _gitops_source_metadata(path: Path) -> dict[str, str | None]:
    resolved_path = path.resolve()
    root = _git(["rev-parse", "--show-toplevel"], cwd=resolved_path.parent)
    if root is None:
        raise RebaseWorkflowError("GitOps deploy requires the deploy file to be inside a git repository.")
    root_path = Path(root).resolve()
    try:
        relative_source_path = resolved_path.relative_to(root_path)
    except ValueError as exc:
        raise RebaseWorkflowError("GitOps deploy file must be inside the current git repository.") from exc
    remote = _git(["config", "--get", "remote.origin.url"], cwd=root_path)
    repo_owner, repo_name = _parse_github_remote(remote)
    if not repo_owner or not repo_name:
        raise RebaseWorkflowError("GitOps deploy requires a GitHub origin remote.")
    dirty = _git(["status", "--porcelain", "--", str(relative_source_path)], cwd=root_path)
    if dirty:
        raise RebaseWorkflowError(
            "GitOps deploy requires the deploy file to be committed and clean. "
            "Commit or discard changes before deploying to a protected environment."
        )
    commit_sha = _git(["rev-parse", "HEAD"], cwd=root_path)
    if not commit_sha:
        raise RebaseWorkflowError("GitOps deploy could not resolve the current commit SHA.")
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root_path)
    if branch == "HEAD":
        branch = None
    return {
        "source_repo": f"{repo_owner}/{repo_name}",
        "repo_owner": repo_owner,
        "repo_name": repo_name,
        "source_path": str(relative_source_path),
        "git_commit_sha": commit_sha,
        "git_branch": branch,
    }


def _create_gitops_intent_for_deploy(
    client: Client,
    *,
    file: Path,
    environment: str,
    object_names: Iterable[str] | None,
    deploy_source: str | None,
) -> dict[str, Any]:
    metadata = _gitops_source_metadata(file)
    plan = {
        "schema_version": 1,
        "kind": "rebase_deploy",
        "file": metadata["source_path"],
        "environment": environment,
        "object_names": sorted(object_names or []),
        "deploy_source": deploy_source,
    }
    return client.create_gitops_deployment_intent(
        environment=environment,
        source_repo=str(metadata["source_repo"]),
        repo_owner=str(metadata["repo_owner"]),
        repo_name=str(metadata["repo_name"]),
        source_path=str(metadata["source_path"]),
        git_commit_sha=str(metadata["git_commit_sha"]),
        git_branch=metadata["git_branch"],
        plan=plan,
    )


def _workspace_value(data: dict[str, Any]) -> str:
    workspace_name = data.get("name") or data.get("workspace_name")
    workspace_id = data.get("id") or data.get("workspace_id")
    if isinstance(workspace_name, str) and workspace_name:
        return workspace_name
    if isinstance(workspace_id, str) and workspace_id:
        return workspace_id
    return "unknown workspace"


def _workspace_id(data: dict[str, Any]) -> str:
    workspace_id = data.get("id") or data.get("workspace_id")
    return workspace_id if isinstance(workspace_id, str) and workspace_id else "-"


def _workspace_repo(data: dict[str, Any]) -> str:
    repo_owner = data.get("repo_owner")
    repo_name = data.get("repo_name")
    if isinstance(repo_owner, str) and repo_owner and isinstance(repo_name, str) and repo_name:
        return f"{repo_owner}/{repo_name}"
    return "-"


def _workspace_details(client: Client, workspace: dict[str, Any]) -> dict[str, Any]:
    workspace_id = workspace.get("id")
    if not isinstance(workspace_id, str) or not workspace_id:
        return workspace
    details = client.request("GET", "/workspace", headers={"X-Rebase-Workspace": workspace_id})
    if not isinstance(details, dict):
        raise RebaseWorkflowError("expected workspace response")
    return {**details, "role": workspace.get("role"), "default": workspace.get("default", False)}


def _workspace_membership_table(
    workspaces: list[dict[str, Any]],
    profiles: dict[str, dict[str, Any]],
    *,
    active_profile: str,
) -> Table:
    table = Table(
        title="Workspaces",
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    table.add_column("Active", justify="center", no_wrap=True, style="rebase.active")
    table.add_column("Workspace")
    table.add_column("Workspace ID", style="rebase.muted")
    table.add_column("Role", no_wrap=True)
    table.add_column("Git Repo")
    active_workspace_id = _workspace_id(profiles.get(active_profile, {}))
    for workspace in sorted(workspaces, key=lambda item: _workspace_value(item).lower()):
        workspace_id = _workspace_id(workspace)
        is_active = workspace_id == active_workspace_id
        style = "rebase.active" if is_active else None
        table.add_row(
            "*" if is_active else "",
            _workspace_value(workspace),
            workspace_id,
            _format_value(workspace.get("role")),
            _workspace_repo(workspace),
            style=style,
        )
    return table


def _format_cents(value: Any, currency: str = "EUR") -> str:
    cents = int(value or 0)
    return f"{cents / 100:.2f} {currency}"


def _workspace_usage_table(usage: dict[str, Any]) -> Table:
    currency = str(usage.get("currency") or "EUR")
    table = Table(
        title="Workspace Usage",
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    table.add_column("Metric", style="rebase.muted")
    table.add_column("Value", style="rebase.value")
    table.add_row("Workspace", _format_value(usage.get("workspace_id")))
    table.add_row("Monthly credits", _format_cents(usage.get("monthly_credit_cents"), currency))
    table.add_row("Used", _format_cents(usage.get("finalized_spend_cents"), currency))
    table.add_row("Reserved", _format_cents(usage.get("active_reservation_cents"), currency))
    table.add_row("Remaining", _format_cents(usage.get("remaining_cents"), currency))
    table.add_row("Period end", _format_value(usage.get("period_end")))
    table.add_row("Blocked", "yes" if usage.get("compute_blocked") else "no")
    return table


def _environment_policy_table(policies: list[dict[str, Any]]) -> Table:
    table = Table(
        title="Workspace Environments",
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    table.add_column("Environment", style="rebase.value")
    table.add_column("Mode", no_wrap=True)
    table.add_column("Protected", no_wrap=True)
    table.add_column("Require PR", no_wrap=True)
    table.add_column("Allowed branches", style="rebase.muted")
    for policy in policies:
        branches = policy.get("allowed_branches") or []
        table.add_row(
            _format_value(policy.get("environment")),
            _format_value(policy.get("deploy_mode")),
            "yes" if policy.get("protected") else "no",
            "yes" if policy.get("require_pr") else "no",
            ", ".join(str(branch) for branch in branches) if branches else "-",
        )
    return table


def _environment_grants_table(grants: list[dict[str, Any]]) -> Table:
    table = Table(
        title="Environment Grants",
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
    )
    table.add_column("Grant ID", style="rebase.muted")
    table.add_column("Principal", style="rebase.value")
    table.add_column("Kind")
    table.add_column("Access")
    for grant in grants:
        profile_id = grant.get("profile_id")
        api_key_id = grant.get("api_key_id")
        table.add_row(
            _format_value(grant.get("id")),
            _format_value(profile_id or api_key_id),
            "profile" if profile_id else "api key",
            _format_value(grant.get("access")),
        )
    return table


def _workspace_member_identity(data: dict[str, Any]) -> str:
    email = data.get("email")
    if isinstance(email, str) and email:
        return email
    github_username = data.get("github_username")
    if isinstance(github_username, str) and github_username:
        return f"@{github_username}"
    display_name = data.get("display_name")
    if isinstance(display_name, str) and display_name:
        return display_name
    profile_id = data.get("profile_id")
    if isinstance(profile_id, str) and profile_id:
        return profile_id
    return str(data.get("id", "-"))


def _workspace_members_table(members: list[dict[str, Any]], pending_invites: list[dict[str, Any]]) -> Table:
    table = Table(
        title="Workspace Members",
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    table.add_column("Identity", style="rebase.value")
    table.add_column("Role", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Kind", no_wrap=True, style="rebase.muted")
    table.add_column("Created", style="rebase.muted")

    for member in members:
        status = "active" if member.get("enabled", True) else "disabled"
        table.add_row(
            _workspace_member_identity(member),
            _format_value(member.get("role")),
            status,
            "member",
            _format_value(member.get("created_at")),
        )
    for invite in pending_invites:
        table.add_row(
            _workspace_member_identity(invite),
            _format_value(invite.get("role")),
            _format_value(invite.get("status")),
            "invite",
            _format_value(invite.get("created_at")),
        )
    return table


def _load_auth_session_for_display() -> tuple[Any | None, str | None]:
    try:
        return load_session(), None
    except AuthError as exc:
        return None, str(exc)


def _profile_workspace_label(profile_data: dict[str, Any]) -> str:
    workspace_name = profile_data.get("workspace_name")
    if isinstance(workspace_name, str) and workspace_name:
        return workspace_name
    workspace_id = profile_data.get("workspace_id")
    if isinstance(workspace_id, str) and workspace_id:
        return workspace_id
    return "-"


def _profile_workspace_id(profile_data: dict[str, Any]) -> str:
    workspace_id = profile_data.get("workspace_id")
    return workspace_id if isinstance(workspace_id, str) and workspace_id else "-"


def _profile_api_url(profile_data: dict[str, Any]) -> str:
    api_url = profile_data.get("api_url")
    return api_url if isinstance(api_url, str) and api_url else DEFAULT_SERVER_URL


def _auth_session_data(session: Any | None, auth_error: str | None) -> dict[str, Any]:
    if session is None:
        return {
            "exists": False,
            "email": None,
            "user_id": None,
            "expires_at": None,
            "error": auth_error,
            "auth_file": str(auth_file_path()),
        }
    expires_at = getattr(session, "expires_at_datetime", None)
    return {
        "exists": True,
        "email": session.email,
        "user_id": session.user_id,
        "expires_at": expires_at.isoformat() if expires_at is not None else None,
        "error": auth_error,
        "auth_file": str(auth_file_path()),
    }


def _profile_credential_label(profile_data: dict[str, Any], *, has_auth_session: bool) -> str:
    if profile_data.get("api_key"):
        return "api key"
    if has_auth_session:
        return "supabase session"
    return "none"


def _profile_summary(
    name: str,
    profile_data: dict[str, Any],
    *,
    active_profile: str,
    has_auth_session: bool,
) -> dict[str, Any]:
    return {
        "active": name == active_profile,
        "profile": name,
        "workspace": _profile_workspace_label(profile_data),
        "workspace_id": _profile_workspace_id(profile_data),
        "api_url": _profile_api_url(profile_data),
        "has_api_key": bool(profile_data.get("api_key")),
        "credential": _profile_credential_label(profile_data, has_auth_session=has_auth_session),
    }


def _profiles_table(
    profiles: dict[str, dict[str, Any]],
    *,
    active_profile: str,
    has_auth_session: bool,
) -> Table:
    table = Table(
        title="Profiles",
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    table.add_column("Active", justify="center", no_wrap=True, style="rebase.active")
    table.add_column("Profile")
    table.add_column("Workspace", overflow="fold")
    table.add_column("Workspace ID", style="rebase.muted", overflow="fold")
    table.add_column("API URL", overflow="fold")
    table.add_column("Credential", no_wrap=True)
    for profile_name, profile_data in sorted(profiles.items()):
        summary = _profile_summary(
            profile_name,
            profile_data,
            active_profile=active_profile,
            has_auth_session=has_auth_session,
        )
        style = "rebase.active" if summary["active"] else None
        table.add_row(
            "*" if summary["active"] else "",
            profile_name,
            summary["workspace"],
            summary["workspace_id"],
            summary["api_url"],
            summary["credential"],
            style=style,
        )
    return table


def _profile_show_data(profile: str | None = None) -> dict[str, Any]:
    profiles = list_profiles()
    active_profile = selected_profile_name()
    profile_name = profile or active_profile
    profile_exists = profile_name in profiles
    if profile and not profile_exists:
        raise RebaseWorkflowError(f"unknown profile: {profile}. Run `rebase setup --profile {profile}` first.")
    profile_data = profiles.get(profile_name, {})
    session, auth_error = _load_auth_session_for_display()
    auth = _auth_session_data(session, auth_error)
    summary = _profile_summary(
        profile_name,
        profile_data,
        active_profile=active_profile,
        has_auth_session=auth["exists"],
    )
    local_config = find_local_config()
    return {
        **summary,
        "exists": profile_exists,
        "active_profile": active_profile,
        "config_file": str(config_path()),
        "local_config_file": str(local_config) if local_config else None,
        "local_workspace": local_workspace_id(),
        "local_workspace_unreachable": local_workspace_mismatch(),
        "auth": auth,
    }


def _profile_show_table(data: dict[str, Any]) -> Table:
    table = Table(
        title="Profile",
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=False,
        title_style="rebase.title",
    )
    table.add_column("Field", style="rebase.muted")
    table.add_column("Value", style="rebase.value", overflow="fold")
    auth = data.get("auth") if isinstance(data.get("auth"), dict) else {}
    table.add_row("Active profile", _format_value(data.get("active_profile")))
    table.add_row("Profile", _format_value(data.get("profile")))
    table.add_row("Profile exists", "yes" if data.get("exists") else "no")
    table.add_row("Workspace", _format_value(data.get("workspace")))
    table.add_row("Workspace ID", _format_value(data.get("workspace_id")))
    table.add_row("API URL", _format_value(data.get("api_url")))
    table.add_row("Credential", _format_value(data.get("credential")))
    table.add_row("API key", "configured" if data.get("has_api_key") else "not configured")
    table.add_row("Auth email", _format_value(auth.get("email")))
    table.add_row("Auth user ID", _format_value(auth.get("user_id")))
    table.add_row("Auth expires", _format_value(auth.get("expires_at")))
    if auth.get("error"):
        table.add_row("Auth error", _format_value(auth.get("error")))
    table.add_row("Config file", _format_value(data.get("config_file")))
    table.add_row("Auth file", _format_value(auth.get("auth_file")))
    if data.get("local_config_file"):
        table.add_row("Repo workspace", _format_value(data.get("local_workspace")))
        table.add_row("Repo marker", _format_value(data.get("local_config_file")))
    unreachable = data.get("local_workspace_unreachable")
    if unreachable:
        table.add_row(
            "Repo warning",
            f"no local profile has credentials for {unreachable} — using {data.get('active_profile')}",
        )
    return table


def _permissions_summary(permissions: Any) -> str:
    if not isinstance(permissions, list) or not permissions:
        return "-"
    values = [str(permission) for permission in permissions]
    if len(values) == 1:
        return values[0]
    return f"{len(values)}p"


def _api_keys_table(api_keys: list[dict[str, Any]]) -> Table:
    table = Table(
        title="API Keys",
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    table.add_column("Name", style="rebase.value")
    table.add_column("Prefix", no_wrap=True)
    table.add_column("Project", style="rebase.muted")
    table.add_column("State", no_wrap=True)
    table.add_column("Used", style="rebase.muted")
    table.add_column("Expires", style="rebase.muted")
    table.add_column("Revoked", style="rebase.muted")
    table.add_column("Perms")
    table.add_column("ID", style="rebase.muted")
    for api_key in api_keys:
        state = "active" if api_key.get("enabled") else "disabled"
        if api_key.get("revoked_at"):
            state = "revoked"
        table.add_row(
            str(api_key.get("name", "-")),
            _format_value(api_key.get("key_prefix")),
            _format_value(api_key.get("project_id")),
            state,
            _format_value(api_key.get("last_used_at")),
            _format_value(api_key.get("expires_at")),
            _format_value(api_key.get("revoked_at")),
            _permissions_summary(api_key.get("permissions")),
            str(api_key.get("id", "-")),
        )
    return table


def _selected_api_key_permissions(permissions: list[str] | None) -> list[str]:
    if not permissions:
        return list(DEFAULT_API_KEY_PERMISSIONS)
    selected = list(dict.fromkeys(permissions))
    unknown = sorted(set(selected) - KNOWN_PERMISSIONS)
    if unknown:
        raise RebaseWorkflowError(f"unknown permissions: {', '.join(unknown)}")
    return selected


def _resolve_api_key_selector(api_keys: list[dict[str, Any]], selector: str) -> dict[str, Any]:
    matches_by_id: dict[str, dict[str, Any]] = {}
    for api_key in api_keys:
        api_key_id = str(api_key.get("id", ""))
        if selector in {
            api_key_id,
            str(api_key.get("key_prefix", "")),
            str(api_key.get("name", "")),
        }:
            matches_by_id[api_key_id] = api_key
    matches = list(matches_by_id.values())
    if not matches:
        raise RebaseWorkflowError(f"api key not found: {selector}")
    if len(matches) > 1:
        raise RebaseWorkflowError(f"api key selector is ambiguous: {selector}. Use the exact API key id.")
    return matches[0]


def _endpoints_table(endpoints: list[dict[str, Any]]) -> Table:
    table = Table(
        title="Endpoints",
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    table.add_column("Project", style="rebase.muted")
    table.add_column("Name", style="rebase.value")
    table.add_column("Method", no_wrap=True)
    table.add_column("Path")
    table.add_column("Auth", no_wrap=True)
    table.add_column("Mode", no_wrap=True)
    table.add_column("Target", no_wrap=True)
    table.add_column("State", no_wrap=True)
    table.add_column("URL", style="rebase.muted")
    for endpoint in endpoints:
        state = "active" if endpoint.get("enabled") else "disabled"
        table.add_row(
            _format_value(endpoint.get("project_name")),
            str(endpoint.get("name", "-")),
            _format_value(endpoint.get("method")),
            _format_value(endpoint.get("path")),
            _format_value(endpoint.get("auth")),
            _format_value(endpoint.get("mode")),
            f"{endpoint.get('target_type', '-')}/{_format_value(endpoint.get('target_id'))}",
            state,
            _format_value(endpoint.get("url")),
        )
    return table


def _endpoint_selector_values(endpoint: dict[str, Any]) -> set[str]:
    project_name = str(endpoint.get("project_name") or "")
    name = str(endpoint.get("name") or "")
    path = str(endpoint.get("path") or "")
    values = {
        str(endpoint.get("id") or ""),
        name,
        path,
        str(endpoint.get("url_path") or ""),
        str(endpoint.get("url") or ""),
    }
    if project_name and name:
        values.add(f"{project_name}/{name}")
    if project_name and path:
        values.add(f"{project_name}{path}")
        values.add(f"{project_name}/{path.lstrip('/')}")
    return {value for value in values if value}


def _resolve_endpoint_selector(endpoints: list[dict[str, Any]], selector: str) -> dict[str, Any]:
    matches_by_id: dict[str, dict[str, Any]] = {}
    for endpoint in endpoints:
        if selector in _endpoint_selector_values(endpoint):
            matches_by_id[str(endpoint.get("id"))] = endpoint
    matches = list(matches_by_id.values())
    if not matches:
        raise RebaseWorkflowError(f"endpoint not found: {selector}")
    if len(matches) > 1:
        raise RebaseWorkflowError(f"endpoint selector is ambiguous: {selector}. Use the exact endpoint id.")
    return matches[0]


DeployRow = tuple[str, str, str | None] | tuple[str, str, str | None, str | None]


def _deploy_table(deployed: list[DeployRow]) -> Table:
    table = Table(
        title="Deployed Targets",
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    table.add_column("Type", no_wrap=True, style="rebase.muted")
    table.add_column("Name", style="rebase.value")
    table.add_column("ID", style="rebase.muted")
    show_url = any(len(row) == 4 and row[3] for row in deployed)
    if show_url:
        table.add_column("Endpoint", style="rebase.muted")
    for row in deployed:
        target_type, name, target_id = row[:3]
        cells = [target_type, name, target_id or "-"]
        if show_url:
            cells.append(row[3] if len(row) == 4 and row[3] else "-")
        table.add_row(*cells)
    return table


def _deployed_endpoint_url(target: Any) -> str | None:
    data = getattr(target, "data", None)
    if not isinstance(data, dict):
        return None
    url = data.get("url")
    if isinstance(url, str) and url:
        return url
    url_path = data.get("url_path")
    if isinstance(url_path, str) and url_path:
        client = getattr(target, "_client", None)
        api_url = getattr(client, "api_url", None)
        if isinstance(api_url, str) and api_url:
            return f"{api_url.rstrip('/')}{url_path}"
    endpoint = data.get("endpoint")
    if not isinstance(endpoint, dict):
        return None
    url = endpoint.get("url")
    if isinstance(url, str) and url:
        return url
    url_path = endpoint.get("url_path")
    if isinstance(url_path, str) and url_path:
        client = getattr(target, "_client", None)
        api_url = getattr(client, "api_url", None)
        if isinstance(api_url, str) and api_url:
            return f"{api_url.rstrip('/')}{url_path}"
    return None


def _format_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, sort_keys=True)
    return str(value)


def _execution_label(value: dict[str, Any]) -> str:
    mode = value.get("mode")
    isolation = value.get("isolation")
    if mode is not None:
        return f"{mode}/{isolation}" if isolation is not None else str(mode)
    return _format_value(value.get("run_type"))


def _print_json(data: Any) -> None:
    console.print_json(data=data)


def _detail_table(title: str, data: dict[str, Any], *, preferred_keys: Iterable[str] | None = None) -> Table:
    ordered_keys: list[str] = []
    seen: set[str] = set()
    for key in preferred_keys or []:
        if key in data:
            ordered_keys.append(key)
            seen.add(key)
    for key in data:
        if key not in seen:
            ordered_keys.append(key)

    table = Table(
        title=title,
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    table.add_column("Field", style="rebase.muted", no_wrap=True)
    table.add_column("Value", style="rebase.value")
    for key in ordered_keys:
        table.add_row(key, _format_value(data[key]))
    return table


def _project_table(projects: list[dict[str, Any]]) -> Table:
    table = Table(
        title="Projects",
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    table.add_column("Name", style="rebase.value")
    table.add_column("ID", style="rebase.muted")
    table.add_column("Source Mode")
    table.add_column("Updated", style="rebase.muted")
    for project in projects:
        table.add_row(
            str(project.get("name", "-")),
            str(project.get("id", "-")),
            _format_value(project.get("source_mode")),
            _format_value(project.get("updated_at")),
        )
    return table


def _function_table(functions: list[dict[str, Any]], *, project_names: dict[str, str]) -> Table:
    table = Table(
        title="Functions",
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    table.add_column("Name", style="rebase.value")
    table.add_column("Project")
    table.add_column("Execution")
    table.add_column("Enabled")
    table.add_column("ID", style="rebase.muted")
    table.add_column("Updated", style="rebase.muted")
    for function in functions:
        project_id = str(function.get("project_id", ""))
        table.add_row(
            str(function.get("name", "-")),
            project_names.get(project_id, project_id or "-"),
            _execution_label(function),
            _format_value(function.get("enabled")),
            str(function.get("id", "-")),
            _format_value(function.get("updated_at")),
        )
    return table


def _workflow_table(workflows: list[dict[str, Any]], *, project_names: dict[str, str]) -> Table:
    table = Table(
        title="Workflows",
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    table.add_column("Name", style="rebase.value")
    table.add_column("Project")
    table.add_column("Execution")
    table.add_column("Enabled")
    table.add_column("Schedule")
    table.add_column("ID", style="rebase.muted")
    table.add_column("Updated", style="rebase.muted")
    for workflow in workflows:
        project_id = str(workflow.get("project_id", ""))
        table.add_row(
            str(workflow.get("name", "-")),
            project_names.get(project_id, project_id or "-"),
            _execution_label(workflow),
            _format_value(workflow.get("enabled")),
            _format_schedule(workflow.get("schedule")),
            str(workflow.get("id", "-")),
            _format_value(workflow.get("updated_at")),
        )
    return table


def _format_schedule(schedule: Any) -> str:
    if not isinstance(schedule, dict):
        return "-"
    cron = str(schedule.get("cron") or "-")
    if not schedule.get("active", True):
        return f"{cron} (paused)"
    return cron


def _format_trigger(trigger: Any) -> str:
    if not isinstance(trigger, dict):
        return "-"
    if trigger.get("type") == "on_workflow":
        detail = f"{trigger.get('source', '-')} ({trigger.get('on', 'success')})"
    elif trigger.get("type") == "on_update":
        detail = ", ".join(str(dataset) for dataset in trigger.get("datasets") or []) or "-"
    else:
        detail = str(trigger.get("type") or "-")
    if not trigger.get("active", True):
        return f"{detail} (paused)"
    return detail


def _model_table(models: list[dict[str, Any]], *, project_names: dict[str, str]) -> Table:
    table = Table(
        title="Models",
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    table.add_column("Name", style="rebase.value")
    table.add_column("Project")
    table.add_column("Kind")
    table.add_column("Operation")
    table.add_column("Execution")
    table.add_column("ID", style="rebase.muted")
    table.add_column("Updated", style="rebase.muted")
    for model in models:
        project_id = str(model.get("project_id", ""))
        table.add_row(
            str(model.get("name", "-")),
            project_names.get(project_id, project_id or "-"),
            _format_value(model.get("kind")),
            _format_value(model.get("operation_name")),
            _execution_label(model),
            str(model.get("id", "-")),
            _format_value(model.get("updated_at")),
        )
    return table


def _deployment_table(deployments: list[dict[str, Any]]) -> Table:
    table = Table(
        title="Model Deployments",
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    table.add_column("Environment", style="rebase.value")
    table.add_column("Version ID", style="rebase.muted")
    table.add_column("Previous Version")
    table.add_column("Updated By")
    table.add_column("Updated", style="rebase.muted")
    for deployment in deployments:
        table.add_row(
            _format_value(deployment.get("environment")),
            str(deployment.get("model_version_id", "-")),
            _format_value(deployment.get("previous_model_version_id")),
            _format_value(deployment.get("updated_by")),
            _format_value(deployment.get("updated_at")),
        )
    return table


def _promotion_request_table(requests: list[dict[str, Any]]) -> Table:
    table = Table(
        title="Model Promotion Requests",
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    table.add_column("ID", style="rebase.muted")
    table.add_column("Status", style="rebase.value")
    table.add_column("Version ID")
    table.add_column("From")
    table.add_column("To")
    table.add_column("Requested By")
    table.add_column("Updated", style="rebase.muted")
    for request in requests:
        table.add_row(
            str(request.get("id", "-")),
            _format_value(request.get("status")),
            str(request.get("model_version_id", "-")),
            _format_value(request.get("from_environment")),
            _format_value(request.get("to_environment")),
            _format_value(request.get("requested_by")),
            _format_value(request.get("updated_at")),
        )
    return table


def _version_table(title: str, versions: list[dict[str, Any]]) -> Table:
    table = Table(
        title=title,
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    table.add_column("Version", style="rebase.value")
    table.add_column("ID", style="rebase.muted")
    table.add_column("Fingerprint")
    table.add_column("Execution")
    table.add_column("Created", style="rebase.muted")
    for version in versions:
        table.add_row(
            _format_value(version.get("version_number")),
            str(version.get("id", "-")),
            _format_value(version.get("fingerprint")),
            _execution_label(version),
            _format_value(version.get("created_at")),
        )
    return table


def _resolve_project_by_name(client: Client, name: str) -> dict[str, Any]:
    project = client.find_project(name)
    if project is None:
        raise RebaseWorkflowError(f"project not found: {name}")
    return project


def _resolve_project_selector(client: Client, name: str | None, *, project_id: str | None = None) -> dict[str, Any]:
    if project_id is not None:
        if name is not None:
            raise RebaseWorkflowError("provide either a project name or --id, not both")
        return client.get_project(project_id)
    if name is None:
        raise RebaseWorkflowError("project name is required unless --id is provided")
    return _resolve_project_by_name(client, name)


def _resolve_target_selector(
    client: Client,
    name: str | None,
    *,
    target_id: str | None,
    project_name: str | None,
    target_type: str,
    get_by_id: Callable[[str], dict[str, Any]],
    list_by_project: Callable[..., list[dict[str, Any]]],
) -> dict[str, Any]:
    if target_id is not None:
        if name is not None:
            raise RebaseWorkflowError(f"provide either a {target_type} name or --id, not both")
        return get_by_id(target_id)
    if name is None:
        raise RebaseWorkflowError(f"{target_type} name is required unless --id is provided")
    if not project_name:
        raise RebaseWorkflowError(f"--project is required when selecting a {target_type} by name")
    project = _resolve_project_by_name(client, project_name)
    for target in list_by_project(project_id=str(project["id"])):
        if target.get("name") == name:
            return target
    raise RebaseWorkflowError(f"{target_type} not found: {project_name}/{name}")


def _resolve_function_selector(
    client: Client,
    name: str | None,
    *,
    function_id: str | None = None,
    project_name: str | None = None,
) -> dict[str, Any]:
    return _resolve_target_selector(
        client,
        name,
        target_id=function_id,
        project_name=project_name,
        target_type="function",
        get_by_id=client.get_function,
        list_by_project=client.list_functions,
    )


def _resolve_workflow_selector(
    client: Client,
    name: str | None,
    *,
    workflow_id: str | None = None,
    project_name: str | None = None,
) -> dict[str, Any]:
    return _resolve_target_selector(
        client,
        name,
        target_id=workflow_id,
        project_name=project_name,
        target_type="workflow",
        get_by_id=client.get_workflow,
        list_by_project=client.list_workflows,
    )


def _resolve_model_selector(
    client: Client,
    name: str | None,
    *,
    model_id: str | None = None,
    project_name: str | None = None,
) -> dict[str, Any]:
    return _resolve_target_selector(
        client,
        name,
        target_id=model_id,
        project_name=project_name,
        target_type="model",
        get_by_id=client.get_model,
        list_by_project=client.list_models,
    )


def _project_name_map(projects: list[dict[str, Any]]) -> dict[str, str]:
    return {str(project.get("id", "")): str(project.get("name", "-")) for project in projects}


def _list_project_targets(
    client: Client,
    project_name: str | None,
    load: Callable[..., list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    if project_name is None:
        return load(), _project_name_map(client.list_projects())
    project = _resolve_project_by_name(client, project_name)
    return load(project_id=str(project["id"])), {str(project["id"]): str(project["name"])}


@app.command("setup")
def setup_command(
    profile: Annotated[str, typer.Option("--profile", "-p", help="Credential profile name.")] = DEFAULT_PROFILE,
    api_key: Annotated[str | None, typer.Option("--api-key", hidden=True)] = None,
    api_url: Annotated[
        str | None,
        typer.Option(
            "--api-url",
            "-a",
            help="Rebase API URL to store for this profile. Useful for local development with port-forwarding.",
        ),
    ] = None,
    verify: Annotated[
        bool,
        typer.Option(
            "--verify/--no-verify",
            "-v",
            help="Verify an API key against the hosted Rebase API before saving it.",
        ),
    ] = True,
    provider: Annotated[str | None, typer.Option("--provider", help="Supabase social auth provider.")] = None,
    force_auth: Annotated[
        bool,
        typer.Option("--force-auth", "-f", help="Ignore any stored Supabase session and authenticate again."),
    ] = False,
    callback_port: Annotated[
        int, typer.Option("--callback-port", "-c", help="Local Supabase OAuth callback port.")
    ] = 17658,
    auth_timeout: Annotated[
        float,
        typer.Option("--auth-timeout", help="Seconds to wait for Supabase auth callback."),
    ] = 300,
    no_browser: Annotated[
        bool,
        typer.Option("--no-browser", "-n", help="Print auth URLs instead of opening the browser."),
    ] = False,
    workspace: Annotated[str | None, typer.Option("--workspace", "-w", help="Workspace id to use or create.")] = None,
    workspace_name: Annotated[
        str | None,
        typer.Option("--workspace-name", help="Workspace display name when creating a workspace."),
    ] = None,
    handle: Annotated[
        str | None,
        typer.Option("--handle", help="Unique Rebase user handle to claim during setup."),
    ] = None,
) -> None:
    """Authenticate and select a Rebase workspace on this computer."""
    if api_key is None:
        from rebase.setup import run_setup

        try:
            run_setup(
                SimpleNamespace(
                    profile=profile,
                    api_url=api_url,
                    provider=provider,
                    force_auth=force_auth,
                    callback_port=callback_port,
                    auth_timeout=auth_timeout,
                    no_browser=no_browser,
                    workspace=workspace,
                    workspace_name=workspace_name,
                    handle=handle,
                )
            )
        except KeyboardInterrupt:
            error_console.print("Aborted.", style="rebase.error")
            raise SystemExit(130) from None
        return

    api_key = api_key.strip()
    if not api_key:
        api_key = getpass.getpass("Rebase API key: ").strip()
    if not api_key:
        raise RebaseWorkflowError("API key is required")

    workspace: dict[str, Any] | None = None
    if verify:
        verify_url = api_url or DEFAULT_SERVER_URL
        try:
            workspace = Client(api_key=api_key, api_url=verify_url).get_workspace()
        except Exception as exc:
            raise RebaseWorkflowError(f"could not verify API key against {verify_url}: {exc}") from exc

    path = write_profile(api_key=api_key, profile=profile, api_url=api_url, workspace=workspace)
    console.print(f"Saved Rebase credentials for profile '[rebase.value]{profile}[/rebase.value]'")
    console.print(f"[rebase.muted]{path}[/rebase.muted]")


@app.command("init")
def init_command(
    workspace: Annotated[
        str | None,
        typer.Argument(help="Workspace to connect this repository to. Omit to choose from your workspaces."),
    ] = None,
    directory: Annotated[
        str | None,
        typer.Option("--directory", "-d", help="Directory to mark. Defaults to the current git root."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Replace a marker that names a different workspace."),
    ] = False,
) -> None:
    """Connect this repository to a workspace you already belong to.

    Writes the committed `.rebase/config.json` marker and nothing else — no sign-in,
    no new workspace, and the machine's active workspace is left alone. Use
    `rebase setup` to authenticate or to create a workspace.
    """
    from rebase.init import init_repository

    init_repository(workspace=workspace, directory=directory, force=force)


@app.command("tui")
def tui_command(
    project: Annotated[
        str | None,
        typer.Option("--project", "-p", help="Filter functions and workflows by project name."),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", min=1, max=500, help="Maximum latest runs to load per selected target."),
    ] = 100,
    refresh_interval: Annotated[
        float | None,
        typer.Option(
            "--refresh-interval",
            "-i",
            min=0,
            max=3600,
            help="Seconds between automatic refreshes of what is on screen. 0 turns it off.",
        ),
    ] = None,
) -> None:
    """Open the Rebase terminal UI."""
    # Imported here, not at module scope: pulling in textual costs every other command
    # startup time. Which is also why the default lives in tui rather than being repeated
    # in this signature.
    from rebase.tui import AUTO_REFRESH_SECONDS, run_tui

    run_tui(
        project=project,
        limit=limit,
        refresh_interval=AUTO_REFRESH_SECONDS if refresh_interval is None else refresh_interval,
    )


def _profile_list_data() -> dict[str, Any]:
    profiles = list_profiles()
    if not profiles:
        raise RebaseWorkflowError("no Rebase profiles found. Run `rebase setup` first.")
    active_profile = selected_profile_name()
    session, auth_error = _load_auth_session_for_display()
    auth = _auth_session_data(session, auth_error)
    return {
        "active_profile": active_profile,
        "config_file": str(config_path()),
        "auth": auth,
        "profiles": [
            _profile_summary(
                profile_name,
                profile_data,
                active_profile=active_profile,
                has_auth_session=auth["exists"],
            )
            for profile_name, profile_data in sorted(profiles.items())
        ],
    }


def _show_profile(profile: str | None = None, *, json_output: bool = False) -> None:
    data = _profile_show_data(profile)
    if json_output:
        _print_json(data)
        return
    console.print(_profile_show_table(data))


def _switch_profile(profile: str, *, workspace_alias: bool = False) -> None:
    try:
        set_default_profile(profile)
    except KeyError as exc:
        if workspace_alias:
            raise RebaseWorkflowError(
                f"unknown workspace profile: {profile}. Run `rebase setup --profile {profile}` first."
            ) from exc
        raise RebaseWorkflowError(f"unknown profile: {profile}. Run `rebase setup --profile {profile}` first.") from exc
    if workspace_alias:
        console.print(f"Switched workspace profile to '[rebase.value]{profile}[/rebase.value]'")
        return
    console.print(f"Switched profile to '[rebase.value]{profile}[/rebase.value]'")


@profile_app.callback(invoke_without_command=True)
def profile_command(ctx: typer.Context) -> None:
    """Show the active local Rebase CLI profile."""
    if ctx.invoked_subcommand is None:
        _show_profile()


@profile_app.command("list")
def profile_list_command(
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List local Rebase CLI profiles."""
    data = _profile_list_data()
    if json_output:
        _print_json(data)
        return
    profiles = list_profiles()
    console.print(
        _profiles_table(
            profiles,
            active_profile=data["active_profile"],
            has_auth_session=bool(data.get("auth", {}).get("exists")),
        )
    )


@profile_app.command("show")
def profile_show_command(
    profile: Annotated[str | None, typer.Argument(help="Profile name. Defaults to the active profile.")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Show one local Rebase CLI profile."""
    _show_profile(profile, json_output=json_output)


@profile_app.command("switch")
def profile_switch_command(profile: Annotated[str, typer.Argument(help="Profile name.")]) -> None:
    """Switch the active local Rebase CLI profile."""
    _switch_profile(profile)


@profile_app.command("logout")
def profile_logout_command() -> None:
    """Clear the stored Supabase auth session without removing local profiles."""
    path = auth_file_path()
    if clear_session(path=path):
        console.print(f"Cleared Rebase auth session at [rebase.value]{path}[/rebase.value]")
        return
    console.print(f"No Rebase auth session found at [rebase.value]{path}[/rebase.value]")


def _show_workspace_memberships() -> None:
    profiles = list_profiles()
    active_profile = selected_profile_name()
    client = Client()
    workspaces = [_workspace_details(client, workspace) for workspace in client.list_my_workspaces()]
    if not workspaces:
        raise RebaseWorkflowError("no workspace memberships found. Run `rebase workspace create` first.")
    console.print(_workspace_membership_table(workspaces, profiles, active_profile=active_profile))


@workspace_app.callback(invoke_without_command=True)
def workspace_command(ctx: typer.Context) -> None:
    """List Rebase workspaces you belong to."""
    if ctx.invoked_subcommand is None:
        _show_workspace_memberships()


@workspace_app.command("list")
def workspace_list_command() -> None:
    """List Rebase workspaces you belong to."""
    _show_workspace_memberships()


@workspace_app.command("create")
def workspace_create_command(
    workspace: Annotated[
        str | None,
        typer.Argument(help="Workspace handle to create."),
    ] = None,
    profile: Annotated[str, typer.Option("--profile", "-p", help="Credential profile name.")] = DEFAULT_PROFILE,
    api_url: Annotated[
        str | None,
        typer.Option(
            "--api-url",
            "-a",
            help="Rebase API URL to store for this profile. Useful for local development with port-forwarding.",
        ),
    ] = None,
    workspace_name: Annotated[
        str | None,
        typer.Option("--workspace-name", "-w", help="Workspace display name."),
    ] = None,
    handle: Annotated[
        str | None,
        typer.Option("--handle", help="Unique Rebase user handle to claim before creating the workspace."),
    ] = None,
    provider: Annotated[str | None, typer.Option("--provider", help="Supabase social auth provider.")] = None,
    force_auth: Annotated[
        bool,
        typer.Option("--force-auth", "-f", help="Ignore any stored Supabase session and authenticate again."),
    ] = False,
    callback_port: Annotated[
        int, typer.Option("--callback-port", "-c", help="Local Supabase OAuth callback port.")
    ] = 17658,
    auth_timeout: Annotated[
        float,
        typer.Option("--auth-timeout", help="Seconds to wait for Supabase auth callback."),
    ] = 300,
    no_browser: Annotated[
        bool,
        typer.Option("--no-browser", "-n", help="Print auth URLs instead of opening the browser."),
    ] = False,
) -> None:
    """Create a workspace and save it as a local profile."""
    from rebase.setup import run_workspace_create

    try:
        run_workspace_create(
            SimpleNamespace(
                profile=profile,
                api_url=api_url,
                workspace=workspace,
                workspace_name=workspace_name,
                handle=handle,
                provider=provider,
                force_auth=force_auth,
                callback_port=callback_port,
                auth_timeout=auth_timeout,
                no_browser=no_browser,
            )
        )
    except KeyboardInterrupt:
        error_console.print("Aborted.", style="rebase.error")
        raise SystemExit(130) from None


def _switch_workspace(workspace: str) -> None:
    """Point the active profile at one of the workspaces it can reach.

    The profile is the identity and stays put; only the selection moves, since
    the workspace travels per request in a header. Membership is checked against
    the server rather than the local config so that a workspace joined on
    another machine is switchable here without re-running setup.
    """
    client = Client()
    memberships = client.list_my_workspaces()
    match = next(
        (
            item
            for item in memberships
            if workspace in {item.get("id"), item.get("workspace_id"), item.get("name")}
        ),
        None,
    )
    if match is None:
        reachable = ", ".join(sorted(str(item.get("name") or item.get("id")) for item in memberships))
        raise RebaseWorkflowError(
            f"not a member of workspace {workspace!r}. This profile can reach: {reachable or '(none)'}"
        )

    workspace_id = str(match.get("id") or match.get("workspace_id") or workspace)
    workspace_name = match.get("name") if isinstance(match.get("name"), str) else None
    profile_name = selected_profile_name()
    try:
        set_profile_workspace(workspace_id, workspace_name, profile=profile_name)
    except KeyError as exc:
        raise RebaseWorkflowError(f"unknown profile: {profile_name}. Run `rebase setup` first.") from exc
    label = workspace_name or workspace_id
    console.print(
        f"Profile '[rebase.value]{profile_name}[/rebase.value]' now uses workspace "
        f"'[rebase.value]{label}[/rebase.value]'"
    )


@workspace_app.command("switch")
def workspace_switch_command(
    workspace: Annotated[str, typer.Argument(help="Workspace name or id you belong to.")],
) -> None:
    """Switch the active workspace, keeping the current profile."""
    _switch_workspace(workspace)


@workspace_app.command("use", hidden=True)
def workspace_use_command(
    workspace: Annotated[str, typer.Argument(help="Workspace name or id you belong to.")],
) -> None:
    """Alias for `rebase workspace switch`."""
    _switch_workspace(workspace)


def _validate_workspace_role(role: str) -> None:
    if role not in WORKSPACE_ROLES:
        raise RebaseWorkflowError(f"role must be one of: {', '.join(WORKSPACE_ROLES)}")


def _resolve_workspace_member(client: Client, target: str) -> dict[str, Any]:
    """Find a workspace member by email, GitHub username, or profile id."""
    needle = target.strip().lstrip("@").lower()
    if not needle:
        raise RebaseWorkflowError("provide a member email, GitHub username, or profile id")
    members = client.list_workspace_members()
    matches = [
        member
        for member in members
        if needle
        in {
            str(member.get("email") or "").lower(),
            str(member.get("github_username") or "").lower(),
            str(member.get("profile_id") or "").lower(),
        }
    ]
    if not matches:
        known = ", ".join(sorted(_workspace_member_identity(member) for member in members)) or "none"
        raise RebaseWorkflowError(f"no workspace member matches {target!r}. Current members: {known}")
    if len(matches) > 1:
        raise RebaseWorkflowError(f"{target!r} matches more than one member; use the profile id instead")
    return matches[0]


def _workspace_invite_identity(
    target: str | None,
    *,
    email: str | None,
    github_username: str | None,
) -> tuple[str | None, str | None]:
    provided = [value for value in (target, email, github_username) if value]
    if len(provided) != 1:
        raise RebaseWorkflowError("provide exactly one invite target: TARGET, --email, or --github")
    if email:
        return email, None
    if github_username:
        return None, github_username
    if target and "@" in target:
        return target, None
    return None, target


@workspace_app.command("invite")
def workspace_invite_command(
    target: Annotated[
        str | None,
        typer.Argument(help="Email address or GitHub username to invite."),
    ] = None,
    email: Annotated[str | None, typer.Option("--email", "-e", help="Email address to invite.")] = None,
    github_username: Annotated[str | None, typer.Option("--github", "-g", help="GitHub username to invite.")] = None,
    role: Annotated[
        str,
        typer.Option("--role", "-r", help="Workspace role: Viewer, Developer, Admin, or Owner."),
    ] = "Viewer",
) -> None:
    """Invite a person to the active workspace."""
    _validate_workspace_role(role)
    invite_email, invite_github_username = _workspace_invite_identity(
        target,
        email=email,
        github_username=github_username,
    )
    invite = Client().create_workspace_invite(
        email=invite_email,
        github_username=invite_github_username,
        role=role,
    )
    identity = invite.get("email") or f"@{invite.get('github_username')}"
    status = invite.get("status", "pending")
    console.print(
        f"Invited [rebase.value]{identity}[/rebase.value] to workspace as "
        f"[rebase.value]{invite.get('role', role)}[/rebase.value] ([rebase.value]{status}[/rebase.value])"
    )


@workspace_app.command("set-role")
def workspace_set_role_command(
    target: Annotated[
        str,
        typer.Argument(help="Member email, GitHub username, or profile id."),
    ],
    role: Annotated[
        str,
        typer.Option("--role", "-r", help="Workspace role: Viewer, Developer, Admin, or Owner."),
    ],
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Change an existing workspace member's role.

    Requires members:write (Owner or Admin). Only an Owner can assign the Owner
    role or modify another Owner, and the last active Owner cannot be demoted.
    """
    _validate_workspace_role(role)
    client = Client()
    member = _resolve_workspace_member(client, target)
    previous_role = _format_value(member.get("role"))
    updated = client.update_workspace_member(str(member["profile_id"]), role=role)
    if json_output:
        _print_json(updated)
        return
    identity = _workspace_member_identity(updated)
    console.print(
        f"Changed [rebase.value]{identity}[/rebase.value] from [rebase.value]{previous_role}[/rebase.value] "
        f"to [rebase.value]{_format_value(updated.get('role', role))}[/rebase.value]"
    )


@workspace_app.command("members")
def workspace_members_command(
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List active workspace members and pending invites."""
    client = Client()
    members = client.list_workspace_members()
    pending_invites = [invite for invite in client.list_workspace_invites() if invite.get("status") == "pending"]
    if json_output:
        _print_json({"members": members, "pending_invites": pending_invites})
        return
    console.print(_workspace_members_table(members, pending_invites))


def _usage_breakdown_table(breakdown: dict[str, Any]) -> Table | None:
    entries = breakdown.get("entries")
    if not isinstance(entries, list) or not entries:
        return None
    currency = str(breakdown.get("currency") or "EUR")
    table = Table(
        title="Spend by target",
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    table.add_column("Target", style="rebase.value")
    table.add_column("Type", style="rebase.muted")
    table.add_column("Runs", justify="right")
    table.add_column("Charged", justify="right")
    table.add_column("Reserved", justify="right", style="rebase.muted")
    for entry in entries:
        target_type = str(entry.get("target_type") or "-")
        name = entry.get("name") or (str(entry.get("target_id"))[:8] if entry.get("target_id") else target_type)
        table.add_row(
            str(name),
            target_type,
            str(entry.get("runs") or 0),
            _format_cents(entry.get("charged_cents"), currency),
            _format_cents(entry.get("reserved_cents"), currency),
        )
    return table


@workspace_app.command("usage")
def workspace_usage_command(
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Show monthly compute credits for the active workspace, and where they went."""
    client = Client()
    usage = client.get_workspace_usage()
    breakdown: dict[str, Any] | None
    try:
        breakdown = client.get_workspace_usage_breakdown()
    except RebaseWorkflowError:
        breakdown = None  # older API without the breakdown route
    if json_output:
        _print_json({**usage, "breakdown": (breakdown or {}).get("entries")})
        return
    console.print(_workspace_usage_table(usage))
    if breakdown is not None:
        table = _usage_breakdown_table(breakdown)
        if table is not None:
            console.print(table)


notifications_app = typer.Typer(
    add_completion=False,
    cls=AlphabeticalTyperGroup,
    help="Configure run failure notifications for the active workspace.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

NOTIFICATION_DETAIL_KEYS = [
    "workspace_id",
    "notify_on_failure",
    "notify_on_stale",
    "webhook_url",
    "has_webhook_secret",
    "updated_at",
]


@notifications_app.command("show")
def workspace_notifications_show_command(
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Show the workspace's failure notification settings."""
    policy = Client().get_workspace_notifications()
    if json_output:
        _print_json(policy)
        return
    console.print(_detail_table("Notification Settings", policy, preferred_keys=NOTIFICATION_DETAIL_KEYS))


@notifications_app.command("set")
def workspace_notifications_set_command(
    webhook_url: Annotated[
        str | None, typer.Option("--webhook-url", "-w", help="HTTPS URL that receives run.failed webhooks.")
    ] = None,
    webhook_secret: Annotated[
        str | None,
        typer.Option("--webhook-secret", help="Secret for the HMAC-SHA256 X-Rebase-Signature header."),
    ] = None,
    on_failure: Annotated[
        bool | None,
        typer.Option("--on-failure/--no-on-failure", "-o", help="Enable or disable run failure notifications."),
    ] = None,
    on_stale: Annotated[
        bool | None,
        typer.Option("--on-stale/--no-on-stale", help="Enable or disable stale dataset notifications."),
    ] = None,
    clear_webhook: Annotated[
        bool, typer.Option("--clear-webhook", "-c", help="Remove the stored webhook URL and secret.")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Update the workspace's failure notification settings."""
    if clear_webhook and (webhook_url is not None or webhook_secret is not None):
        raise RebaseWorkflowError("--clear-webhook cannot be combined with --webhook-url/--webhook-secret")
    if not clear_webhook and webhook_url is None and webhook_secret is None and on_failure is None and on_stale is None:
        raise RebaseWorkflowError(
            "nothing to update; pass --webhook-url, --webhook-secret, --on-failure, --on-stale, or --clear-webhook"
        )
    kwargs: dict[str, Any] = {}
    if on_failure is not None:
        kwargs["notify_on_failure"] = on_failure
    if on_stale is not None:
        kwargs["notify_on_stale"] = on_stale
    if clear_webhook:
        kwargs["webhook_url"] = None
        kwargs["webhook_secret"] = None
    else:
        if webhook_url is not None:
            kwargs["webhook_url"] = webhook_url
        if webhook_secret is not None:
            kwargs["webhook_secret"] = webhook_secret
    policy = Client().update_workspace_notifications(**kwargs)
    if json_output:
        _print_json(policy)
        return
    console.print(_detail_table("Notification Settings", policy, preferred_keys=NOTIFICATION_DETAIL_KEYS))


workspace_app.add_typer(notifications_app, name="notifications")


compute_policy_app = typer.Typer(
    add_completion=False,
    cls=AlphabeticalTyperGroup,
    help="Inspect and adjust the workspace's compute limits.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

COMPUTE_POLICY_DETAIL_KEYS = [
    "workspace_id",
    "max_run_timeout_seconds",
    "max_concurrent_cloud_run_runs",
    "max_cloud_run_instances",
    "max_cloud_run_concurrency",
    "cloud_run_enabled",
    "gpu_allowed",
    "updated_at",
]


@compute_policy_app.command("show")
def workspace_compute_policy_show_command(
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Show the workspace's compute limits."""
    policy = Client().get_workspace_compute_policy()
    if json_output:
        _print_json(policy)
        return
    console.print(_detail_table("Compute Policy", policy, preferred_keys=COMPUTE_POLICY_DETAIL_KEYS))


@compute_policy_app.command("set")
def workspace_compute_policy_set_command(
    max_run_timeout_seconds: Annotated[
        int | None,
        typer.Option(
            "--max-run-timeout-seconds",
            "-m",
            help="Ceiling for a single request, in seconds (max 3600). Raising it requires superadmin.",
        ),
    ] = None,
    max_concurrent_runs: Annotated[
        int | None, typer.Option("--max-concurrent-runs", help="Cloud Run runs allowed in flight at once.")
    ] = None,
    max_instances: Annotated[
        int | None, typer.Option("--max-instances", help="Ceiling for a service's max instance count.")
    ] = None,
    max_concurrency: Annotated[
        int | None, typer.Option("--max-concurrency", help="Ceiling for a service's per-instance concurrency.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Update the workspace's compute limits.

    The policy is a ceiling, not a default: an app still opts in to a longer timeout
    through its own `cloud_run_timeout_seconds`. A longer timeout also costs
    proportionally more credits, since ASGI traffic is charged on elapsed runtime.
    """
    if (
        max_run_timeout_seconds is None
        and max_concurrent_runs is None
        and max_instances is None
        and max_concurrency is None
    ):
        raise RebaseWorkflowError(
            "nothing to update; pass --max-run-timeout-seconds, --max-concurrent-runs, "
            "--max-instances, or --max-concurrency"
        )
    kwargs: dict[str, Any] = {}
    if max_run_timeout_seconds is not None:
        kwargs["max_run_timeout_seconds"] = max_run_timeout_seconds
    if max_concurrent_runs is not None:
        kwargs["max_concurrent_cloud_run_runs"] = max_concurrent_runs
    if max_instances is not None:
        kwargs["max_cloud_run_instances"] = max_instances
    if max_concurrency is not None:
        kwargs["max_cloud_run_concurrency"] = max_concurrency
    policy = Client().update_workspace_compute_policy(**kwargs)
    if json_output:
        _print_json(policy)
        return
    console.print(_detail_table("Compute Policy", policy, preferred_keys=COMPUTE_POLICY_DETAIL_KEYS))


workspace_app.add_typer(compute_policy_app, name="compute-policy")


@environment_app.command("list")
def environment_list_command(
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List deployment environment policies for the active workspace."""
    policies = Client().list_environment_policies()
    if json_output:
        _print_json(policies)
        return
    console.print(_environment_policy_table(policies))


@environment_app.command("create")
def environment_create_command(
    environment: Annotated[str, typer.Argument(help="Environment name.")],
    protected: Annotated[
        bool, typer.Option("--protected/--direct", "-p/-d", help="Create a GitOps-protected environment.")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", "-j")] = False,
) -> None:
    """Create an environment in the active workspace."""
    created = Client().create_environment(
        environment,
        deploy_mode="gitops" if protected else "direct",
        protected=protected,
        require_pr=protected,
        allowed_branches=["main", "master"] if protected else [],
    )
    _print_json(created) if json_output else console.print(_detail_table("Environment", created))


@environment_app.command("show")
def environment_show_command(
    environment: Annotated[str, typer.Argument(help="Environment name.")],
    json_output: Annotated[bool, typer.Option("--json", "-j")] = False,
) -> None:
    """Show one environment."""
    value = Client().get_environment(environment)
    _print_json(value) if json_output else console.print(_detail_table("Environment", value))


@environment_app.command("use")
def environment_use_command(
    environment: Annotated[str, typer.Argument(help="Environment name.")],
) -> None:
    """Select the environment for this workspace on this machine."""
    client = Client()
    client.get_environment(environment)
    workspace_id = client.workspace_id or str(client.get_workspace()["id"])
    set_active_environment(workspace_id, environment)
    console.print(f"Active environment: [rebase.value]{environment}[/rebase.value]")


@environment_app.command("grants")
def environment_grants_command(
    environment: Annotated[str, typer.Argument(help="Environment name.")],
    json_output: Annotated[bool, typer.Option("--json", "-j")] = False,
) -> None:
    """List explicit profile and API-key access grants."""
    grants = Environment.from_name(environment).grants()
    _print_json(grants) if json_output else console.print(_environment_grants_table(grants))


@environment_app.command("grant")
def environment_grant_command(
    environment: Annotated[str, typer.Argument(help="Environment name.")],
    profile_id: Annotated[str | None, typer.Option("--profile-id", "-p", help="Workspace profile UUID.")] = None,
    api_key_id: Annotated[str | None, typer.Option("--api-key-id", help="Workspace API key UUID.")] = None,
    access: Annotated[str, typer.Option("--access", "-a", help="read, write, or admin.")] = "read",
    json_output: Annotated[bool, typer.Option("--json", "-j")] = False,
) -> None:
    """Grant one profile or API key access to an environment."""
    grant = Environment.from_name(environment).grant(
        profile_id=profile_id,
        api_key_id=api_key_id,
        access=access,
    )
    _print_json(grant) if json_output else console.print(_detail_table("Environment Grant", grant))


@environment_app.command("revoke-grant")
def environment_revoke_grant_command(
    environment: Annotated[str, typer.Argument(help="Environment name.")],
    grant_id: Annotated[str, typer.Argument(help="Environment grant UUID.")],
) -> None:
    """Remove one explicit environment access grant."""
    Environment.from_name(environment).revoke(grant_id)
    console.print(f"Revoked environment grant [rebase.value]{grant_id}[/rebase.value]")


@environment_app.command("delete")
def environment_delete_command(
    environment: Annotated[str, typer.Argument(help="Empty, unprotected environment to delete.")],
) -> None:
    """Delete an empty, unprotected environment."""
    Client().delete_environment(environment)
    console.print(f"Deleted environment [rebase.value]{environment}[/rebase.value]")


@environment_app.command("track-project")
def environment_track_project_command(
    environment: Annotated[str, typer.Argument(help="Environment name.")],
    project: Annotated[str, typer.Argument(help="Project name in that environment.")],
    connection_id: Annotated[str, typer.Option("--connection", "-c", help="GitHub repository connection ID.")],
    ref: Annotated[str, typer.Option("--ref", "-r", help="Tracked branch or tag ref.")],
    entrypoint: Annotated[str, typer.Option("--entrypoint", "-e", help="Python declaration file.")],
    repo_path: Annotated[str | None, typer.Option("--repo-path")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j")] = False,
) -> None:
    """Bind an environment project to a GitHub ref and Python entrypoint."""
    client = Client(environment_name=environment)
    target = client.ensure_project(project, environment_name=environment)
    track = client.track_project(
        str(target["id"]),
        github_connection_id=connection_id,
        tracked_ref=ref,
        entrypoint=entrypoint,
        repo_path=repo_path,
    )
    _print_json(track) if json_output else console.print(_detail_table("Project Git Track", track))


@environment_app.command("protect")
def environment_protect_command(
    environment: Annotated[str, typer.Argument(help="Environment to protect, for example prod or staging.")],
    allowed_branch: Annotated[
        list[str] | None,
        typer.Option(
            "--allowed-branch",
            "-a",
            help="Branch allowed for GitOps reconciliation. Can be passed more than once.",
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Require GitOps for an environment."""
    policy = Client().update_environment_policy(
        environment,
        deploy_mode="gitops",
        protected=True,
        require_pr=True,
        allowed_branches=allowed_branch or ["main", "master"],
    )
    _print_json(policy) if json_output else console.print(_detail_table("Environment Policy", policy))


@environment_app.command("unprotect")
def environment_unprotect_command(
    environment: Annotated[str, typer.Argument(help="Environment to allow direct deploys for.")],
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Allow direct deploys for an environment."""
    policy = Client().update_environment_policy(
        environment,
        deploy_mode="direct",
        protected=False,
        require_pr=False,
        allowed_branches=[],
    )
    _print_json(policy) if json_output else console.print(_detail_table("Environment Policy", policy))


app.add_typer(profile_app, name="profile")
app.add_typer(workspace_app, name="workspace")
app.add_typer(environment_app, name="environment")


@connect_app.command("github")
def connect_github_command(
    profile: Annotated[
        str | None,
        typer.Option("--profile", "-p", help="Credential profile name. Defaults to the active workspace profile."),
    ] = None,
    api_url: Annotated[
        str | None,
        typer.Option(
            "--api-url",
            "-a",
            help="Rebase API URL to use for this connection. Useful for local development with port-forwarding.",
        ),
    ] = None,
    no_browser: Annotated[
        bool,
        typer.Option("--no-browser", "-n", help="Print GitHub URLs instead of opening the browser."),
    ] = False,
    github_installation_id: Annotated[
        int | None,
        typer.Option("--github-installation-id", "-g", help="Existing GitHub App installation id."),
    ] = None,
    github_timeout: Annotated[
        float,
        typer.Option("--github-timeout", help="Seconds to wait for GitHub installation."),
    ] = 300,
    poll_interval: Annotated[
        float,
        typer.Option("--poll-interval", help="Seconds between GitHub setup status checks."),
    ] = 1.0,
    repo: Annotated[
        str | None,
        typer.Option("--repo", "-r", help="GitHub repository full name, for example owner/name."),
    ] = None,
    repo_path: Annotated[
        str | None,
        typer.Option("--repo-path", help="Optional path inside the repository."),
    ] = None,
    create_repo: Annotated[
        bool,
        typer.Option("--create-repo", "-c", help="Open GitHub to create a repository before installing the app."),
    ] = False,
) -> None:
    """Connect and verify workspace-level GitHub source backing."""
    from rebase.setup import run_connect_github

    try:
        run_connect_github(
            SimpleNamespace(
                profile=profile,
                api_url=api_url,
                no_browser=no_browser,
                github_installation_id=github_installation_id,
                github_timeout=github_timeout,
                poll_interval=poll_interval,
                repo=repo,
                repo_path=repo_path,
                create_repo=create_repo,
            )
        )
    except KeyboardInterrupt:
        error_console.print("Aborted.", style="rebase.error")
        raise SystemExit(130) from None


@connect_app.command("gitlab")
def connect_gitlab_command(
    repo: Annotated[
        str | None,
        typer.Argument(help="Project path with namespace, e.g. group/project. Defaults to the local git origin."),
    ] = None,
    token: Annotated[
        str | None,
        typer.Option("--token", "-t", help="GitLab access token. Falls back to $GITLAB_ACCESS_TOKEN, then a prompt."),
    ] = None,
    host: Annotated[str, typer.Option("--host", help="GitLab host; self-managed instances supported.")] = "gitlab.com",
    scope: Annotated[str, typer.Option("--scope", "-s", help="Connection scope: workspace or project.")] = "workspace",
    project: Annotated[str | None, typer.Option("--project", "-p", help="Rebase project name (scope=project).")] = None,
    repo_path: Annotated[
        str | None, typer.Option("--repo-path", "-r", help="Folder inside the repo, e.g. projects/x.")
    ] = None,
    branch: Annotated[str | None, typer.Option("--branch", "-b", help="Default branch override.")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Connect a GitLab repository with an access token.

    The token is validated against GitLab, stored in the platform's Secret Manager
    (never in the Rebase database), and used for repo reads, starter workflows, and
    promotion merge requests. Reconnecting rotates the stored token.
    """
    import os as _os
    import subprocess as _subprocess

    if scope not in {"workspace", "project"}:
        raise RebaseWorkflowError("scope must be workspace or project")
    resolved_token = token or _os.environ.get("GITLAB_ACCESS_TOKEN")
    if not resolved_token:
        resolved_token = typer.prompt("GitLab access token", hide_input=True)
    if repo is None:
        try:
            remote = _subprocess.run(
                ["git", "remote", "get-url", "origin"], capture_output=True, text=True, check=True
            ).stdout.strip()
        except (OSError, _subprocess.CalledProcessError):
            remote = None
        from rebase.client import _parse_gitlab_remote

        owner, name = _parse_gitlab_remote(remote)
        if owner is None or name is None:
            raise RebaseWorkflowError("pass REPO (group/project): the local git origin is not a GitLab remote")
        repo = f"{owner}/{name}"
    client = Client()
    resolved_project_id = None
    if scope == "project":
        if project is None:
            raise RebaseWorkflowError("--project is required for --scope project")
        resolved_project_id = str(_resolve_project_by_name(client, project)["id"])
    connection = client.connect_gitlab_repo(
        scope=scope,
        repo=repo,
        token=resolved_token,
        host=host,
        repo_path=repo_path,
        default_branch=branch,
        project_id=resolved_project_id,
    )
    if json_output:
        _print_json(connection)
        return
    console.print(
        _detail_table(
            "Connected GitLab Repository",
            connection,
            preferred_keys=["id", "host", "repo_owner", "repo_name", "scope", "default_branch", "enabled"],
        )
    )
    console.print("Token stored in Secret Manager; reconnect any time to rotate it.")


@connect_app.command("huggingface")
def connect_huggingface_command(
    profile: Annotated[str, typer.Option("--profile", "-p", help="Credential profile name.")] = DEFAULT_PROFILE,
    api_url: Annotated[
        str | None,
        typer.Option(
            "--api-url",
            "-a",
            help="Rebase API URL to read setup configuration from. Useful for local development.",
        ),
    ] = None,
    client_id: Annotated[
        str | None,
        typer.Option(
            "--client-id",
            "-c",
            help="Hugging Face public OAuth app client id. Defaults to REBASE_HUGGINGFACE_OAUTH_CLIENT_ID.",
        ),
    ] = None,
    scope: Annotated[
        list[str] | None,
        typer.Option("--scope", "-s", help="Hugging Face OAuth scope. Repeat to override the default publish scopes."),
    ] = None,
    no_browser: Annotated[
        bool,
        typer.Option(
            "--no-browser", "-n", help="Print the Hugging Face authorization URL instead of opening a browser."
        ),
    ] = False,
    timeout: Annotated[
        float,
        typer.Option("--timeout", "-t", help="Seconds to wait for Hugging Face authorization."),
    ] = 900,
    poll_interval: Annotated[
        float | None,
        typer.Option("--poll-interval", help="Seconds between Hugging Face token polls."),
    ] = None,
    add_to_git_credential: Annotated[
        bool,
        typer.Option(
            "--add-to-git-credential/--no-add-to-git-credential",
            help="Also store the token in Git's credential helper for Hugging Face Git operations.",
        ),
    ] = False,
) -> None:
    """Connect local Hugging Face auth for model publication."""
    from rebase.setup import run_connect_huggingface

    try:
        run_connect_huggingface(
            SimpleNamespace(
                profile=profile,
                api_url=api_url,
                client_id=client_id,
                scope=scope,
                no_browser=no_browser,
                timeout=timeout,
                poll_interval=poll_interval,
                add_to_git_credential=add_to_git_credential,
            )
        )
    except KeyboardInterrupt:
        error_console.print("Aborted.", style="rebase.error")
        raise SystemExit(130) from None


app.add_typer(connect_app, name="connect")


def _parse_secret_keyvalues(entries: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for entry in entries:
        key, separator, value = entry.partition("=")
        if not separator or not key:
            raise RebaseWorkflowError(f"expected KEY=value, got {entry!r}")
        values[key] = value
    return values


@secret_app.command("create")
def secret_create_command(
    name: Annotated[str, typer.Argument(help="Bundle name to reference with rebase.Secret.from_name(...).")],
    keyvalues: Annotated[
        list[str] | None,
        typer.Argument(help="KEY=value pairs. Use KEY=- to read that value from stdin."),
    ] = None,
    from_dotenv: Annotated[
        str | None,
        typer.Option("--from-dotenv", "-f", help="Read KEY=value pairs from a dotenv file."),
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite the bundle if it already exists.")] = False,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Create a workspace secret bundle: a named set of environment variables.

    Values are written to Secret Manager and never stored by Rebase. Attach the bundle in
    code with secrets=[rebase.Secret.from_name(NAME)]; every key becomes an env var.
    """
    values = _parse_secret_keyvalues(keyvalues or [])
    if from_dotenv is not None:
        from rebase.client import Secret

        values = {**Secret.from_dotenv(from_dotenv).env_dict, **values}  # type: ignore[dict-item]
    stdin_keys = [key for key, value in values.items() if value == "-"]
    if len(stdin_keys) > 1:
        raise RebaseWorkflowError("only one KEY=- may read from stdin")
    for key in stdin_keys:
        values[key] = sys.stdin.read().rstrip("\n")
    if not values:
        raise RebaseWorkflowError("provide KEY=value pairs or --from-dotenv")
    client = Client()
    if not force and any(existing.get("name") == name for existing in client.list_secrets()):
        raise RebaseWorkflowError(f"secret {name!r} already exists; pass --force to overwrite")
    secret = client.set_secret(name, values)
    if json_output:
        _print_json(secret)
        return
    keys = ", ".join(sorted((secret.get("secret_refs") or {}).keys()))
    console.print(f"Created secret [rebase.value]{secret.get('name', name)}[/rebase.value] with keys: {keys}")
    # Name the workspace explicitly. `rebase workspace list` marks one profile
    # active, but a mutating command resolves its target separately, and when the
    # two disagree the write lands somewhere else entirely -- silently, because
    # nothing in the output says where it went. Secrets are the worst case: this
    # put a service-account key and a bot token in an unrelated workspace.
    if client.workspace_id:
        console.print(f"Workspace:   [rebase.value]{client.workspace_id}[/rebase.value]")
    console.print(
        f'Use it with: [rebase.value]secrets=[rebase.Secret.from_name("{secret.get("name", name)}")][/rebase.value]'
    )


@secret_app.command("list")
def secret_list_command(
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List workspace secret bundles and their env keys (never values)."""
    secrets = Client().list_secrets()
    if json_output:
        _print_json(secrets)
        return
    if not secrets:
        console.print("No workspace secrets yet. Create one with: rebase secret create NAME KEY=value")
        return
    for secret in secrets:
        keys = ", ".join(secret.get("keys") or [])
        console.print(f"[rebase.value]{secret.get('name')}[/rebase.value]  ({keys})")


@secret_app.command("delete")
def secret_delete_command(
    name: Annotated[str, typer.Argument(help="Bundle name to delete.")],
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Delete a workspace secret bundle and all of its keys."""
    deleted = Client().delete_secret(name)
    if json_output:
        _print_json(deleted)
        return
    console.print(f"Deleted secret [rebase.value]{deleted.get('name', name)}[/rebase.value]")


volume_app = typer.Typer(
    add_completion=False,
    cls=AlphabeticalTyperGroup,
    help="Persistent file volumes mounted into deployed functions and apps.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

VOLUME_DETAIL_KEYS = ["name", "provider", "bucket", "prefix", "workspace_id", "created_at"]


@volume_app.command("create")
def volume_create_command(
    name: Annotated[str, typer.Argument(help="Volume name (lowercase letters, digits, '.', '_', '-').")],
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Create a volume (idempotent: returns the existing one if present)."""
    volume = Client().create_volume(name)
    if json_output:
        _print_json(volume)
        return
    console.print(_detail_table("Volume", volume, preferred_keys=VOLUME_DETAIL_KEYS))


@volume_app.command("list")
def volume_list_command(
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List volumes in the active workspace."""
    volumes = Client().list_volumes()
    if json_output:
        _print_json(volumes)
        return
    table = Table(
        title="Volumes",
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    table.add_column("Name", style="rebase.value")
    table.add_column("Provider")
    table.add_column("Bucket", style="rebase.muted")
    table.add_column("Created", style="rebase.muted")
    for volume in volumes:
        table.add_row(
            str(volume.get("name", "-")),
            _format_value(volume.get("provider")),
            str(volume.get("bucket", "-")),
            _format_value(volume.get("created_at")),
        )
    console.print(table)


@volume_app.command("get")
def volume_get_command(
    name: Annotated[str, typer.Argument(help="Volume name.")],
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Show volume metadata."""
    volume = Client().get_volume(name)
    if json_output:
        _print_json(volume)
        return
    console.print(_detail_table("Volume", volume, preferred_keys=VOLUME_DETAIL_KEYS))


@volume_app.command("ls")
def volume_ls_command(
    name: Annotated[str, typer.Argument(help="Volume name.")],
    path: Annotated[str, typer.Argument(help="Path prefix inside the volume.")] = "",
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List files in a volume."""
    objects = Client().list_volume_objects(name, prefix=path)
    if json_output:
        _print_json(objects)
        return
    if not objects:
        console.print(f"[rebase.muted]Volume {name} has no files{f' under {path}' if path else ''}.[/rebase.muted]")
        return
    table = Table(
        title=f"Volume {name}",
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    table.add_column("Path", style="rebase.value")
    table.add_column("Size", justify="right")
    table.add_column("Updated", style="rebase.muted")
    for item in objects:
        table.add_row(
            str(item.get("path", "-")),
            _format_bytes(item.get("size")),
            _format_value(item.get("updated")),
        )
    console.print(table)


def _format_bytes(value: Any) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "-"


@volume_app.command("put")
def volume_put_command(
    name: Annotated[str, typer.Argument(help="Volume name.")],
    local_path: Annotated[str, typer.Argument(help="Local file or directory to upload.")],
    remote_path: Annotated[
        str | None, typer.Argument(help="Destination path in the volume. Defaults to the local name.")
    ] = None,
) -> None:
    """Upload a file or directory into a volume."""
    volume = Volume(name, client=Client())
    source = Path(local_path)
    if source.is_dir():
        written = volume.put_directory(source, remote_path or "")
        console.print(f"[rebase.success]Uploaded {len(written)} files to volume {name}.[/rebase.success]")
        return
    written_path = volume.put_file(source, remote_path)
    console.print(f"[rebase.success]Uploaded {local_path} to {name}/{written_path}.[/rebase.success]")


@volume_app.command("download")
def volume_download_command(
    name: Annotated[str, typer.Argument(help="Volume name.")],
    remote_path: Annotated[str, typer.Argument(help="Path inside the volume.")],
    local_path: Annotated[
        str | None, typer.Argument(help="Local destination. Defaults to the remote file name.")
    ] = None,
) -> None:
    """Download a file from a volume."""
    volume = Volume(name, client=Client())
    destination = Path(local_path) if local_path else Path(Path(remote_path).name)
    volume.get_file(remote_path, destination)
    console.print(f"[rebase.success]Downloaded {name}/{remote_path.lstrip('/')} to {destination}.[/rebase.success]")


@volume_app.command("rm")
def volume_rm_command(
    name: Annotated[str, typer.Argument(help="Volume name.")],
    remote_path: Annotated[str, typer.Argument(help="Path inside the volume.")],
) -> None:
    """Delete one file from a volume."""
    Client().delete_volume_object(name, remote_path.lstrip("/"))
    console.print(f"[rebase.success]Deleted {name}/{remote_path.lstrip('/')}.[/rebase.success]")


@volume_app.command("delete")
def volume_delete_command(
    name: Annotated[str, typer.Argument(help="Volume name.")],
    force: Annotated[bool, typer.Option("--force", "-f", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Delete a volume and every file stored in it."""
    if not force and not typer.confirm(f"Delete volume {name} and ALL of its files?"):
        raise typer.Abort()
    Client().delete_volume(name)
    console.print(f"[rebase.success]Deleted volume {name}.[/rebase.success]")


bucket_app = typer.Typer(
    add_completion=False,
    cls=AlphabeticalTyperGroup,
    help="Object storage buckets, addressed by key. Attach to functions with buckets=[...].",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

BUCKET_DETAIL_KEYS = ["name", "provider", "bucket", "uri", "location", "workspace_id", "created_at"]


@bucket_app.command("create")
def bucket_create_command(
    name: Annotated[str, typer.Argument(help="Bucket name (lowercase letters, digits, '.', '_', '-').")],
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Create a bucket (idempotent: returns the existing one if present)."""
    bucket = Client().create_bucket(name)
    if json_output:
        _print_json(bucket)
        return
    console.print(_detail_table("Bucket", bucket, preferred_keys=BUCKET_DETAIL_KEYS))


@bucket_app.command("list")
def bucket_list_command(
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List buckets in the active workspace."""
    buckets = Client().list_buckets()
    if json_output:
        _print_json(buckets)
        return
    if not buckets:
        console.print("[rebase.muted]No buckets yet.[/rebase.muted]")
        return
    table = Table(
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    table.add_column("Name", style="rebase.value")
    table.add_column("URI", style="rebase.muted")
    table.add_column("Location", style="rebase.muted")
    table.add_column("Created", style="rebase.muted")
    for bucket in buckets:
        table.add_row(
            str(bucket.get("name", "-")),
            _format_value(bucket.get("uri")),
            _format_value(bucket.get("location")),
            _format_value(bucket.get("created_at")),
        )
    console.print(table)


@bucket_app.command("get")
def bucket_get_command(
    name: Annotated[str, typer.Argument(help="Bucket name.")],
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Show bucket metadata."""
    bucket = Client().get_bucket(name)
    if json_output:
        _print_json(bucket)
        return
    console.print(_detail_table("Bucket", bucket, preferred_keys=BUCKET_DETAIL_KEYS))


@bucket_app.command("uri")
def bucket_uri_command(
    name: Annotated[str, typer.Argument(help="Bucket name.")],
) -> None:
    """Print the gs:// URI, for piping into other tools."""
    console.print(Bucket(name, client=Client()).uri)


@bucket_app.command("ls")
def bucket_ls_command(
    name: Annotated[str, typer.Argument(help="Bucket name.")],
    prefix: Annotated[str, typer.Argument(help="Key prefix to list under.")] = "",
    delimiter: Annotated[
        str | None, typer.Option("--delimiter", "-d", help="Group keys by this separator, e.g. '/'.")
    ] = None,
    all_pages: Annotated[bool, typer.Option("--all", "-a", help="Follow pagination and list every object.")] = False,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List objects in a bucket."""
    bucket = Bucket(name, client=Client())
    if all_pages:
        objects = list(bucket.iter_all(prefix))
        prefixes: list[str] = []
        truncated = False
    else:
        page = bucket.list(prefix, delimiter=delimiter)
        objects = page["objects"]
        prefixes = page["prefixes"]
        truncated = bool(page["next_page_token"])
    if json_output:
        _print_json(
            {
                "objects": [
                    {"key": obj.key, "size": obj.size, "updated": obj.updated, "content_type": obj.content_type}
                    for obj in objects
                ],
                "prefixes": prefixes,
                "truncated": truncated,
            }
        )
        return
    if not objects and not prefixes:
        console.print("[rebase.muted]No objects.[/rebase.muted]")
        return
    table = Table(
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    table.add_column("Key", style="rebase.value")
    table.add_column("Size", style="rebase.muted")
    table.add_column("Updated", style="rebase.muted")
    for common in prefixes:
        table.add_row(common, "-", "-")
    for obj in objects:
        table.add_row(obj.key, _format_bytes(obj.size), _format_value(obj.updated))
    console.print(table)
    if truncated:
        console.print("[rebase.muted]More objects remain; pass --all to list them.[/rebase.muted]")


@bucket_app.command("stat")
def bucket_stat_command(
    name: Annotated[str, typer.Argument(help="Bucket name.")],
    key: Annotated[str, typer.Argument(help="Object key.")],
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Show metadata for one object."""
    stat = Client().stat_bucket_object(name, key.lstrip("/"))
    if json_output:
        _print_json(stat)
        return
    console.print(_detail_table("Object", stat))


@bucket_app.command("put")
def bucket_put_command(
    name: Annotated[str, typer.Argument(help="Bucket name.")],
    source: Annotated[str, typer.Argument(help="Local file or directory to upload.")],
    key: Annotated[str | None, typer.Argument(help="Destination key. Defaults to the local name.")] = None,
) -> None:
    """Upload a file or directory into a bucket."""
    bucket = Bucket(name, client=Client())
    path = Path(source)
    if path.is_dir():
        written = bucket.put_directory(source, key or "")
        console.print(f"[rebase.success]Uploaded {len(written)} objects to bucket {name}.[/rebase.success]")
        return
    written_key = bucket.put_file(source, key)
    console.print(f"[rebase.success]Uploaded {source} to {name}/{written_key}.[/rebase.success]")


@bucket_app.command("download")
def bucket_download_command(
    name: Annotated[str, typer.Argument(help="Bucket name.")],
    key: Annotated[str, typer.Argument(help="Object key.")],
    local_path: Annotated[
        str | None, typer.Argument(help="Local destination. Defaults to the key's file name.")
    ] = None,
) -> None:
    """Download one object from a bucket."""
    bucket = Bucket(name, client=Client())
    destination = Path(local_path) if local_path else Path(Path(key).name)
    bucket.download(key, destination)
    console.print(f"[rebase.success]Downloaded {name}/{key.lstrip('/')} to {destination}.[/rebase.success]")


@bucket_app.command("rm")
def bucket_rm_command(
    name: Annotated[str, typer.Argument(help="Bucket name.")],
    # Optional so `rm <name> --recursive` empties the whole bucket, which is what
    # the "bucket is not empty" error from `bucket delete` tells people to run.
    key: Annotated[
        str | None, typer.Argument(help="Object key, or prefix with --recursive. Omit to mean the whole bucket.")
    ] = None,
    recursive: Annotated[
        bool, typer.Option("--recursive", "-r", help="Delete every object under the key as a prefix.")
    ] = False,
    force: Annotated[bool, typer.Option("--force", "-f", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Delete one object, or everything under a prefix."""
    bucket = Bucket(name, client=Client())
    if not recursive:
        if key is None:
            raise RebaseWorkflowError("KEY is required unless --recursive is passed")
        bucket.delete(key)
        console.print(f"[rebase.success]Deleted {name}/{key.lstrip('/')}.[/rebase.success]")
        return
    prefix = (key or "").lstrip("/")
    target = f"{name}/{prefix}" if prefix else f"bucket {name}"
    if not force and not typer.confirm(f"Delete ALL objects in {target}?"):
        raise typer.Abort()
    # Paged from the client: emptying a large bucket in one API call would time
    # out, and the retry would resume against a half-emptied bucket.
    deleted = bucket.delete_prefix(prefix)
    console.print(f"[rebase.success]Deleted {deleted} objects from bucket {name}.[/rebase.success]")


@bucket_app.command("delete")
def bucket_delete_command(
    name: Annotated[str, typer.Argument(help="Bucket name.")],
    force: Annotated[bool, typer.Option("--force", "-f", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Delete an empty bucket.

    Refuses while objects remain; empty it first with `rebase bucket rm <name> <prefix> --recursive`.
    """
    if not force and not typer.confirm(f"Delete bucket {name}?"):
        raise typer.Abort()
    Client().delete_bucket(name)
    console.print(f"[rebase.success]Deleted bucket {name}.[/rebase.success]")


dataset_app = typer.Typer(
    add_completion=False,
    cls=AlphabeticalTyperGroup,
    help="Named datasets that signal on-update workflow triggers when fresh data lands.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

DATASET_DETAIL_KEYS = [
    "name",
    "id",
    "description",
    "watermark",
    "freshness_status",
    "stale_since",
    "last_validation",
    "last_updated_at",
    "last_signal_run_id",
    "workspace_id",
    "created_at",
    "updated_at",
]


def _freshness_status_label(dataset: dict[str, Any]) -> str:
    status = dataset.get("freshness_status")
    if status == "stale":
        since = dataset.get("stale_since")
        return f"stale (since {since})" if since else "stale"
    if status == "fresh":
        return "fresh"
    return "unknown"


def _last_validation_label(dataset: dict[str, Any]) -> str:
    validation = dataset.get("last_validation")
    if not isinstance(validation, dict):
        return "-"
    if validation.get("skipped"):
        return "skipped"
    return "passed" if validation.get("passed") else "failed"


@dataset_app.command("create")
def dataset_create_command(
    name: Annotated[str, typer.Argument(help="Dataset name, e.g. 'nordpool/prices'.")],
    description: Annotated[str | None, typer.Option("--description", "-d", help="Human-readable description.")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Create a dataset (idempotent: returns the existing one if present)."""
    dataset = Client().create_dataset(name, description=description)
    if json_output:
        _print_json(dataset)
        return
    console.print(_detail_table("Dataset", dataset, preferred_keys=DATASET_DETAIL_KEYS))


@dataset_app.command("list")
def dataset_list_command(
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List datasets in the active workspace."""
    datasets = Client().list_datasets()
    if json_output:
        _print_json(datasets)
        return
    table = Table(
        title="Datasets",
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    table.add_column("Name", style="rebase.value", no_wrap=True)
    table.add_column("Watermark")
    table.add_column("Freshness")
    table.add_column("Validation")
    table.add_column("Last Updated", style="rebase.muted")
    for dataset in datasets:
        table.add_row(
            str(dataset.get("name", "-")),
            _format_value(dataset.get("watermark")),
            _freshness_status_label(dataset),
            _last_validation_label(dataset),
            _format_value(dataset.get("last_updated_at")),
        )
    console.print(table)


@dataset_app.command("get")
def dataset_get_command(
    name: Annotated[str, typer.Argument(help="Dataset name.")],
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Show dataset metadata, including its current watermark."""
    dataset = Client().get_dataset(name)
    if json_output:
        _print_json(dataset)
        return
    console.print(_detail_table("Dataset", dataset, preferred_keys=DATASET_DETAIL_KEYS))


@dataset_app.command("delete")
def dataset_delete_command(
    name: Annotated[str, typer.Argument(help="Dataset name.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Delete a dataset (fails while workflow triggers still watch it)."""
    if not yes and not typer.confirm(f"Delete dataset {name}?"):
        raise typer.Abort()
    deleted = Client().delete_dataset(name)
    console.print(f"[rebase.success]Deleted dataset {deleted.get('deleted', name)}.[/rebase.success]")


@dataset_app.command("signal")
def dataset_signal_command(
    name: Annotated[str, typer.Argument(help="Dataset name.")],
    watermark: Annotated[
        str | None,
        typer.Option("--watermark", "-w", help="New watermark as JSON (falls back to a raw string)."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Signal that fresh data landed, firing any listening on-update triggers."""
    parsed: Any = None
    if watermark is not None:
        try:
            parsed = json.loads(watermark)
        except ValueError:
            parsed = watermark
    result = Client().signal_dataset(name, watermark=parsed, source="cli")
    if json_output:
        _print_json(result)
        return
    fired = result.get("fired") or []
    console.print(f"[rebase.success]Signaled dataset {result.get('dataset', name)}.[/rebase.success]")
    if fired:
        console.print(f"Fired {len(fired)} run(s): {', '.join(str(run_id) for run_id in fired)}")
    else:
        console.print("[rebase.muted]No triggers fired.[/rebase.muted]")


@dataset_app.command("listeners")
def dataset_listeners_command(
    name: Annotated[str, typer.Argument(help="Dataset name.")],
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List workflows whose triggers watch this dataset."""
    listeners = Client().list_dataset_listeners(name)
    if json_output:
        _print_json(listeners)
        return
    if not listeners:
        console.print(f"[rebase.muted]No workflows listen to dataset {name}.[/rebase.muted]")
        return
    for listener in listeners:
        console.print(f"[rebase.value]{listener}[/rebase.value]")


@dataset_app.command("validate")
def dataset_validate_command(
    name: Annotated[str, typer.Argument(help="Dataset name.")],
    file: Annotated[str, typer.Argument(help="Local .parquet or .csv file to validate.")],
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Validate a local file against the dataset's stored contract (exit code 1 on failure)."""
    try:
        import pandas as pd
    except ImportError as exc:
        raise RebaseWorkflowError(
            "'rebase dataset validate' needs pandas; install it with e.g. pip install 'rebase-toolkit[sources]'"
        ) from exc
    path = Path(file)
    if not path.exists():
        raise RebaseWorkflowError(f"file not found: {file}")
    if path.suffix == ".parquet":
        df = pd.read_parquet(path)
    elif path.suffix == ".csv":
        df = pd.read_csv(path)
    else:
        raise RebaseWorkflowError("file must be a .parquet or .csv file")
    contract = Client().get_dataset(name).get("contract")
    if not contract:
        raise RebaseWorkflowError(f"dataset {name} has no contract; set one via rb.Contract or the write pipeline")
    if path.suffix == ".csv":
        # CSV has no datetime types; coerce the contract's timestamp/date columns so the
        # dtype checks assess the values rather than the file format.
        for column_name, prop in (contract.get("properties") or {}).items():
            if not isinstance(prop, dict) or column_name not in df.columns:
                continue
            if prop.get("type") == "string" and prop.get("format") in {"date-time", "date"}:
                # Unparseable values stay as-is and the dtype check reports them.
                with contextlib.suppress(ValueError, TypeError):
                    df[column_name] = pd.to_datetime(df[column_name])
    report = validate_frame(df, contract, dataset_name=name)
    if json_output:
        _print_json(report.to_payload())
    else:
        summary = "passed" if report.passed else "FAILED"
        console.print(
            f"Validation {summary}: {len(report.failures)} of {report.checks} checks failed ({report.row_count} rows)."
        )
        if report.failures:
            table = Table(
                title="Contract Failures",
                box=box.ASCII,
                border_style="rebase.border",
                header_style="rebase.title",
                show_header=True,
                title_style="rebase.title",
            )
            table.add_column("Check", style="rebase.value")
            table.add_column("Column")
            table.add_column("Rows", justify="right")
            table.add_column("Detail", style="rebase.muted")
            for failure in report.failures:
                table.add_row(failure.check, failure.column or "-", str(failure.count), failure.detail)
            console.print(table)
    if not report.passed:
        raise typer.Exit(code=1)


def _collect_declared_datasets(file: str) -> list[Any]:
    """Import a Python file and return the configured datasets it declared.

    Importing populates the process-level dataset registry, so this catches
    datasets constructed anywhere at import time, not just module attributes.
    """
    from rebase.client import _dataset_registry

    before = set(_dataset_registry)
    _load_module(Path(file))
    declared = [dataset for name, dataset in _dataset_registry.items() if name not in before]
    if not declared:
        raise RebaseWorkflowError(
            f"{file} declares no datasets with a contract or freshness config "
            "(rb.Dataset.from_name(..., contract=..., freshness=...))"
        )
    return declared


def _dataset_config_rows(datasets: list[Any], client: Client) -> tuple[list[tuple[str, str, str, str]], bool, bool]:
    """Diff each dataset against the platform. Returns (rows, has_drift, has_error)."""
    from rebase.client import _config_diff_lines

    rows: list[tuple[str, str, str, str]] = []
    has_drift = False
    has_error = False
    for dataset in datasets:
        try:
            diff = dataset.config_diff(client)
        except Exception as exc:
            rows.append((dataset.name, "-", "error", str(exc)))
            has_error = True
            continue
        if not diff:
            rows.append((dataset.name, "-", "in-sync", ""))
            continue
        for key, (stored, local) in sorted(diff.items()):
            if stored is None:
                rows.append((dataset.name, key, "new", "declared in code, not yet published"))
            else:
                has_drift = True
                rows.append((dataset.name, key, "drift", "; ".join(_config_diff_lines(key, stored, local))))
    return rows, has_drift, has_error


def _render_dataset_config_rows(rows: list[tuple[str, str, str, str]], *, title: str) -> None:
    table = Table(
        title=title,
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    table.add_column("Dataset", style="rebase.value")
    table.add_column("Key")
    table.add_column("Status")
    table.add_column("Detail", style="rebase.muted")
    for row in rows:
        table.add_row(*row)
    console.print(table)


@dataset_app.command("check")
def dataset_check_command(
    file: Annotated[str, typer.Argument(help="Python file declaring rb.Dataset configs (imported, not run).")],
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Diff in-code dataset contracts/freshness against the platform (CI gate; exit 1 on drift)."""
    datasets = _collect_declared_datasets(file)
    rows, has_drift, has_error = _dataset_config_rows(datasets, Client())
    if json_output:
        _print_json([{"dataset": d, "key": k, "status": s, "detail": detail} for d, k, s, detail in rows])
    else:
        _render_dataset_config_rows(rows, title="Dataset Config Check")
        if has_drift:
            console.print("Drift found. Review and apply with: rebase dataset sync " + file)
    if has_drift or has_error:
        raise typer.Exit(code=1)


@dataset_app.command("sync")
def dataset_sync_command(
    file: Annotated[str, typer.Argument(help="Python file declaring rb.Dataset configs (imported, not run).")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Apply without confirmation.")] = False,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Publish in-code dataset contracts/freshness to the platform (diff, confirm, apply)."""
    client = Client()
    datasets = _collect_declared_datasets(file)
    rows, has_drift, has_error = _dataset_config_rows(datasets, client)
    if has_error:
        _render_dataset_config_rows(rows, title="Dataset Config Sync")
        raise typer.Exit(code=1)
    pending = [dataset for dataset in datasets if dataset.config_diff(client)]
    if not pending:
        if json_output:
            _print_json({"applied": []})
        else:
            _render_dataset_config_rows(rows, title="Dataset Config Sync")
            console.print("All dataset configs are in sync; nothing to apply.")
        return
    if not json_output:
        _render_dataset_config_rows(rows, title="Dataset Config Sync")
    if not yes and not typer.confirm(f"Apply {len(pending)} dataset config change(s)?"):
        raise typer.Exit(code=1)
    applied = []
    for dataset in pending:
        dataset.push_config(client)
        applied.append(dataset.name)
    if json_output:
        _print_json({"applied": applied})
    else:
        console.print(f"Applied: {', '.join(applied)}")


freshness_app = typer.Typer(
    add_completion=False,
    cls=AlphabeticalTyperGroup,
    help="Configure how recently a dataset must have been signalled.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

FRESHNESS_DETAIL_KEYS = ["name", "freshness", "freshness_status", "stale_since", "last_updated_at", "watermark"]


@freshness_app.command("set")
def dataset_freshness_set_command(
    name: Annotated[str, typer.Argument(help="Dataset name.")],
    max_age: Annotated[
        str, typer.Option("--max-age", "-m", help="Maximum age before the dataset is stale, e.g. '45m'.")
    ],
    check_at: Annotated[
        str | None,
        typer.Option(
            "--check-at", "-c", help="Optional five-field cron expression for when to check, e.g. '15 9 * * *'."
        ),
    ] = None,
    timezone: Annotated[
        str | None, typer.Option("--timezone", "-t", help="IANA timezone for --check-at, e.g. 'Europe/Stockholm'.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Set the dataset's freshness policy."""
    cron = Cron(check_at, timezone=timezone) if check_at is not None else None
    freshness = Freshness(max_age, check_at=cron).to_dict()
    dataset = Client().update_dataset(name, freshness=freshness)
    if json_output:
        _print_json(dataset)
        return
    console.print(_detail_table("Dataset Freshness", dataset, preferred_keys=FRESHNESS_DETAIL_KEYS))


@freshness_app.command("show")
def dataset_freshness_show_command(
    name: Annotated[str, typer.Argument(help="Dataset name.")],
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Show the dataset's freshness policy and current status."""
    dataset = Client().get_dataset(name)
    details = {
        "name": dataset.get("name", name),
        "freshness": dataset.get("freshness"),
        "freshness_status": _freshness_status_label(dataset),
        "stale_since": dataset.get("stale_since"),
        "last_updated_at": dataset.get("last_updated_at"),
        "watermark": dataset.get("watermark"),
    }
    if json_output:
        _print_json(details)
        return
    if not dataset.get("freshness"):
        console.print(f"[rebase.muted]Dataset {name} has no freshness policy.[/rebase.muted]")
        return
    console.print(_detail_table("Dataset Freshness", details, preferred_keys=FRESHNESS_DETAIL_KEYS))


@freshness_app.command("clear")
def dataset_freshness_clear_command(
    name: Annotated[str, typer.Argument(help="Dataset name.")],
) -> None:
    """Remove the dataset's freshness policy."""
    Client().update_dataset(name, freshness=None)
    console.print(f"[rebase.success]Cleared freshness policy on dataset {name}.[/rebase.success]")


contract_app = typer.Typer(
    add_completion=False,
    cls=AlphabeticalTyperGroup,
    help="Inspect and manage the schema contract stored on a dataset.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


def _contract_constraints_label(prop: dict[str, Any]) -> str:
    constraints: list[str] = []
    if prop.get("minimum") is not None or prop.get("maximum") is not None:
        low = prop.get("minimum", "-inf") if prop.get("minimum") is not None else "-inf"
        high = prop.get("maximum", "inf") if prop.get("maximum") is not None else "inf"
        constraints.append(f"between [{low}, {high}]")
    if prop.get("enum"):
        constraints.append(f"isin {list(prop['enum'])}")
    return "; ".join(constraints) or "-"


@contract_app.command("show")
def dataset_contract_show_command(
    name: Annotated[str, typer.Argument(help="Dataset name.")],
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Show the dataset's stored contract: columns and table-level policies."""
    from rebase.contract import _property_dtype_label

    contract = Client().get_dataset(name).get("contract")
    if json_output:
        _print_json(contract)
        return
    if not contract:
        console.print(f"[rebase.muted]Dataset {name} has no contract.[/rebase.muted]")
        return
    required = set(contract.get("required") or [])
    columns_table = Table(
        title=f"Contract Columns ({name})",
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    columns_table.add_column("Name", style="rebase.value")
    columns_table.add_column("Type")
    columns_table.add_column("Nullable")
    columns_table.add_column("Constraints", style="rebase.muted")
    for column_name, prop in (contract.get("properties") or {}).items():
        prop = prop if isinstance(prop, dict) else {}
        not_null = column_name in required or bool(prop.get("x-not-null"))
        columns_table.add_row(
            str(column_name),
            _property_dtype_label(prop),
            "no" if not_null else "yes",
            _contract_constraints_label(prop),
        )
    console.print(columns_table)
    policies = dict(contract.get("x-rebase") or {})
    if policies:
        console.print(_detail_table("Contract Policies", policies))


@contract_app.command("clear")
def dataset_contract_clear_command(
    name: Annotated[str, typer.Argument(help="Dataset name.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Remove the dataset's contract."""
    if not yes and not typer.confirm(f"Clear the contract on dataset {name}?"):
        raise typer.Abort()
    Client().update_dataset(name, contract=None)
    console.print(f"[rebase.success]Cleared contract on dataset {name}.[/rebase.success]")


dataset_app.add_typer(freshness_app, name="freshness")
dataset_app.add_typer(contract_app, name="contract")


app.add_typer(secret_app, name="secret")
app.add_typer(volume_app, name="volume")
app.add_typer(bucket_app, name="bucket")
app.add_typer(dataset_app, name="dataset")


@api_key_app.command("list")
def api_key_list_command(
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List workspace API keys."""
    api_keys = Client().list_api_keys()
    if json_output:
        _print_json(api_keys)
        return
    console.print(_api_keys_table(api_keys))


@api_key_app.command("create")
def api_key_create_command(
    name: Annotated[str, typer.Argument(help="Operator-facing API key name.")],
    project: Annotated[str | None, typer.Option("--project", "-p", help="Scope the key to a project name.")] = None,
    project_id: Annotated[
        str | None,
        typer.Option("--project-id", help="Scope the key to an exact project ID."),
    ] = None,
    permission: Annotated[
        list[str] | None,
        typer.Option("--permission", help="Permission to grant. Repeat to override the read-only agent preset."),
    ] = None,
    expires_at: Annotated[
        str | None, typer.Option("--expires-at", "-e", help="ISO datetime when the key expires.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Create a workspace API key."""
    if project is not None and project_id is not None:
        raise RebaseWorkflowError("provide either --project or --project-id, not both")
    client = Client()
    resolved_project_id = project_id
    if project is not None:
        project_data = _resolve_project_by_name(client, project)
        resolved_project_id = str(project_data["id"])
    api_key = client.create_api_key(
        name,
        project_id=resolved_project_id,
        permissions=_selected_api_key_permissions(permission),
        expires_at=expires_at,
    )
    if json_output:
        _print_json(api_key)
        return
    secret = api_key.get("api_key")
    metadata = {key: value for key, value in api_key.items() if key != "api_key"}
    console.print(
        _detail_table(
            "Created API Key",
            metadata,
            preferred_keys=[
                "name",
                "id",
                "key_prefix",
                "project_id",
                "permissions",
                "enabled",
                "expires_at",
                "created_at",
            ],
        )
    )
    if secret:
        console.print(f"API key secret (shown once): [rebase.value]{secret}[/rebase.value]")
        console.print("Store this key securely. It cannot be retrieved again.")


@api_key_app.command("revoke")
def api_key_revoke_command(
    selector: Annotated[str, typer.Argument(help="API key id, key prefix, or unique name.")],
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Revoke a workspace API key."""
    client = Client()
    api_key = _resolve_api_key_selector(client.list_api_keys(), selector)
    revoked = client.revoke_api_key(str(api_key["id"]))
    if json_output:
        _print_json(revoked)
        return
    console.print(
        _detail_table(
            "Revoked API Key",
            revoked,
            preferred_keys=[
                "name",
                "id",
                "key_prefix",
                "enabled",
                "revoked_at",
                "updated_at",
            ],
        )
    )


app.add_typer(api_key_app, name="api-key")


@endpoint_app.command("list")
def endpoint_list_command(
    project: Annotated[str | None, typer.Option("--project", "-p", help="Filter by project name.")] = None,
    project_id: Annotated[str | None, typer.Option("--project-id", help="Filter by exact project ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List workspace endpoints."""
    if project is not None and project_id is not None:
        raise RebaseWorkflowError("provide either --project or --project-id, not both")
    client = Client()
    resolved_project_id = project_id
    if project is not None:
        project_data = _resolve_project_by_name(client, project)
        resolved_project_id = str(project_data["id"])
    endpoints = client.list_endpoints(project_id=resolved_project_id)
    if json_output:
        _print_json(endpoints)
        return
    console.print(_endpoints_table(endpoints))


@endpoint_app.command("get")
def endpoint_get_command(
    selector: Annotated[str, typer.Argument(help="Endpoint id, name, path, or project/name.")],
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Inspect an endpoint."""
    client = Client()
    endpoint = _resolve_endpoint_selector(client.list_endpoints(), selector)
    endpoint = client.get_endpoint(str(endpoint["id"]))
    if json_output:
        _print_json(endpoint)
        return
    console.print(
        _detail_table(
            "Endpoint",
            endpoint,
            preferred_keys=[
                "project_name",
                "name",
                "method",
                "path",
                "auth",
                "mode",
                "enabled",
                "target_type",
                "target_id",
                "target_version_id",
                "url",
                "id",
            ],
        )
    )


@endpoint_app.command("invoke")
def endpoint_invoke_command(
    selector: Annotated[str, typer.Argument(help="Endpoint id, name, path, or project/name.")],
    json_body: Annotated[str | None, typer.Option("--json", "-j", help="JSON object to send to the endpoint.")] = None,
    parameter: Annotated[
        list[str] | None,
        typer.Option("--param", "-p", help="Endpoint parameter as name=json_value. Can be repeated."),
    ] = None,
) -> None:
    """Invoke an endpoint."""
    client = Client()
    endpoint = _resolve_endpoint_selector(client.list_endpoints(), selector)
    parameters = _parse_run_parameters(json_body, parameter)
    result = client.invoke_endpoint(endpoint, parameters)
    _print_json(result)


@endpoint_app.command("disable")
def endpoint_disable_command(
    selector: Annotated[str, typer.Argument(help="Endpoint id, name, path, or project/name.")],
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Disable an endpoint."""
    client = Client()
    endpoint = _resolve_endpoint_selector(client.list_endpoints(), selector)
    disabled = client.disable_endpoint(str(endpoint["id"]))
    if json_output:
        _print_json(disabled)
        return
    console.print(
        _detail_table(
            "Disabled Endpoint",
            disabled,
            preferred_keys=["project_name", "name", "method", "path", "enabled", "url", "id"],
        )
    )


@endpoint_app.command("versions")
def endpoint_versions_command(
    selector: Annotated[str, typer.Argument(help="Endpoint id, name, path, or project/name.")],
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List endpoint versions."""
    client = Client()
    endpoint = _resolve_endpoint_selector(client.list_endpoints(), selector)
    versions = client.list_endpoint_versions(str(endpoint["id"]))
    if json_output:
        _print_json(versions)
        return
    console.print(_version_table("Endpoint Versions", versions))


app.add_typer(endpoint_app, name="endpoint")


@project_app.command("list")
def project_list_command(
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List projects in the active workspace."""
    client = Client()
    projects = client.list_projects()
    if json_output:
        _print_json(projects)
        return
    console.print(_project_table(projects))


@project_app.command("get")
def project_get_command(
    name: Annotated[
        str | None,
        typer.Argument(help="Project name. Omit when using --id."),
    ] = None,
    project_id: Annotated[str | None, typer.Option("--id", "-i", help="Exact project ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Show project metadata."""
    client = Client()
    project = _resolve_project_selector(client, name, project_id=project_id)
    if json_output:
        _print_json(project)
        return
    console.print(
        _detail_table(
            "Project",
            project,
            preferred_keys=[
                "name",
                "id",
                "workspace_id",
                "description",
                "source_mode",
                "repo_owner",
                "repo_name",
                "repo_path",
                "created_at",
                "updated_at",
            ],
        )
    )


@project_app.command("delete")
def project_delete_command(
    name: Annotated[
        str | None,
        typer.Argument(help="Project name. Omit when using --id."),
    ] = None,
    project_id: Annotated[str | None, typer.Option("--id", "-i", help="Exact project ID.")] = None,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Also delete the project's contents and run history."),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Delete a project.

    Refused while the project still holds functions, workflows, apps, models,
    endpoints or runs; pass --force to delete those along with it.
    """
    client = Client()
    project = _resolve_project_selector(client, name, project_id=project_id)
    label = project.get("name") or project["id"]
    prompt = f"Delete project {label} and ALL of its contents?" if force else f"Delete project {label}?"
    if not yes and not typer.confirm(prompt):
        raise typer.Abort()
    client.delete_project(project["id"], force=force)
    console.print(f"[rebase.success]Deleted project {label}.[/rebase.success]")


def _active_workspace_key() -> str:
    profile_name = selected_profile_name()
    return workspace_key(load_profile(profile_name), profile_name)


def _open_in_editor(path: Path, line: int, roots: Sequence[Path] = ()) -> None:
    settings = editor_settings()
    configured = settings.get("command")
    configured_terminal = settings.get("terminal")
    command = resolve_editor(
        configured=configured if isinstance(configured, str) else None,
        configured_terminal=configured_terminal if isinstance(configured_terminal, bool) else None,
    )
    if command is None:
        raise RebaseWorkflowError(NO_EDITOR_HINT)
    argv = build_argv(command, path, line=line, folder=project_folder(path, roots))
    if command.terminal:
        run_foreground(argv)
    else:
        spawn_detached(argv)


@project_app.command("open")
def project_open_command(
    name: Annotated[
        str | None,
        typer.Argument(help="Project name. Omit when using --id."),
    ] = None,
    project_id: Annotated[str | None, typer.Option("--id", "-i", help="Exact project ID.")] = None,
    path_only: Annotated[
        bool,
        typer.Option("--path", "-p", help="Print the resolved path instead of opening an editor."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Open the Python file that declares a project.

    The file is found by searching this workspace's search paths for the
    `rb.project(...)` call that names it, so a file that has moved since it was last
    deployed still resolves. Projects created from the web app have no declaring
    file and will not be found.
    """
    client = Client()
    project = _resolve_project_selector(client, name, project_id=project_id)
    project_name = str(project.get("name") or "")
    roots = [Path(entry) for entry in search_paths(_active_workspace_key())]
    result = find_project_declarations(project_name, roots)

    if json_output:
        _print_json(
            {
                "project": {"id": project.get("id"), "name": project_name},
                "status": result.status,
                "roots": [str(root) for root in result.roots],
                "matches": [
                    {"path": str(match.path), "line": match.line, "column": match.column} for match in result.matches
                ],
                "unresolved": [str(path) for path in result.unresolved],
                "files_scanned": result.files_scanned,
                "files_parsed": result.files_parsed,
                "truncated": result.truncated,
            }
        )
        return

    if path_only:
        # Ambiguity is not an error here: printing every candidate is what makes this
        # usable from a script.
        if not result.matches:
            raise RebaseWorkflowError(describe_failure(result))
        for match in result.matches:
            console.print(f"{match.path}:{match.line}")
        return

    if result.status == "ambiguous":
        listed = "\n".join(f"  {match.path}:{match.line}" for match in result.matches)
        raise RebaseWorkflowError(
            f'{len(result.matches)} files declare project "{project_name}":\n{listed}\n'
            "Open one directly, or narrow the search paths."
        )
    if result.status != "found":
        raise RebaseWorkflowError(describe_failure(result))

    match = result.matches[0]
    _open_in_editor(match.path, match.line, result.roots)
    console.print(f"[rebase.success]Opened {match.path}:{match.line}.[/rebase.success]")


@search_path_app.command("list")
def project_search_path_list_command(
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List the directories searched for this workspace's project files."""
    paths = search_paths(_active_workspace_key())
    if json_output:
        _print_json([{"path": entry, "exists": Path(entry).is_dir()} for entry in paths])
        return
    if not paths:
        console.print(
            "No search paths configured. Start the TUI inside a repository, or add one with: "
            "rebase project search-path add <dir>"
        )
        return
    table = Table(box=box.SIMPLE_HEAD, header_style=f"bold {BRAND_BRIGHT_GREEN}")
    table.add_column("Path")
    table.add_column("Exists")
    for entry in paths:
        table.add_row(entry, "yes" if Path(entry).is_dir() else "no")
    console.print(table)


@search_path_app.command("add")
def project_search_path_add_command(
    directory: Annotated[Path, typer.Argument(help="Directory to search for project files.")],
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Allow a very broad directory such as your home folder."),
    ] = False,
) -> None:
    """Add a directory to this workspace's search paths."""
    if not directory.is_dir():
        raise RebaseWorkflowError(f"not a directory: {directory}")
    if is_risky_root(directory) and not force:
        raise RebaseWorkflowError(
            f"{directory} is broad enough that every lookup would scan it in full. "
            "Pass --force if that is really what you want."
        )
    resolved = directory.expanduser().resolve()
    if add_search_path(_active_workspace_key(), resolved):
        console.print(f"[rebase.success]Added search path {resolved}.[/rebase.success]")
    else:
        console.print(f"{resolved} is already a search path.")


@search_path_app.command("remove")
def project_search_path_remove_command(
    directory: Annotated[Path, typer.Argument(help="Directory to stop searching.")],
) -> None:
    """Remove a directory from this workspace's search paths."""
    resolved = directory.expanduser().resolve()
    if not remove_search_path(_active_workspace_key(), resolved):
        raise RebaseWorkflowError(f"not a search path: {resolved}")
    console.print(f"[rebase.success]Removed search path {resolved}.[/rebase.success]")


project_app.add_typer(search_path_app, name="search-path")
app.add_typer(project_app, name="project")


@function_app.command("list")
def function_list_command(
    project: Annotated[str | None, typer.Option("--project", "-p", help="Filter by project name.")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List functions in the active workspace."""
    client = Client()
    functions, project_names = _list_project_targets(client, project, client.list_functions)
    if json_output:
        _print_json(functions)
        return
    console.print(_function_table(functions, project_names=project_names))


@function_app.command("get")
def function_get_command(
    name: Annotated[
        str | None,
        typer.Argument(help="Function name. Omit when using --id."),
    ] = None,
    project: Annotated[str | None, typer.Option("--project", "-p", help="Project name for name-based lookup.")] = None,
    function_id: Annotated[str | None, typer.Option("--id", "-i", help="Exact function ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Show function metadata."""
    client = Client()
    function = _resolve_function_selector(client, name, function_id=function_id, project_name=project)
    if json_output:
        _print_json(function)
        return
    console.print(
        _detail_table(
            "Function",
            function,
            preferred_keys=[
                "name",
                "id",
                "project_id",
                "workspace_id",
                "description",
                "entrypoint",
                "mode",
                "isolation",
                "enabled",
                "default_parameters",
                "image_spec",
                "image_fingerprint",
                "cloud_run_min_instances",
                "cloud_run_concurrency",
                "cloud_run_service_name",
                "cloud_run_url",
                "current_version_id",
                "deployment_timings",
                "created_at",
                "updated_at",
                "source_code",
            ],
        )
    )


@function_app.command("versions")
def function_versions_command(
    name: Annotated[
        str | None,
        typer.Argument(help="Function name. Omit when using --id."),
    ] = None,
    project: Annotated[str | None, typer.Option("--project", "-p", help="Project name for name-based lookup.")] = None,
    function_id: Annotated[str | None, typer.Option("--id", "-i", help="Exact function ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List versions for a function."""
    client = Client()
    function = _resolve_function_selector(client, name, function_id=function_id, project_name=project)
    versions = client.list_function_versions(str(function["id"]))
    if json_output:
        _print_json(versions)
        return
    console.print(_version_table("Function Versions", versions))


@function_app.command("delete")
def function_delete_command(
    name: Annotated[
        str | None,
        typer.Argument(help="Function name. Omit when using --id."),
    ] = None,
    project: Annotated[str | None, typer.Option("--project", "-p", help="Project name for name-based lookup.")] = None,
    function_id: Annotated[str | None, typer.Option("--id", "-i", help="Exact function ID.")] = None,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Also delete its endpoints and run history."),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Delete a function, its versions and its endpoints.

    Refused while the function still has endpoints or runs; pass --force to
    delete those along with it.
    """
    client = Client()
    function = _resolve_function_selector(client, name, function_id=function_id, project_name=project)
    label = function.get("name") or function["id"]
    if not yes and not typer.confirm(f"Delete function {label}?"):
        raise typer.Abort()
    client.delete_function(function["id"], force=force)
    console.print(f"[rebase.success]Deleted function {label}.[/rebase.success]")


app.add_typer(function_app, name="function")


@workflow_app.command("list")
def workflow_list_command(
    project: Annotated[str | None, typer.Option("--project", "-p", help="Filter by project name.")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List workflows in the active workspace."""
    client = Client()
    workflows, project_names = _list_project_targets(client, project, client.list_workflows)
    if json_output:
        _print_json(workflows)
        return
    console.print(_workflow_table(workflows, project_names=project_names))


@workflow_app.command("get")
def workflow_get_command(
    name: Annotated[
        str | None,
        typer.Argument(help="Workflow name. Omit when using --id."),
    ] = None,
    project: Annotated[str | None, typer.Option("--project", "-p", help="Project name for name-based lookup.")] = None,
    workflow_id: Annotated[str | None, typer.Option("--id", "-i", help="Exact workflow ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Show workflow metadata."""
    client = Client()
    workflow = _resolve_workflow_selector(client, name, workflow_id=workflow_id, project_name=project)
    if json_output:
        _print_json(workflow)
        return
    console.print(
        _detail_table(
            "Workflow",
            workflow,
            preferred_keys=[
                "name",
                "id",
                "project_id",
                "workspace_id",
                "description",
                "entrypoint",
                "flow_ref",
                "mode",
                "isolation",
                "enabled",
                "default_parameters",
                "current_version_id",
                "created_at",
                "updated_at",
                "source_code",
            ],
        )
    )


@workflow_app.command("versions")
def workflow_versions_command(
    name: Annotated[
        str | None,
        typer.Argument(help="Workflow name. Omit when using --id."),
    ] = None,
    project: Annotated[str | None, typer.Option("--project", "-p", help="Project name for name-based lookup.")] = None,
    workflow_id: Annotated[str | None, typer.Option("--id", "-i", help="Exact workflow ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List versions for a workflow."""
    client = Client()
    workflow = _resolve_workflow_selector(client, name, workflow_id=workflow_id, project_name=project)
    versions = client.list_workflow_versions(str(workflow["id"]))
    if json_output:
        _print_json(versions)
        return
    console.print(_version_table("Workflow Versions", versions))


schedule_app = typer.Typer(
    add_completion=False,
    cls=AlphabeticalTyperGroup,
    help="Manage workflow cron schedules.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

SCHEDULE_DETAIL_KEYS = ["workflow", "cron", "timezone", "day_or", "active", "next_run_at", "workflow_id", "version_id"]


def _schedule_detail(workflow: dict[str, Any], schedule_data: dict[str, Any]) -> dict[str, Any]:
    schedule = schedule_data.get("schedule") or {}
    return {
        "workflow": workflow.get("name"),
        "cron": schedule.get("cron"),
        "timezone": schedule.get("timezone"),
        "day_or": schedule.get("day_or", True),
        "active": schedule_data.get("active"),
        "next_run_at": schedule_data.get("next_run_at"),
        "workflow_id": schedule_data.get("workflow_id"),
        "version_id": schedule_data.get("version_id"),
    }


def _require_schedule(client: Client, workflow: dict[str, Any]) -> dict[str, Any]:
    schedule_data = client.get_workflow_schedule(str(workflow["id"]))
    if schedule_data.get("schedule") is None:
        raise RebaseWorkflowError(
            f"workflow {workflow.get('name')} has no schedule; set one with 'rebase workflow schedule set'"
        )
    return schedule_data


def _set_schedule_active(
    name: str | None,
    project: str | None,
    workflow_id: str | None,
    json_output: bool,
    *,
    active: bool,
) -> None:
    client = Client()
    workflow = _resolve_workflow_selector(client, name, workflow_id=workflow_id, project_name=project)
    schedule_data = _require_schedule(client, workflow)
    schedule = dict(schedule_data["schedule"])
    schedule["active"] = active
    client.update_workflow(str(workflow["id"]), schedule=schedule)
    refreshed = client.get_workflow_schedule(str(workflow["id"]))
    if json_output:
        _print_json(refreshed)
        return
    title = "Schedule Resumed" if active else "Schedule Paused"
    console.print(_detail_table(title, _schedule_detail(workflow, refreshed), preferred_keys=SCHEDULE_DETAIL_KEYS))


@schedule_app.command("show")
def workflow_schedule_show_command(
    name: Annotated[str | None, typer.Argument(help="Workflow name. Omit when using --id.")] = None,
    project: Annotated[str | None, typer.Option("--project", "-p", help="Project name for name-based lookup.")] = None,
    workflow_id: Annotated[str | None, typer.Option("--id", "-i", help="Exact workflow ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Show a workflow's schedule and next run time."""
    client = Client()
    workflow = _resolve_workflow_selector(client, name, workflow_id=workflow_id, project_name=project)
    schedule_data = client.get_workflow_schedule(str(workflow["id"]))
    if json_output:
        _print_json(schedule_data)
        return
    if schedule_data.get("schedule") is None:
        console.print(f"[rebase.muted]Workflow {workflow.get('name')} has no schedule.[/rebase.muted]")
        return
    console.print(
        _detail_table(
            "Workflow Schedule", _schedule_detail(workflow, schedule_data), preferred_keys=SCHEDULE_DETAIL_KEYS
        )
    )


@schedule_app.command("set")
def workflow_schedule_set_command(
    name: Annotated[str | None, typer.Argument(help="Workflow name. Omit when using --id.")] = None,
    cron: Annotated[str, typer.Option("--cron", "-c", help='Five-field cron expression, e.g. "0 * * * *".')] = "",
    timezone: Annotated[
        str | None, typer.Option("--timezone", "-t", help="IANA timezone, e.g. Europe/Stockholm.")
    ] = None,
    day_and: Annotated[
        bool, typer.Option("--day-and", "-d", help="Require day-of-month AND day-of-week to match (default OR).")
    ] = False,
    inactive: Annotated[bool, typer.Option("--inactive", "-i", help="Register the schedule paused.")] = False,
    project: Annotated[str | None, typer.Option("--project", "-p", help="Project name for name-based lookup.")] = None,
    workflow_id: Annotated[str | None, typer.Option("--id", help="Exact workflow ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Set or replace a workflow's cron schedule."""
    if not cron.strip():
        raise RebaseWorkflowError("--cron is required (five-field cron expression)")
    client = Client()
    workflow = _resolve_workflow_selector(client, name, workflow_id=workflow_id, project_name=project)
    try:
        schedule = Cron(cron, timezone=timezone, day_or=not day_and, active=not inactive).to_dict()
    except ValueError as exc:
        raise RebaseWorkflowError(str(exc)) from exc
    client.update_workflow(str(workflow["id"]), schedule=schedule)
    refreshed = client.get_workflow_schedule(str(workflow["id"]))
    if json_output:
        _print_json(refreshed)
        return
    console.print(
        _detail_table("Schedule Set", _schedule_detail(workflow, refreshed), preferred_keys=SCHEDULE_DETAIL_KEYS)
    )


@schedule_app.command("clear")
def workflow_schedule_clear_command(
    name: Annotated[str | None, typer.Argument(help="Workflow name. Omit when using --id.")] = None,
    project: Annotated[str | None, typer.Option("--project", "-p", help="Project name for name-based lookup.")] = None,
    workflow_id: Annotated[str | None, typer.Option("--id", "-i", help="Exact workflow ID.")] = None,
) -> None:
    """Remove a workflow's schedule."""
    client = Client()
    workflow = _resolve_workflow_selector(client, name, workflow_id=workflow_id, project_name=project)
    _require_schedule(client, workflow)
    client.update_workflow(str(workflow["id"]), schedule=None)
    console.print(f"[rebase.success]Schedule removed from workflow {workflow.get('name')}.[/rebase.success]")


@schedule_app.command("pause")
def workflow_schedule_pause_command(
    name: Annotated[str | None, typer.Argument(help="Workflow name. Omit when using --id.")] = None,
    project: Annotated[str | None, typer.Option("--project", "-p", help="Project name for name-based lookup.")] = None,
    workflow_id: Annotated[str | None, typer.Option("--id", "-i", help="Exact workflow ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Pause a schedule (keeps it registered; no runs fire)."""
    _set_schedule_active(name, project, workflow_id, json_output, active=False)


@schedule_app.command("resume")
def workflow_schedule_resume_command(
    name: Annotated[str | None, typer.Argument(help="Workflow name. Omit when using --id.")] = None,
    project: Annotated[str | None, typer.Option("--project", "-p", help="Project name for name-based lookup.")] = None,
    workflow_id: Annotated[str | None, typer.Option("--id", "-i", help="Exact workflow ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Resume a paused schedule."""
    _set_schedule_active(name, project, workflow_id, json_output, active=True)


@schedule_app.command("trigger")
def workflow_schedule_trigger_command(
    name: Annotated[str | None, typer.Argument(help="Workflow name. Omit when using --id.")] = None,
    parameter: Annotated[
        list[str] | None,
        typer.Option("--param", "-p", help="Run parameter as name=json_value. Can be repeated."),
    ] = None,
    parameters_json: Annotated[
        str | None, typer.Option("--parameters-json", help="JSON object with run parameters.")
    ] = None,
    wait: Annotated[bool, typer.Option("--wait/--no-wait", "-w", help="Follow the run until it finishes.")] = False,
    timeout: Annotated[int, typer.Option("--timeout", "-t", help="Maximum seconds to wait with --wait.")] = 600,
    project: Annotated[str | None, typer.Option("--project", help="Project name for name-based lookup.")] = None,
    workflow_id: Annotated[str | None, typer.Option("--id", "-i", help="Exact workflow ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Trigger a deployed workflow run now."""
    client = Client()
    workflow = _resolve_workflow_selector(client, name, workflow_id=workflow_id, project_name=project)
    parameters = _parse_run_parameters(parameters_json, parameter)
    run = client.run_workflow(str(workflow["id"]), parameters=parameters)
    if json_output:
        _print_json(run.data)
        return
    if not wait:
        console.print(
            _detail_table(
                "Run Submitted",
                run.data,
                preferred_keys=["id", "status", "target_type", "mode", "isolation", "execution_backend", "created_at"],
            )
        )
        return
    with _run_progress_reporter() as reporter:
        result = _stream_run_result(
            run,
            target_type="workflow",
            reporter=reporter,
            started_at=time.monotonic(),
            timeout=timeout,
            poll_interval=1.0,
        )
    console.print_json(data=result)


@schedule_app.command("list")
def workflow_schedule_list_command(
    project: Annotated[str | None, typer.Option("--project", "-p", help="Filter by project name.")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List workflows that have schedules."""
    client = Client()
    if project is not None:
        project_data = _resolve_project_by_name(client, project)
        workflows = client.list_workflows(project_id=str(project_data["id"]))
    else:
        workflows = client.list_workflows()
    scheduled = [workflow for workflow in workflows if workflow.get("schedule")]
    if json_output:
        _print_json(scheduled)
        return
    table = Table(
        title="Workflow Schedules",
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    table.add_column("Workflow", style="rebase.value")
    table.add_column("Cron")
    table.add_column("Timezone")
    table.add_column("Active")
    table.add_column("Next run")
    table.add_column("ID", style="rebase.muted")
    for workflow in scheduled:
        schedule = workflow.get("schedule") or {}
        table.add_row(
            str(workflow.get("name", "-")),
            str(schedule.get("cron", "-")),
            _format_value(schedule.get("timezone")),
            _format_value(schedule.get("active", True)),
            _format_value(workflow.get("next_run_at")),
            str(workflow.get("id", "-")),
        )
    console.print(table)


trigger_app = typer.Typer(
    add_completion=False,
    cls=AlphabeticalTyperGroup,
    help="Manage workflow event triggers (on-workflow and on-update).",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

TRIGGER_DETAIL_KEYS = [
    "workflow",
    "type",
    "source",
    "on",
    "datasets",
    "require",
    "at_most_every",
    "deadline",
    "active",
    "last_fired_at",
    "next_deadline_at",
    "workflow_id",
    "version_id",
]


def _trigger_detail(workflow: dict[str, Any], trigger_data: dict[str, Any]) -> dict[str, Any]:
    trigger = trigger_data.get("trigger") or {}
    detail: dict[str, Any] = {"workflow": workflow.get("name")}
    detail.update({key: value for key, value in trigger.items() if key != "active"})
    detail["active"] = trigger_data.get("active")
    detail["last_fired_at"] = trigger_data.get("last_fired_at")
    detail["next_deadline_at"] = trigger_data.get("next_deadline_at")
    detail["workflow_id"] = trigger_data.get("workflow_id")
    detail["version_id"] = trigger_data.get("version_id")
    return detail


def _trigger_state_table(state: list[dict[str, Any]]) -> Table:
    table = Table(
        title="Trigger State",
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    table.add_column("Source", style="rebase.value")
    table.add_column("Type")
    table.add_column("On")
    table.add_column("Pending")
    table.add_column("Pending Since", style="rebase.muted")
    table.add_column("Last Event", style="rebase.muted")
    table.add_column("Last Consumed Watermark")
    table.add_column("Last Consumed", style="rebase.muted")
    for entry in state:
        table.add_row(
            str(entry.get("source", "-")),
            _format_value(entry.get("source_type")),
            _format_value(entry.get("on_status")),
            _format_value(entry.get("pending")),
            _format_value(entry.get("pending_since")),
            _format_value(entry.get("last_event_at")),
            _format_value(entry.get("last_consumed_watermark")),
            _format_value(entry.get("last_consumed_at")),
        )
    return table


def _require_trigger(client: Client, workflow: dict[str, Any]) -> dict[str, Any]:
    trigger_data = client.get_workflow_trigger(str(workflow["id"]))
    if trigger_data.get("trigger") is None:
        raise RebaseWorkflowError(
            f"workflow {workflow.get('name')} has no trigger; set one with 'rebase workflow trigger set'"
        )
    return trigger_data


def _set_trigger_active(
    name: str | None,
    project: str | None,
    workflow_id: str | None,
    json_output: bool,
    *,
    active: bool,
) -> None:
    client = Client()
    workflow = _resolve_workflow_selector(client, name, workflow_id=workflow_id, project_name=project)
    trigger_data = _require_trigger(client, workflow)
    trigger = dict(trigger_data["trigger"])
    trigger["active"] = active
    client.update_workflow(str(workflow["id"]), trigger=trigger)
    refreshed = client.get_workflow_trigger(str(workflow["id"]))
    if json_output:
        _print_json(refreshed)
        return
    title = "Trigger Resumed" if active else "Trigger Paused"
    console.print(_detail_table(title, _trigger_detail(workflow, refreshed), preferred_keys=TRIGGER_DETAIL_KEYS))


@trigger_app.command("show")
def workflow_trigger_show_command(
    name: Annotated[str | None, typer.Argument(help="Workflow name. Omit when using --id.")] = None,
    project: Annotated[str | None, typer.Option("--project", "-p", help="Project name for name-based lookup.")] = None,
    workflow_id: Annotated[str | None, typer.Option("--id", "-i", help="Exact workflow ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Show a workflow's trigger, per-source state, and next deadline."""
    client = Client()
    workflow = _resolve_workflow_selector(client, name, workflow_id=workflow_id, project_name=project)
    trigger_data = client.get_workflow_trigger(str(workflow["id"]))
    if json_output:
        _print_json(trigger_data)
        return
    if trigger_data.get("trigger") is None:
        console.print(f"[rebase.muted]Workflow {workflow.get('name')} has no trigger.[/rebase.muted]")
        return
    console.print(
        _detail_table("Workflow Trigger", _trigger_detail(workflow, trigger_data), preferred_keys=TRIGGER_DETAIL_KEYS)
    )
    state = trigger_data.get("state") or []
    if state:
        console.print(_trigger_state_table(state))


@trigger_app.command("set")
def workflow_trigger_set_command(
    name: Annotated[str | None, typer.Argument(help="Workflow name. Omit when using --id.")] = None,
    on_workflow: Annotated[
        str | None,
        typer.Option("--on-workflow", "-o", help="Fire after another workflow, as 'project/workflow'."),
    ] = None,
    on: Annotated[
        str, typer.Option("--on", help="Upstream status to fire on: success, failure, or completion.")
    ] = "success",
    on_update: Annotated[
        list[str] | None,
        typer.Option("--on-update", help="Dataset name to watch. Repeat or comma-separate for several."),
    ] = None,
    require: Annotated[
        str, typer.Option("--require", "-r", help="Fire when 'all' or 'any' watched datasets have updated.")
    ] = "all",
    at_most_every: Annotated[
        str | None, typer.Option("--at-most-every", "-a", help='Debounce window, e.g. "15m" or "1h".')
    ] = None,
    deadline_cron: Annotated[
        str | None, typer.Option("--deadline-cron", "-d", help='Five-field cron deadline, e.g. "0 9 * * *".')
    ] = None,
    deadline_timezone: Annotated[
        str | None, typer.Option("--deadline-timezone", help="IANA timezone for the deadline cron.")
    ] = None,
    project: Annotated[str | None, typer.Option("--project", "-p", help="Project name for name-based lookup.")] = None,
    workflow_id: Annotated[str | None, typer.Option("--id", "-i", help="Exact workflow ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Set or replace a workflow's event trigger."""
    if bool(on_workflow) == bool(on_update):
        raise RebaseWorkflowError("provide exactly one of --on-workflow or --on-update")
    client = Client()
    workflow = _resolve_workflow_selector(client, name, workflow_id=workflow_id, project_name=project)
    try:
        if on_workflow:
            trigger = OnWorkflow(on_workflow, on=on).to_dict()
        else:
            datasets = [part.strip() for item in on_update or [] for part in item.split(",") if part.strip()]
            deadline = Cron(deadline_cron, timezone=deadline_timezone) if deadline_cron else None
            trigger = OnUpdate(
                datasets,
                require=require,
                at_most_every=at_most_every,
                deadline=deadline,
            ).to_dict()
    except (TypeError, ValueError) as exc:
        raise RebaseWorkflowError(str(exc)) from exc
    client.update_workflow(str(workflow["id"]), trigger=trigger)
    refreshed = client.get_workflow_trigger(str(workflow["id"]))
    if json_output:
        _print_json(refreshed)
        return
    console.print(
        _detail_table("Trigger Set", _trigger_detail(workflow, refreshed), preferred_keys=TRIGGER_DETAIL_KEYS)
    )


@trigger_app.command("clear")
def workflow_trigger_clear_command(
    name: Annotated[str | None, typer.Argument(help="Workflow name. Omit when using --id.")] = None,
    project: Annotated[str | None, typer.Option("--project", "-p", help="Project name for name-based lookup.")] = None,
    workflow_id: Annotated[str | None, typer.Option("--id", "-i", help="Exact workflow ID.")] = None,
) -> None:
    """Remove a workflow's trigger."""
    client = Client()
    workflow = _resolve_workflow_selector(client, name, workflow_id=workflow_id, project_name=project)
    _require_trigger(client, workflow)
    client.update_workflow(str(workflow["id"]), trigger=None)
    console.print(f"[rebase.success]Trigger removed from workflow {workflow.get('name')}.[/rebase.success]")


@trigger_app.command("pause")
def workflow_trigger_pause_command(
    name: Annotated[str | None, typer.Argument(help="Workflow name. Omit when using --id.")] = None,
    project: Annotated[str | None, typer.Option("--project", "-p", help="Project name for name-based lookup.")] = None,
    workflow_id: Annotated[str | None, typer.Option("--id", "-i", help="Exact workflow ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Pause a trigger (keeps it registered; no runs fire)."""
    _set_trigger_active(name, project, workflow_id, json_output, active=False)


@trigger_app.command("resume")
def workflow_trigger_resume_command(
    name: Annotated[str | None, typer.Argument(help="Workflow name. Omit when using --id.")] = None,
    project: Annotated[str | None, typer.Option("--project", "-p", help="Project name for name-based lookup.")] = None,
    workflow_id: Annotated[str | None, typer.Option("--id", "-i", help="Exact workflow ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Resume a paused trigger."""
    _set_trigger_active(name, project, workflow_id, json_output, active=True)


@trigger_app.command("list")
def workflow_trigger_list_command(
    project: Annotated[str | None, typer.Option("--project", "-p", help="Filter by project name.")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List workflows that have event triggers."""
    client = Client()
    if project is not None:
        project_data = _resolve_project_by_name(client, project)
        workflows = client.list_workflows(project_id=str(project_data["id"]))
    else:
        workflows = client.list_workflows()
    triggered = [workflow for workflow in workflows if workflow.get("trigger")]
    if json_output:
        _print_json(triggered)
        return
    table = Table(
        title="Workflow Triggers",
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    table.add_column("Workflow", style="rebase.value")
    table.add_column("Type")
    table.add_column("Trigger")
    table.add_column("Active")
    table.add_column("ID", style="rebase.muted")
    for workflow in triggered:
        trigger = workflow.get("trigger") or {}
        table.add_row(
            str(workflow.get("name", "-")),
            _format_value(trigger.get("type")),
            _format_trigger(trigger),
            _format_value(trigger.get("active", True)),
            str(workflow.get("id", "-")),
        )
    console.print(table)


@workflow_app.command("delete")
def workflow_delete_command(
    name: Annotated[
        str | None,
        typer.Argument(help="Workflow name. Omit when using --id."),
    ] = None,
    project: Annotated[str | None, typer.Option("--project", "-p", help="Project name for name-based lookup.")] = None,
    workflow_id: Annotated[str | None, typer.Option("--id", "-i", help="Exact workflow ID.")] = None,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Also delete its endpoints and run history."),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Delete a workflow, its versions, schedule and triggers.

    Refused while the workflow still has endpoints or runs; pass --force to
    delete those along with it.
    """
    client = Client()
    workflow = _resolve_workflow_selector(client, name, workflow_id=workflow_id, project_name=project)
    label = workflow.get("name") or workflow["id"]
    if not yes and not typer.confirm(f"Delete workflow {label}?"):
        raise typer.Abort()
    client.delete_workflow(workflow["id"], force=force)
    console.print(f"[rebase.success]Deleted workflow {label}.[/rebase.success]")


workflow_app.add_typer(schedule_app, name="schedule")
workflow_app.add_typer(trigger_app, name="trigger")
app.add_typer(workflow_app, name="workflow")


@model_app.command("list")
def model_list_command(
    project: Annotated[str | None, typer.Option("--project", "-p", help="Filter by project name.")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List models in the active workspace."""
    client = Client()
    models, project_names = _list_project_targets(client, project, client.list_models)
    if json_output:
        _print_json(models)
        return
    console.print(_model_table(models, project_names=project_names))


@model_app.command("get")
def model_get_command(
    name: Annotated[str | None, typer.Argument(help="Model name. Omit when using --id.")] = None,
    project: Annotated[str | None, typer.Option("--project", "-p", help="Project name for name-based lookup.")] = None,
    model_id: Annotated[str | None, typer.Option("--id", "-i", help="Exact model ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Show model metadata."""
    client = Client()
    model = _resolve_model_selector(client, name, model_id=model_id, project_name=project)
    if json_output:
        _print_json(model)
        return
    console.print(
        _detail_table(
            "Model",
            model,
            preferred_keys=[
                "name",
                "id",
                "project_id",
                "workspace_id",
                "kind",
                "operation_name",
                "description",
                "mode",
                "isolation",
                "enabled",
                "default_parameters",
                "image_spec",
                "image_fingerprint",
                "cloud_run_min_instances",
                "cloud_run_concurrency",
                "current_version_id",
                "created_at",
                "updated_at",
            ],
        )
    )


@model_app.command("deploy")
def model_deploy_command(
    file: Annotated[Path, typer.Argument(help="Python file containing a top-level Rebase model.")],
    name: Annotated[
        list[str] | None,
        typer.Option("--name", "-n", help="Deploy only the top-level variable name or model name."),
    ] = None,
    environment: Annotated[
        str | None, typer.Option("--env", "-e", help="Environment; defaults to the active environment.")
    ] = None,
) -> None:
    """Deploy model objects from a Python file."""
    module = _load_module(file)
    selected_names = set(name or [])
    models = _unique_named_objects(module, (Model,))
    if selected_names:
        models = [
            (object_name, model)
            for object_name, model in models
            if object_name in selected_names or getattr(model, "name", None) in selected_names
        ]
    if not models:
        raise RebaseWorkflowError("No Rebase model objects found in the file.")
    deployed: list[tuple[str, str, str | None]] = []
    for object_name, model in models:
        model.deploy(environment=environment)
        deployed.append((_model_target_type(model), model.name or object_name, model.id))
    console.print(_deploy_table(deployed))


@model_app.command("run")
def model_run_command(
    name: Annotated[str | None, typer.Argument(help="Model name. Omit when using --id.")] = None,
    project: Annotated[str | None, typer.Option("--project", help="Project name for name-based lookup.")] = None,
    model_id: Annotated[str | None, typer.Option("--id", "-i", help="Exact model ID.")] = None,
    environment: Annotated[str, typer.Option("--env", "-e", help="Deployment environment to run.")] = "dev",
    parameter: Annotated[
        list[str] | None,
        typer.Option("--param", "-p", help="Model parameter as name=json_value. Can be passed more than once."),
    ] = None,
    parameters_json: Annotated[str | None, typer.Option("--parameters-json", help="JSON object of parameters.")] = None,
    wait: Annotated[bool, typer.Option("--wait/--no-wait", "-w", help="Wait for the run to finish.")] = True,
    timeout: Annotated[int, typer.Option("--timeout", "-t", help="Maximum seconds to wait.")] = 600,
    poll_interval: Annotated[float, typer.Option("--poll-interval", help="Seconds between status polls.")] = 1.0,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Run a deployed model."""
    client = Client()
    model = _resolve_model_selector(client, name, model_id=model_id, project_name=project)
    parameters = _parse_run_parameters(parameters_json, parameter)
    run = client.run_model(str(model["id"]), parameters, environment=environment)
    if not wait:
        data = run.data or {"id": run.id, "status": run.status}
        _print_json(data) if json_output else console.print(_run_table(run.id, run.status))
        return
    with _run_progress_reporter() as reporter:
        result = _stream_run_result(
            run,
            target_type="model",
            reporter=reporter,
            started_at=time.monotonic(),
            timeout=timeout,
            poll_interval=poll_interval,
        )
    if json_output:
        _print_json(result)
    else:
        console.print_json(data=result)


@model_app.command("versions")
def model_versions_command(
    name: Annotated[str | None, typer.Argument(help="Model name. Omit when using --id.")] = None,
    project: Annotated[str | None, typer.Option("--project", "-p", help="Project name for name-based lookup.")] = None,
    model_id: Annotated[str | None, typer.Option("--id", "-i", help="Exact model ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List versions for a model."""
    client = Client()
    model = _resolve_model_selector(client, name, model_id=model_id, project_name=project)
    versions = client.list_model_versions(str(model["id"]))
    if json_output:
        _print_json(versions)
        return
    console.print(_version_table("Model Versions", versions))


@model_app.command("deployments")
def model_deployments_command(
    name: Annotated[str | None, typer.Argument(help="Model name. Omit when using --id.")] = None,
    project: Annotated[str | None, typer.Option("--project", "-p", help="Project name for name-based lookup.")] = None,
    model_id: Annotated[str | None, typer.Option("--id", "-i", help="Exact model ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List model environment deployments."""
    client = Client()
    model = _resolve_model_selector(client, name, model_id=model_id, project_name=project)
    deployments = client.list_model_deployments(str(model["id"]))
    if json_output:
        _print_json(deployments)
        return
    console.print(_deployment_table(deployments))


@model_app.command("promote")
def model_promote_command(
    name: Annotated[str | None, typer.Argument(help="Model name. Omit when using --id.")] = None,
    project: Annotated[str | None, typer.Option("--project", "-p", help="Project name for name-based lookup.")] = None,
    model_id: Annotated[str | None, typer.Option("--id", "-i", help="Exact model ID.")] = None,
    from_environment: Annotated[str, typer.Option("--from", "-f", help="Source environment.")] = "dev",
    to_environment: Annotated[str, typer.Option("--to", "-t", help="Target environment.")] = "staging",
    model_version_id: Annotated[
        str | None, typer.Option("--version-id", "-v", help="Specific model version ID.")
    ] = None,
    promotion_request_id: Annotated[
        str | None,
        typer.Option("--promotion-request-id", help="Approved request ID required for prod."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Promote a model version between environments."""
    client = Client()
    model = _resolve_model_selector(client, name, model_id=model_id, project_name=project)
    deployment = client.promote_model(
        str(model["id"]),
        from_environment=from_environment,
        to_environment=to_environment,
        model_version_id=model_version_id,
        promotion_request_id=promotion_request_id,
    )
    _print_json(deployment) if json_output else console.print(_detail_table("Model Deployment", deployment))


@model_app.command("request-promotion")
def model_request_promotion_command(
    name: Annotated[str | None, typer.Argument(help="Model name. Omit when using --id.")] = None,
    project: Annotated[str | None, typer.Option("--project", "-p", help="Project name for name-based lookup.")] = None,
    model_id: Annotated[str | None, typer.Option("--id", "-i", help="Exact model ID.")] = None,
    model_version_id: Annotated[str | None, typer.Option("--version-id", "-v", help="Model version ID.")] = None,
    from_environment: Annotated[str, typer.Option("--from", "-f", help="Source environment.")] = "staging",
    to_environment: Annotated[str, typer.Option("--to", "-t", help="Target environment.")] = "prod",
    reason: Annotated[str | None, typer.Option("--reason", "-r", help="Promotion reason.")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Request approval to promote a model to prod."""
    client = Client()
    model = _resolve_model_selector(client, name, model_id=model_id, project_name=project)
    resolved_version_id = model_version_id or str(model.get("current_version_id") or "")
    if not resolved_version_id:
        raise RebaseWorkflowError("model has no current version; pass --version-id")
    request = client.create_model_promotion_request(
        str(model["id"]),
        model_version_id=resolved_version_id,
        from_environment=from_environment,
        to_environment=to_environment,
        reason=reason,
    )
    _print_json(request) if json_output else console.print(_detail_table("Model Promotion Request", request))


@model_app.command("promotion-requests")
def model_promotion_requests_command(
    name: Annotated[str | None, typer.Argument(help="Model name. Omit when using --id.")] = None,
    project: Annotated[str | None, typer.Option("--project", "-p", help="Project name for name-based lookup.")] = None,
    model_id: Annotated[str | None, typer.Option("--id", "-i", help="Exact model ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List promotion requests for a model."""
    client = Client()
    model = _resolve_model_selector(client, name, model_id=model_id, project_name=project)
    requests = client.request("GET", f"/models/{model['id']}/promotion-requests")
    if not isinstance(requests, list):
        raise RebaseWorkflowError("expected model promotion request list response")
    if json_output:
        _print_json(requests)
        return
    console.print(_promotion_request_table(requests))


@model_app.command("approve-promotion")
def model_approve_promotion_command(
    request_id: Annotated[str, typer.Argument(help="Promotion request ID.")],
    reason: Annotated[str | None, typer.Option("--reason", "-r", help="Review reason.")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Approve a model promotion request."""
    request = Client().approve_model_promotion_request(request_id, reason=reason)
    _print_json(request) if json_output else console.print(_detail_table("Model Promotion Request", request))


@model_app.command("reject-promotion")
def model_reject_promotion_command(
    request_id: Annotated[str, typer.Argument(help="Promotion request ID.")],
    reason: Annotated[str | None, typer.Option("--reason", "-r", help="Review reason.")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Reject a model promotion request."""
    request = Client().reject_model_promotion_request(request_id, reason=reason)
    _print_json(request) if json_output else console.print(_detail_table("Model Promotion Request", request))


@model_app.command("rollback")
def model_rollback_command(
    name: Annotated[str | None, typer.Argument(help="Model name. Omit when using --id.")] = None,
    project: Annotated[str | None, typer.Option("--project", "-p", help="Project name for name-based lookup.")] = None,
    model_id: Annotated[str | None, typer.Option("--id", "-i", help="Exact model ID.")] = None,
    environment: Annotated[str, typer.Option("--env", "-e", help="Environment to roll back.")] = "prod",
    model_version_id: Annotated[
        str | None, typer.Option("--version-id", "-v", help="Specific prior version ID.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Roll back a model environment."""
    client = Client()
    model = _resolve_model_selector(client, name, model_id=model_id, project_name=project)
    deployment = client.rollback_model(str(model["id"]), environment=environment, model_version_id=model_version_id)
    _print_json(deployment) if json_output else console.print(_detail_table("Model Deployment", deployment))


@model_app.command("events")
def model_events_command(
    name: Annotated[str | None, typer.Argument(help="Model name. Omit when using --id.")] = None,
    project: Annotated[str | None, typer.Option("--project", "-p", help="Project name for name-based lookup.")] = None,
    model_id: Annotated[str | None, typer.Option("--id", "-i", help="Exact model ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List model audit events."""
    client = Client()
    model = _resolve_model_selector(client, name, model_id=model_id, project_name=project)
    events = client.list_model_events(str(model["id"]))
    if json_output:
        _print_json(events)
        return
    console.print(_detail_table("Latest Model Event", events[0]) if events else _detail_table("Latest Model Event", {}))


app.add_typer(model_app, name="model")


@app.command("deploy")
def deploy_command(
    file: Annotated[Path, typer.Argument(help="Python file containing a top-level Rebase target.")],
    name: Annotated[
        list[str] | None,
        typer.Option(
            "--name",
            "-n",
            help="Deploy only the top-level variable name or Rebase target name. Can be passed more than once.",
        ),
    ] = None,
    source: Annotated[
        str | None,
        typer.Option("--source", "-s", help="Override deploy source for this command: rebase or github."),
    ] = None,
    environment: Annotated[
        str | None, typer.Option("--env", "-e", help="Environment; defaults to the active environment.")
    ] = None,
    plan: Annotated[bool, typer.Option("--plan", "-p", help="Show the deployment path without applying it.")] = False,
    sync: Annotated[bool, typer.Option("--sync", help="Apply a platform-authorized GitOps reconciliation.")] = False,
) -> None:
    """Deploy Rebase objects from a Python file."""
    client = Client()
    environment = environment or getattr(client, "environment_name", "dev")
    policy = _environment_policy(client, environment)
    if _policy_requires_gitops(policy):
        if source == "rebase":
            raise RebaseWorkflowError("protected environments require GitHub-backed source; remove `--source rebase`.")
        if sync:
            if not os.getenv("REBASE_GITOPS_RELEASE_ID"):
                raise RebaseWorkflowError("--sync is reserved for the platform GitOps reconciler.")
            deployed = deploy_file(
                file,
                object_names=name,
                deploy_source=source or "github",
                environment=environment,
            )
            console.print(_deploy_table(deployed))
            return
        if plan:
            metadata = _gitops_source_metadata(file)
            console.print(
                _detail_table(
                    "GitOps Deploy Plan",
                    {
                        "environment": environment,
                        "mode": "gitops",
                        "source_repo": metadata["source_repo"],
                        "source_path": metadata["source_path"],
                        "git_commit_sha": metadata["git_commit_sha"],
                        "git_branch": metadata["git_branch"],
                    },
                )
            )
            return
        intent = _create_gitops_intent_for_deploy(
            client,
            file=file,
            environment=environment,
            object_names=name,
            deploy_source=source or "github",
        )
        console.print(_detail_table("GitOps Deployment Request", intent))
        return
    if plan:
        console.print(
            _detail_table(
                "Deploy Plan",
                {
                    "environment": environment,
                    "mode": "direct",
                    "file": str(file),
                    "object_names": ", ".join(name or []) if name else "all",
                },
            )
        )
        return
    # Stream raw build output while any image builds — without this a first
    # deploy of a new image is a silent blocking call of up to 30 minutes.
    set_build_log_consumer(_build_log_printer())
    try:
        deployed = deploy_file(file, object_names=name, deploy_source=source, environment=environment)
    finally:
        set_build_log_consumer(None)
    console.print(_deploy_table(deployed))


def _build_log_printer() -> Callable[[str], None]:
    """Raw build lines, Modal-style: announce once, then verbatim output.

    markup=False matters — build output is arbitrary text and rich would
    otherwise eat anything in square brackets; highlight=False keeps rich from
    syntax-coloring it. The lines themselves stay full brightness: this is the
    user's own toolchain talking, not decoration.
    """
    announced = False

    def emit(line: str) -> None:
        nonlocal announced
        if not announced:
            console.print("• Building image — streaming build output:", style="rebase.muted", highlight=False)
            announced = True
        console.print(line, markup=False, highlight=False)

    return emit


def _normalize_local_result(result: Any) -> dict[str, Any]:
    # Mirrors the server-side importer.normalize_result contract so --local
    # output has the same shape as a cloud run result.
    if result is None:
        return {}
    if isinstance(result, dict):
        return result
    return {"value": result}


def _run_local_target(
    target_ref: str,
    *,
    as_module: bool,
    parameters_json: str | None,
    parameter: list[str] | None,
) -> None:
    import asyncio
    import inspect

    target = _resolve_run_target(target_ref, as_module=as_module)
    if isinstance(target, Model):
        raise RebaseWorkflowError("Models cannot run with --local; submit a cloud run instead")
    if getattr(target, "fn", None) is None:
        raise RebaseWorkflowError("remote handles cannot run with --local; run against the source file")
    parameters = _parse_run_parameters(parameters_json, parameter)
    kind = "workflow" if isinstance(target, Workflow) else "function"
    console.print(f"[rebase.muted]Running {kind} {target.name or target_ref} locally...[/rebase.muted]")
    result = target(**parameters)
    if inspect.isawaitable(result):
        result = asyncio.run(_await_value(result))
    console.print_json(data=_normalize_local_result(result))


async def _await_value(value: Any) -> Any:
    return await value


RUN_INSPECTION_COMMANDS = {"list", "get", "logs", "cancel", "replay"}


@app.command("shell")
def shell_command(
    name: Annotated[
        str | None,
        typer.Argument(help="Function (default) or workflow name. Omit when using --id."),
    ] = None,
    project: Annotated[str | None, typer.Option("--project", "-p", help="Project name for name-based lookup.")] = None,
    target_id: Annotated[str | None, typer.Option("--id", "-i", help="Exact function or workflow ID.")] = None,
    workflow: Annotated[
        bool,
        typer.Option("--workflow", "-w", help="Open a shell in a workflow's environment instead of a function's."),
    ] = False,
    version_id: Annotated[
        str | None,
        typer.Option("--version-id", "-v", help="Shell into a specific version instead of the current one."),
    ] = None,
    idle_timeout: Annotated[
        int | None,
        typer.Option("--idle-timeout", help="Close the session after this many seconds without activity."),
    ] = None,
    ttl: Annotated[
        int | None,
        typer.Option("--ttl", "-t", help="Hard session limit in seconds (capped by the server)."),
    ] = None,
) -> None:
    """Open an interactive shell in a cloud container with the target's
    image, environment variables, secrets and volume mounts."""
    client = Client()
    if workflow:
        target = _resolve_workflow_selector(client, name, workflow_id=target_id, project_name=project)
        payload: dict[str, Any] = {"workflow_id": str(target["id"])}
    else:
        target = _resolve_function_selector(client, name, function_id=target_id, project_name=project)
        payload = {"function_id": str(target["id"])}
    if version_id is not None:
        payload["version_id"] = version_id
    if idle_timeout is not None:
        payload["idle_timeout_seconds"] = idle_timeout
    if ttl is not None:
        payload["ttl_seconds"] = ttl

    console.print(f"Starting shell container for [bold]{target.get('name', target['id'])}[/bold]…")
    session = client.create_shell_session(payload)
    session_id = str(session["id"])
    relay_ws_url = session.get("relay_ws_url")
    client_token = session.get("client_token")
    if not relay_ws_url or not client_token:
        raise RebaseWorkflowError("the server did not return shell connection details")

    result = None
    try:
        result = _run_shell_bridge(
            relay_ws_url,
            session_id,
            client_token,
            on_waiting=lambda: console.print(
                "Waiting for the container to dial in (first run installs dependencies; may take a minute)…"
            ),
            on_ready=lambda: console.print("Connected. Press Ctrl-D or type 'exit' to end the session.\n"),
        )
    except KeyboardInterrupt:
        console.print("\nCancelled.")
    finally:
        with contextlib.suppress(Exception):
            client.delete_shell_session(session_id)
    if result is not None:
        message = _shell_close_message(result)
        if message is not None:
            error_console.print(f"[{BRAND_CORAL_RED}]{message}[/]")
            raise typer.Exit(code=1)
        console.print("Session ended.")


@app.command("run")
def run_command(
    target_ref: Annotated[
        str,
        typer.Argument(
            help="Target reference: file.py::target_name. Omit ::target_name when the file has one runnable target."
        ),
    ],
    parameter: Annotated[
        list[str] | None,
        typer.Option(
            "--param",
            "-p",
            help="Target parameter as name=json_value. Can be passed more than once.",
        ),
    ] = None,
    parameters_json: Annotated[
        str | None,
        typer.Option("--parameters-json", help="JSON object with target parameters."),
    ] = None,
    mode: Annotated[
        str | None,
        typer.Option("--mode", help="Execution mode: interactive (default) or job."),
    ] = None,
    isolation: Annotated[
        str | None,
        typer.Option("--isolation", "-i", help="Interactive isolation: shared (default) or dedicated."),
    ] = None,
    run_type: Annotated[
        str | None,
        typer.Option(
            "--run-type",
            "-r",
            help="Deprecated alias for execution mode/isolation: quick, quick_shared, or long.",
        ),
    ] = None,
    module: Annotated[
        bool,
        typer.Option("--module", "-m", help="Interpret the target source as a Python module path instead of a file."),
    ] = False,
    wait: Annotated[
        bool,
        typer.Option("--wait/--no-wait", "-w", help="Wait for the function result before exiting."),
    ] = True,
    timeout: Annotated[int, typer.Option("--timeout", "-t", help="Maximum seconds to wait for the result.")] = 600,
    poll_interval: Annotated[
        float,
        typer.Option("--poll-interval", help="Seconds between run status polls."),
    ] = 1.0,
    local: Annotated[
        bool,
        typer.Option("--local", "-l", help="Execute the target in this process instead of submitting a cloud run."),
    ] = False,
) -> None:
    """Run local Rebase targets and inspect submitted runs."""
    if local:
        if mode is not None or isolation is not None or run_type is not None:
            raise RebaseWorkflowError("--local runs in-process; execution options select cloud execution")
        if not wait:
            raise RebaseWorkflowError("--local always runs synchronously; drop --no-wait")
        _run_local_target(target_ref, as_module=module, parameters_json=parameters_json, parameter=parameter)
        return

    run: Run | None = None
    result: dict[str, Any] | None = None
    wait_for_result = wait
    started_at = time.monotonic()
    with _run_progress_reporter() as reporter:
        reporter.update("Loading local Rebase target...")
        target = _resolve_run_target(target_ref, as_module=module)
        reporter.complete("Loaded local Rebase target.")

        execution_override = _validate_execution_override(mode, isolation, run_type, target)
        if execution_override is not None:
            target.mode, target.isolation = execution_override

        parameters = _parse_run_parameters(parameters_json, parameter)
        if isinstance(target, Workflow):
            reporter.update("Packaging workflow source and step graph...")
        elif isinstance(target, Model):
            reporter.update("Packaging model source...")
        else:
            reporter.update("Packaging function source...")
        run = target.ephemeral_run(**parameters)
        if isinstance(target, Workflow):
            reporter.complete("Packaged workflow source and step graph.")
        elif isinstance(target, Model):
            reporter.complete("Packaged model source.")
        else:
            reporter.complete("Packaged function source.")
        reporter.complete(f"Created ephemeral run {run.id}.")

        if not wait:
            reporter.finish("Run submitted.")
        else:
            result = _stream_run_result(
                run,
                target=target,
                reporter=reporter,
                started_at=started_at,
                timeout=timeout,
                poll_interval=poll_interval,
            )

    if run is None:
        raise RebaseWorkflowError("run did not start")
    if not wait_for_result:
        console.print(_run_table(run.id, run.status))
        return
    if result is None:
        raise RebaseWorkflowError("run completed without a result")
    console.print_json(data=result)


@run_app.command("list")
def run_list_command(
    project: Annotated[str | None, typer.Option("--project", "-p", help="Filter by project name.")] = None,
    target_type: Annotated[
        str | None,
        typer.Option("--target-type", "-t", help="Filter by target type: function, workflow, or model."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-l", min=1, max=500, help="Maximum number of runs to list.")] = 100,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List submitted runs in the active workspace."""
    if target_type is not None and target_type not in {"function", "workflow", "model"}:
        raise RebaseWorkflowError("--target-type must be 'function', 'workflow', or 'model'")

    client = Client()
    project_id: str | None = None
    if project is not None:
        project_data = _resolve_project_by_name(client, project)
        project_id = str(project_data["id"])

    runs = client.list_runs(project_id=project_id, target_type=target_type, limit=limit)
    if json_output:
        _print_json(runs)
        return
    if project is not None:
        if project_id is None:
            raise RebaseWorkflowError("project lookup did not return an ID")
        project_names = {project_id: str(project_data["name"])}
    else:
        project_names = _project_name_map(client.list_projects())
    console.print(_runs_table(runs, project_names=project_names))


def _parse_timeline_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _timeline_table(run: dict[str, Any], events: list[dict[str, Any]], steps: list[dict[str, Any]]) -> Table | None:
    """The run's life as one chronological table with offsets from creation.

    Composed entirely from data the platform already records — run
    timestamps, staged events, workflow steps — so it works identically for
    every execution mode and both providers.
    """
    entries: list[tuple[datetime, str]] = []
    created = _parse_timeline_timestamp(run.get("created_at"))
    if created is not None:
        entries.append((created, "Run created"))
    for event in events:
        stamp = _parse_timeline_timestamp(event.get("created_at"))
        message = str(event.get("message") or "").rstrip(".")
        if stamp is not None and message:
            entries.append((stamp, message))
    for step in steps:
        name = str(step.get("name") or "step")
        started = _parse_timeline_timestamp(step.get("started_at"))
        if started is not None:
            entries.append((started, f"Step {name} started"))
        finished = _parse_timeline_timestamp(step.get("finished_at"))
        if finished is not None:
            entries.append((finished, f"Step {name} {step.get('status') or 'finished'}"))
    finished_at = _parse_timeline_timestamp(run.get("finished_at"))
    if finished_at is not None:
        entries.append((finished_at, f"Run {run.get('status') or 'finished'}"))
    if len(entries) < 2:
        return None
    entries.sort(key=lambda item: item[0])
    base = entries[0][0]
    table = Table(title="Timeline", box=box.SIMPLE, title_justify="left")
    table.add_column("Time", style="rebase.muted", no_wrap=True)
    table.add_column("Offset", style="rebase.muted", justify="right", no_wrap=True)
    table.add_column("Event")
    for stamp, message in entries:
        offset = (stamp - base).total_seconds()
        table.add_row(stamp.strftime("%H:%M:%S"), f"+{offset:.1f}s", message)
    return table


@run_app.command("get")
def run_get_command(
    run_id: Annotated[str, typer.Argument(help="Run ID.")],
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Show run metadata."""
    client = Client()
    run = client.get_run(run_id)
    if json_output:
        _print_json(run)
        return
    console.print(
        _detail_table(
            "Run",
            run,
            preferred_keys=[
                "id",
                "target_type",
                "status",
                "mode",
                "isolation",
                "execution_backend",
                "project_id",
                "workflow_id",
                "function_id",
                "backend_run_id",
                "prefect_flow_run_id",
                "parameters",
                "result",
                "error",
                "timings",
                "created_at",
                "started_at",
                "finished_at",
            ],
        )
    )
    # The composed story: chronological timeline plus where the time went.
    # Both degrade silently — an older API without events still shows the
    # detail table above.
    try:
        events = client.list_run_events(run_id)
        steps = client.list_run_steps(run_id) if run.get("target_type") == "workflow" else []
    except RebaseWorkflowError:
        events, steps = [], []
    timeline = _timeline_table(run, events, steps)
    if timeline is not None:
        console.print(timeline)
    timing = run_timing_summary(run)
    if timing:
        console.print(f"Where the time went: {timing}", style="rebase.muted", highlight=False)
    try:
        cost = client.get_run_cost(run_id)
    except RebaseWorkflowError:
        cost = None  # older API, or a backend that never touches credits
    if cost is not None and (cost.get("charged_cents") or cost.get("reserved_cents")):
        currency = str(cost.get("currency") or "EUR")
        if cost.get("settled"):
            line = f"Cost: {_format_cents(cost.get('charged_cents'), currency)}"
            runtime = cost.get("runtime_seconds")
            if isinstance(runtime, int | float) and runtime > 0:
                line += f" for {runtime:.1f}s billed"
        else:
            line = f"Cost: {_format_cents(cost.get('reserved_cents'), currency)} reserved while the run is active"
        console.print(line, style="rebase.muted", highlight=False)


@run_app.command("logs")
def run_logs_command(
    run_id: Annotated[str, typer.Argument(help="Run ID.")],
    follow: Annotated[
        bool,
        typer.Option("--follow/--no-follow", "-f", help="Follow until the run reaches a terminal state."),
    ] = True,
    poll_interval: Annotated[
        float,
        typer.Option("--poll-interval", "-p", help="Seconds between run status polls when following."),
    ] = 1.0,
    timeout: Annotated[int, typer.Option("--timeout", "-t", help="Maximum seconds to follow the run.")] = 600,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print raw event and step JSON output.")] = False,
) -> None:
    """Show run events, workflow step state, and captured stdout/stderr logs."""
    client = Client()
    run_data = client.get_run(run_id)
    events = client.list_run_events(run_id)
    steps = client.list_run_steps(run_id) if run_data.get("target_type") == "workflow" else []
    if json_output:
        payload = _raw_run_logs(run_data, events, steps)
        payload["logs"] = client.get_run_logs(run_id)
        _print_json(payload)
        return

    run = Run(run_id, client=client, data=run_data)
    with _run_progress_reporter() as reporter:
        if not follow:
            _render_run_snapshot(run=run_data, events=events, steps=steps, reporter=reporter)
            _RunLogFollower(run).poll(reporter)
            return
        try:
            _stream_run_result(
                run,
                target_type=str(run_data.get("target_type") or ""),
                reporter=reporter,
                started_at=time.monotonic(),
                timeout=timeout,
                poll_interval=poll_interval,
                return_result=False,
                log_follower=_RunLogFollower(run),
            )
        except TimeoutError:
            # Following a run whose worker died means waiting the full timeout
            # and then printing a stack trace, which reads like a bug in the CLI
            # rather than what it is: the run stopped reporting. Say that, show
            # what did arrive, and exit non-zero without the traceback.
            reporter.fail(
                f"Stopped following after {timeout}s; the run has not reached a terminal state. "
                "If it is also producing no output, its worker may be gone -- "
                "the platform settles such runs, or `rebase run cancel` ends it now."
            )
            _render_run_snapshot(
                run=run.refresh(),
                events=client.list_run_events(run_id),
                steps=client.list_run_steps(run_id) if run_data.get("target_type") == "workflow" else [],
                reporter=reporter,
            )
            raise typer.Exit(code=1) from None


@run_app.command("cancel")
def run_cancel_command(
    run_id: Annotated[str, typer.Argument(help="Run ID.")],
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Cancel a run."""
    client = Client()
    cancelled = client.cancel_run(run_id)
    if json_output:
        _print_json(cancelled)
        return
    console.print(
        _detail_table(
            "Cancelled Run",
            cancelled,
            preferred_keys=[
                "id",
                "status",
                "target_type",
                "mode",
                "isolation",
                "execution_backend",
                "error",
                "finished_at",
            ],
        )
    )


def _parse_since(value: str) -> datetime:
    """Parse --since: a duration back from now (7d, 24h, 90m) or an ISO datetime."""
    import re as _re

    match = _re.fullmatch(r"(\d+)\s*([dhm])", value.strip())
    if match:
        seconds = int(match.group(1)) * {"d": 86400, "h": 3600, "m": 60}[match.group(2)]
        return datetime.now(UTC) - timedelta(seconds=seconds)
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise RebaseWorkflowError(
            f"cannot parse --since {value!r} (use a duration like 7d, 24h, 90m, or an ISO datetime)"
        ) from exc


def _parse_until(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise RebaseWorkflowError(f"cannot parse --until {value!r} (use an ISO datetime)") from exc


def _await_replay(replay: Run, *, timeout: int, poll_interval: float) -> tuple[str, Any]:
    """Poll a replay run to a terminal state, returning (status, result). Never raises for failed runs."""
    try:
        result = replay.result(timeout=timeout, poll_interval=poll_interval)
        return str(replay.data.get("status") or "succeeded"), result
    except (RebaseWorkflowError, TimeoutError):
        return str(replay.data.get("status") or "failed"), None


def _compare_results(original: Any, replay: Any) -> tuple[bool, list[str]]:
    """Compare two run results; when they differ, return the first three differing flattened keys."""
    if json.dumps(original, sort_keys=True, default=str) == json.dumps(replay, sort_keys=True, default=str):
        return True, []
    flat_original = _flatten_config(original)
    flat_replay = _flatten_config(replay)
    differing = [
        key
        for key in sorted(set(flat_original) | set(flat_replay))
        if flat_original.get(key, "<absent>") != flat_replay.get(key, "<absent>")
    ]
    return False, differing[:3]


def _run_duration(run: dict[str, Any]) -> str:
    try:
        started = datetime.fromisoformat(str(run.get("started_at")))
        finished = datetime.fromisoformat(str(run.get("finished_at")))
    except ValueError:
        return "-"
    return f"{(finished - started).total_seconds():.2f}s"


def _replay_candidates_table(runs: list[dict[str, Any]]) -> Table:
    table = Table(
        title="Replay Candidates",
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    table.add_column("ID", style="rebase.muted")
    table.add_column("Status", style="rebase.value")
    table.add_column("Trigger")
    table.add_column("Created", style="rebase.muted")
    for run in runs:
        table.add_row(
            str(run.get("id", "-")),
            _format_value(run.get("status")),
            _format_value(run.get("trigger_source")),
            _format_value(run.get("created_at")),
        )
    return table


def _replay_result_cell(row: dict[str, Any]) -> str:
    if row.get("result_identical") is True:
        return "identical"
    if row.get("result_identical") is False:
        differing = row.get("differing_keys") or []
        return f"differs: {differing[0]}" if differing else "differs"
    return "-"


def _replay_compare_table(rows: list[dict[str, Any]]) -> Table:
    table = Table(
        title="Replays",
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    table.add_column("Original", style="rebase.muted")
    table.add_column("Replay", style="rebase.value")
    table.add_column("Status")
    table.add_column("Result")
    table.add_column("Created", style="rebase.muted")
    for row in rows:
        table.add_row(
            str(row.get("original", "-")),
            str(row.get("replay") or "-"),
            f"{row.get('original_status') or '-'} -> {row.get('replay_status') or '-'}",
            _replay_result_cell(row),
            _format_value(row.get("created_at")),
        )
    return table


def _replay_single(
    client: Client,
    run_id: str,
    *,
    version: str | None,
    parameters: dict[str, Any],
    wait: bool,
    timeout: int,
    poll_interval: float,
    json_output: bool,
) -> None:
    replay = client.replay_run(run_id, version=version, parameters=parameters or None)
    detail = {
        "id": replay.id,
        "replay_of": replay.data.get("replay_of"),
        "target_version_id": replay.data.get("target_version_id"),
        "status": replay.data.get("status"),
    }
    if not json_output:
        console.print(_detail_table("Replay Run", detail, preferred_keys=list(detail)))
    if not wait:
        if json_output:
            _print_json(replay.data)
        return

    replay_status, replay_result = _await_replay(replay, timeout=timeout, poll_interval=poll_interval)
    original = client.get_run(run_id)
    original_status = str(original.get("status") or "-")
    identical: bool | None = None
    differing: list[str] = []
    if replay_status == "succeeded" and original_status == "succeeded":
        identical, differing = _compare_results(original.get("result"), replay_result)
    if json_output:
        _print_json(
            {
                "original": run_id,
                "replay": replay.id,
                "original_status": original_status,
                "replay_status": replay_status,
                "result_identical": identical,
                "differing_keys": differing or None,
            }
        )
    else:
        if identical is True:
            result_cell = "identical"
        elif identical is False:
            result_cell = f"differs: {', '.join(differing)}" if differing else "differs"
        else:
            result_cell = "-"
        comparison = {
            "status": f"{original_status} -> {replay_status}",
            "result": result_cell,
            "original_duration": _run_duration(original),
            "replay_duration": _run_duration(replay.data),
        }
        console.print(_detail_table("Replay Comparison", comparison, preferred_keys=list(comparison)))
    if replay_status != "succeeded":
        raise typer.Exit(1)


def _replay_batch(
    client: Client,
    *,
    workflow: str,
    project: str | None,
    version: str | None,
    parameters: dict[str, Any],
    since: str | None,
    until: str | None,
    status: str | None,
    trigger_source: str | None,
    max_parallel: int,
    compare: bool,
    yes: bool,
    timeout: int,
    poll_interval: float,
    json_output: bool,
) -> None:
    if since is None:
        raise RebaseWorkflowError("batch replay requires --since (e.g. --since 7d)")
    if "/" in workflow:
        project_name, _, workflow_name = workflow.partition("/")
    else:
        project_name, workflow_name = project, workflow
    workflow_data = _resolve_workflow_selector(client, workflow_name, project_name=project_name)

    runs = client.list_runs(
        workflow_id=str(workflow_data["id"]),
        target_type="workflow",
        since=_parse_since(since),
        until=_parse_until(until) if until is not None else None,
        status=status,
        trigger_source=trigger_source,
        limit=500,
    )
    if trigger_source != "replay":
        # Never replay replays unless explicitly asked to.
        runs = [run for run in runs if run.get("trigger_source") != "replay"]
    if not runs:
        console.print("No matching runs to replay.")
        return
    if not json_output:
        console.print(_replay_candidates_table(runs))
    if not yes:
        typer.confirm(f"Replay {len(runs)} runs?", abort=True)

    def _replay_one(original: dict[str, Any]) -> dict[str, Any]:
        original_id = str(original.get("id"))
        row: dict[str, Any] = {
            "original": original_id,
            "replay": None,
            "original_status": original.get("status"),
            "replay_status": None,
            "result_identical": None,
            "differing_keys": None,
            "created_at": original.get("created_at"),
        }
        try:
            replay = client.replay_run(original_id, version=version, parameters=parameters or None)
        except RebaseWorkflowError as exc:
            row["replay_status"] = f"error: {exc}"
            return row
        row["replay"] = replay.id
        row["replay_status"] = replay.data.get("status")
        if not compare:
            return row
        replay_status, replay_result = _await_replay(replay, timeout=timeout, poll_interval=poll_interval)
        row["replay_status"] = replay_status
        original_detail = client.get_run(original_id)
        row["original_status"] = original_detail.get("status")
        if replay_status == "succeeded" and original_detail.get("status") == "succeeded":
            identical, differing = _compare_results(original_detail.get("result"), replay_result)
            row["result_identical"] = identical
            row["differing_keys"] = differing or None
        return row

    with ThreadPoolExecutor(max_workers=max_parallel) as executor:
        rows = list(executor.map(_replay_one, runs))

    if json_output:
        keys = ("original", "replay", "original_status", "replay_status", "result_identical", "differing_keys")
        _print_json([{key: row.get(key) for key in keys} for row in rows])
    else:
        console.print(_replay_compare_table(rows))
        if not compare:
            console.print("[rebase.muted]Replays submitted; results not compared (--no-compare).[/rebase.muted]")
    submission_failed = any(row["replay"] is None for row in rows)
    replay_failed = compare and any(row["replay_status"] != "succeeded" for row in rows)
    if submission_failed or replay_failed:
        raise typer.Exit(1)


@run_app.command("replay")
def run_replay_command(
    run_id: Annotated[
        str | None,
        typer.Argument(help="Run ID to replay. Omit when using batch mode with --workflow."),
    ] = None,
    workflow: Annotated[
        str | None,
        typer.Option(
            "--workflow",
            "-w",
            help="Batch mode: replay runs of this workflow ('project/name', or a bare name with --project).",
        ),
    ] = None,
    project: Annotated[
        str | None,
        typer.Option("--project", help="Project name when --workflow is a bare workflow name."),
    ] = None,
    code: Annotated[
        str | None,
        typer.Option(
            "--code",
            "-c",
            help="Code to run: omit for the original pinned version, 'latest' for the current one, or a version ID.",
        ),
    ] = None,
    parameter: Annotated[
        list[str] | None,
        typer.Option("--param", "-p", help="Parameter override as name=json_value. Can be passed more than once."),
    ] = None,
    since: Annotated[
        str | None,
        typer.Option(
            "--since", "-s", help="Batch mode: runs created after this ISO datetime or duration (e.g. 7d, 24h, 90m)."
        ),
    ] = None,
    until: Annotated[
        str | None,
        typer.Option("--until", "-u", help="Batch mode: runs created before this ISO datetime."),
    ] = None,
    status: Annotated[
        str | None,
        typer.Option("--status", help="Batch mode: only replay runs with this status (e.g. succeeded, failed)."),
    ] = None,
    trigger_source: Annotated[
        str | None,
        typer.Option(
            "--trigger-source",
            "-t",
            help="Batch mode: filter candidates by trigger source (api, schedule, trigger, replay).",
        ),
    ] = None,
    max_parallel: Annotated[
        int,
        typer.Option("--max-parallel", "-m", min=1, help="Batch mode: maximum concurrent replays."),
    ] = 4,
    compare: Annotated[
        bool,
        typer.Option(
            "--compare/--no-compare",
            help="Batch mode: wait for each replay and compare its result with the original run.",
        ),
    ] = True,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Batch mode: skip the confirmation prompt.")] = False,
    wait: Annotated[
        bool,
        typer.Option("--wait/--no-wait", help="Wait for the replay result and compare it with the original run."),
    ] = True,
    timeout: Annotated[int, typer.Option("--timeout", help="Maximum seconds to wait for each replay result.")] = 600,
    poll_interval: Annotated[
        float,
        typer.Option("--poll-interval", help="Seconds between run status polls."),
    ] = 5.0,
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Replay a run — or a period of workflow runs — bounded to what was knowable at the time."""
    if run_id is not None and workflow is not None:
        raise RebaseWorkflowError("provide either a RUN_ID or --workflow, not both")
    if run_id is None and workflow is None:
        raise RebaseWorkflowError("provide a RUN_ID to replay, or --workflow with --since for batch mode")

    client = Client()
    parameters = _parse_run_parameters(None, parameter)
    if run_id is not None:
        _replay_single(
            client,
            run_id,
            version=code,
            parameters=parameters,
            wait=wait,
            timeout=timeout,
            poll_interval=poll_interval,
            json_output=json_output,
        )
        return
    _replay_batch(
        client,
        workflow=workflow,
        project=project,
        version=code,
        parameters=parameters,
        since=since,
        until=until,
        status=status,
        trigger_source=trigger_source,
        max_parallel=max_parallel,
        compare=compare,
        yes=yes,
        timeout=timeout,
        poll_interval=poll_interval,
        json_output=json_output,
    )


hillclimb_app = typer.Typer(
    add_completion=False,
    cls=AlphabeticalTyperGroup,
    help="Agentic model searches (hillclimb) — hosted on the platform or local.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


def _hillclimb():
    from rebase import hillclimb as module

    return module


def _hillclimb_sync_id(client: Client, run_id: str) -> str:
    run = client.get_run(run_id)
    sync_id = (run.get("parameters") or {}).get("sync_id")
    if not sync_id:
        raise RebaseWorkflowError(f"run {run_id} is not a hillclimb search (no sync_id)")
    return str(sync_id)


def _parse_budget_seconds(value: str) -> int:
    import re as _re

    match = _re.fullmatch(r"(\d+)\s*([hms]?)", value.strip())
    if not match:
        raise RebaseWorkflowError(f"cannot parse budget {value!r} (use e.g. 2h, 30m, 3600s)")
    return int(match.group(1)) * {"h": 3600, "m": 60, "s": 1, "": 1}[match.group(2)]


@hillclimb_app.command("init")
def hillclimb_init_command(
    directory: Annotated[
        str,
        typer.Argument(help="Directory to initialize as a local Hillclimb workspace."),
    ] = ".",
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Initialize even when already inside a Hillclimb workspace."),
    ] = False,
) -> None:
    """Prepare this repository for local Hillclimb searches."""
    module = _hillclimb()
    try:
        root = module.init_local_workspace(Path(directory), force=force)
    except RuntimeError as exc:
        raise RebaseWorkflowError(str(exc)) from exc
    console.print(f"Initialized Hillclimb workspace at [bold]{root}[/bold]")
    console.print("  config: hillclimb/config.yaml")
    console.print("  runs:   hillclimb/runs/ (gitignored)")
    console.print("Next: rebase hillclimb problems gefcom2014")


@hillclimb_app.command("problems")
def hillclimb_problems_command(
    family: Annotated[
        str | None,
        typer.Argument(help="Optional emflow family, e.g. gefcom2014."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", "-j")] = False,
) -> None:
    """Discover forecast problems available to Hillclimb."""
    problems = _hillclimb().discover_emflow_problems(family)
    if json_output:
        _print_json(problems)
        return
    if not problems:
        suffix = f" in family {family!r}" if family else ""
        console.print(f"no installed emflow problems{suffix}")
        return
    table = Table(title="Hillclimb Problems", box=box.SIMPLE)
    table.add_column("Target", style=f"bold {BRAND_BRIGHT_GREEN}")
    table.add_column("Family")
    table.add_column("Track")
    for problem in problems:
        table.add_row(problem["target"], problem["family"], problem["track"])
    console.print(table)


@hillclimb_app.command("start")
def hillclimb_start_command(
    target: Annotated[str, typer.Argument(help="Problem target, e.g. emflow://gefcom2014:solar.")],
    budget: Annotated[str, typer.Option("--budget", "-b", help="Wall-clock budget, e.g. 2h / 30m.")] = "2h",
    name: Annotated[str | None, typer.Option("--name", "-n", help="Search name.")] = None,
    model: Annotated[str | None, typer.Option("--model", "-m", help="Agent model, e.g. sonnet.")] = None,
    backend: Annotated[
        str | None,
        typer.Option("--backend", help="Operator backend: claude-code (default) | dummy (smoke tests)."),
    ] = None,
    holdout: Annotated[
        bool,
        typer.Option(
            "--holdout/--no-holdout",
            help="Use the hidden holdout for final selection; disable it only for smoke tests.",
        ),
    ] = True,
    project: Annotated[str, typer.Option("--project", "-p", help="Project for the platform run.")] = "hillclimb",
    local: Annotated[bool, typer.Option("--local", "-l", help="Run on this machine instead of the platform.")] = False,
) -> None:
    """Start a hillclimb search (hosted by default; --local runs it here)."""
    module = _hillclimb()
    budget_s = _parse_budget_seconds(budget)
    if local:
        outcome = module.run_local_search(
            target,
            budget_s=budget_s,
            name=name,
            model=model,
            backend=backend,
            holdout=holdout,
            log=console.print,
        )
        console.print(f"[bold]{outcome.state}[/bold] {outcome.ref}")
        if outcome.selected is not None:
            console.print(f"selected {outcome.selected.candidate_id}: val={outcome.selected.val_score}")
        return
    run = module.start_hosted_search(
        Client(),
        target,
        budget_s=budget_s,
        name=name,
        project=project,
        model=model,
        backend=backend,
        holdout=holdout,
    )
    console.print(f"Submitted hosted search run [bold]{run.id}[/bold]")
    console.print(f"  status: rebase hillclimb status {run.id}")
    console.print(f"  events: rebase run get {run.id}")


@hillclimb_app.command("list")
def hillclimb_list_command(
    limit: Annotated[int, typer.Option("--limit", "-l", min=1, max=500)] = 50,
    json_output: Annotated[bool, typer.Option("--json", "-j")] = False,
) -> None:
    """List hosted hillclimb search runs."""
    module = _hillclimb()
    client = Client()
    runs = [
        run
        for run in client.list_runs(target_type="function", limit=limit)
        if str(run.get("name", "")).startswith(module.RUN_NAME_PREFIX)
    ]
    if json_output:
        _print_json(runs)
        return
    for run in runs:
        console.print(f"{run.get('id')}  {run.get('status'):<10}  {run.get('name')}  {run.get('created_at', '')}")
    if not runs:
        console.print("no hillclimb runs yet — rebase hillclimb start <target>")


@hillclimb_app.command("status")
def hillclimb_status_command(
    run_id: Annotated[str, typer.Argument(help="Platform run ID from `hillclimb start`.")],
    bucket: Annotated[str | None, typer.Option("--bucket", "-b", help="Artifacts bucket override.")] = None,
) -> None:
    """Live search state (candidates, best score, budget) from synced GCS state."""
    module = _hillclimb()
    client = Client()
    run = client.get_run(run_id)
    console.print(f"platform run: {run.get('status')}")
    sync_id = _hillclimb_sync_id(client, run_id)
    statuses = module.read_hosted_state(sync_id, bucket=bucket)
    console.print(module.format_hosted_status(statuses))


@hillclimb_app.command("stop")
def hillclimb_stop_command(
    run_id: Annotated[str, typer.Argument(help="Platform run ID.")],
    bucket: Annotated[str | None, typer.Option("--bucket", "-b")] = None,
) -> None:
    """Gracefully stop a hosted search (parks after the current operator)."""
    module = _hillclimb()
    sync_id = _hillclimb_sync_id(Client(), run_id)
    module.request_hosted_stop(sync_id, bucket=bucket)
    console.print("stop queued: delivered to the search within one sync interval (~30s)")


@hillclimb_app.command("promote")
def hillclimb_promote_command(
    run_id: Annotated[
        str,
        typer.Argument(help="Platform run ID, or with --local a runs/ id, unique substring, or 'latest'."),
    ],
    dest: Annotated[str, typer.Option("--dest", "-d", help="Directory for the model files.")] = "models",
    bucket: Annotated[str | None, typer.Option("--bucket", "-b")] = None,
    local: Annotated[
        bool, typer.Option("--local", "-l", help="Promote from a local search (state in ./runs/).")
    ] = False,
    pr: Annotated[
        bool, typer.Option("--pr", "-p", help="Open a promotion PR on the connected workspace repo.")
    ] = False,
) -> None:
    """Fetch the selected model(s) into the workspace repo (models/<id>.py).

    With --pr, the platform opens a pull request on the connected GitHub
    repo instead of leaving a local commit to you."""
    module = _hillclimb()
    if local:
        try:
            written = module.promote_local(run_id, Path(dest))
        except RuntimeError as exc:
            raise RebaseWorkflowError(str(exc)) from exc
    else:
        sync_id = _hillclimb_sync_id(Client(), run_id)
        written = module.fetch_best_solution(sync_id, Path(dest), bucket=bucket)
        if not written:
            raise RebaseWorkflowError("no best/solution.py synced yet — is the search finished?")
    for path in written:
        console.print(f"wrote {path}")
    if pr:
        client = Client()
        connections = client.list_github_repo_connections()
        workspace_conn = next((c for c in connections if c.get("scope") == "workspace"), None)
        if workspace_conn is None:
            raise RebaseWorkflowError("no workspace-scoped GitHub connection — run `rebase connect github` first")
        for path in written:
            repo_path = f"{dest}/{Path(path).name}"
            result = client.create_github_promotion_pr(
                workspace_conn["id"],
                path=repo_path,
                content=Path(path).read_text(),
                title=f"hillclimb: promote {Path(path).stem} (run {run_id})",
                body=(
                    f"Automated promotion from hillclimb search run `{run_id}`.\n\n"
                    f"The candidate beat the incumbent on the hidden holdout; "
                    f"see the run's journal for the full search history."
                ),
            )
            console.print(f"opened PR: {result.get('pr_url')}")
        return
    console.print(
        "review, then: git checkout -b hillclimb-promotion && git add "
        f"{dest} && git commit && open a PR — protected deploys go through gitops"
    )


app.add_typer(hillclimb_app, name="hillclimb")


def _parse_logo_variant(args: list[str]) -> tuple[int, list[str]]:
    """Strip --logo N from args and return (variant, remaining_args)."""
    variant = 1
    remaining: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--logo" and i + 1 < len(args):
            try:
                variant = int(args[i + 1])
            except ValueError:
                remaining.extend(args[i : i + 2])
            i += 2
        elif args[i].startswith("--logo="):
            try:
                variant = int(args[i].split("=", 1)[1])
            except ValueError:
                remaining.append(args[i])
            i += 1
        else:
            remaining.append(args[i])
            i += 1
    return variant, remaining


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    logo_variant, args = _parse_logo_variant(list(args))
    if not args or args in (["--help"], ["-h"]):
        console.print(_banner(logo_variant))
        try:
            app(args=["--help"], prog_name="rebase", standalone_mode=True)
        except SystemExit as exc:
            return int(exc.code or 0)
        return 0

    try:
        if args in (["run", "--help"], ["run", "-h"]):
            _print_run_help()
            return 0
        if len(args) > 1 and args[0] == "run" and args[1] in RUN_INSPECTION_COMMANDS:
            result = run_app(args=args[1:], prog_name="rebase run", standalone_mode=False)
        else:
            result = app(args=args, prog_name="rebase", standalone_mode=False)
        # With standalone_mode=False click returns typer.Exit codes instead of raising.
        return int(result) if isinstance(result, int) else 0
    except RebaseWorkflowError as exc:
        error_console.print(f"Error: {exc}", style="rebase.error")
        return 1
    except CLICK_EXCEPTIONS as exc:
        exc.show(file=sys.stderr)
        return int(exc.exit_code)
    except EXIT_EXCEPTIONS as exc:
        return int(exc.exit_code or 0)
    except ABORT_EXCEPTIONS:
        error_console.print("Aborted.", style="rebase.error")
        return 1
    except KeyboardInterrupt:
        error_console.print("Aborted.", style="rebase.error")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
