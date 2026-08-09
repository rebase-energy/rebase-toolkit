from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, available_timezones

from rich.json import JSON
from rich.rule import Rule
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult, RenderResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.coordinate import Coordinate
from textual.geometry import Offset
from textual.message import Message
from textual.screen import ModalScreen
from textual.selection import SELECT_ALL, Selection
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    OptionList,
    Static,
    TabbedContent,
    TabPane,
)

# Recomposing the header means naming its pieces, and restyling the toast means naming
# it at all — Textual exports neither. `test_tui_header_parts_still_exist` and
# `test_tui_notifications_wear_the_app_s_colours_and_hug_their_text` fail loudly if
# either moves.
from textual.widgets._header import HeaderClock, HeaderIcon, HeaderTitle

from rebase.brand import (
    BRAND_AMBER,
    BRAND_BRIGHT_GREEN,
    BRAND_CORAL_RED,
    BRAND_MAIN_GREEN,
    BRAND_MEDIUM_GRAY,
    BRAND_SLATE_BLUE,
)
from rebase.client import Client, RebaseWorkflowError
from rebase.config import (
    add_search_path,
    editor_settings,
    list_profiles,
    load_profile,
    local_workspace_id,
    search_paths,
    selected_profile_name,
    set_default_profile,
    workspace_key,
)
from rebase.editor import NO_EDITOR_HINT, build_argv, resolve_editor, run_foreground, spawn_detached
from rebase.locate import (
    ProjectDeclaration,
    describe_failure,
    find_project_declarations,
    git_toplevel,
    is_risky_root,
    project_folder,
)

TargetType = Literal["function", "workflow"]
ViewName = Literal["workspace", "project", "workspace-switcher"]
DeletableKind = Literal["project", "function", "workflow"]

#: Tables whose rows `d` can delete, and the kind of object each row is.
DELETABLE_TABLES: dict[str, DeletableKind] = {
    "projects-table": "project",
    "functions-table": "function",
    "workflows-table": "workflow",
}
#: What a multi-item delete asks the user to type. Compared case-insensitively.
CONFIRM_WORD = "delete"
MARK_STYLE = f"bold {BRAND_AMBER}"
#: Concurrent requests used to collect the workspace overview's per-project function counts.
OVERVIEW_FANOUT_WORKERS = 16
#: Concurrent requests used to collect a project's per-workflow step graphs.
STEP_GRAPH_FANOUT_WORKERS = 8
#: The run, its events, its steps and its tasks: four independent reads behind one
#: keypress, so they go together rather than one after another.
RUN_DETAIL_FANOUT_WORKERS = 4
#: What each level of the project view adds, outermost first. Level 0 is the target
#: table alone; selecting a target reveals level 1, selecting a run reveals level 2.
REVEAL_LEVELS: tuple[tuple[str, ...], ...] = (
    ("#runs-table",),
    ("#timeline-table",),
)
#: Header text per table, added when the rows are and never before. See `_setup_tables`.
TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "projects-table": ("Project", "Functions", "Workflows", "Cron jobs", "Endpoints"),
    "workspace-profiles-table": ("Active", "Profile", "Workspace", "Workspace ID", "API URL"),
    "functions-table": ("Name", "Workflow", "Step", "Run type", "State", "Endpoint", "Version", "Updated"),
    "workflows-table": ("Name", "Run type", "State", "Endpoint", "Schedule", "Next run", "Version", "Updated"),
    "runs-table": ("Run", "Status", "Trigger", "Created", "Started", "Finished", "Duration"),
    "timeline-table": ("Time", "Stage", "Status", "Message"),
}
#: The tab each target table belongs to, in the order `left`/`right` cycle them.
TARGET_TABS: tuple[tuple[str, str], ...] = (
    ("workflows-tab", "#workflows-table"),
    ("functions-tab", "#functions-table"),
)
#: The project view's stacked boxes, top to bottom. One per reveal level. Each box below
#: the first resizes the one above it by its own column header — see `DragHeaderTable`.
BOX_SELECTORS: tuple[str, ...] = ("#target-tabs", "#runs-table", "#timeline-table")
#: The rows a box keeps whatever you do to it: its column header. For the tabbed box that
#: is two rows, not one — the chip strip above the table costs a row of its own.
MIN_TABLE_HEIGHT = 1
MIN_TARGET_BOX_HEIGHT = 2
#: How many rows `+`/`-` move a box.
BOX_STEP = 2
#: Log lines fetched per run. The API answers an empty list — not an error — somewhere
#: above 200, so this is a ceiling to respect rather than one to raise on a hunch.
RUN_LOG_LIMIT = 200
#: Fields too long to belong in the details drawer, and what to say instead.
ELIDED_DETAIL_KEYS = ("source_code",)


@dataclass(frozen=True)
class ProjectSummary:
    project: dict[str, Any]
    function_count: int
    workflow_count: int
    endpoint_count: int = 0
    cron_count: int = 0


@dataclass(frozen=True)
class OverviewCounts:
    functions: dict[str, int]
    workflows: dict[str, int]
    endpoints: dict[str, int]
    crons: dict[str, int]


@dataclass(frozen=True)
class WorkspaceOverviewData:
    projects: list[dict[str, Any]]
    project_summaries: list[ProjectSummary]
    project_names: dict[str, str]


@dataclass(frozen=True)
class DetailField:
    """One labelled line at the top of the details drawer."""

    label: str
    value: str
    style: str = ""


@dataclass(frozen=True)
class WorkflowStep:
    """One node of a workflow's step graph, named by the function it runs.

    A step *is* a function — the deploy registers it as one, so it shows up in the
    project's function list with nothing to say which workflow calls it. This is that
    missing link, read back off the workflow version that was compiled from it.
    """

    workflow_id: str
    workflow_name: str
    node_key: str
    name: str
    function_id: str
    upstream: tuple[str, ...]
    #: Position in the workflow's node list, which is the order the graph was traced in.
    order: int


@dataclass(frozen=True)
class ProjectTargetsData:
    project: dict[str, Any]
    functions: list[dict[str, Any]]
    workflows: list[dict[str, Any]]
    endpoints: list[dict[str, Any]]
    #: Every step of every workflow in the project, workflow by workflow.
    steps: tuple[WorkflowStep, ...] = ()

    def steps_by_function(self) -> dict[str, list[WorkflowStep]]:
        grouped: dict[str, list[WorkflowStep]] = {}
        for step in self.steps:
            grouped.setdefault(step.function_id, []).append(step)
        return grouped


@dataclass(frozen=True)
class RunDetailData:
    run: dict[str, Any]
    events: list[dict[str, Any]]
    steps: list[dict[str, Any]]
    #: The fan-out inside those steps, one row per unit of work.
    tasks: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class TimelineRow:
    """One line of a run's timeline: a lifecycle event, a step, or a log line."""

    at: datetime | None
    stage: str
    status: str
    message: str
    kind: Literal["event", "step", "task", "log"]


