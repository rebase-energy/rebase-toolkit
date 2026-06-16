from __future__ import annotations

import getpass
import importlib
import importlib.util
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Annotated, Any

import click
import typer
import typer.rich_utils as typer_rich
from rich import box
from rich.console import Console
from rich.table import Table
from rich.theme import Theme

from rebase.client import Client, Function, FunctionBackend, Project, RebaseWorkflowError, Step, Workflow
from rebase.config import (
    DEFAULT_PROFILE,
    DEFAULT_SERVER_URL,
    list_profiles,
    selected_profile_name,
    set_default_profile,
    write_profile,
)

BRAND_MAIN_GREEN = "#0D9373"
BRAND_BRIGHT_GREEN = "#03C497"
BRAND_MEDIUM_GRAY = "#656565"
BRAND_CORAL_RED = "#E46962"
BRAND_AMBER = "#FBAE40"
BRAND_SLATE_BLUE = "#3F6E91"

REBASE_THEME = Theme(
    {
        "rebase.active": f"bold {BRAND_BRIGHT_GREEN}",
        "rebase.border": f"dim {BRAND_MEDIUM_GRAY}",
        "rebase.error": BRAND_CORAL_RED,
        "rebase.info": BRAND_SLATE_BLUE,
        "rebase.muted": BRAND_MEDIUM_GRAY,
        "rebase.success": BRAND_MAIN_GREEN,
        "rebase.title": f"bold {BRAND_MAIN_GREEN}",
        "rebase.value": f"bold {BRAND_BRIGHT_GREEN}",
        "rebase.warning": BRAND_AMBER,
    }
)


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


RunnableTarget = Function | Workflow


def _function_objects(module: ModuleType) -> list[tuple[str, Function]]:
    return [
        (name, function)
        for name, function in _unique_named_objects(module, (Function,))
        if not isinstance(function, Step)
    ]


def _runnable_objects(module: ModuleType) -> list[tuple[str, RunnableTarget]]:
    return [
        (name, target)
        for name, target in _unique_named_objects(module, (Function, Workflow))
        if not isinstance(target, Step)
    ]


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
        if not isinstance(target, (Function, Workflow)):
            raise RebaseWorkflowError(f"target is not a Rebase function or workflow: {object_ref}")
        return target

    targets = _runnable_objects(module)
    if not targets:
        raise RebaseWorkflowError(
            "No runnable Rebase targets found. Define one top-level rb.function(...) or rb.workflow(...) target."
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
    if backend not in {"modal", "prefect", "prefect_cloud", "cloud_run", "cloud_run_shared"}:
        raise RebaseWorkflowError(
            "backend must be 'modal', 'prefect', 'prefect_cloud', 'cloud_run', or 'cloud_run_shared'"
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


def deploy_file(path: str | Path, *, object_names: Iterable[str] | None = None) -> list[tuple[str, str, str | None]]:
    module = _load_module(Path(path))
    selected_names = set(object_names or [])

    all_projects = _unique_named_objects(module, (Project,))
    projects = all_projects
    if selected_names:
        projects = [
            (name, project)
            for name, project in projects
            if name in selected_names or project.name in selected_names
        ]

    deployed: list[tuple[str, str, str | None]] = []
    if projects:
        for name, project in projects:
            project.deploy()
            deployed.append(("project", project.name or name, project.id))
        return deployed
    if all_projects and selected_names:
        raise RebaseWorkflowError(f"No matching Rebase project found for: {', '.join(sorted(selected_names))}")

    deployables = _unique_named_objects(module, (Workflow, Function))
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
            "rb.workflow(...), or rb.function(...)."
        )

    for name, deployable in deployables:
        deployable.deploy()
        deployed.append((deployable.__class__.__name__.lower(), deployable.name or name, deployable.id))
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


def _deploy_table(deployed: list[tuple[str, str, str | None]]) -> Table:
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
    for target_type, name, target_id in deployed:
        table.add_row(target_type, name, target_id or "-")
    return table


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
            help="Verify the API key against the hosted Rebase API before saving it.",
        ),
    ] = True,
) -> None:
    """Store a Rebase API key on this computer."""
    api_key = api_key or getpass.getpass("Rebase API key: ")
    api_key = api_key.strip()
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


app.add_typer(workspace_app, name="workspace")


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
) -> None:
    """Deploy Rebase objects from a Python file."""
    deployed = deploy_file(file, object_names=name)
    console.print(_deploy_table(deployed))


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
    """Run a Rebase function or workflow from local source without deploying it."""
    target = _resolve_run_target(target_ref, as_module=module)
    backend_override = _validate_run_backend_override(backend, target)
    if backend_override is not None:
        target.execution_backend = backend_override

    parameters = _parse_run_parameters(parameters_json, parameter)
    run = target.ephemeral_run(**parameters)

    if not wait:
        console.print(_run_table(run.id, run.status))
        return

    result = run.result(timeout=timeout, poll_interval=poll_interval)
    console.print_json(data=result)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        try:
            app(args=["--help"], prog_name="rebase", standalone_mode=True)
        except SystemExit as exc:
            return int(exc.code or 0)
        return 0

    try:
        app(args=args, prog_name="rebase", standalone_mode=False)
        return 0
    except RebaseWorkflowError as exc:
        error_console.print(f"Error: {exc}", style="rebase.error")
        return 1
    except click.ClickException as exc:
        exc.show(file=sys.stderr)
        return int(exc.exit_code)
    except click.Abort:
        error_console.print("Aborted.", style="rebase.error")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
