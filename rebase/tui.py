from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable, Footer, Header, Static, TabbedContent, TabPane

from rebase.brand import (
    BRAND_AMBER,
    BRAND_BRIGHT_GREEN,
    BRAND_CORAL_RED,
    BRAND_MAIN_GREEN,
    BRAND_MEDIUM_GRAY,
    BRAND_SLATE_BLUE,
)
from rebase.client import Client, RebaseWorkflowError
from rebase.config import list_profiles, load_profile, selected_profile_name, set_default_profile

TargetType = Literal["function", "workflow"]
ViewName = Literal["workspace", "project", "workspace-switcher"]
#: Concurrent requests used to collect the workspace overview's per-project function counts.
OVERVIEW_FANOUT_WORKERS = 16


@dataclass(frozen=True)
class ProjectSummary:
    project: dict[str, Any]
    function_count: int
    workflow_count: int


@dataclass(frozen=True)
class WorkspaceOverviewData:
    projects: list[dict[str, Any]]
    project_summaries: list[ProjectSummary]
    project_names: dict[str, str]


@dataclass(frozen=True)
class ProjectTargetsData:
    project: dict[str, Any]
    functions: list[dict[str, Any]]
    workflows: list[dict[str, Any]]
    endpoints: list[dict[str, Any]]
    asgi_apps: list[dict[str, Any]]


@dataclass(frozen=True)
class RunDetailData:
    run: dict[str, Any]
    events: list[dict[str, Any]]
    steps: list[dict[str, Any]]