def _optional_list(load: Callable[[], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    try:
        return load()
    except RebaseWorkflowError:
        return []


class RebaseTuiData:
    def __init__(self, client: Client | None = None, *, project: str | None = None, limit: int = 100) -> None:
        self.client = client or Client()
        self.project = project
        self.limit = limit

    def load_workspace_overview(self) -> WorkspaceOverviewData:
        projects = self.client.list_projects()
        if self.project is not None and not any(project.get("name") == self.project for project in projects):
            raise RebaseWorkflowError(f"project not found: {self.project}")

        counts = self._overview_counts(projects)
        return WorkspaceOverviewData(
            projects=projects,
            project_summaries=[
                ProjectSummary(
                    project=project,
                    function_count=counts.functions.get(str(project["id"]), 0),
                    workflow_count=counts.workflows.get(str(project["id"]), 0),
                    endpoint_count=counts.endpoints.get(str(project["id"]), 0),
                    cron_count=counts.crons.get(str(project["id"]), 0),
                )
                for project in projects
            ],
            project_names={str(project.get("id", "")): str(project.get("name", "-")) for project in projects},
        )

    def _overview_counts(self, projects: list[dict[str, Any]]) -> OverviewCounts:
        """Every count the project table shows, gathered concurrently.

        Workflows and endpoints each come from one workspace-wide call, because those
        objects carry their `project_id`. Functions have no such route and stay one
        request per project. All of it shares a single pool, so the overview costs
        roughly one round trip's wait instead of one per project.
        """
        project_ids = [str(project["id"]) for project in projects]
        with ThreadPoolExecutor(max_workers=min(OVERVIEW_FANOUT_WORKERS, len(project_ids) + 2)) as executor:
            workflows = executor.submit(self._workflow_and_cron_counts)
            # Endpoints are supplementary here, as they are in load_project_targets: an API
            # without the route should cost the column, not the whole overview.
            endpoints = executor.submit(self._counts_by_project, lambda: _optional_list(self.client.list_endpoints))
            functions = list(
                executor.map(lambda project_id: len(self.client.list_functions(project_id=project_id)), project_ids)
            )
        workflow_counts, cron_counts = workflows.result()
        return OverviewCounts(
            functions=dict(zip(project_ids, functions, strict=True)),
            workflows=workflow_counts,
            endpoints=endpoints.result(),
            crons=cron_counts,
        )

    def _workflow_and_cron_counts(self) -> tuple[dict[str, int], dict[str, int]]:
        """Workflows per project, and how many of them are on a live cron.

        Both come out of the one workspace-wide call, so the cron column costs no
        request of its own. A workflow counts as a cron job when the API gives it a
        `next_run_at`: that is the platform's own verdict, computed per read, and it
        already accounts for a missing or paused schedule, a disabled workflow or
        version, and an unusable cron expression. Re-deriving those rules here would
        only give them somewhere to drift apart.
        """
        workflows = self.client.list_workflows()
        return (
            Counter(str(item["project_id"]) for item in workflows if item.get("project_id")),
            Counter(
                str(item["project_id"]) for item in workflows if item.get("project_id") and item.get("next_run_at")
            ),
        )

    @staticmethod
    def _counts_by_project(load: Callable[[], list[dict[str, Any]]]) -> dict[str, int]:
        return Counter(str(item["project_id"]) for item in load() if item.get("project_id"))

    def load_project_targets(self, project: dict[str, Any]) -> ProjectTargetsData:
        project_id = str(project["id"])
        workflows = self.client.list_workflows(project_id=project_id)
        return ProjectTargetsData(
            project=project,
            functions=self.client.list_functions(project_id=project_id),
            workflows=workflows,
            # Supplementary data: never let it take down the function/workflow view.
            endpoints=_optional_list(lambda: self.client.list_project_endpoints(project_id)),
            steps=self.load_workflow_steps(workflows),
        )

    def load_workflow_steps(self, workflows: list[dict[str, Any]]) -> tuple[WorkflowStep, ...]:
        """The step graph of every workflow in the project, read concurrently.

        The graph lives on the workflow *version*, not on the workflow, so this costs a
        request per workflow — hence the pool. Like the endpoint list it is supplementary:
        an old API, a workflow with no current version, or a single failed read costs the
        Workflow column for that workflow and nothing more.
        """
        versioned = [
            (str(workflow["id"]), str(workflow.get("name", "-")), str(workflow["current_version_id"]))
            for workflow in workflows
            if workflow.get("id") is not None and workflow.get("current_version_id")
        ]
        if not versioned:
            return ()

        def load(entry: tuple[str, str, str]) -> tuple[WorkflowStep, ...]:
            workflow_id, workflow_name, version_id = entry
            try:
                version = self.client.get_workflow_version(workflow_id, version_id)
            except Exception:
                return ()
            return workflow_steps(workflow_id, workflow_name, version.get("step_graph"))

        with ThreadPoolExecutor(max_workers=min(STEP_GRAPH_FANOUT_WORKERS, len(versioned))) as executor:
            return tuple(step for steps in executor.map(load, versioned) for step in steps)

    def load_target_runs(self, target_type: TargetType, target_id: str) -> list[dict[str, Any]]:
        if target_type == "function":
            return self.client.list_runs(function_id=target_id, target_type="function", limit=self.limit)
        return self.client.list_runs(workflow_id=target_id, target_type="workflow", limit=self.limit)

    def load_run_detail(self, run_id: str, *, target_type: str | None = None) -> RunDetailData:
        """Everything behind one run, fetched in one round trip's worth of waiting.

        These were four sequential calls — the run, its events, its steps, its tasks —
        and none of them needs another's answer, so pressing enter on a run cost the sum
        of all four: about 1.3 seconds against the deployed API. Issued together it costs
        the slowest one.

        `target_type` saves asking what the run is before knowing which reads apply; the
        caller has it on the row it just selected. Without it both are issued anyway and
        the answers dropped, which is cheaper than a round trip spent finding out.
        """
        with ThreadPoolExecutor(max_workers=RUN_DETAIL_FANOUT_WORKERS) as executor:
            run = executor.submit(self.client.get_run, run_id)
            events = executor.submit(self.client.list_run_events, run_id)
            # Supplementary, like the endpoint list: an API without the route costs the
            # rows and not the run view.
            wanted = target_type in (None, "workflow")
            steps = executor.submit(_optional_list, lambda: self.client.list_run_steps(run_id)) if wanted else None
            tasks = executor.submit(_optional_list, lambda: self.client.list_run_tasks(run_id)) if wanted else None
            resolved = run.result()
            is_workflow = resolved.get("target_type") == "workflow"
            return RunDetailData(
                run=resolved,
                events=events.result(),
                steps=steps.result() if steps is not None and is_workflow else [],
                tasks=tasks.result() if tasks is not None and is_workflow else [],
            )

    def load_run_logs(self, run_id: str) -> list[dict[str, Any]]:
        entries = self.client.get_run_logs(run_id, limit=RUN_LOG_LIMIT).get("entries")
        return entries if isinstance(entries, list) else []


def workflow_steps(workflow_id: str, workflow_name: str, step_graph: Any) -> tuple[WorkflowStep, ...]:
    """Read a compiled step graph into steps, skipping nodes with no function behind them.

    A workflow whose body does the work itself has no graph at all — `step_graph` is
    null — and that is the common case, not a fault.
    """
    if not isinstance(step_graph, dict):
        return ()
    nodes = step_graph.get("nodes")
    if not isinstance(nodes, list):
        return ()
    steps = []
    for order, node in enumerate(nodes):
        if not isinstance(node, dict) or not node.get("function_id"):
            continue
        upstream = node.get("upstream_node_keys")
        steps.append(
            WorkflowStep(
                workflow_id=workflow_id,
                workflow_name=workflow_name,
                node_key=str(node.get("node_key") or node.get("name") or "-"),
                name=str(node.get("name") or node.get("node_key") or "-"),
                function_id=str(node["function_id"]),
                upstream=tuple(str(key) for key in upstream) if isinstance(upstream, list) else (),
                order=order,
            )
        )
    return tuple(steps)


def build_timeline(
    events: Sequence[dict[str, Any]],
    steps: Sequence[dict[str, Any]],
    logs: Sequence[dict[str, Any]] | None,
    tasks: Sequence[dict[str, Any]] = (),
) -> list[TimelineRow]:
    """Everything that happened during a run, in the order it happened.

    Events, steps, tasks and log lines are four separate routes. Tasks say which step
    fanned them out, so they could be nested properly — but a log entry carries a
    timestamp, a severity and a message and nothing else, so the grouping here is
    chronological rather than declared: a row sits under the last step or stage that
    began before it, which is what "belongs to" means when the producer never said.
    Sorting them together is the whole mechanism; the indent is what makes it read as
    grouping.
    """
    rows = [
        TimelineRow(
            at=_parse_timestamp(event.get("created_at")),
            stage=str(event.get("stage", "-")),
            status=str(event.get("status", "-")),
            message=format_json_summary(event.get("message"), max_length=200),
            kind="event",
        )
        for event in events
    ]
    for step in steps:
        detail = format_json_summary(step.get("error"), max_length=160)
        if detail == "-":
            attempt = step.get("attempt")
            finished = step.get("finished_at")
            detail = " · ".join(
                part
                for part in (
                    f"attempt {attempt}" if attempt not in {None, ""} else "",
                    f"finished {format_timestamp(finished)}" if finished else "",
                )
                if part
            )
        rows.append(
            TimelineRow(
                at=_parse_timestamp(step.get("started_at")),
                stage=str(step.get("name") or step.get("node_key") or "-"),
                status=str(step.get("status", "-")),
                message=detail or "-",
                kind="step",
            )
        )
    for task in tasks:
        detail = format_json_summary(task.get("error"), max_length=160)
        if detail == "-":
            detail = format_json_summary(task.get("result"), max_length=160)
        rows.append(
            TimelineRow(
                # A queued task has not started, and sorting it by `created_at` keeps it
                # with its batch instead of at the top of the run.
                at=_parse_timestamp(task.get("started_at") or task.get("created_at")),
                stage=f"task {task.get('item_index', '-')}",
                status=str(task.get("status", "-")),
                message=f"{format_json_summary(task.get('parameters'), max_length=90)} -> {detail}",
                kind="task",
            )
        )
    for entry in logs or []:
        rows.append(
            TimelineRow(
                at=_parse_timestamp(entry.get("timestamp")),
                stage="",
                status=str(entry.get("severity") or "-"),
                message=str(entry.get("message") or ""),
                kind="log",
            )
        )
    # A row with no usable timestamp sorts to the top rather than being dropped: it is
    # still something that happened, and hiding it would be worse than misplacing it.
    epoch = datetime.min.replace(tzinfo=UTC)
    return sorted(rows, key=lambda row: (row.at or epoch, row.kind == "log"))


def format_step_workflows(steps: Sequence[WorkflowStep]) -> str:
    """The workflow a function is a step of, and a count when it is a step of several."""
    if not steps:
        return "-"
    names = list(dict.fromkeys(step.workflow_name for step in steps))
    return names[0] if len(names) == 1 else f"{names[0]} (+{len(names) - 1})"


def format_step_keys(steps: Sequence[WorkflowStep]) -> str:
    """The node keys a function is called under, which differ from its name on reuse."""
    if not steps:
        return "-"
    return ", ".join(dict.fromkeys(step.node_key for step in steps))


def format_duration(started: Any, finished: Any) -> str:
    """How long a run took, from the two timestamps the API reports."""
    start, end = _parse_timestamp(started), _parse_timestamp(finished)
    if start is None or end is None:
        return "-"
    seconds = (end - start).total_seconds()
    if seconds < 0:
        return "-"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, seconds = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{seconds:02d}s"


def _parse_timestamp(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def compact_id(value: Any, *, length: int = 8) -> str:
    if value is None:
        return "-"
    text = str(value)
    if len(text) <= length + 4:
        return text
    return f"{text[:length]}..."


def format_timestamp(value: Any, tz: tzinfo | None = None) -> str:
    """Render an API timestamp, converted to *tz* when it carries an offset to convert from."""
    if value in {None, ""}:
        return "-"
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value)
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw
    if tz is not None and parsed.tzinfo is not None:
        parsed = parsed.astimezone(tz)
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


def detail_payload(record: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """A record as the drawer should show it: whole, minus the fields nothing can read.

    A workflow's `source_code` is the entire deployed function body. Printing it into a
    JSON drawer buries every other field under it, and `o` already opens the real file.
    """
    payload = {}
    for key, value in record.items():
        if key in ELIDED_DETAIL_KEYS and isinstance(value, str) and value:
            payload[key] = f"<{len(value)} characters — press o to open the source>"
        else:
            payload[key] = value
    payload.update({key: value for key, value in extra.items() if value not in (None, [], {})})
    return payload


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


class RebaseClock(HeaderClock):
    """The header clock, named with the zone it is showing and clickable to change it.

    Textual's clock renders bare local time, which says nothing about which zone the
    rest of the screen is in — the times in the tables come from the API in UTC. This
    one names its zone and hands the choice to the user.
    """

    DEFAULT_CSS = """
    RebaseClock {
        width: auto;
        padding: 0;
        content-align: right middle;
    }
    """

    def render(self) -> RenderResult:
        now = datetime.now(getattr(self.app, "display_tzinfo", None))
        return Text(f"{now:%H:%M:%S} {now.tzname() or ''}".rstrip())

    async def on_click(self, event: events.Click) -> None:
        event.stop()
        await self.run_action("app.choose_timezone")


class RebaseHeader(Header):
    def compose(self) -> ComposeResult:
        yield HeaderIcon().data_bind(Header.icon)
        yield HeaderTitle()
        yield RebaseClock()

    def _on_click(self, event: events.Click) -> None:
        return None


class DragHeaderTable(DataTable):
    """A table whose column header doubles as the splitter for the box above it.

    Textual has no splitter widget, so the usual answer — the one the hillclimb TUI uses —
    is a one-row handle between the panes. A row per boundary is a row the tables do not
    get, and this header is already sitting on the boundary, pinned there while the rows
    scroll underneath. So it is the handle: grab it and the box above follows the pointer.
    Header clicks otherwise only post `HeaderSelected`, which nothing here listens for.
    """

    def __init__(self, *, resizes: str, id: str) -> None:
        #: The box this header resizes — the one directly above it.
        super().__init__(id=id)
        self.resizes = resizes

    def _on_header(self, event: events.MouseEvent) -> bool:
        return self.show_header and (event.style.meta.get("row") == -1 or event.y == 0)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if not self._on_header(event):
            return
        self.app.begin_box_drag(self.resizes, self._screen_y(event))  # type: ignore[attr-defined]
        self.capture_mouse()
        event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self.app.mouse_captured is self:
            self.app.drag_box_to(self._screen_y(event))  # type: ignore[attr-defined]
            event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self.app.mouse_captured is self:
            self.app.end_box_drag()  # type: ignore[attr-defined]
            self.release_mouse()
            event.stop()

    def _screen_y(self, event: events.MouseEvent) -> int:
        # screen_y is what survives the widget moving under the pointer mid-drag.
        return int(getattr(event, "screen_y", self.region.y + event.y))


class SelectableDataTable(DataTable):
    """A DataTable whose rows can be *marked* in bulk, on top of the single-row cursor.

    Textual's DataTable has a cursor but no notion of a selection, so the marks live
    here: `shift+up` / `shift+down` grow an inclusive range anchored where the shifted
    run started, any unshifted cursor move drops the range again, and `escape` clears
    it outright. Marked rows are restyled in place rather than tracked invisibly, and
    the cursor's foreground is demoted to `renderable` priority so the mark still
    shows on the one row the cursor is sitting on.
    """

    BINDINGS = [
        Binding("shift+up", "extend_mark(-1)", "Mark up", show=False),
        Binding("shift+down", "extend_mark(1)", "Mark down", show=False),
        Binding("escape", "clear_marks", "Clear marks", show=False),
        # The keys that step between the Workflows and Functions chips. Bound here rather
        # than on the app because an app-level binding would have to be `priority` to beat
        # DataTable's own inert cursor_left/cursor_right — and a priority binding on an
        # arrow key takes it away from every Input in every dialog too. They no-op
        # anywhere but the project view.
        Binding("left", "app.switch_target_tab(-1)", "Previous target", show=False),
        Binding("right", "app.switch_target_tab(1)", "Next target", show=False),
    ]

    class MarksChanged(Message):
        """Posted whenever the set of marked rows changes."""

        def __init__(self, table: SelectableDataTable, marked: list[str]) -> None:
            super().__init__()
            self.table = table
            self.marked = marked

        @property
        def control(self) -> SelectableDataTable:
            return self.table

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("cursor_foreground_priority", "renderable")
        super().__init__(*args, **kwargs)
        self._marked: set[str] = set()
        self._anchor: int | None = None
        self._extending = False
        # Cells are restyled in place, so the unmarked renderables have to be kept.
        self._unmarked_cells: dict[str, dict[Any, Any]] = {}

    @property
    def marked_keys(self) -> list[str]:
        """Row keys of the marked rows, in table order."""
        return [
            str(row.key.value)
            for row in self.ordered_rows
            if row.key.value is not None and row.key.value in self._marked
        ]

    @property
    def cursor_key(self) -> str | None:
        """Row key under the cursor, or None on an empty table."""
        if not 0 <= self.cursor_row < len(self.ordered_rows):
            return None
        value = self.ordered_rows[self.cursor_row].key.value
        return None if value is None else str(value)

    def clear(self, columns: bool = False) -> SelectableDataTable:
        self._reset_marks()
        return super().clear(columns)

    def watch_cursor_coordinate(self, old_coordinate: Coordinate, new_coordinate: Coordinate) -> None:
        super().watch_cursor_coordinate(old_coordinate, new_coordinate)
        # Moving off a range without shift held is how you abandon it.
        if getattr(self, "_extending", True) or old_coordinate.row == new_coordinate.row:
            return
        if getattr(self, "_marked", None):
            self.action_clear_marks()

    def action_extend_mark(self, delta: int) -> None:
        if not self.ordered_rows:
            return
        if self._anchor is None:
            self._anchor = self.cursor_row
        target = max(0, min(len(self.ordered_rows) - 1, self.cursor_row + delta))
        self._extending = True
        try:
            self.move_cursor(row=target)
        finally:
            self._extending = False
        anchor = self._anchor
        rows = self.ordered_rows[min(anchor, target) : max(anchor, target) + 1]
        self._apply_marks({str(row.key.value) for row in rows if row.key.value is not None})

    def action_clear_marks(self) -> None:
        self._anchor = None
        self._apply_marks(set())

    def _apply_marks(self, marked: set[str]) -> None:
        if marked == self._marked:
            return
        for row_key in self._marked - marked:
            self._restyle_row(row_key, marked=False)
        for row_key in marked - self._marked:
            self._restyle_row(row_key, marked=True)
        self._marked = marked
        self.post_message(self.MarksChanged(self, self.marked_keys))

    def _restyle_row(self, row_key: str, *, marked: bool) -> None:
        if marked:
            unmarked: dict[Any, Any] = {}
            for column_key in list(self.columns):
                value = self.get_cell(row_key, column_key)
                unmarked[column_key] = value
                self.update_cell(row_key, column_key, self._mark_text(value))
            self._unmarked_cells[row_key] = unmarked
            return
        for column_key, value in self._unmarked_cells.pop(row_key, {}).items():
            self.update_cell(row_key, column_key, value)

    @staticmethod
    def _mark_text(value: Any) -> Text:
        text = value.copy() if isinstance(value, Text) else Text(str(value))
        # Appended last, so it wins over whatever styling the cell already carried.
        text.stylize(MARK_STYLE)
        return text

    def _reset_marks(self) -> None:
        had_marks = bool(self._marked)
        self._marked = set()
        self._anchor = None
        self._unmarked_cells = {}
        if had_marks:
            self.post_message(self.MarksChanged(self, []))


class DeleteConfirmScreen(ModalScreen[bool]):
    """Type-to-confirm gate in front of every delete.

    Deletes here are forced and irreversible, so a keypress is not enough: one item
    asks for its own name back, and a batch asks for the literal word `delete` —
    the only phrase that can be typed once for rows with different names.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    CSS = f"""
    DeleteConfirmScreen {{
        align: center middle;
        background: #101412 70%;
    }}

    #delete-dialog {{
        width: 78;
        height: auto;
        padding: 1 2;
        background: #101412;
        border: solid {BRAND_CORAL_RED};
    }}

    #delete-title {{
        color: {BRAND_CORAL_RED};
        text-style: bold;
        padding-bottom: 1;
    }}

    #delete-names {{
        color: {BRAND_AMBER};
        padding: 1 0;
    }}

    #delete-hint {{
        color: {BRAND_MEDIUM_GRAY};
        padding-top: 1;
    }}

    #delete-input {{
        background: #101412;
        border: solid {BRAND_MEDIUM_GRAY};
    }}
    """
    #: How many names the dialog spells out before it starts counting.
    NAME_PREVIEW = 8

    def __init__(self, *, kind: DeletableKind, names: list[str]) -> None:
        super().__init__()
        self.kind = kind
        self.names = names

    @property
    def required_phrase(self) -> str:
        return self.names[0] if len(self.names) == 1 else CONFIRM_WORD

    def matches(self, typed: str) -> bool:
        value = typed.strip()
        if len(self.names) == 1:
            return value == self.names[0]
        return value.casefold() == CONFIRM_WORD

    def compose(self) -> ComposeResult:
        plural = self.kind if len(self.names) == 1 else f"{self.kind}s"
        with Vertical(id="delete-dialog"):
            yield Static(f"Delete {len(self.names)} {plural}?", id="delete-title")
            yield Static(
                "This also deletes their contents and run history — endpoints, runs, and (for a "
                "project) every function and workflow inside it. It cannot be undone.",
                id="delete-body",
            )
            yield Static(self._names_preview(), id="delete-names")
            yield Input(placeholder=f"type {self.required_phrase}", id="delete-input")
            yield Static(self._hint_text(), id="delete-hint")

    def on_mount(self) -> None:
        self.query_one("#delete-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self.matches(event.value):
            self.dismiss(True)
            return
        event.input.value = ""
        self.query_one("#delete-hint", Static).update(
            Text(f"That did not match. Type {self.required_phrase} exactly, or press escape.", style=BRAND_CORAL_RED)
        )

    def action_cancel(self) -> None:
        self.dismiss(False)

    def _names_preview(self) -> str:
        shown = self.names[: self.NAME_PREVIEW]
        lines = [f"  {name}" for name in shown]
        remaining = len(self.names) - len(shown)
        if remaining > 0:
            lines.append(f"  ... and {remaining} more")
        return "\n".join(lines)

    def _hint_text(self) -> str:
        if len(self.names) == 1:
            return f"Type the {self.kind} name to confirm, then enter. Escape cancels."
        return f'Type "{CONFIRM_WORD}" to confirm, then enter. Escape cancels.'


class OpenSourceChoiceScreen(ModalScreen[ProjectDeclaration | None]):
    """Asks which file to open when several declare the same project.

    Two repositories can each declare a project of the same name, and the search has
    no way to rank them, so the choice belongs to the user. It is deliberately not
    remembered: a stored answer would go stale exactly the way the deploy-time path
    this feature replaced does.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    CSS = f"""
    OpenSourceChoiceScreen {{
        align: center middle;
        background: #101412 70%;
    }}

    #open-source-dialog {{
        width: 88;
        height: auto;
        padding: 1 2;
        background: #101412;
        border: solid {BRAND_BRIGHT_GREEN};
    }}

    #open-source-title {{
        color: {BRAND_BRIGHT_GREEN};
        text-style: bold;
        margin-bottom: 1;
    }}

    #open-source-options {{
        height: auto;
        max-height: 12;
        background: #101412;
        border: none;
    }}

    #open-source-hint {{
        color: {BRAND_MEDIUM_GRAY};
        margin-top: 1;
    }}
    """

    def __init__(self, *, project_name: str, matches: Sequence[ProjectDeclaration], roots: Sequence[Path]) -> None:
        super().__init__()
        self.project_name = project_name
        self.matches = list(matches)
        self.roots = list(roots)

    def compose(self) -> ComposeResult:
        with Vertical(id="open-source-dialog"):
            yield Static(
                f'{len(self.matches)} files declare project "{self.project_name}"',
                id="open-source-title",
            )
            yield OptionList(*[self._label(match) for match in self.matches], id="open-source-options")
            yield Static("Enter opens the highlighted file. Escape cancels.", id="open-source-hint")

    def on_mount(self) -> None:
        self.query_one("#open-source-options", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.matches[event.option_index])

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _label(self, match: ProjectDeclaration) -> str:
        for root in self.roots:
            with suppress(ValueError):
                return f"{match.path.relative_to(root)}:{match.line}"
        return f"{match.path}:{match.line}"


