from __future__ import annotations

import getpass
import importlib.util
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Annotated, Any

import click
import typer
from rich import box
from rich.console import Console
from rich.table import Table

from rebase.client import Client, Function, Project, RebaseWorkflowError, Step, Workflow
from rebase.config import (
    DEFAULT_API_URL,
    DEFAULT_PROFILE,
    list_profiles,
    selected_profile_name,
    set_default_profile,
    write_profile,
)

console = Console(highlight=False, soft_wrap=True)
error_console = Console(stderr=True, highlight=False, soft_wrap=True)

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
    table = Table(title="Workspace Profiles", box=box.ASCII, show_header=True)
    table.add_column("Active", justify="center", no_wrap=True)
    table.add_column("Profile", style="bold", no_wrap=True)
    table.add_column("Workspace")
    table.add_column("Workspace ID")
    for profile, data in sorted(profiles.items()):
        is_active = profile == active_profile
        style = "green" if is_active else None
        table.add_row("*" if is_active else "", profile, _workspace_value(data), _workspace_id(data), style=style)
    return table


def _deploy_table(deployed: list[tuple[str, str, str | None]]) -> Table:
    table = Table(title="Deployed Targets", box=box.ASCII, show_header=True)
    table.add_column("Type", no_wrap=True)
    table.add_column("Name", style="bold")
    table.add_column("ID")
    for target_type, name, target_id in deployed:
        table.add_row(target_type, name, target_id or "-")
    return table


@app.command("setup")
def setup_command(
    profile: Annotated[str, typer.Option("--profile", help="Credential profile name.")] = DEFAULT_PROFILE,
    api_key: Annotated[str | None, typer.Option("--api-key", hidden=True)] = None,
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
        try:
            workspace = Client(api_key=api_key).get_workspace()
        except Exception as exc:
            raise RebaseWorkflowError(f"could not verify API key against {DEFAULT_API_URL}: {exc}") from exc

    path = write_profile(api_key=api_key, profile=profile, workspace=workspace)
    console.print(f"Saved Rebase credentials for profile '[bold]{profile}[/bold]'")
    console.print(f"[dim]{path}[/dim]")


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
    console.print(f"Switched workspace profile to '[bold green]{profile}[/bold green]'")


@workspace_app.command("switch")
def workspace_switch_command(profile: Annotated[str, typer.Argument(help="Profile name.")]) -> None:
    """Switch the active workspace profile."""
    _switch_workspace(profile)


@workspace_app.command("use", hidden=True)
def workspace_use_command(profile: Annotated[str, typer.Argument(help="Profile name.")]) -> None:
    """Alias for `rebase workspace switch`."""
    _switch_workspace(profile)


app.add_typer(workspace_app, name="workspace")


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


def main(argv: list[str] | None = None) -> int:
    try:
        app(args=argv, prog_name="rebase", standalone_mode=False)
        return 0
    except RebaseWorkflowError as exc:
        error_console.print(f"Error: {exc}", style="red")
        return 1
    except click.ClickException as exc:
        exc.show(file=sys.stderr)
        return int(exc.exit_code)
    except click.Abort:
        error_console.print("Aborted.", style="red")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