def _optional_list(load: Callable[[], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    try:
        return load()
    except RebaseWorkflowError:
        return []


class RebaseTuiData:
    def __init__(self, client: Client | None = None, *, project: str | None = None, limit: int = 25) -> None:
        self.client = client or Client()
        self.project = project
        self.limit = limit

    def load_workspace_overview(self) -> WorkspaceOverviewData:
        projects = self.client.list_projects()
        if self.project is not None and not any(project.get("name") == self.project for project in projects):
            raise RebaseWorkflowError(f"project not found: {self.project}")

        workflow_counts = self._workflow_counts()
        function_counts = self._function_counts(projects)
        return WorkspaceOverviewData(
            projects=projects,
            project_summaries=[
                ProjectSummary(
                    project=project,
                    function_count=function_counts.get(str(project["id"]), 0),
                    workflow_count=workflow_counts.get(str(project["id"]), 0),
                )
                for project in projects
            ],
            project_names={str(project.get("id", "")): str(project.get("name", "-")) for project in projects},
        )

    def _workflow_counts(self) -> dict[str, int]:
        """Workflow counts per project id, from a single workspace-wide call.

        Every workflow carries its `project_id`, so one `/workflows` request stands in for
        one request per project.
        """
        return Counter(
            str(workflow["project_id"]) for workflow in self.client.list_workflows() if workflow.get("project_id")
        )

    def _function_counts(self, projects: list[dict[str, Any]]) -> dict[str, int]:
        """Function counts per project id, fanned out concurrently.

        There is no workspace-wide functions route, so this stays one request per project.
        Run end to end those requests are pure round-trip latency, and they dominated the
        startup wait on workspaces with many projects.
        """
        project_ids = [str(project["id"]) for project in projects]
        if not project_ids:
            return {}
        with ThreadPoolExecutor(max_workers=min(OVERVIEW_FANOUT_WORKERS, len(project_ids))) as executor:
            counts = executor.map(
                lambda project_id: len(self.client.list_functions(project_id=project_id)), project_ids
            )
            return dict(zip(project_ids, counts, strict=True))

    def load_project_targets(self, project: dict[str, Any]) -> ProjectTargetsData:
        project_id = str(project["id"])
        return ProjectTargetsData(
            project=project,
            functions=self.client.list_functions(project_id=project_id),
            workflows=self.client.list_workflows(project_id=project_id),
            # Supplementary data: never let it take down the function/workflow view.
            endpoints=_optional_list(lambda: self.client.list_project_endpoints(project_id)),
            asgi_apps=_optional_list(lambda: self.client.list_asgi_apps(project_id=project_id)),
        )

    def load_target_runs(self, target_type: TargetType, target_id: str) -> list[dict[str, Any]]:
        if target_type == "function":
            return self.client.list_runs(function_id=target_id, target_type="function", limit=self.limit)
        return self.client.list_runs(workflow_id=target_id, target_type="workflow", limit=self.limit)

    def load_run_detail(self, run_id: str) -> RunDetailData:
        run = self.client.get_run(run_id)
        events = self.client.list_run_events(run_id)
        steps = self.client.list_run_steps(run_id) if run.get("target_type") == "workflow" else []
        return RunDetailData(run=run, events=events, steps=steps)


def compact_id(value: Any, *, length: int = 8) -> str:
    if value is None:
        return "-"
    text = str(value)
    if len(text) <= length + 4:
        return text
    return f"{text[:length]}..."


def format_timestamp(value: Any) -> str:
    if value in {None, ""}:
        return "-"
    raw = str(value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def format_bool(value: Any) -> str:
    if value is True:
        return "enabled"
    if value is False:
        return "disabled"
    return "-"


def format_schedule(value: Any) -> str:
    if not isinstance(value, dict):
        return "-"
    cron = str(value.get("cron") or "-")
    if not value.get("active", True):
        return f"{cron} ⏸"
    return cron


def format_json_summary(value: Any, *, max_length: int = 180) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, str):
        text = value
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, sort_keys=True)
    else:
        text = str(value)
    return text if len(text) <= max_length else f"{text[: max_length - 3]}..."


def endpoints_by_target(endpoints: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Index endpoints by the (target_type, target_id) pair they are attached to."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for endpoint in endpoints:
        target_type = endpoint.get("target_type")
        target_id = endpoint.get("target_id")
        if not isinstance(target_type, str) or target_id is None:
            continue
        grouped.setdefault((target_type, str(target_id)), []).append(endpoint)
    return grouped


def format_endpoint(endpoints: list[dict[str, Any]]) -> Text:
    """Render the route of a target's primary endpoint, dimmed when it is disabled.

    The API can hold several endpoints per target (the deploy upsert keys on name,
    not on target), so surface the newest one and count the rest.
    """
    if not endpoints:
        return Text("-")
    primary = endpoints[0]
    label = f"{primary.get('method', '-')} {primary.get('path', '-')}"
    extra = len(endpoints) - 1
    if extra > 0:
        label = f"{label} (+{extra})"
    return Text(label, style="" if primary.get("enabled", True) else BRAND_MEDIUM_GRAY)


def format_endpoint_detail(endpoints: list[dict[str, Any]], *, api_url: str) -> list[str]:
    if not endpoints:
        return ["Endpoint: none"]
    primary = endpoints[0]
    lines = [
        f"Endpoint: {primary.get('method', '-')} {primary.get('path', '-')} | "
        f"auth: {primary.get('auth', '-')} | mode: {primary.get('mode', '-')} | "
        f"{format_bool(primary.get('enabled'))}",
        f"URL: {format_url(primary, api_url=api_url)}",
    ]
    if len(endpoints) > 1:
        others = ", ".join(f"{item.get('method', '-')} {item.get('path', '-')}" for item in endpoints[1:])
        lines.append(f"Also: {others}")
    return lines


def format_url(item: dict[str, Any], *, api_url: str) -> str:
    url = item.get("url")
    if isinstance(url, str) and url:
        return url
    url_path = item.get("url_path")
    if isinstance(url_path, str) and url_path:
        return f"{api_url}{url_path}"
    return "-"


def status_style(status: Any) -> str:
    normalized = str(status or "unknown").lower()
    if normalized in {"completed", "succeeded", "success"}:
        return BRAND_MAIN_GREEN
    if normalized == "running":
        return BRAND_BRIGHT_GREEN
    if normalized in {"submitted", "queued", "accepted"}:
        return BRAND_SLATE_BLUE
    if normalized in {"failed", "error", "cancelled", "canceled"}:
        return BRAND_CORAL_RED
    if normalized in {"pending", "starting", "warning"}:
        return BRAND_AMBER
    return BRAND_MEDIUM_GRAY


def status_text(status: Any) -> Text:
    value = str(status or "unknown")
    return Text(value, style=status_style(value))


class RebaseHeader(Header):
    def _on_click(self, event: events.Click) -> None:
        return None


class RebaseTuiApp(App[None]):
    TITLE = "Rebase TUI"
    SUB_TITLE = ""
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("b", "back", "Back"),
        # priority: the screen's default `tab` -> focus_next otherwise shadows this.
        Binding("tab", "toggle_target_tab", "Switch target", priority=True),
    ]
    CSS = f"""
    Screen {{
        background: #101412;
        color: #E8F0ED;
    }}

    RebaseHeader, Header, Footer {{
        background: #101412;
        color: {BRAND_BRIGHT_GREEN};
    }}

    RebaseHeader.-tall, Header.-tall {{
        height: 1;
    }}

    HeaderIcon {{
        color: {BRAND_BRIGHT_GREEN};
        width: 13;
        content-align: center middle;
    }}

    HeaderClock, HeaderClockSpace {{
        width: 13;
    }}

    #workspace-view, #project-view {{
        height: 1fr;
    }}

    #workspace-switcher-view {{
        height: 1fr;
    }}

    #projects-table,
    #workflows-table,
    #functions-table,
    #asgi-apps-table,
    #runs-table,
    #events-table,
    #steps-table {{
        overflow-x: hidden;
        scrollbar-size-horizontal: 0;
        scrollbar-background: #101412;
        scrollbar-background-hover: #101412;
        scrollbar-background-active: #101412;
    }}

    #workflows-table {{
        scrollbar-color: {BRAND_BRIGHT_GREEN};
        scrollbar-color-hover: {BRAND_BRIGHT_GREEN};
        scrollbar-color-active: {BRAND_MAIN_GREEN};
    }}

    #projects-table {{
        height: 1fr;
    }}

    #projects-table > .datatable--odd-row,
    #projects-table > .datatable--even-row {{
        background: #101412;
    }}

    #summary {{
        height: 3;
        padding: 0 1;
        border-bottom: solid {BRAND_MEDIUM_GRAY};
    }}

    #target-tabs {{
        height: 10;
        border-bottom: solid {BRAND_MEDIUM_GRAY};
    }}

    .panel {{
        height: 4;
        padding: 0 1;
        border-bottom: solid {BRAND_MEDIUM_GRAY};
    }}

    #target-detail {{
        height: 7;
    }}

    #runs-table {{
        height: 8;
        border-bottom: solid {BRAND_MEDIUM_GRAY};
    }}

    #events-table {{
        height: 1fr;
        border-bottom: solid {BRAND_MEDIUM_GRAY};
    }}

    #steps-table {{
        height: 7;
    }}

    DataTable {{
        background: #101412;
        scrollbar-size-vertical: 1;
    }}
    """

    def __init__(
        self,
        *,
        client: Client | None = None,
        data: RebaseTuiData | None = None,
        project: str | None = None,
        limit: int = 25,
    ) -> None:
        super().__init__()
        self.data = data or RebaseTuiData(client, project=project, limit=limit)
        self.project = project
        self.limit = limit
        self.profile_name = selected_profile_name()
        self.profile_data = load_profile(self.profile_name)
        self.workspace_overview: WorkspaceOverviewData | None = None
        self.project_targets: ProjectTargetsData | None = None
        self.selected_project: dict[str, Any] | None = None
        self.selected_target_type: TargetType | None = None
        self.selected_target: dict[str, Any] | None = None
        self.current_view: ViewName = "workspace"
        self.view_before_switcher: ViewName = "workspace"
        self._project_rows: dict[str, ProjectSummary] = {}
        self._profile_rows: dict[str, dict[str, Any]] = {}
        self._function_rows: dict[str, dict[str, Any]] = {}
        self._workflow_rows: dict[str, dict[str, Any]] = {}
        self._asgi_app_rows: dict[str, dict[str, Any]] = {}
        self._endpoints_by_target: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._run_rows: dict[str, dict[str, Any]] = {}

    def compose(self) -> ComposeResult:
        yield RebaseHeader(show_clock=True, icon="• Commands")
        with Vertical(id="workspace-view"):
            yield DataTable(id="projects-table")
        with Vertical(id="workspace-switcher-view"):
            yield DataTable(id="workspace-profiles-table")
        with Vertical(id="project-view"):
            yield Static("", id="summary")
            yield Static("Select a project.", id="project-detail", classes="panel")
            with TabbedContent(initial="workflows-tab", id="target-tabs"):
                with TabPane("Workflows", id="workflows-tab"):
                    yield DataTable(id="workflows-table")
                with TabPane("Functions", id="functions-tab"):
                    yield DataTable(id="functions-table")
                with TabPane("ASGI apps", id="asgi-apps-tab"):
                    yield DataTable(id="asgi-apps-table")
            yield Static("Select a workflow or function.", id="target-detail", classes="panel")
            yield DataTable(id="runs-table")
            yield Static("Select a run.", id="run-detail", classes="panel")
            yield DataTable(id="events-table")
            yield DataTable(id="steps-table")
        yield Footer()

    def on_mount(self) -> None:
        self._setup_tables()
        self._update_workspace_title()
        self._show_workspace_view()
        self.action_refresh()

    def _setup_tables(self) -> None:
        projects = self.query_one("#projects-table", DataTable)
        projects.cursor_type = "row"
        projects.zebra_stripes = False
        projects.add_columns("Project", "Workflows", "Functions")

        profiles = self.query_one("#workspace-profiles-table", DataTable)
        profiles.cursor_type = "row"
        profiles.zebra_stripes = True
        profiles.add_columns("Active", "Profile", "Workspace", "Workspace ID", "API URL")

        functions = self.query_one("#functions-table", DataTable)
        functions.cursor_type = "row"
        functions.zebra_stripes = True
        functions.add_columns("Name", "Run type", "State", "Endpoint", "Version", "Updated")

        workflows = self.query_one("#workflows-table", DataTable)
        workflows.cursor_type = "row"
        workflows.zebra_stripes = True
        workflows.add_columns("Name", "Run type", "State", "Endpoint", "Schedule", "Next run", "Version", "Updated")

        asgi_apps = self.query_one("#asgi-apps-table", DataTable)
        asgi_apps.cursor_type = "row"
        asgi_apps.zebra_stripes = True
        asgi_apps.add_columns("Name", "Base path", "Auth", "State", "URL path", "Updated")

        runs = self.query_one("#runs-table", DataTable)
        runs.cursor_type = "row"
        runs.zebra_stripes = True
        runs.add_columns("Run", "Status", "Backend", "Created", "Finished")

        events = self.query_one("#events-table", DataTable)
        events.zebra_stripes = True
        events.add_columns("Time", "Stage", "Status", "Message")

        steps = self.query_one("#steps-table", DataTable)
        steps.zebra_stripes = True
        steps.add_columns("Step", "Status", "Attempt", "Started", "Finished", "Error")

    def action_refresh(self) -> None:
        if self.current_view == "workspace-switcher":
            self._render_workspace_profiles()
            return
        if self.current_view == "project" and self.selected_project is not None:
            self.run_worker(
                self._load_project_targets(self.selected_project),
                name="project-targets",
                group="tui",
                exclusive=True,
            )
            return
        self.run_worker(self._load_workspace_overview(), name="overview", group="tui", exclusive=True)

    def action_back(self) -> None:
        if self.current_view == "workspace-switcher":
            if self.view_before_switcher == "project":
                self._show_project_view()
            else:
                self._show_workspace_view()
            return
        if self.current_view == "project":
            self.selected_project = None
            self.selected_target = None
            self.selected_target_type = None
            self.project_targets = None
            self._clear_target_detail()
            self._show_workspace_view()

    def on_click(self, event: events.Click) -> None:
        if event.widget.__class__.__name__ == "HeaderTitle":
            event.stop()
            self._open_workspace_switcher()

    def action_toggle_target_tab(self) -> None:
        tabs = self.query_one("#target-tabs", TabbedContent)
        order = ["workflows-tab", "functions-tab", "asgi-apps-tab"]
        current = order.index(tabs.active) if tabs.active in order else 0
        tabs.active = order[(current + 1) % len(order)]

    async def _load_workspace_overview(self) -> None:
        try:
            overview = await asyncio.to_thread(self.data.load_workspace_overview)
        except Exception as exc:
            self._set_error(exc)
            return
        self.workspace_overview = overview
        self.project_targets = None
        self.selected_project = None
        self.selected_target = None
        self.selected_target_type = None
        self._render_workspace_overview(overview)
        self._clear_target_detail()
        self._show_workspace_view()

    async def _load_project_targets(self, project: dict[str, Any]) -> None:
        self._set_summary(f"Loading project {project.get('name', project.get('id', '-'))}...")
        try:
            targets = await asyncio.to_thread(self.data.load_project_targets, project)
        except Exception as exc:
            self._set_error(exc)
            return
        self.project_targets = targets
        self._render_project_targets(targets)
        self._set_summary(self._summary_text())

    async def _load_runs(self, target_type: TargetType, target_id: str) -> None:
        self._set_summary(f"Loading latest {target_type} runs...")
        try:
            runs = await asyncio.to_thread(self.data.load_target_runs, target_type, target_id)
        except Exception as exc:
            self._set_error(exc)
            return
        self._render_runs(runs)
        self._set_summary(self._summary_text())

    async def _load_run_detail(self, run_id: str) -> None:
        self.query_one("#run-detail", Static).update(f"Loading run {compact_id(run_id)}...")
        try:
            detail = await asyncio.to_thread(self.data.load_run_detail, run_id)
        except Exception as exc:
            self._set_error(exc)
            return
        self._render_run_detail(detail)

    def _render_workspace_overview(self, overview: WorkspaceOverviewData) -> None:
        self._project_rows = {
            str(summary.project["id"]): summary
            for summary in overview.project_summaries
            if summary.project.get("id") is not None
        }

        projects = self.query_one("#projects-table", DataTable)
        projects.clear()
        for project_id, summary in self._project_rows.items():
            project = summary.project
            projects.add_row(
                str(project.get("name", "-")),
                str(summary.workflow_count),
                str(summary.function_count),
                key=project_id,
            )

        self._set_summary(self._summary_text())

    def _render_workspace_profiles(self) -> None:
        profiles = list_profiles()
        self._profile_rows = profiles
        table = self.query_one("#workspace-profiles-table", DataTable)
        table.clear()
        for profile_name, profile_data in sorted(profiles.items()):
            table.add_row(
                "*" if profile_name == self.profile_name else "",
                profile_name,
                self._profile_workspace_label(profile_data, fallback=profile_name),
                self._profile_workspace_id(profile_data),
                str(profile_data.get("api_url") or "-"),
                key=profile_name,
            )

    def _render_project_targets(self, targets: ProjectTargetsData) -> None:
        self._function_rows = {str(item["id"]): item for item in targets.functions if item.get("id") is not None}
        self._workflow_rows = {str(item["id"]): item for item in targets.workflows if item.get("id") is not None}
        self._asgi_app_rows = {str(item["id"]): item for item in targets.asgi_apps if item.get("id") is not None}
        self._endpoints_by_target = endpoints_by_target(targets.endpoints)

        functions = self.query_one("#functions-table", DataTable)
        functions.clear()
        for function_id, function in self._function_rows.items():
            functions.add_row(
                str(function.get("name", "-")),
                str(function.get("run_type") or "-"),
                format_bool(function.get("enabled")),
                format_endpoint(self._target_endpoints("function", function_id)),
                compact_id(function.get("current_version_id")),
                format_timestamp(function.get("updated_at")),
                key=function_id,
            )

        workflows = self.query_one("#workflows-table", DataTable)
        workflows.clear()
        for workflow_id, workflow in self._workflow_rows.items():
            workflows.add_row(
                str(workflow.get("name", "-")),
                str(workflow.get("run_type") or "-"),
                format_bool(workflow.get("enabled")),
                format_endpoint(self._target_endpoints("workflow", workflow_id)),
                format_schedule(workflow.get("schedule")),
                format_timestamp(workflow.get("next_run_at")),
                compact_id(workflow.get("current_version_id")),
                format_timestamp(workflow.get("updated_at")),
                key=workflow_id,
            )

        asgi_apps = self.query_one("#asgi-apps-table", DataTable)
        asgi_apps.clear()
        for asgi_app_id, asgi_app in self._asgi_app_rows.items():
            asgi_apps.add_row(
                str(asgi_app.get("name", "-")),
                str(asgi_app.get("base_path") or "-"),
                str(asgi_app.get("auth") or "-"),
                format_bool(asgi_app.get("enabled")),
                str(asgi_app.get("url_path") or "-"),
                format_timestamp(asgi_app.get("updated_at")),
                key=asgi_app_id,
            )

        self._clear_target_detail(clear_project=False)
        self.query_one("#target-detail", Static).update("Select a workflow, function, or ASGI app.")

    def _target_endpoints(self, target_type: str, target_id: str) -> list[dict[str, Any]]:
        return self._endpoints_by_target.get((target_type, target_id), [])

    def _render_runs(self, runs: list[dict[str, Any]]) -> None:
        self._run_rows = {str(item["id"]): item for item in runs if item.get("id") is not None}
        table = self.query_one("#runs-table", DataTable)
        table.clear()
        for run_id, run in self._run_rows.items():
            table.add_row(
                compact_id(run_id),
                status_text(run.get("status")),
                str(run.get("execution_backend", "-")),
                format_timestamp(run.get("created_at")),
                format_timestamp(run.get("finished_at")),
                key=run_id,
            )
        if not runs:
            self.query_one("#run-detail", Static).update("No runs found for the selected target.")
        else:
            self.query_one("#run-detail", Static).update("Select a run.")
        self.query_one("#events-table", DataTable).clear()
        self.query_one("#steps-table", DataTable).clear()

    def _render_run_detail(self, detail: RunDetailData) -> None:
        run = detail.run
        lines = [
            f"Run {compact_id(run.get('id'))} | {run.get('target_type', '-')}",
            f"Status: {run.get('status', 'unknown')} | Backend: {run.get('execution_backend', '-')}",
            f"Parameters: {format_json_summary(run.get('parameters'), max_length=110)}",
            f"Result: {format_json_summary(run.get('result'), max_length=130)}",
        ]
        if run.get("error"):
            lines[-1] = f"Error: {format_json_summary(run.get('error'), max_length=130)}"
        self.query_one("#run-detail", Static).update("\n".join(lines))

        events = self.query_one("#events-table", DataTable)
        events.clear()
        for event in detail.events:
            events.add_row(
                format_timestamp(event.get("created_at")),
                str(event.get("stage", "-")),
                status_text(event.get("status")),
                format_json_summary(event.get("message"), max_length=120),
            )

        steps = self.query_one("#steps-table", DataTable)
        steps.clear()
        for step in detail.steps:
            steps.add_row(
                str(step.get("name") or step.get("node_key") or "-"),
                status_text(step.get("status")),
                str(step.get("attempt", "-")),
                format_timestamp(step.get("started_at")),
                format_timestamp(step.get("finished_at")),
                format_json_summary(step.get("error"), max_length=80),
            )

    def _clear_target_detail(self, *, clear_project: bool = True) -> None:
        if clear_project:
            self.query_one("#project-detail", Static).update("Select a project.")
            self.query_one("#functions-table", DataTable).clear()
            self.query_one("#workflows-table", DataTable).clear()
            self.query_one("#asgi-apps-table", DataTable).clear()
            self._function_rows = {}
            self._workflow_rows = {}
            self._asgi_app_rows = {}
            self._endpoints_by_target = {}
        self.query_one("#target-detail", Static).update("Select a workflow, function, or ASGI app.")
        self.query_one("#runs-table", DataTable).clear()
        self.query_one("#run-detail", Static).update("Select a run.")
        self.query_one("#events-table", DataTable).clear()
        self.query_one("#steps-table", DataTable).clear()
        self._run_rows = {}

    def _render_project_detail(self, summary: ProjectSummary) -> None:
        project = summary.project
        description = format_json_summary(project.get("description"), max_length=110)
        lines = [
            f"Project {project.get('name', '-')}",
            f"Functions: {summary.function_count} | Workflows: {summary.workflow_count}",
            f"Updated: {format_timestamp(project.get('updated_at'))} | ID: {project.get('id', '-')}",
            f"Description: {description}",
        ]
        self.query_one("#project-detail", Static).update("\n".join(lines))

    def _select_project(self, project_id: str) -> None:
        summary = self._project_rows.get(project_id)
        if summary is None:
            return
        self.selected_project = summary.project
        self.selected_target = None
        self.selected_target_type = None
        self.project_targets = None
        self._render_project_detail(summary)
        self._clear_target_detail(clear_project=False)
        self.run_worker(
            self._load_project_targets(summary.project),
            name="project-targets",
            group="tui",
            exclusive=True,
        )
        self._show_project_view()

    def _show_workspace_view(self) -> None:
        self.current_view = "workspace"
        self.query_one("#workspace-view", Vertical).styles.display = "block"
        self.query_one("#project-view", Vertical).styles.display = "none"
        self.query_one("#workspace-switcher-view", Vertical).styles.display = "none"
        self.query_one("#projects-table", DataTable).focus()

    def _show_project_view(self) -> None:
        self.current_view = "project"
        self.query_one("#workspace-view", Vertical).styles.display = "none"
        self.query_one("#project-view", Vertical).styles.display = "block"
        self.query_one("#workspace-switcher-view", Vertical).styles.display = "none"

    def _show_workspace_switcher_view(self) -> None:
        self.current_view = "workspace-switcher"
        self.query_one("#workspace-view", Vertical).styles.display = "none"
        self.query_one("#project-view", Vertical).styles.display = "none"
        self.query_one("#workspace-switcher-view", Vertical).styles.display = "block"
        self.query_one("#workspace-profiles-table", DataTable).focus()

    def _open_workspace_switcher(self) -> None:
        self.view_before_switcher = self.current_view
        self._render_workspace_profiles()
        self._show_workspace_switcher_view()

    def _select_workspace_profile(self, profile_name: str) -> None:
        if profile_name not in self._profile_rows:
            return
        if profile_name == self.profile_name:
            self._show_workspace_view()
            return
        try:
            set_default_profile(profile_name)
        except KeyError:
            self._set_error(RebaseWorkflowError(f"unknown workspace profile: {profile_name}"))
            return
        self.profile_name = selected_profile_name()
        self.profile_data = load_profile(self.profile_name)
        self.project = None
        self.data = RebaseTuiData(Client(profile=self.profile_name), limit=self.limit)
        self.workspace_overview = None
        self.project_targets = None
        self.selected_project = None
        self.selected_target = None
        self.selected_target_type = None
        self._project_rows = {}
        self._function_rows = {}
        self._workflow_rows = {}
        self._asgi_app_rows = {}
        self._endpoints_by_target = {}
        self._run_rows = {}
        self._update_workspace_title()
        self._clear_target_detail()
        self._show_workspace_view()
        self.action_refresh()

    def _render_target_detail(self, target_type: TargetType, target: dict[str, Any]) -> None:
        lines = [
            f"{target_type.title()} {target.get('name', '-')}",
            f"Project: {self._project_name(target)} | Run type: {target.get('run_type') or '-'}",
            "State: "
            f"{format_bool(target.get('enabled'))} | Current version: {compact_id(target.get('current_version_id'))}",
            f"ID: {target.get('id', '-')}",
        ]
        lines.extend(
            format_endpoint_detail(
                self._target_endpoints(target_type, str(target.get("id", ""))),
                api_url=self._api_url(),
            )
        )
        self.query_one("#target-detail", Static).update("\n".join(lines))

    def _render_asgi_app_detail(self, asgi_app: dict[str, Any]) -> None:
        lines = [
            f"ASGI app {asgi_app.get('name', '-')}",
            f"Project: {self._project_name(asgi_app)} | Base path: {asgi_app.get('base_path') or '-'} | "
            f"Auth: {asgi_app.get('auth') or '-'}",
            f"State: {format_bool(asgi_app.get('enabled'))} | "
            f"Current version: {compact_id(asgi_app.get('current_version_id'))}",
            f"ID: {asgi_app.get('id', '-')}",
            f"URL: {format_url(asgi_app, api_url=self._api_url())}",
        ]
        self.query_one("#target-detail", Static).update("\n".join(lines))

    def _api_url(self) -> str:
        return str(getattr(self.data.client, "api_url", ""))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        row_id = event.row_key.value
        if row_id is None:
            return
        if event.data_table.id == "projects-table":
            self._select_project(row_id)
        elif event.data_table.id == "functions-table":
            target = self._function_rows.get(row_id)
            if target is None:
                return
            self.selected_target_type = "function"
            self.selected_target = target
            self._render_target_detail("function", target)
            self.run_worker(self._load_runs("function", row_id), name="runs", group="tui", exclusive=True)
        elif event.data_table.id == "workflows-table":
            target = self._workflow_rows.get(row_id)
            if target is None:
                return
            self.selected_target_type = "workflow"
            self.selected_target = target
            self._render_target_detail("workflow", target)
            self.run_worker(self._load_runs("workflow", row_id), name="runs", group="tui", exclusive=True)
        elif event.data_table.id == "asgi-apps-table":
            asgi_app = self._asgi_app_rows.get(row_id)
            if asgi_app is None:
                return
            # ASGI apps serve HTTP directly, so they have no runs to drill into.
            self.selected_target_type = None
            self.selected_target = None
            self._render_asgi_app_detail(asgi_app)
            self._render_runs([])
            self.query_one("#run-detail", Static).update("ASGI apps serve requests directly and have no runs.")
        elif event.data_table.id == "runs-table" and row_id in self._run_rows:
            self.run_worker(self._load_run_detail(row_id), name="run-detail", group="tui", exclusive=True)
        elif event.data_table.id == "workspace-profiles-table":
            self._select_workspace_profile(row_id)

    def _project_name(self, item: dict[str, Any]) -> str:
        if self.workspace_overview is None:
            return str(item.get("project_id", "-"))
        project_id = str(item.get("project_id", ""))
        return self.workspace_overview.project_names.get(project_id, project_id or "-")

    def _summary_text(self) -> str:
        if self.workspace_overview is None:
            return "No Rebase data loaded."
        selected = self.selected_project.get("name", "-") if self.selected_project is not None else "none"
        total_functions = sum(summary.function_count for summary in self.workspace_overview.project_summaries)
        total_workflows = sum(summary.workflow_count for summary in self.workspace_overview.project_summaries)
        api_url = str(getattr(self.data.client, "api_url", "-"))
        return (
            f"Profile: {self.profile_name} | API: {api_url} | Projects: {len(self.workspace_overview.projects)} | "
            f"Functions: {total_functions} | Workflows: {total_workflows} | Selected project: {selected} | "
            f"Latest runs per target: {self.limit}"
        )

    def _workspace_label(self) -> str:
        return self._profile_workspace_label(self.profile_data, fallback=self.profile_name)

    @staticmethod
    def _profile_workspace_label(profile_data: dict[str, Any], *, fallback: str) -> str:
        workspace_name = profile_data.get("workspace_name")
        if isinstance(workspace_name, str) and workspace_name:
            return workspace_name
        workspace_id = profile_data.get("workspace_id")
        if isinstance(workspace_id, str) and workspace_id:
            return workspace_id
        return fallback

    @staticmethod
    def _profile_workspace_id(profile_data: dict[str, Any]) -> str:
        workspace_id = profile_data.get("workspace_id")
        return workspace_id if isinstance(workspace_id, str) and workspace_id else "-"

    def _update_workspace_title(self) -> None:
        self.title = f"Rebase TUI - Workspace: {self._workspace_label()}"

    def _set_summary(self, message: str) -> None:
        self.query_one("#summary", Static).update(message)

    def _set_error(self, error: Exception) -> None:
        self._show_project_view()
        self._set_summary(f"Error: {error}")
        self.query_one("#project-detail", Static).update("The Rebase API request failed. Press r to retry.")


def run_tui(*, project: str | None = None, limit: int = 25, client: Client | None = None) -> None:
    RebaseTuiApp(client=client, project=project, limit=limit).run()