class DetailDrawer(ModalScreen[None]):
    """The selected row's own record, as JSON, in a panel over the right of the screen.

    This is where the project view's two static detail panels went. They sat permanently
    across the middle of the screen restating the table row above them, and the one thing
    they had that the table did not — a run's parameters — was cut off at one line. A
    drawer costs no rows until it is asked for, and can be as tall as it needs to be.
    """

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("p", "close", "Close"),
        Binding("q", "close", "Close"),
    ]
    CSS = f"""
    DetailDrawer {{
        align: right top;
        background: #101412 40%;
    }}

    #detail-drawer {{
        width: 62%;
        height: 100%;
        padding: 1 2;
        background: #101412;
        border-left: solid {BRAND_BRIGHT_GREEN};
    }}

    /* Rules the eye stops at. The input a run was given and the output it produced are
       two different things, and one JSON blob made them look like one. Rich draws these
       to the drawer's width, so they stay lines rather than a guess at a line. */
    .detail-rule {{
        color: {BRAND_MEDIUM_GRAY};
        margin: 1 0;
    }}

    #detail-body {{
        height: 1fr;
        background: #101412;
        scrollbar-size-vertical: 1;
        scrollbar-color: {BRAND_BRIGHT_GREEN};
    }}

    #detail-hint {{
        color: {BRAND_MEDIUM_GRAY};
        margin-top: 1;
    }}
    """

    def __init__(self, *, fields: Sequence[DetailField], sections: Sequence[Any]) -> None:
        super().__init__()
        #: Labelled lines at the top: what this is, and its state.
        self.fields = list(fields)
        #: One JSON body per section, separated by a rule. The section says what it is
        #: through its own top-level key -- `parameters`, `result` -- rather than through
        #: a caption above it, because that key is genuinely part of the document.
        self.sections = list(sections)

    @property
    def drawer_title(self) -> str:
        return self.fields[0].value if self.fields else ""

    @property
    def payload(self) -> Any:
        """The first section's body, which is the whole record for a single-section drawer."""
        return self.sections[0] if self.sections else None

    def compose(self) -> ComposeResult:
        # Labels padded to a common width so the values line up under each other.
        width = max((len(field.label) for field in self.fields), default=0)
        with Vertical(id="detail-drawer"):
            for field in self.fields:
                yield Static(
                    Text.assemble(
                        (f"{field.label + ':':<{width + 1}} ", f"bold {BRAND_BRIGHT_GREEN}"),
                        (field.value, field.style),
                    )
                )
            yield Static(Rule(style=BRAND_MEDIUM_GRAY), classes="detail-rule")
            with VerticalScroll(id="detail-body"):
                for index, payload in enumerate(self.sections):
                    if index:
                        yield Static(Rule(style=BRAND_MEDIUM_GRAY), classes="detail-rule")
                    yield Static(JSON(json.dumps(payload, indent=2, sort_keys=True, default=str)))
            yield Static("Arrow keys scroll. p or escape closes.", id="detail-hint")

    def on_mount(self) -> None:
        self.query_one("#detail-body", VerticalScroll).focus()

    def action_close(self) -> None:
        self.dismiss(None)


