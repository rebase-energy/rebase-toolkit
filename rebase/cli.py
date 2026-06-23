from __future__ import annotations

import getpass
import importlib
import importlib.util
import json
import sys
import time
from collections.abc import Iterable
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
    Client,
    Function,
    FunctionBackend,
    Model,
    Optimizer,
    Predictor,
    Project,
    RebaseWorkflowError,
    Run,
    Step,
    Workflow,
)
from rebase.config import (
    DEFAULT_PROFILE,
    DEFAULT_SERVER_URL,
    list_profiles,
    selected_profile_name,
    set_default_profile,
    write_profile,
)

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

app = typer.Typer(
    add_completion=False,
    help="Rebase Platform toolkit.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
workspace_app = typer.Typer(
    add_completion=False,
    help="Show, list, or switch Rebase workspace profiles.",
    no_args_is_help=False,
    rich_markup_mode="rich",
)
api_key_app = typer.Typer(
    add_completion=False,
    help="Create, list, and revoke workspace API keys.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
endpoint_app = typer.Typer(
    add_completion=False,
    help="Inspect and invoke Rebase endpoints.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
project_app = typer.Typer(
    add_completion=False,
    help="Inspect Rebase projects.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
function_app = typer.Typer(
    add_completion=False,
    help="Inspect Rebase functions.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
workflow_app = typer.Typer(
    add_completion=False,
    help="Inspect Rebase workflows.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
model_app = typer.Typer(
    add_completion=False,
    help="Deploy and operate Rebase models.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
run_app = typer.Typer(
    add_completion=False,
    help="Run local Rebase targets and inspect submitted runs.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

KNOWN_PERMISSIONS = frozenset(
    {
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
    }
)


def _print_run_help() -> None:
    console.print("Usage: rebase run [OPTIONS] TARGET_REF")
    console.print("       rebase run COMMAND [ARGS]...")
    console.print()
    console.print("Run a Rebase function, workflow, or model from local source without deploying it.")
    console.print("Inspect submitted runs with the list, get, logs, and cancel subcommands.")
    console.print()

    options = Table(title="Execution Options", box=box.SIMPLE)
    options.add_column("Option", style="rebase.value")
    options.add_column("Description")
    options.add_row("--param, -p", "Target parameter as name=json_value. Can be passed more than once.")
    options.add_row("--parameters-json", "JSON object with target parameters.")
    options.add_row("--backend", "Override the cloud execution backend for this ephemeral run.")
    options.add_row("--module, -m", "Interpret the target source as a Python module path instead of a file.")
    options.add_row("--wait / --no-wait", "Wait for the function result before exiting. Defaults to --wait.")
    options.add_row("--timeout", "Maximum seconds to wait for the result. Defaults to 600.")
    options.add_row("--poll-interval", "Seconds between run status polls. Defaults to 1.0.")
    options.add_row("--help", "Show this message and exit.")
    console.print(options)

    commands = Table(title="Inspection Commands", box=box.SIMPLE)
    commands.add_column("Command", style="rebase.value")
    commands.add_column("Description")
    commands.add_row("list", "List submitted runs in the active workspace.")
    commands.add_row("get", "Show run metadata.")
    commands.add_row("logs", "Show persisted run events and workflow step state.")
    commands.add_row("cancel", "Cancellation placeholder. Exits with an unsupported error.")
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


def _function_objects(module: ModuleType) -> list[tuple[str, Function]]:
    return [
        (name, function)
        for name, function in _unique_named_objects(module, (Function,))
        if not isinstance(function, Step)
    ]


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


def _validate_backend_override(backend: str | None) -> FunctionBackend | None:
    if backend is None:
        return None
    if backend not in {"modal", "prefect", "prefect_cloud", "cloud_run", "cloud_run_shared", "cloud_run_jobs"}:
        raise RebaseWorkflowError(
            "backend must be 'modal', 'prefect', 'prefect_cloud', 'cloud_run', "
            "'cloud_run_shared', or 'cloud_run_jobs'"
        )
    return backend


def _validate_run_backend_override(backend: str | None, target: RunnableTarget) -> str | None:
    if backend is None:
        return None
    if isinstance(target, Workflow):
        if backend not in {"prefect", "prefect_cloud_run_jobs", "prefect_cloud_run_service"}:
            raise RebaseWorkflowError(
                "workflow backend must be 'prefect', 'prefect_cloud_run_jobs', or 'prefect_cloud_run_service'"
            )
        return backend
    return _validate_backend_override(backend)


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
    table.add_column("Backend")
    table.add_column("Project")
    table.add_column("Created", style="rebase.muted")
    table.add_column("Finished", style="rebase.muted")
    for run in runs:
        project_id = str(run.get("project_id", ""))
        table.add_row(
            str(run.get("id", "-")),
            _format_value(run.get("target_type")),
            _format_value(run.get("status")),
            _format_value(run.get("execution_backend")),
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
            reporter.fail(str(run.get("error") or "Run failed."))
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

    def complete(self, message: str) -> None:
        prefix, style = _progress_prefix("completed")
        console.print(f"{prefix} {message}", style=style)

    def fail(self, message: str) -> None:
        prefix, style = _progress_prefix("failed")
        console.print(f"{prefix} {message}", style=style)

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

    def complete(self, message: str) -> None:
        self._tree.add(f"[green]✓[/green] {message}")
        self._refresh()

    def fail(self, message: str) -> None:
        self._tree.add(f"[red]✗[/red] {message}")
        self._header = Text.from_markup(f"[red]✗[/red] {message}")
        self._refresh()

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
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    seen_event_ids: set[str] = set()
    seen_step_statuses: dict[str, str] = {}
    events_supported = True
    steps_supported = isinstance(target, Workflow) or target_type == "workflow"
    terminal_statuses = {"succeeded", "failed", "cancelled"}
    first_iteration = True

    while True:
        if events_supported:
            try:
                for event in run.events():
                    event_id = str(event.get("id", ""))
                    if not event_id or event_id in seen_event_ids:
                        continue
                    seen_event_ids.add(event_id)
                    event_status = str(event.get("status") or "info")
                    message = str(event.get("message") or "")
                    if not message:
                        continue
                    if event_status == "running":
                        reporter.update(message)
                        continue
                    if event_status == "failed":
                        reporter.fail(message)
                    else:
                        reporter.complete(message)
            except RebaseWorkflowError:
                events_supported = False

        if steps_supported:
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

        data = run.data if first_iteration and run.data else run.refresh()
        first_iteration = False
        status = str(data.get("status") or "queued")
        if status in terminal_statuses:
            if status == "succeeded":
                if return_result:
                    reporter.finish(f"Run completed in {_format_duration(time.monotonic() - started_at)} seconds.")
                else:
                    reporter.finish("Run completed.")
                return data.get("result") if return_result else None
            error = data.get("error") or f"run ended with status {status}"
            reporter.fail(str(error))
            if return_result:
                raise RebaseWorkflowError(str(error))
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
        for name, project in projects:
            if deploy_source is None:
                project.deploy()
            else:
                project.deploy(deploy_source=deploy_source)
            deployed.append(("project", project.name or name, project.id))
            for function in project._functions:
                endpoint_url = _deployed_endpoint_url(function)
                if endpoint_url is not None:
                    deployed.append(("function", function.name or "-", function.id, endpoint_url))
            for workflow in project._workflows:
                endpoint_url = _deployed_endpoint_url(workflow)
                if endpoint_url is not None:
                    deployed.append(("workflow", workflow.name or "-", workflow.id, endpoint_url))
        return deployed
    if all_projects and selected_names:
        raise RebaseWorkflowError(f"No matching Rebase project found for: {', '.join(sorted(selected_names))}")

    deployables = _unique_named_objects(module, (Workflow, Function, Model))
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
            "rb.workflow(...), rb.function(...), rb.Predictor, rb.Optimizer, or rb.Agent instance."
        )

    for name, deployable in deployables:
        if deploy_source is None:
            deployable.deploy()
        else:
            deployable.deploy(deploy_source=deploy_source)
        target_type = (
            _model_target_type(deployable) if isinstance(deployable, Model) else deployable.__class__.__name__.lower()
        )
        endpoint_url = _deployed_endpoint_url(deployable)
        if endpoint_url is not None:
            deployed.append((target_type, deployable.name or name, deployable.id, endpoint_url))
        else:
            deployed.append((target_type, deployable.name or name, deployable.id))
    return deployed


def _workspace_value(data: dict[str, Any]) -> str:
    workspace_name = data.get("workspace_name")
    workspace_id = data.get("workspace_id")
    if isinstance(workspace_name, str) and workspace_name:
        return workspace_name
    if isinstance(workspace_id, str) and workspace_id:
        return workspace_id
    return "unknown workspace"


def _workspace_id(data: dict[str, Any]) -> str:
    workspace_id = data.get("workspace_id")
    return workspace_id if isinstance(workspace_id, str) and workspace_id else "-"


def _workspace_table(profiles: dict[str, dict[str, Any]], *, active_profile: str) -> Table:
    table = Table(
        title="Workspace Profiles",
        box=box.ASCII,
        border_style="rebase.border",
        header_style="rebase.title",
        show_header=True,
        title_style="rebase.title",
    )
    table.add_column("Active", justify="center", no_wrap=True, style="rebase.active")
    table.add_column("Profile", style="rebase.value", no_wrap=True)
    table.add_column("Workspace")
    table.add_column("Workspace ID", style="rebase.muted")
    for profile, data in sorted(profiles.items()):
        is_active = profile == active_profile
        style = "rebase.active" if is_active else None
        table.add_row("*" if is_active else "", profile, _workspace_value(data), _workspace_id(data), style=style)
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
    table.add_column("Backend")
    table.add_column("Enabled")
    table.add_column("ID", style="rebase.muted")
    table.add_column("Updated", style="rebase.muted")
    for function in functions:
        project_id = str(function.get("project_id", ""))
        table.add_row(
            str(function.get("name", "-")),
            project_names.get(project_id, project_id or "-"),
            _format_value(function.get("execution_backend")),
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
    table.add_column("Backend")
    table.add_column("Enabled")
    table.add_column("ID", style="rebase.muted")
    table.add_column("Updated", style="rebase.muted")
    for workflow in workflows:
        project_id = str(workflow.get("project_id", ""))
        table.add_row(
            str(workflow.get("name", "-")),
            project_names.get(project_id, project_id or "-"),
            _format_value(workflow.get("execution_backend")),
            _format_value(workflow.get("enabled")),
            str(workflow.get("id", "-")),
            _format_value(workflow.get("updated_at")),
        )
    return table


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
    table.add_column("Backend")
    table.add_column("ID", style="rebase.muted")
    table.add_column("Updated", style="rebase.muted")
    for model in models:
        project_id = str(model.get("project_id", ""))
        table.add_row(
            str(model.get("name", "-")),
            project_names.get(project_id, project_id or "-"),
            _format_value(model.get("kind")),
            _format_value(model.get("operation_name")),
            _format_value(model.get("execution_backend")),
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
    table.add_column("Backend")
    table.add_column("Created", style="rebase.muted")
    for version in versions:
        table.add_row(
            _format_value(version.get("version_number")),
            str(version.get("id", "-")),
            _format_value(version.get("fingerprint")),
            _format_value(version.get("execution_backend")),
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


def _resolve_function_selector(
    client: Client,
    name: str | None,
    *,
    function_id: str | None = None,
    project_name: str | None = None,
) -> dict[str, Any]:
    if function_id is not None:
        if name is not None:
            raise RebaseWorkflowError("provide either a function name or --id, not both")
        return client.get_function(function_id)
    if name is None:
        raise RebaseWorkflowError("function name is required unless --id is provided")
    if not project_name:
        raise RebaseWorkflowError("--project is required when selecting a function by name")
    project = _resolve_project_by_name(client, project_name)
    for function in client.list_functions(project_id=str(project["id"])):
        if function.get("name") == name:
            return function
    raise RebaseWorkflowError(f"function not found: {project_name}/{name}")


def _resolve_workflow_selector(
    client: Client,
    name: str | None,
    *,
    workflow_id: str | None = None,
    project_name: str | None = None,
) -> dict[str, Any]:
    if workflow_id is not None:
        if name is not None:
            raise RebaseWorkflowError("provide either a workflow name or --id, not both")
        return client.get_workflow(workflow_id)
    if name is None:
        raise RebaseWorkflowError("workflow name is required unless --id is provided")
    if not project_name:
        raise RebaseWorkflowError("--project is required when selecting a workflow by name")
    project = _resolve_project_by_name(client, project_name)
    for workflow in client.list_workflows(project_id=str(project["id"])):
        if workflow.get("name") == name:
            return workflow
    raise RebaseWorkflowError(f"workflow not found: {project_name}/{name}")


def _resolve_model_selector(
    client: Client,
    name: str | None,
    *,
    model_id: str | None = None,
    project_name: str | None = None,
) -> dict[str, Any]:
    if model_id is not None:
        if name is not None:
            raise RebaseWorkflowError("provide either a model name or --id, not both")
        return client.get_model(model_id)
    if name is None:
        raise RebaseWorkflowError("model name is required unless --id is provided")
    if not project_name:
        raise RebaseWorkflowError("--project is required when selecting a model by name")
    project = _resolve_project_by_name(client, project_name)
    for model in client.list_models(project_id=str(project["id"])):
        if model.get("name") == name:
            return model
    raise RebaseWorkflowError(f"model not found: {project_name}/{name}")


def _project_name_map(projects: list[dict[str, Any]]) -> dict[str, str]:
    return {str(project.get("id", "")): str(project.get("name", "-")) for project in projects}


@app.command("setup")
def setup_command(
    profile: Annotated[str, typer.Option("--profile", help="Credential profile name.")] = DEFAULT_PROFILE,
    api_key: Annotated[str | None, typer.Option("--api-key", hidden=True)] = None,
    api_url: Annotated[
        str | None,
        typer.Option(
            "--api-url",
            help="Rebase API URL to store for this profile. Useful for local development with port-forwarding.",
        ),
    ] = None,
    verify: Annotated[
        bool,
        typer.Option(
            "--verify/--no-verify",
            help="Verify an API key against the hosted Rebase API before saving it.",
        ),
    ] = True,
    provider: Annotated[str | None, typer.Option("--provider", help="Supabase social auth provider.")] = None,
    force_auth: Annotated[
        bool,
        typer.Option("--force-auth", help="Ignore any stored Supabase session and authenticate again."),
    ] = False,
    callback_port: Annotated[int, typer.Option("--callback-port", help="Local Supabase OAuth callback port.")] = 17658,
    auth_timeout: Annotated[
        float,
        typer.Option("--auth-timeout", help="Seconds to wait for Supabase auth callback."),
    ] = 300,
    no_browser: Annotated[
        bool,
        typer.Option("--no-browser", help="Print URLs instead of opening the browser."),
    ] = False,
    workspace: Annotated[str | None, typer.Option("--workspace", help="Workspace id to use or create.")] = None,
    workspace_name: Annotated[
        str | None,
        typer.Option("--workspace-name", help="Workspace display name when creating a workspace."),
    ] = None,
    handle: Annotated[
        str | None,
        typer.Option("--handle", help="Unique Rebase user handle to claim during setup."),
    ] = None,
    github: Annotated[
        bool | None,
        typer.Option("--github/--no-github", help="Connect or skip GitHub during setup."),
    ] = None,
    github_installation_id: Annotated[
        int | None,
        typer.Option("--github-installation-id", help="Existing GitHub App installation id."),
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
        typer.Option("--repo", help="GitHub repository full name, for example owner/name."),
    ] = None,
    repo_scope: Annotated[
        str | None,
        typer.Option("--repo-scope", help="Connect repo at workspace or project level."),
    ] = None,
    repo_path: Annotated[
        str | None,
        typer.Option("--repo-path", help="Optional path inside the repository."),
    ] = None,
    create_repo: Annotated[
        bool,
        typer.Option("--create-repo", help="Open GitHub to create a repository during setup."),
    ] = False,
    project: Annotated[
        str | None,
        typer.Option("--project", help="Project name for project-level repo connections."),
    ] = None,
) -> None:
    """Authenticate and configure Rebase on this computer."""
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
                    github=github,
                    github_installation_id=github_installation_id,
                    github_timeout=github_timeout,
                    poll_interval=poll_interval,
                    repo=repo,
                    repo_scope=repo_scope,
                    repo_path=repo_path,
                    create_repo=create_repo,
                    project=project,
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


@app.command("tui")
def tui_command(
    project: Annotated[
        str | None,
        typer.Option("--project", help="Filter functions and workflows by project name."),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, max=500, help="Maximum latest runs to load per selected target."),
    ] = 25,
) -> None:
    """Open the Rebase terminal UI."""
    from rebase.tui import run_tui

    run_tui(project=project, limit=limit)


def _show_active_workspace() -> None:
    profiles = list_profiles()
    active_profile = selected_profile_name()

    if not profiles:
        raise RebaseWorkflowError("no workspace profile configured. Run `rebase setup` first.")
    data = profiles.get(active_profile, {})
    console.print(_workspace_table({active_profile: data}, active_profile=active_profile))


@workspace_app.callback(invoke_without_command=True)
def workspace_command(ctx: typer.Context) -> None:
    """Show the active Rebase workspace profile."""
    if ctx.invoked_subcommand is None:
        _show_active_workspace()


@workspace_app.command("list")
def workspace_list_command() -> None:
    """List configured workspace profiles."""
    profiles = list_profiles()
    active_profile = selected_profile_name()
    if not profiles:
        raise RebaseWorkflowError("no workspace profiles configured. Run `rebase setup` first.")
    console.print(_workspace_table(profiles, active_profile=active_profile))


def _switch_workspace(profile: str) -> None:
    try:
        set_default_profile(profile)
    except KeyError as exc:
        raise RebaseWorkflowError(
            f"unknown workspace profile: {profile}. Run `rebase setup --profile {profile}` first."
        ) from exc
    console.print(f"Switched workspace profile to '[rebase.value]{profile}[/rebase.value]'")


@workspace_app.command("switch")
def workspace_switch_command(profile: Annotated[str, typer.Argument(help="Profile name.")]) -> None:
    """Switch the active workspace profile."""
    _switch_workspace(profile)


@workspace_app.command("use", hidden=True)
def workspace_use_command(profile: Annotated[str, typer.Argument(help="Profile name.")]) -> None:
    """Alias for `rebase workspace switch`."""
    _switch_workspace(profile)


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
    email: Annotated[str | None, typer.Option("--email", help="Email address to invite.")] = None,
    github_username: Annotated[str | None, typer.Option("--github", help="GitHub username to invite.")] = None,
    role: Annotated[
        str,
        typer.Option("--role", help="Workspace role: Viewer, Developer, Admin, or Owner."),
    ] = "Viewer",
) -> None:
    """Invite a person to the active workspace."""
    if role not in {"Viewer", "Developer", "Admin", "Owner"}:
        raise RebaseWorkflowError("role must be one of: Viewer, Developer, Admin, Owner")
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


@workspace_app.command("members")
def workspace_members_command(
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List active workspace members and pending invites."""
    client = Client()
    members = client.list_workspace_members()
    pending_invites = [invite for invite in client.list_workspace_invites() if invite.get("status") == "pending"]
    if json_output:
        _print_json({"members": members, "pending_invites": pending_invites})
        return
    console.print(_workspace_members_table(members, pending_invites))


app.add_typer(workspace_app, name="workspace")


@api_key_app.command("list")
def api_key_list_command(
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON output.")] = False,
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
    project: Annotated[str | None, typer.Option("--project", help="Scope the key to a project name.")] = None,
    project_id: Annotated[
        str | None,
        typer.Option("--project-id", help="Scope the key to an exact project ID."),
    ] = None,
    permission: Annotated[
        list[str] | None,
        typer.Option("--permission", help="Permission to grant. Repeat to override the read-only agent preset."),
    ] = None,
    expires_at: Annotated[str | None, typer.Option("--expires-at", help="ISO datetime when the key expires.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON output.")] = False,
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
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON output.")] = False,
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
    project: Annotated[str | None, typer.Option("--project", help="Filter by project name.")] = None,
    project_id: Annotated[str | None, typer.Option("--project-id", help="Filter by exact project ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON output.")] = False,
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
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON output.")] = False,
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
    json_body: Annotated[str | None, typer.Option("--json", help="JSON object to send to the endpoint.")] = None,
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
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON output.")] = False,
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
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON output.")] = False,
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
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON output.")] = False,
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
    project_id: Annotated[str | None, typer.Option("--id", help="Exact project ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON output.")] = False,
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


app.add_typer(project_app, name="project")


@function_app.command("list")
def function_list_command(
    project: Annotated[str | None, typer.Option("--project", help="Filter by project name.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List functions in the active workspace."""
    client = Client()
    if project is not None:
        project_data = _resolve_project_by_name(client, project)
        functions = client.list_functions(project_id=str(project_data["id"]))
        project_names = {str(project_data["id"]): str(project_data["name"])}
    else:
        functions = client.list_functions()
        project_names = _project_name_map(client.list_projects())
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
    project: Annotated[str | None, typer.Option("--project", help="Project name for name-based lookup.")] = None,
    function_id: Annotated[str | None, typer.Option("--id", help="Exact function ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON output.")] = False,
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
                "execution_backend",
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
    project: Annotated[str | None, typer.Option("--project", help="Project name for name-based lookup.")] = None,
    function_id: Annotated[str | None, typer.Option("--id", help="Exact function ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List versions for a function."""
    client = Client()
    function = _resolve_function_selector(client, name, function_id=function_id, project_name=project)
    versions = client.list_function_versions(str(function["id"]))
    if json_output:
        _print_json(versions)
        return
    console.print(_version_table("Function Versions", versions))


app.add_typer(function_app, name="function")


@workflow_app.command("list")
def workflow_list_command(
    project: Annotated[str | None, typer.Option("--project", help="Filter by project name.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List workflows in the active workspace."""
    client = Client()
    if project is not None:
        project_data = _resolve_project_by_name(client, project)
        workflows = client.list_workflows(project_id=str(project_data["id"]))
        project_names = {str(project_data["id"]): str(project_data["name"])}
    else:
        workflows = client.list_workflows()
        project_names = _project_name_map(client.list_projects())
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
    project: Annotated[str | None, typer.Option("--project", help="Project name for name-based lookup.")] = None,
    workflow_id: Annotated[str | None, typer.Option("--id", help="Exact workflow ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON output.")] = False,
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
                "execution_backend",
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
    project: Annotated[str | None, typer.Option("--project", help="Project name for name-based lookup.")] = None,
    workflow_id: Annotated[str | None, typer.Option("--id", help="Exact workflow ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List versions for a workflow."""
    client = Client()
    workflow = _resolve_workflow_selector(client, name, workflow_id=workflow_id, project_name=project)
    versions = client.list_workflow_versions(str(workflow["id"]))
    if json_output:
        _print_json(versions)
        return
    console.print(_version_table("Workflow Versions", versions))


app.add_typer(workflow_app, name="workflow")


@model_app.command("list")
def model_list_command(
    project: Annotated[str | None, typer.Option("--project", help="Filter by project name.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON output.")] = False,
) -> None:
    """List models in the active workspace."""
    client = Client()
    if project is not None:
        project_data = _resolve_project_by_name(client, project)
        models = client.list_models(project_id=str(project_data["id"]))
        project_names = {str(project_data["id"]): str(project_data["name"])}
    else:
        models = client.list_models()
        project_names = _project_name_map(client.list_projects())
    if json_output:
        _print_json(models)
        return
    console.print(_model_table(models, project_names=project_names))


@model_app.command("get")
def model_get_command(
    name: Annotated[str | None, typer.Argument(help="Model name. Omit when using --id.")] = None,
    project: Annotated[str | None, typer.Option("--project", help="Project name for name-based lookup.")] = None,
    model_id: Annotated[str | None, typer.Option("--id", help="Exact model ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON output.")] = False,
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
                "execution_backend",
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
    environment: Annotated[str, typer.Option("--env", help="Deployment environment: dev, staging, or prod.")] = "dev",
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
    model_id: Annotated[str | None, typer.Option("--id", help="Exact model ID.")] = None,
    environment: Annotated[str, typer.Option("--env", help="Deployment environment to run.")] = "dev",
    parameter: Annotated[
        list[str] | None,
        typer.Option("--param", "-p", help="Model parameter as name=json_value. Can be passed more than once."),
    ] = None,
    parameters_json: Annotated[str | None, typer.Option("--parameters-json", help="JSON object of parameters.")] = None,
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="Wait for the run to finish.")] = True,
    timeout: Annotated[int, typer.Option("--timeout", help="Maximum seconds to wait.")] = 600,
    poll_interval: Annotated[float, typer.Option("--poll-interval", help="Seconds between status polls.")] = 1.0,
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON output.")] = False,
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
    project: Annotated[str | None, typer.Option("--project", help="Project name for name-based lookup.")] = None,
    model_id: Annotated[str | None, typer.Option("--id", help="Exact model ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON output.")] = False,
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
    project: Annotated[str | None, typer.Option("--project", help="Project name for name-based lookup.")] = None,
    model_id: Annotated[str | None, typer.Option("--id", help="Exact model ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON output.")] = False,
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
    project: Annotated[str | None, typer.Option("--project", help="Project name for name-based lookup.")] = None,
    model_id: Annotated[str | None, typer.Option("--id", help="Exact model ID.")] = None,
    from_environment: Annotated[str, typer.Option("--from", help="Source environment.")] = "dev",
    to_environment: Annotated[str, typer.Option("--to", help="Target environment.")] = "staging",
    model_version_id: Annotated[str | None, typer.Option("--version-id", help="Specific model version ID.")] = None,
    promotion_request_id: Annotated[
        str | None,
        typer.Option("--promotion-request-id", help="Approved request ID required for prod."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON output.")] = False,
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
    project: Annotated[str | None, typer.Option("--project", help="Project name for name-based lookup.")] = None,
    model_id: Annotated[str | None, typer.Option("--id", help="Exact model ID.")] = None,
    model_version_id: Annotated[str | None, typer.Option("--version-id", help="Model version ID.")] = None,
    from_environment: Annotated[str, typer.Option("--from", help="Source environment.")] = "staging",
    to_environment: Annotated[str, typer.Option("--to", help="Target environment.")] = "prod",
    reason: Annotated[str | None, typer.Option("--reason", help="Promotion reason.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON output.")] = False,
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
    project: Annotated[str | None, typer.Option("--project", help="Project name for name-based lookup.")] = None,
    model_id: Annotated[str | None, typer.Option("--id", help="Exact model ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON output.")] = False,
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
    reason: Annotated[str | None, typer.Option("--reason", help="Review reason.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Approve a model promotion request."""
    request = Client().approve_model_promotion_request(request_id, reason=reason)
    _print_json(request) if json_output else console.print(_detail_table("Model Promotion Request", request))


@model_app.command("reject-promotion")
def model_reject_promotion_command(
    request_id: Annotated[str, typer.Argument(help="Promotion request ID.")],
    reason: Annotated[str | None, typer.Option("--reason", help="Review reason.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Reject a model promotion request."""
    request = Client().reject_model_promotion_request(request_id, reason=reason)
    _print_json(request) if json_output else console.print(_detail_table("Model Promotion Request", request))


@model_app.command("rollback")
def model_rollback_command(
    name: Annotated[str | None, typer.Argument(help="Model name. Omit when using --id.")] = None,
    project: Annotated[str | None, typer.Option("--project", help="Project name for name-based lookup.")] = None,
    model_id: Annotated[str | None, typer.Option("--id", help="Exact model ID.")] = None,
    environment: Annotated[str, typer.Option("--env", help="Environment to roll back.")] = "prod",
    model_version_id: Annotated[str | None, typer.Option("--version-id", help="Specific prior version ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON output.")] = False,
) -> None:
    """Roll back a model environment."""
    client = Client()
    model = _resolve_model_selector(client, name, model_id=model_id, project_name=project)
    deployment = client.rollback_model(str(model["id"]), environment=environment, model_version_id=model_version_id)
    _print_json(deployment) if json_output else console.print(_detail_table("Model Deployment", deployment))


@model_app.command("events")
def model_events_command(
    name: Annotated[str | None, typer.Argument(help="Model name. Omit when using --id.")] = None,
    project: Annotated[str | None, typer.Option("--project", help="Project name for name-based lookup.")] = None,
    model_id: Annotated[str | None, typer.Option("--id", help="Exact model ID.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON output.")] = False,
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
        typer.Option("--source", help="Override deploy source for this command: rebase or github."),
    ] = None,
) -> None:
    """Deploy Rebase objects from a Python file."""
    deployed = deploy_file(file, object_names=name, deploy_source=source)
    console.print(_deploy_table(deployed))


RUN_INSPECTION_COMMANDS = {"list", "get", "logs", "cancel"}


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
    backend: Annotated[
        str | None,
        typer.Option(
            "--backend",
            help="Override the cloud execution backend for this ephemeral run.",
        ),
    ] = None,
    module: Annotated[
        bool,
        typer.Option("--module", "-m", help="Interpret the target source as a Python module path instead of a file."),
    ] = False,
    wait: Annotated[
        bool,
        typer.Option("--wait/--no-wait", help="Wait for the function result before exiting."),
    ] = True,
    timeout: Annotated[int, typer.Option("--timeout", help="Maximum seconds to wait for the result.")] = 600,
    poll_interval: Annotated[
        float,
        typer.Option("--poll-interval", help="Seconds between run status polls."),
    ] = 1.0,
) -> None:
    """Run local Rebase targets and inspect submitted runs."""
    run: Run | None = None
    result: dict[str, Any] | None = None
    wait_for_result = wait
    started_at = time.monotonic()
    with _run_progress_reporter() as reporter:
        reporter.update("Loading local Rebase target...")
        target = _resolve_run_target(target_ref, as_module=module)
        reporter.complete("Loaded local Rebase target.")

        backend_override = _validate_run_backend_override(backend, target)
        if backend_override is not None:
            target.execution_backend = backend_override

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
    project: Annotated[str | None, typer.Option("--project", help="Filter by project name.")] = None,
    target_type: Annotated[
        str | None,
        typer.Option("--target-type", help="Filter by target type: function, workflow, or model."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=500, help="Maximum number of runs to list.")] = 100,
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON output.")] = False,
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


@run_app.command("get")
def run_get_command(
    run_id: Annotated[str, typer.Argument(help="Run ID.")],
    json_output: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON output.")] = False,
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


@run_app.command("logs")
def run_logs_command(
    run_id: Annotated[str, typer.Argument(help="Run ID.")],
    follow: Annotated[
        bool,
        typer.Option("--follow/--no-follow", help="Follow until the run reaches a terminal state."),
    ] = True,
    poll_interval: Annotated[
        float,
        typer.Option("--poll-interval", help="Seconds between run status polls when following."),
    ] = 1.0,
    timeout: Annotated[int, typer.Option("--timeout", help="Maximum seconds to follow the run.")] = 600,
    json_output: Annotated[bool, typer.Option("--json", help="Print raw event and step JSON output.")] = False,
) -> None:
    """Show persisted run events and workflow step state."""
    client = Client()
    run_data = client.get_run(run_id)
    events = client.list_run_events(run_id)
    steps = client.list_run_steps(run_id) if run_data.get("target_type") == "workflow" else []
    if json_output:
        _print_json(_raw_run_logs(run_data, events, steps))
        return

    run = Run(run_id, client=client, data=run_data)
    with _run_progress_reporter() as reporter:
        if not follow:
            _render_run_snapshot(run=run_data, events=events, steps=steps, reporter=reporter)
            return
        _stream_run_result(
            run,
            target_type=str(run_data.get("target_type") or ""),
            reporter=reporter,
            started_at=time.monotonic(),
            timeout=timeout,
            poll_interval=poll_interval,
            return_result=False,
        )


@run_app.command("cancel")
def run_cancel_command(
    run_id: Annotated[str, typer.Argument(help="Run ID.")],
) -> None:
    """Cancel a run."""
    raise RebaseWorkflowError(f"run cancellation is not supported yet: {run_id}")


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
    if not args or args == ["--help"]:
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
            run_app(args=args[1:], prog_name="rebase run", standalone_mode=False)
        else:
            app(args=args, prog_name="rebase", standalone_mode=False)
        return 0
    except RebaseWorkflowError as exc:
        error_console.print(f"Error: {exc}", style="rebase.error")
        return 1
    except click.ClickException as exc:
        exc.show(file=sys.stderr)
        return int(exc.exit_code)
    except click.exceptions.Exit as exc:
        return int(exc.exit_code or 0)
    except click.Abort:
        error_console.print("Aborted.", style="rebase.error")
        return 1
    except KeyboardInterrupt:
        error_console.print("Aborted.", style="rebase.error")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