class TimezoneChoiceScreen(ModalScreen[str | None]):
    """Pick the zone the TUI shows times in.

    The zone list is every name `zoneinfo` knows, which is far too many to scroll, so
    the filter box takes the focus and the arrow keys drive the list from inside it.
    """

    SYSTEM = "System default"
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("down", "highlight(1)", "Next", show=False),
        Binding("up", "highlight(-1)", "Previous", show=False),
    ]
    CSS = f"""
    TimezoneChoiceScreen {{
        align: center middle;
        background: #101412 70%;
    }}

    #timezone-dialog {{
        width: 62;
        height: auto;
        padding: 1 2;
        background: #101412;
        border: solid {BRAND_BRIGHT_GREEN};
    }}

    #timezone-title {{
        color: {BRAND_BRIGHT_GREEN};
        text-style: bold;
        margin-bottom: 1;
    }}

    #timezone-filter {{
        background: #101412;
        border: solid {BRAND_MEDIUM_GRAY};
    }}

    #timezone-options {{
        height: auto;
        max-height: 12;
        background: #101412;
        border: none;
    }}

    #timezone-hint {{
        color: {BRAND_MEDIUM_GRAY};
        margin-top: 1;
    }}
    """

    def __init__(self, *, current: str) -> None:
        super().__init__()
        self.current = current
        # UTC first because the API speaks it; the rest of the database follows.
        self.zones = [self.SYSTEM, "UTC", *sorted(available_timezones() - {"UTC"})]

    def compose(self) -> ComposeResult:
        with Vertical(id="timezone-dialog"):
            yield Static(f"Show times in — currently {self.current}", id="timezone-title")
            yield Input(placeholder="filter, e.g. stockholm", id="timezone-filter")
            yield OptionList(*self.zones, id="timezone-options")
            yield Static("Enter picks the highlighted zone. Escape cancels.", id="timezone-hint")

    def on_mount(self) -> None:
        self.query_one("#timezone-filter", Input).focus()

    def matches(self, query: str) -> list[str]:
        needle = query.strip().casefold().replace(" ", "_")
        if not needle:
            return self.zones
        return [zone for zone in self.zones if needle in zone.casefold()]

    def on_input_changed(self, event: Input.Changed) -> None:
        options = self.query_one("#timezone-options", OptionList)
        options.clear_options()
        options.add_options(self.matches(event.value))
        if options.option_count:
            options.highlighted = 0

    def on_input_submitted(self, event: Input.Submitted) -> None:
        options = self.query_one("#timezone-options", OptionList)
        if options.option_count and options.highlighted is not None:
            self.dismiss(str(options.get_option_at_index(options.highlighted).prompt))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.prompt))

    def action_highlight(self, delta: int) -> None:
        options = self.query_one("#timezone-options", OptionList)
        if not options.option_count:
            return
        current = options.highlighted if options.highlighted is not None else -1
        options.highlighted = max(0, min(options.option_count - 1, current + delta))

    def action_cancel(self) -> None:
        self.dismiss(None)


class RebaseTuiApp(App[None]):
    TITLE = "Rebase TUI"
    SUB_TITLE = ""
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("b", "back", "Back"),
        # Everything below stays out of the footer and lives in the key panel, which
        # lists `show=False` bindings too. Ten hints did not fit the width, so the four
        # you move around with kept their places and the rest went one keystroke away.
        Binding("d", "delete_selection", "Delete", show=False),
        Binding("o", "open_source", "Open source", show=False),
        Binding("s", "toggle_terminal_select", "Select text", show=False),
        Binding("p", "show_details", "Details", show=False),
        Binding("l", "toggle_logs", "Logs", show=False),
        Binding("m", "maximise_box", "Maximise pane", show=False),
        Binding("plus,+,equals_sign,=", "resize_box(1)", "Grow pane", show=False),
        Binding("minus,-,underscore,_", "resize_box(-1)", "Shrink pane", show=False),
        Binding("0", "reset_box_heights", "Reset pane sizes", show=False),
        # priority: the screen's default `tab` -> focus_next otherwise shadows this.
        Binding("tab", "cycle_box", "Next pane", priority=True),
        # Textual's own `ctrl+c,super+c` copies the selection; these adjust it first.
        Binding("shift+right", "adjust_text_selection(1)", "Grow selection", show=False),
        Binding("shift+left", "adjust_text_selection(-1)", "Shrink selection", show=False),
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

    HeaderClockSpace {{
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
    #runs-table,
    #timeline-table {{
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

    #workspace-empty {{
        height: auto;
        padding: 1;
        color: {BRAND_MEDIUM_GRAY};
    }}

    #projects-table > .datatable--odd-row,
    #projects-table > .datatable--even-row {{
        background: #101412;
    }}

    #target-tabs {{
        height: 1fr;
    }}

    /* A pair of chips rather than Textual's underlined labels: the selected one is a
       filled rectangle, which reads as "this is the one" at a glance and, unlike a bar
       drawn under the text, costs no row of its own. */
    #target-tabs Tabs {{
        height: 1;
    }}

    #target-tabs Underline {{
        display: none;
    }}

    #target-tabs Tab {{
        padding: 0 1;
        margin: 0 1 0 0;
        color: {BRAND_MEDIUM_GRAY};
    }}

    #target-tabs Tab:hover {{
        color: {BRAND_BRIGHT_GREEN};
    }}

    /* The `:focus` rule repeats the unfocused one because Textual's own
       `Tabs:focus .-active` would otherwise repaint it in the block-cursor colours. */
    #target-tabs Tab.-active,
    #target-tabs Tabs:focus Tab.-active {{
        background: {BRAND_BRIGHT_GREEN};
        color: #101412;
        text-style: bold;
    }}

    /* These headers are splitters. Lighting up under the pointer is the only affordance
       a terminal can offer for that — there is no cursor to change shape. */
    DragHeaderTable > .datatable--header-hover {{
        color: {BRAND_BRIGHT_GREEN};
        background: #223029;
    }}

    #project-error {{
        color: {BRAND_CORAL_RED};
    }}

    .panel {{
        height: 4;
        padding: 0 1;
        border-bottom: solid {BRAND_MEDIUM_GRAY};
    }}

    #runs-table {{
        height: 8;
    }}

    #timeline-table {{
        height: 1fr;
    }}

    DataTable {{
        background: #101412;
        scrollbar-size-vertical: 1;
    }}

    /* Textual tints the focused table 5% lighter, which turned the pane you were in a
       visibly different shade from the rest of the app — most obvious in the workspace
       view, where one table fills the screen and the whole background changed with it.
       The cursor row already says where the focus is, in colour rather than in wash. */
    DataTable:focus {{
        background-tint: transparent;
    }}

    /* Textual's toast is a grey `$panel` slab 60 cells wide whatever it has to say.
       This one is the app's own: a bordered card in the brand green, sized to its text,
       standing clear of the footer rather than sharing a row with the key hints. The
       border is the whole point — a message has to be unmissable, and a background a
       shade off the app's is not. Stacking and severity are Textual's, re-coloured. */
    ToastRack {{
        margin-bottom: 1;
        margin-right: 2;
    }}

    Toast {{
        width: auto;
        min-width: 32;
        max-width: 60%;
        padding: 0 2;
        margin-top: 1;
        background: #16211d;
        color: #E8F0ED;
        text-style: bold;
        border: round {BRAND_BRIGHT_GREEN};
    }}

    Toast.-information {{
        border: round {BRAND_BRIGHT_GREEN};
    }}

    Toast.-warning {{
        border: round {BRAND_AMBER};
        background: #241f14;
    }}

    Toast.-error {{
        border: round {BRAND_CORAL_RED};
        background: #241618;
    }}

    Toast .toast--title {{
        text-style: bold;
        color: {BRAND_BRIGHT_GREEN};
    }}

    Toast.-warning .toast--title {{
        color: {BRAND_AMBER};
    }}

    Toast.-error .toast--title {{
        color: {BRAND_CORAL_RED};
    }}
    """

    def __init__(
        self,
        *,
        client: Client | None = None,
        data: RebaseTuiData | None = None,
        project: str | None = None,
        limit: int = 100,
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
        self._steps_by_function: dict[str, list[WorkflowStep]] = {}
        self._endpoints_by_target: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._run_rows: dict[str, dict[str, Any]] = {}
        self._run_detail: RunDetailData | None = None
        #: Log entries per run id, kept so toggling `l` back on costs no request.
        self._run_logs: dict[str, list[dict[str, Any]]] = {}
        self._logs_expanded = False
        self._marked_count = 0
        self._terminal_select = False
        self._display_timezone: ZoneInfo | None = None
        self._reveal_level = 0
        #: Box heights the user has set with `+`/`-` or a header drag, overriding the
        #: per-level defaults until `0` clears them.
        self._box_heights: dict[str, int] = {}
        #: The box `m` gave the whole view to, if any.
        self._maximised: str | None = None
        self._drag_box: tuple[str, int, int] | None = None

    def compose(self) -> ComposeResult:
        yield RebaseHeader(show_clock=True, icon="• Commands")
        with Vertical(id="workspace-view"):
            yield SelectableDataTable(id="projects-table")
            yield Static("", id="workspace-empty")
        with Vertical(id="workspace-switcher-view"):
            yield DataTable(id="workspace-profiles-table")
        with Vertical(id="project-view"):
            yield Static("", id="project-error", classes="panel")
            with TabbedContent(initial="workflows-tab", id="target-tabs"):
                # The brackets are part of the label so an unselected chip still reads as
                # something you can press, with no colour to say so. `Text`, not `str`:
                # Textual parses a label as content markup and would read `[ Workflows ]`
                # as a style tag and render nothing at all; `Content` is the unparsed form.
                with TabPane(Content("[ Workflows ]"), id="workflows-tab"):
                    yield SelectableDataTable(id="workflows-table")
                with TabPane(Content("[ Functions ]"), id="functions-tab"):
                    yield SelectableDataTable(id="functions-table")
            yield DragHeaderTable(resizes="#target-tabs", id="runs-table")
            yield DragHeaderTable(resizes="#runs-table", id="timeline-table")
        yield Footer()

    def on_mount(self) -> None:
        self._setup_tables()
        self._reveal(0)
        self._warn_on_local_workspace_mismatch()
        self._update_workspace_title()
        self._show_workspace_view()
        self.action_refresh()
        # Own group: this must never cancel, or be cancelled by, the overview load.
        self.run_worker(self._bootstrap_search_path(), name="bootstrap", group="tui-bootstrap")

    def _workspace_key(self) -> str:
        # A method rather than a cached attribute so switching workspace picks up the
        # new one's search paths without extra bookkeeping.
        return workspace_key(self.profile_data, self.profile_name)

    async def _bootstrap_search_path(self) -> None:
        """Remember the repository the TUI was started in, so `o` works with no setup.

        Runs in a thread because `git rev-parse` can stall on a network filesystem,
        and first paint should not wait for it.
        """

        def bootstrap() -> None:
            root = git_toplevel(Path.cwd())
            # Only a git root, never a bare cwd: that is how a home directory would
            # end up registered and turn every lookup into a full-disk scan.
            if root is None or is_risky_root(root):
                return
            with suppress(OSError):
                add_search_path(self._workspace_key(), root)

        await asyncio.to_thread(bootstrap)

    def _warn_on_local_workspace_mismatch(self) -> None:
        """Say so if the directory's pin is not what the data is coming from.

        The pin normally wins outright, so this is a backstop for the cases where it
        cannot: an unreadable marker, or a session where the workspace was switched by
        hand. Either way the alternative is opening a workspace the user did not ask for
        and leaving them to guess.
        """
        pinned = local_workspace_id()
        effective = getattr(self.data.client, "workspace_id", None)
        if pinned is None or pinned == effective:
            return
        self.notify(
            f"This directory is pinned to workspace {pinned}, but the data is coming from "
            f"{effective or 'the default workspace'}.",
            title="Workspace mismatch",
            severity="warning",
            timeout=30,
        )

    def _reveal(self, level: int) -> None:
        """Show the project view down to *level*, and no further.

        The project view used to paint every box it might ever need and fill them with
        "Select a ..." placeholders. Now it starts as the target table alone and grows a
        box at a time as you drill in.
        """
        opening = level > self._reveal_level
        self._reveal_level = level
        for depth, selectors in enumerate(REVEAL_LEVELS, start=1):
            for selector in selectors:
                self.query_one(selector).styles.display = "block" if level >= depth else "none"
        self._apply_box_heights()
        boxes = self._visible_boxes()
        if not boxes:
            return
        # Opening a box hands it the focus, so the arrow keys drive what you just asked
        # for; closing one hands the focus back up rather than stranding it off screen.
        focused = self.focused
        if opening or (isinstance(focused, DataTable) and not focused.display):
            boxes[-1].focus()

    def _default_box_height(self, selector: str) -> int:
        """What a box is worth before anyone drags or `+`s it.

        Each level costs the ones above it some rows, so the box you just opened is the
        one with room to show something. Expanded logs are the exception that genuinely
        wants the whole screen, so both boxes above shrink to a header and a row or two.
        """
        if self._reveal_level == 2 and self._logs_expanded:
            return {"#target-tabs": 5, "#runs-table": 6}.get(selector, MIN_TABLE_HEIGHT)
        if selector == "#target-tabs":
            return 14 if self._reveal_level == 1 else 9
        return 9

    @staticmethod
    def _min_box_height(selector: str) -> int:
        """The rows a box can never give up: enough to keep its column header on screen.

        Squeezed that far a box is its headings and nothing else, which is the point — it
        says what is still open, and what each column of it means, without spending rows
        on the contents.
        """
        return MIN_TARGET_BOX_HEIGHT if selector == BOX_SELECTORS[0] else MIN_TABLE_HEIGHT

    def _box_height(self, selector: str) -> int:
        return max(self._min_box_height(selector), self._box_heights.get(selector, self._default_box_height(selector)))

    def _box_selectors(self) -> list[str]:
        return list(BOX_SELECTORS[: self._reveal_level + 1])

    def _flexible_selector(self, selectors: list[str]) -> str:
        """The one box holding the leftover rows: the maximised one, else the deepest."""
        return self._maximised if self._maximised in selectors else selectors[-1]

    def _apply_box_heights(self) -> None:
        """Size the boxes: one holds the leftover rows as `1fr`, the rest are fixed.

        Under `m` the others are not shrunk to their headers but taken off screen
        altogether, along with the header bar — a maximised box that still has three
        other headings and a title above it is not maximised, it is merely large.
        """
        selectors = self._box_selectors()
        flexible = self._flexible_selector(selectors)
        maximised = self._maximised is not None
        for selector in selectors:
            widget = self.query_one(selector)
            widget.styles.display = "none" if maximised and selector != flexible else "block"
            widget.styles.height = "1fr" if selector == flexible else self._box_height(selector)
        self.query_one(RebaseHeader).styles.display = "none" if maximised else "block"

    def _available_rows(self) -> int:
        """Rows the boxes have to share, taken off the view rather than off the boxes.

        Summing the boxes' own heights would fold any over-allocation into the total and
        make it look like there was less room than there is, which is exactly the state
        this bound exists to get out of. The boxes have the view to themselves now that
        the splitters are their own headers, less the error line when there is one.
        """
        error = self.query_one("#project-error", Static)
        spent = error.size.height if error.display else 0
        return max(0, self.query_one("#project-view", Vertical).size.height - spent)

    def _take_rows(self, wanted: int, donors: list[str]) -> int:
        """Shrink *donors* in order until *wanted* rows are free. Returns what was freed.

        In order, and not just from the neighbour: growing the timeline should eat the
        runs table first and then keep going into the target box, rather than stopping
        dead the moment the box next to it is down to its header.
        """
        freed = 0
        for donor in donors:
            if freed >= wanted:
                break
            current = self._box_height(donor)
            given = min(wanted - freed, current - self._min_box_height(donor))
            if given > 0:
                self._box_heights[donor] = current - given
                freed += given
        return freed

    def _grow_box(self, selector: str, rows: int) -> None:
        """Give *rows* to a box, taking them from the others nearest-first."""
        selectors = self._box_selectors()
        if selector not in selectors or rows == 0:
            return
        self._maximised = None
        flexible = self._flexible_selector(selectors)
        # Nearest first, so the box you are pushing against is the one that gives.
        index = selectors.index(selector)
        others = sorted(
            (box for box in selectors if box != selector), key=lambda box: abs(selectors.index(box) - index)
        )

        if selector == flexible:
            if rows > 0:
                # It has no height of its own; it grows by taking rows off everything else.
                self._take_rows(rows, others)
            else:
                # And it shrinks by handing them to its neighbour — but only what it has.
                # Without the slack bound the neighbour grew past the bottom of the screen
                # and pushed this box off it, header and all.
                nearest = others[0]
                given = min(-rows, self._flexible_slack(selectors, flexible))
                self._box_heights[nearest] = self._box_height(nearest) + given
        elif rows < 0:
            self._box_heights[selector] = max(self._min_box_height(selector), self._box_height(selector) + rows)
        else:
            # The flexible box gives what it can spare, and the fixed ones cover the rest.
            spare = min(rows, self._flexible_slack(selectors, flexible))
            taken = self._take_rows(rows - spare, [box for box in others if box != flexible])
            self._box_heights[selector] = self._box_height(selector) + spare + taken
        self._apply_box_heights()

    def _flexible_slack(self, selectors: list[str], flexible: str) -> int:
        """Rows the leftover-holding box could give up before it is down to its header."""
        fixed = sum(self._box_height(box) for box in selectors if box != flexible)
        return max(0, self._available_rows() - fixed - self._min_box_height(flexible))

    def action_resize_box(self, delta: int) -> None:
        """Grow or shrink the focused box, in rows. Bound to `+` and `-`."""
        selectors = self._box_selectors()
        if len(selectors) < 2:
            self.notify("Nothing to resize yet — open a run or its timeline first.", severity="warning")
            return
        self._grow_box(self._focused_box_selector(selectors), delta * BOX_STEP)

    def _leave_maximised(self) -> None:
        """Drop out of `m` and put the header back, whatever took us out of the view."""
        if self._maximised is None:
            return
        self._maximised = None
        self._apply_box_heights()

    def action_maximise_box(self) -> None:
        """Give the focused box the whole project view. `b` or `m` again gives it back."""
        selectors = self._box_selectors()
        if len(selectors) < 2:
            self.notify("Only one pane is open — it already has the screen.", severity="warning")
            return
        focused = self._focused_box_selector(selectors)
        self._maximised = None if self._maximised == focused else focused
        self._apply_box_heights()

    def _focused_box_selector(self, selectors: list[str]) -> str:
        """Which box holds the focus. The target tables live inside one rather than being one."""
        focused = self.focused
        focused_id = f"#{focused.id}" if focused is not None and focused.id else ""
        if focused_id in dict(TARGET_TABS).values():
            return BOX_SELECTORS[0]
        return focused_id if focused_id in selectors else selectors[-1]

    def action_reset_box_heights(self) -> None:
        self._box_heights = {}
        self._maximised = None
        self._apply_box_heights()
        self.notify("Pane sizes back to their defaults.")

    def begin_box_drag(self, selector: str, y: int) -> None:
        self._drag_box = (selector, y, self._box_height(selector))

    def drag_box_to(self, y: int) -> None:
        if self._drag_box is None:
            return
        selector, start_y, start_height = self._drag_box
        self._grow_box(selector, start_height + y - start_y - self._box_height(selector))

    def end_box_drag(self) -> None:
        self._drag_box = None

    def _set_project_error(self, message: str | None) -> None:
        """Show the project view's error line, or take it away again.

        The message is wrapped in `Text` like every other panel here: Textual reads a
        plain `str` as content markup, and API text is full of brackets it would choke on.
        """
        panel = self.query_one("#project-error", Static)
        panel.styles.display = "none" if message is None else "block"
        panel.update(Text(message or ""))

    def _fill_table(self, table_id: str) -> DataTable:
        """Empty a table and give it its header back, ready for rows.

        Headers and rows land in the same paint this way, so the columns are sized once
        against real data instead of snapping from header width to content width.
        """
        table = self.query_one(f"#{table_id}", DataTable)
        table.clear(columns=True)
        table.add_columns(*TABLE_COLUMNS[table_id])
        return table

    def _setup_tables(self) -> None:
        """Set the tables up, but leave them without columns.

        Column widths are computed from the header text until the first row lands, so a
        table that shows its headers early shows them at the wrong widths and then jerks
        them into place when the data arrives. Headers are added in `_fill_table` instead,
        in the same paint as the rows.
        """
        projects = self.query_one("#projects-table", DataTable)
        projects.cursor_type = "row"
        projects.zebra_stripes = False

        profiles = self.query_one("#workspace-profiles-table", DataTable)
        profiles.cursor_type = "row"
        profiles.zebra_stripes = True

        functions = self.query_one("#functions-table", DataTable)
        functions.cursor_type = "row"
        functions.zebra_stripes = True

        workflows = self.query_one("#workflows-table", DataTable)
        workflows.cursor_type = "row"
        workflows.zebra_stripes = True

        runs = self.query_one("#runs-table", DataTable)
        runs.cursor_type = "row"
        runs.zebra_stripes = True

        timeline = self.query_one("#timeline-table", DataTable)
        timeline.cursor_type = "row"
        timeline.zebra_stripes = True

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
            # Back undoes `m` before it starts closing anything.
            if self._maximised is not None:
                self._leave_maximised()
                return
            # Then it walks the reveal levels shut before it leaves the project.
            if self._reveal_level > 0:
                self._reveal(self._reveal_level - 1)
                return
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

    def _target_tab_order(self) -> list[tuple[str, str]]:
        return list(TARGET_TABS)

    def _visible_boxes(self) -> list[DataTable]:
        """The tables `tab` moves between, top to bottom, as the screen currently stands."""
        if self.current_view == "workspace":
            return [self.query_one("#projects-table", DataTable)]
        if self.current_view == "workspace-switcher":
            return [self.query_one("#workspace-profiles-table", DataTable)]
        active = self.query_one("#target-tabs", TabbedContent).active
        # The target box holds whichever of its two tables the chip strip has open; the
        # other two boxes *are* their table. Pair them so visibility is read off the box.
        tables = [dict(self._target_tab_order()).get(active, "#workflows-table")]
        tables.extend(selector for level in REVEAL_LEVELS[: self._reveal_level] for selector in level)
        # Under `m` the rest are off screen, and `tab` has nowhere else to go.
        return [
            self.query_one(table, DataTable)
            for box, table in zip(self._box_selectors(), tables, strict=True)
            if self.query_one(box).display
        ]

    def action_cycle_box(self) -> None:
        """Move the focus to the next box on screen, wrapping at the bottom.

        `tab` used to switch the Workflows/Functions tabs and nothing else, which left
        the focus stuck in the top box while the runs and timeline below it could only be
        reached with the mouse. Switching those two tabs is `left`/`right` now, bound on
        the target tables themselves so it costs nothing anywhere else.
        """
        boxes = self._visible_boxes()
        if not boxes:
            return
        focused = self.focused
        current = boxes.index(focused) if isinstance(focused, DataTable) and focused in boxes else -1
        boxes[(current + 1) % len(boxes)].focus()

    def action_switch_target_tab(self, delta: int) -> None:
        """Step between the Workflows and Functions tabs. Bound to left/right."""
        if self.current_view != "project":
            return
        tabs = self.query_one("#target-tabs", TabbedContent)
        order = [tab_id for tab_id, _ in self._target_tab_order()]
        current = order.index(tabs.active) if tabs.active in order else 0
        tabs.active = order[(current + delta) % len(order)]
        # The focus follows, or `tab` would carry on from the box that is no longer there.
        self.query_one(dict(TARGET_TABS)[tabs.active], DataTable).focus()

    @property
    def display_tzinfo(self) -> tzinfo | None:
        """The zone every time in the TUI is rendered in, system local until changed."""
        return self._display_timezone or datetime.now().astimezone().tzinfo

    def _time(self, value: Any) -> str:
        return format_timestamp(value, self.display_tzinfo)

    def _timezone_label(self) -> str:
        if self._display_timezone is not None:
            return str(self._display_timezone)
        now = datetime.now().astimezone()
        return f"{TimezoneChoiceScreen.SYSTEM} ({now.tzname()})"

    def action_choose_timezone(self) -> None:
        """Change the zone times are shown in. Bound to a click on the header clock."""
        self.push_screen(TimezoneChoiceScreen(current=self._timezone_label()), self._on_timezone_chosen)

    def _on_timezone_chosen(self, choice: str | None) -> None:
        if choice is None:
            return
        if choice == TimezoneChoiceScreen.SYSTEM:
            self._display_timezone = None
        else:
            try:
                self._display_timezone = ZoneInfo(choice)
            except Exception as exc:
                self.notify(f"Unknown timezone {choice}: {exc}", severity="error")
                return
        self.query_one(RebaseClock).refresh()
        # Every rendered timestamp is now in the wrong zone; the reload repaints the target
        # and run tables, and the timeline is redrawn here because nothing else will.
        self._render_timeline()
        self.action_refresh()
        self.notify(f"Times now shown in {self._timezone_label()}.")

    def action_toggle_terminal_select(self) -> None:
        """Hand the mouse back to the terminal, and take it again.

        Mouse reporting is all-or-nothing: while it is on, the terminal forwards drags to
        the app instead of making a selection of its own, which is why the terminal's copy
        shortcut has nothing to copy. Turning it off restores native selection — drag, then
        cmd+c, exactly as in a program that never took the mouse — and turning it back on
        restores hover, clicking and wheel scrolling. Keys keep working either way, so `s`
        always gets you back.
        """
        method = "_enable_mouse_support" if self._terminal_select else "_disable_mouse_support"
        toggle = getattr(self._driver, method, None)
        if toggle is None:
            self.notify("This terminal has no mouse support to hand over.", severity="warning")
            return
        toggle()
        self._terminal_select = not self._terminal_select
        if self._terminal_select:
            # Nothing will report the pointer leaving now, so drop the app's own highlights by hand.
            self._set_mouse_over(None, None)
            self.screen.clear_selection()
            self.notify("Mouse handed to the terminal: drag to select, then copy as you would anywhere. s resumes.")
        else:
            self.notify("Mouse back in the TUI: hover, click and scroll again.")
        self._update_workspace_title()

    def action_adjust_text_selection(self, delta: int) -> None:
        """Move the trailing edge of the mouse-made text selection by one cell.

        Textual only builds selections by dragging, and `Selection.from_offsets` sorts
        the two ends, so there is no record of which end the pointer was on: `shift+right`
        always grows the selection at its end and `shift+left` always pulls that end back.
        A selection spanning several widgets has no single edge to move, so it is left
        alone rather than moved arbitrarily.
        """
        screen = self.screen
        if len(screen.selections) != 1:
            return
        widget, selection = next(iter(screen.selections.items()))
        start, end = selection
        if start is None or end is None:
            # A whole-widget selection: no concrete offsets to walk.
            return
        selected = widget.get_selection(SELECT_ALL)
        if selected is None:
            return
        lines = selected[0].splitlines()
        if not 0 <= end.y < len(lines):
            return
        # `end` is exclusive, so it may sit one past the last character of its line.
        lowest = start.x if end.y == start.y else 0
        new_x = min(max(end.x + delta, lowest), len(lines[end.y]))
        if new_x == end.x:
            return
        screen.selections = {widget: Selection(start, Offset(new_x, end.y))}

    def action_delete_selection(self) -> None:
        """Delete the marked rows, or the row under the cursor when nothing is marked."""
        table = self._delete_table()
        if table is None:
            self.notify(
                "Nothing here can be deleted. Runs and workspaces have no delete endpoint.",
                severity="warning",
            )
            return
        kind = DELETABLE_TABLES[str(table.id)]
        keys = table.marked_keys or [key for key in (table.cursor_key,) if key is not None]
        items = [(key, name) for key in keys if (name := self._delete_label(kind, key)) is not None]
        if not items:
            self.notify(f"No {kind} selected.", severity="warning")
            return
        self.push_screen(
            DeleteConfirmScreen(kind=kind, names=[name for _, name in items]),
            lambda confirmed: self._on_delete_confirmed(bool(confirmed), kind, items, table),
        )

    def _open_source_target(self) -> dict[str, Any] | None:
        """The project `o` acts on: the highlighted row, or the one already open."""
        if self.current_view == "project":
            return self.selected_project
        if self.current_view == "workspace":
            key = self.query_one("#projects-table", SelectableDataTable).cursor_key
            summary = None if key is None else self._project_rows.get(key)
            return None if summary is None else summary.project
        return None

    def action_open_source(self) -> None:
        """Open the file that declares the selected project."""
        project = self._open_source_target()
        name = str(project.get("name") or "") if project is not None else ""
        if not name:
            self.notify("Select a project first.", severity="warning")
            return
        self.run_worker(
            self._open_project_source(name),
            name="open-source",
            group="tui-open",
            exclusive=True,
        )

    async def _open_project_source(self, project_name: str) -> None:
        roots = [Path(entry) for entry in search_paths(self._workspace_key())]
        # Off the event loop: walking a large repository must not freeze the UI.
        result = await asyncio.to_thread(find_project_declarations, project_name, roots)
        if result.status == "found":
            self._launch_editor(result.matches[0])
            return
        if result.status == "ambiguous":
            self.push_screen(
                OpenSourceChoiceScreen(project_name=project_name, matches=result.matches, roots=result.roots),
                self._on_source_chosen,
            )
            return
        self.notify(describe_failure(result), severity="warning")

    def _on_source_chosen(self, chosen: ProjectDeclaration | None) -> None:
        if chosen is not None:
            self._launch_editor(chosen)

    def _launch_editor(self, declaration: ProjectDeclaration) -> None:
        settings = editor_settings()
        configured = settings.get("command")
        configured_terminal = settings.get("terminal")
        try:
            command = resolve_editor(
                configured=configured if isinstance(configured, str) else None,
                configured_terminal=configured_terminal if isinstance(configured_terminal, bool) else None,
            )
            if command is None:
                self.notify(NO_EDITOR_HINT, severity="error")
                return
            roots = [Path(entry) for entry in search_paths(self._workspace_key())]
            argv = build_argv(
                command,
                declaration.path,
                line=declaration.line,
                folder=project_folder(declaration.path, roots),
            )
            if command.terminal:
                try:
                    # A terminal editor needs this terminal, so hand it over and take
                    # it back when the editor exits.
                    with self.suspend():
                        run_foreground(argv)
                except SuspendNotSupported:
                    # Headless and web drivers cannot yield the terminal.
                    spawn_detached(argv)
            else:
                spawn_detached(argv)
        except RebaseWorkflowError as exc:
            self.notify(str(exc), severity="error")
            return
        self.notify(f"Opened {declaration.path.name}:{declaration.line}.")

    def _delete_table(self) -> SelectableDataTable | None:
        """The table `d` acts on, resolved from the current view.

        Focus alone is not enough to go on: it stays on the projects table after you
        drill into a project, and deleting a project from inside the project view is
        not what `d` looks like it would do there.
        """
        if self.current_view == "workspace":
            return self.query_one("#projects-table", SelectableDataTable)
        if self.current_view != "project":
            return None
        focused = self.focused
        if isinstance(focused, SelectableDataTable) and str(focused.id) in {"workflows-table", "functions-table"}:
            return focused
        active_tab = self.query_one("#target-tabs", TabbedContent).active
        table_id = {"workflows-tab": "#workflows-table", "functions-tab": "#functions-table"}.get(active_tab)
        return None if table_id is None else self.query_one(table_id, SelectableDataTable)

    def _delete_label(self, kind: DeletableKind, key: str) -> str | None:
        if kind == "project":
            summary = self._project_rows.get(key)
            return None if summary is None else str(summary.project.get("name", key))
        rows = self._function_rows if kind == "function" else self._workflow_rows
        item = rows.get(key)
        return None if item is None else str(item.get("name", key))

    def _on_delete_confirmed(
        self,
        confirmed: bool,
        kind: DeletableKind,
        items: list[tuple[str, str]],
        table: SelectableDataTable,
    ) -> None:
        if not confirmed:
            return
        # Drop the rows before the API is even asked. The requests take a round trip
        # each and there is no bulk delete route, so waiting for them -- and then for
        # the reload that used to follow -- left confirmed-gone rows on screen for
        # seconds. A delete that fails puts its row back, via the refresh below.
        self._forget_deleted(kind, [object_id for object_id, _ in items], table)
        self.run_worker(self._delete_items(kind, items), name="delete", group="tui-delete", exclusive=True)

    def _forget_deleted(self, kind: DeletableKind, object_ids: list[str], table: SelectableDataTable) -> None:
        """Take the rows off screen and out of the view's backing data."""
        table.action_clear_marks()
        for object_id in object_ids:
            with suppress(KeyError):  # already gone -- a refresh landed first
                table.remove_row(object_id)
        gone = set(object_ids)
        if kind == "project":
            self._project_rows = {key: value for key, value in self._project_rows.items() if key not in gone}
            if self.workspace_overview is not None:
                self.workspace_overview = replace(
                    self.workspace_overview,
                    projects=[p for p in self.workspace_overview.projects if str(p.get("id")) not in gone],
                    project_summaries=[
                        s for s in self.workspace_overview.project_summaries if str(s.project.get("id")) not in gone
                    ],
                    project_names={
                        key: value for key, value in self.workspace_overview.project_names.items() if key not in gone
                    },
                )
        elif kind == "function":
            self._function_rows = {key: value for key, value in self._function_rows.items() if key not in gone}
            if self.project_targets is not None:
                self.project_targets = replace(
                    self.project_targets,
                    functions=[f for f in self.project_targets.functions if str(f.get("id")) not in gone],
                )
        else:
            self._workflow_rows = {key: value for key, value in self._workflow_rows.items() if key not in gone}
            if self.project_targets is not None:
                self.project_targets = replace(
                    self.project_targets,
                    workflows=[w for w in self.project_targets.workflows if str(w.get("id")) not in gone],
                )
        self._clear_target_detail(clear_project=kind == "project")

    async def _delete_items(self, kind: DeletableKind, items: list[tuple[str, str]]) -> None:
        # Forced throughout: the confirmation dialog is explicit that contents go
        # too, and an unforced delete is refused for anything that still holds runs.
        if kind == "project":
            deleted, failures = await self._delete_projects(items)
        else:
            deleted, failures = await self._delete_targets(kind, items)
        self._report_deletes(kind, deleted, failures)
        if failures:
            # The rows are already gone from the screen, so reload to put back whatever survived.
            self.action_refresh()

    async def _delete_projects(self, items: list[tuple[str, str]]) -> tuple[list[str], list[str]]:
        """One request for the whole marked run, via the API's batch route."""
        names = dict(items)
        try:
            reported = await asyncio.to_thread(
                self.data.client.delete_projects, [object_id for object_id, _ in items], force=True
            )
        except Exception as exc:
            # The batch was refused whole: auth, a bad payload, an unreachable API.
            return [], [f"{name}: {exc}" for _, name in items]
        failed = {project_id for project_id, _ in reported}
        return (
            [name for object_id, name in items if object_id not in failed],
            [f"{names.get(project_id, project_id)}: {message}" for project_id, message in reported],
        )

    async def _delete_targets(self, kind: DeletableKind, items: list[tuple[str, str]]) -> tuple[list[str], list[str]]:
        """Functions and workflows have no batch route, so fan out concurrently instead."""

        def delete(object_id: str) -> None:
            if kind == "function":
                self.data.client.delete_function(object_id, force=True)
            else:
                self.data.client.delete_workflow(object_id, force=True)

        results = await asyncio.gather(
            *(asyncio.to_thread(delete, object_id) for object_id, _ in items), return_exceptions=True
        )
        return (
            [name for (_, name), result in zip(items, results, strict=True) if not isinstance(result, BaseException)],
            [
                f"{name}: {result}"
                for (_, name), result in zip(items, results, strict=True)
                if isinstance(result, BaseException)
            ],
        )

    def _report_deletes(self, kind: DeletableKind, deleted: list[str], failures: list[str]) -> None:
        if deleted:
            label = deleted[0] if len(deleted) == 1 else f"{len(deleted)} {kind}s"
            self.notify(f"Deleted {label}.")
        for failure in failures:
            self.notify(f"Delete failed — {failure}", severity="error")

    def on_selectable_data_table_marks_changed(self, event: SelectableDataTable.MarksChanged) -> None:
        self._marked_count = len(event.marked)
        self._update_workspace_title()

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
        try:
            targets = await asyncio.to_thread(self.data.load_project_targets, project)
        except Exception as exc:
            self._set_error(exc)
            return
        self.project_targets = targets
        self._render_project_targets(targets)

    async def _load_runs(self, target_type: TargetType, target_id: str) -> None:
        try:
            runs = await asyncio.to_thread(self.data.load_target_runs, target_type, target_id)
        except Exception as exc:
            self._set_error(exc)
            return
        self._render_runs(runs)

    async def _load_run_detail(self, run_id: str) -> None:
        target_type = (self._run_rows.get(run_id) or {}).get("target_type")
        try:
            detail = await asyncio.to_thread(self.data.load_run_detail, run_id, target_type=target_type)
        except Exception as exc:
            self._set_error(exc)
            return
        self._run_detail = detail
        self._render_timeline()
        if self._logs_expanded and run_id not in self._run_logs:
            self.run_worker(self._load_run_logs(run_id), name="run-logs", group="tui-logs", exclusive=True)

    async def _load_run_logs(self, run_id: str) -> None:
        try:
            entries = await asyncio.to_thread(self.data.load_run_logs, run_id)
        except Exception as exc:
            self.notify(f"Could not load logs for run {compact_id(run_id)}: {exc}", severity="error")
            self._logs_expanded = False
            self._render_timeline()
            return
        self._run_logs[run_id] = entries
        self._render_timeline()

    def action_toggle_logs(self) -> None:
        """Fold the run's log output into the timeline, all of it at once.

        Expanded means expanded: there is no per-step fold, because the thing worth
        having is one scrollable read of the whole run rather than a tree to click open.
        """
        if self._run_detail is None:
            self.notify("Select a run first — l folds its logs into the timeline.", severity="warning")
            return
        self._logs_expanded = not self._logs_expanded
        run_id = str(self._run_detail.run.get("id", ""))
        if self._logs_expanded and run_id not in self._run_logs:
            self.run_worker(self._load_run_logs(run_id), name="run-logs", group="tui-logs", exclusive=True)
        self._render_timeline()

    def _render_workspace_overview(self, overview: WorkspaceOverviewData) -> None:
        self._project_rows = {
            str(summary.project["id"]): summary
            for summary in overview.project_summaries
            if summary.project.get("id") is not None
        }

        # An empty workspace and a workspace still loading both draw nothing, and the
        # only way to tell them apart is to say so.
        empty = self.query_one("#workspace-empty", Static)
        empty.styles.display = "none" if overview.projects else "block"
        if not overview.projects:
            empty.update(
                Text(
                    f"No projects in workspace {self._workspace_label()}.\n"
                    "Press r to refresh, or click the title to switch workspace."
                )
            )

        projects = self._fill_table("projects-table")
        for project_id, summary in self._project_rows.items():
            project = summary.project
            projects.add_row(
                str(project.get("name", "-")),
                str(summary.function_count),
                str(summary.workflow_count),
                str(summary.cron_count),
                str(summary.endpoint_count),
                key=project_id,
            )

    def _render_workspace_profiles(self) -> None:
        profiles = list_profiles()
        self._profile_rows = profiles
        table = self._fill_table("workspace-profiles-table")
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
        self._steps_by_function = targets.steps_by_function()
        self._function_rows = self._ordered_functions(targets)
        self._workflow_rows = {str(item["id"]): item for item in targets.workflows if item.get("id") is not None}
        self._endpoints_by_target = endpoints_by_target(targets.endpoints)

        functions = self._fill_table("functions-table")
        for function_id, function in self._function_rows.items():
            steps = self._steps_by_function.get(function_id, [])
            functions.add_row(
                str(function.get("name", "-")),
                format_step_workflows(steps),
                format_step_keys(steps),
                str(function.get("run_type") or "-"),
                format_bool(function.get("enabled")),
                format_endpoint(self._target_endpoints("function", function_id)),
                compact_id(function.get("current_version_id")),
                self._time(function.get("updated_at")),
                key=function_id,
            )

        workflows = self._fill_table("workflows-table")
        for workflow_id, workflow in self._workflow_rows.items():
            workflows.add_row(
                str(workflow.get("name", "-")),
                str(workflow.get("run_type") or "-"),
                format_bool(workflow.get("enabled")),
                format_endpoint(self._target_endpoints("workflow", workflow_id)),
                format_schedule(workflow.get("schedule")),
                self._time(workflow.get("next_run_at")),
                compact_id(workflow.get("current_version_id")),
                self._time(workflow.get("updated_at")),
                key=workflow_id,
            )

        self._clear_target_detail(clear_project=False)

    @staticmethod
    def _ordered_functions(targets: ProjectTargetsData) -> dict[str, dict[str, Any]]:
        """Functions in graph order under the workflow that calls them, strays last.

        The function list comes back in whatever order the API stored it, which puts a
        workflow's steps nowhere near each other. Grouping them is the whole point of the
        Workflow column: read down the table and you read the workflow's shape.
        """
        by_id = {str(item["id"]): item for item in targets.functions if item.get("id") is not None}
        workflow_order = {str(item["id"]): index for index, item in enumerate(targets.workflows)}
        placed: dict[str, dict[str, Any]] = {}
        for step in sorted(
            targets.steps, key=lambda s: (workflow_order.get(s.workflow_id, len(workflow_order)), s.order)
        ):
            # A function called by two workflows is listed once, under the first of them.
            if step.function_id in by_id and step.function_id not in placed:
                placed[step.function_id] = by_id[step.function_id]
        # Everything the graphs did not account for keeps its original position.
        placed.update({key: value for key, value in by_id.items() if key not in placed})
        return placed

    def _target_endpoints(self, target_type: str, target_id: str) -> list[dict[str, Any]]:
        return self._endpoints_by_target.get((target_type, target_id), [])

    def _render_runs(self, runs: list[dict[str, Any]]) -> None:
        self._run_rows = {str(item["id"]): item for item in runs if item.get("id") is not None}
        table = self._fill_table("runs-table")
        for run_id, run in self._run_rows.items():
            table.add_row(
                compact_id(run_id),
                status_text(run.get("status")),
                str(run.get("trigger_source") or "-"),
                self._time(run.get("created_at")),
                self._time(run.get("started_at")),
                self._time(run.get("finished_at")),
                format_duration(run.get("started_at"), run.get("finished_at")),
                key=run_id,
            )
        self.query_one("#timeline-table", DataTable).clear(columns=True)

    def _render_timeline(self) -> None:
        """The selected run's lifecycle events, steps and — when `l` is on — its logs.

        These were two boxes and a detail panel. They are one table because they are one
        sequence: the platform's own stages, the workflow's steps, and the runtime's log
        output all describe the same couple of minutes, and reading them apart meant
        reconstructing the order by eye.
        """
        detail = self._run_detail
        if detail is None:
            return
        self._reveal(2)
        run_id = str(detail.run.get("id", ""))
        logs = self._run_logs.get(run_id) if self._logs_expanded else None
        table = self._fill_table("timeline-table")
        for row in build_timeline(detail.events, detail.steps, logs, detail.tasks):
            # Indented under the step above them: tasks by one level, log lines by two,
            # which is the order they nest in even though the timeline sorts by time.
            indent = {"task": "  ", "log": "    "}.get(row.kind, "")
            table.add_row(
                self._time(row.at),
                Text(f"{indent}{row.stage}", style=MARK_STYLE if row.kind == "step" else ""),
                status_text(row.status),
                Text(row.message, style=BRAND_MEDIUM_GRAY if row.kind == "log" else ""),
            )
        if self._logs_expanded and logs is None:
            table.add_row("", "", Text("loading", style=BRAND_AMBER), "Fetching logs...")
        elif logs is not None and len(logs) >= RUN_LOG_LIMIT:
            table.add_row(
                "",
                "",
                Text("truncated", style=BRAND_AMBER),
                f"Showing the first {RUN_LOG_LIMIT} log lines of this run.",
            )

    def _clear_target_detail(self, *, clear_project: bool = True) -> None:
        if clear_project:
            self.query_one("#functions-table", DataTable).clear(columns=True)
            self.query_one("#workflows-table", DataTable).clear(columns=True)
            self._function_rows = {}
            self._workflow_rows = {}
            self._endpoints_by_target = {}
            self._steps_by_function = {}
        self.query_one("#runs-table", DataTable).clear(columns=True)
        self.query_one("#timeline-table", DataTable).clear(columns=True)
        self._run_rows = {}
        self._run_detail = None
        self._reveal(0)

    def _select_project(self, project_id: str) -> None:
        summary = self._project_rows.get(project_id)
        if summary is None:
            return
        self.selected_project = summary.project
        self.selected_target = None
        self.selected_target_type = None
        self.project_targets = None
        self._set_project_error(None)
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
        # `m` belongs to the project view, and it hid the header on the way in.
        self._leave_maximised()
        self.call_after_refresh(self._update_workspace_title)
        self.query_one("#workspace-view", Vertical).styles.display = "block"
        self.query_one("#project-view", Vertical).styles.display = "none"
        self.query_one("#workspace-switcher-view", Vertical).styles.display = "none"
        self.query_one("#projects-table", DataTable).focus()

    def _show_project_view(self) -> None:
        self.current_view = "project"
        self.call_after_refresh(self._update_workspace_title)
        self.query_one("#workspace-view", Vertical).styles.display = "none"
        self.query_one("#project-view", Vertical).styles.display = "block"
        self.query_one("#workspace-switcher-view", Vertical).styles.display = "none"
        # The focus would otherwise stay on the projects table, which is no longer on
        # screen — and `tab`, `d` and `p` all read it.
        boxes = self._visible_boxes()
        if boxes and self.focused not in boxes:
            boxes[0].focus()

    def _show_workspace_switcher_view(self) -> None:
        self.current_view = "workspace-switcher"
        self._leave_maximised()
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
        chosen_workspace_id = self.profile_data.get("workspace_id")
        self.data = RebaseTuiData(
            Client(
                profile=self.profile_name,
                # Explicit beats the directory marker: picking a workspace here is a
                # deliberate act, and it would otherwise be overridden on the next request.
                workspace_id=chosen_workspace_id if isinstance(chosen_workspace_id, str) else None,
            ),
            limit=self.limit,
        )
        self.workspace_overview = None
        self.project_targets = None
        self.selected_project = None
        self.selected_target = None
        self.selected_target_type = None
        self._project_rows = {}
        self._function_rows = {}
        self._workflow_rows = {}
        self._steps_by_function = {}
        self._endpoints_by_target = {}
        self._run_rows = {}
        self._update_workspace_title()
        self._clear_target_detail()
        self._show_workspace_view()
        self.action_refresh()

    def action_show_details(self) -> None:
        """Open the drawer on whatever row the cursor is on."""
        details = self._selected_details()
        if details is None:
            self.notify("Select a row first — p shows its full record.", severity="warning")
            return
        self.push_screen(details)

    def _run_drawer(self, run: dict[str, Any]) -> DetailDrawer:
        """A run, split the way it actually divides: what it was asked, what came back.

        The id is spelled out in full rather than shortened — the drawer is where you
        come to copy it — and the status gets its own line instead of riding along
        after a separator.
        """
        # The key stays on the document rather than becoming a caption above it: what
        # you are looking at is the run's `parameters`, and that is what it should say.
        output: dict[str, Any] = {"result": run.get("result")}
        if run.get("error"):
            output["error"] = run["error"]
        sections: list[Any] = [{"parameters": run.get("parameters") or {}}, output]
        status = str(run.get("status", "unknown"))
        return DetailDrawer(
            fields=[
                DetailField("Run ID", str(run.get("id", "-"))),
                DetailField("Status", status, status_style(status)),
            ],
            sections=sections,
        )

    def _selected_details(self) -> DetailDrawer | None:
        """The drawer for the focused table's row, or None when there is nothing to show."""
        focused = self.focused
        table_id = str(focused.id) if isinstance(focused, DataTable) else ""
        # The timeline is one run's own story, so `p` there means that run — there is no
        # per-row record behind a log line or a stage to show instead.
        if table_id == "timeline-table":
            return None if self._run_detail is None else self._run_drawer(self._run_detail.run)
        key = focused.cursor_key if isinstance(focused, SelectableDataTable) else self._cursor_key(focused)
        if key is None:
            return None
        if table_id == "runs-table":
            run = self._run_rows.get(key)
            return None if run is None else self._run_drawer(run)
        if table_id == "projects-table":
            summary = self._project_rows.get(key)
            if summary is None:
                return None
            return DetailDrawer(
                fields=[DetailField("Project", str(summary.project.get("name", "-")))],
                sections=[
                    detail_payload(
                        summary.project,
                        functions=summary.function_count,
                        workflows=summary.workflow_count,
                        cron_jobs=summary.cron_count,
                        endpoints=summary.endpoint_count,
                    ),
                ],
            )
        if table_id == "functions-table":
            item = self._function_rows.get(key)
            if item is None:
                return None
            steps = self._steps_by_function.get(key, [])
            return DetailDrawer(
                fields=[
                    DetailField("Function", str(item.get("name", "-"))),
                    DetailField("State", format_bool(item.get("enabled"))),
                ],
                sections=[
                    detail_payload(
                        item,
                        endpoints=self._endpoint_details("function", key),
                        step_of=[
                            {
                                "workflow": step.workflow_name,
                                "node_key": step.node_key,
                                "after": list(step.upstream),
                            }
                            for step in steps
                        ],
                    ),
                ],
            )
        if table_id == "workflows-table":
            item = self._workflow_rows.get(key)
            if item is None:
                return None
            own = sorted(
                (step for steps in self._steps_by_function.values() for step in steps if step.workflow_id == key),
                key=lambda step: step.order,
            )
            return DetailDrawer(
                fields=[
                    DetailField("Workflow", str(item.get("name", "-"))),
                    DetailField("State", format_bool(item.get("enabled"))),
                ],
                sections=[
                    detail_payload(
                        item,
                        endpoints=self._endpoint_details("workflow", key),
                        steps=[
                            {"node_key": step.node_key, "function": step.name, "after": list(step.upstream)}
                            for step in own
                        ],
                    ),
                ],
            )
        return None

    @staticmethod
    def _cursor_key(table: Any) -> str | None:
        if not isinstance(table, DataTable) or not 0 <= table.cursor_row < len(table.ordered_rows):
            return None
        value = table.ordered_rows[table.cursor_row].key.value
        return None if value is None else str(value)

    def _endpoint_details(self, target_type: str, target_id: str) -> list[dict[str, Any]]:
        """Endpoints of a target, each with the absolute URL the table has no room for."""
        return [
            {**endpoint, "url": format_url(endpoint, api_url=self._api_url())}
            for endpoint in self._target_endpoints(target_type, target_id)
        ]

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
            self._reveal(1)
            self.run_worker(self._load_runs("function", row_id), name="runs", group="tui", exclusive=True)
        elif event.data_table.id == "workflows-table":
            target = self._workflow_rows.get(row_id)
            if target is None:
                return
            self.selected_target_type = "workflow"
            self.selected_target = target
            self._reveal(1)
            self.run_worker(self._load_runs("workflow", row_id), name="runs", group="tui", exclusive=True)
        elif event.data_table.id == "runs-table" and row_id in self._run_rows:
            self.run_worker(self._load_run_detail(row_id), name="run-detail", group="tui", exclusive=True)
        elif event.data_table.id == "workspace-profiles-table":
            self._select_workspace_profile(row_id)

    def _project_name(self, item: dict[str, Any]) -> str:
        if self.workspace_overview is None:
            return str(item.get("project_id", "-"))
        project_id = str(item.get("project_id", ""))
        return self.workspace_overview.project_names.get(project_id, project_id or "-")

    def _workspace_label(self) -> str:
        """The workspace the data actually comes from, which a marker may have pinned.

        The profile's own `workspace_name` is the nicer label, but it is only the truth
        while the profile is what decided the workspace.
        """
        effective = getattr(self.data.client, "workspace_id", None)
        if isinstance(effective, str) and effective and self.profile_data.get("workspace_id") != effective:
            return effective
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
        title = f"Rebase TUI - Workspace: {self._workspace_label()}"
        if self.current_view == "project" and self.selected_project is not None:
            title = f"{title} / {self.selected_project.get('name', '-')}"
        if self._marked_count:
            title = f"{title} - {self._marked_count} marked"
        if self._terminal_select:
            title = f"{title} - select text (mouse off, s to resume)"
        self.title = title

    def _set_error(self, error: Exception) -> None:
        self._show_project_view()
        self._set_project_error(f"The Rebase API request failed. Press r to retry.\nError: {error}")
        self.notify(f"Rebase API request failed: {error}", severity="error")


def run_tui(*, project: str | None = None, limit: int = 100, client: Client | None = None) -> None:
    RebaseTuiApp(client=client, project=project, limit=limit).run()
