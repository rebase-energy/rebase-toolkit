from __future__ import annotations

import ast
import asyncio
import json
import subprocess
import textwrap
import webbrowser
from collections import Counter
from collections.abc import Awaitable, Callable, Collection, Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, tzinfo
from functools import partial
from pathlib import Path
from time import monotonic
from typing import Any, Literal
from urllib.parse import quote, urlsplit
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
from textual.css.query import NoMatches
from textual.errors import NoWidget
from textual.geometry import Offset
from textual.message import Message
from textual.screen import ModalScreen
from textual.selection import SELECT_ALL, Selection
from textual.widget import Widget
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    OptionList,
    Static,
    Tab,
    TabbedContent,
    TabPane,
    Tabs,
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
from rebase.client import Client, RebaseWorkflowError, run_timing_summary
from rebase.config import (
    add_search_path,
    editor_settings,
    list_profiles,
    load_profile,
    local_workspace_id,
    search_paths,
    selected_profile_name,
    set_active_environment,
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
#: How many of a project's runs to read when working out when each function last ran.
LAST_RUN_SCAN_LIMIT = 200
#: How many recent workflow runs to open for their step rows. A step's executions are
#: only reachable per run, so this bounds the cost of answering for steps at all.
LAST_RUN_STEP_SCAN = 5
#: Concurrent requests used to collect a project's per-workflow step graphs.
STEP_GRAPH_FANOUT_WORKERS = 8
#: The run, its events, its steps and its tasks: four independent reads behind one
#: keypress, so they go together rather than one after another.
RUN_DETAIL_FANOUT_WORKERS = 6
#: What each level of the project view adds, outermost first. Level 0 is the target
#: table alone; selecting a target reveals level 1, selecting a run reveals level 2.
REVEAL_LEVELS: tuple[tuple[str, ...], ...] = (
    ("#runs-table",),
    ("#timeline-pane",),
)
#: Header text per table, added when the rows are and never before. See `_setup_tables`.
TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "projects-table": (
        "Project",
        "Functions",
        "Workflows",
        "Cron jobs",
        "Endpoints",
        "Status",
        "Last run",
        "Next run",
        "Created",
    ),
    "workspace-profiles-table": ("Active", "Profile", "Workspace", "Workspace ID", "API URL"),
    "buckets-table": ("Bucket", "URI", "Created", "Updated"),
    "secrets-table": ("Secret", "Keys"),
    # `Origin` sits second in both: you read what a thing is called, then what kind of
    # thing it is, and every column after it is one a one-off has no answer for.
    "functions-table": (
        "Name",
        "Origin",
        "Workflow",
        "Step",
        "Execution",
        "State",
        "Endpoint",
        "Last run",
        "Version",
        "Updated",
    ),
    "workflows-table": (
        "Name",
        "Origin",
        "Source",
        "Commit",
        "Execution",
        "State",
        "Endpoint",
        "Schedule",
        "Next run",
        "Last run",
        "Version",
        "Updated",
    ),
    "runs-table": ("Run", "Status", "Trigger", "Created", "Started", "Finished", "Duration"),
    # The Type column only earns its place under Activity; every other filter would
    # repeat one word down the whole table. See `_timeline_columns`.
    "timeline-table": ("Time", "Stage", "Status", "Message"),
    "timeline-table-all": ("Time", "Type", "Scope", "Item", "Status", "Summary"),
    "timeline-table-tasks": (
        "Task",
        "Step",
        "Kind",
        "Status",
        "Started",
        "Duration",
        "Artifacts",
        "Result / Error",
    ),
    # `Depends on` sits next to the step it qualifies: the DAG is the reason the Steps
    # view exists as something other than a filtered Activity, and the edges are what
    # make an ordering readable rather than merely chronological. Attempt, the two
    # timestamps and Duration were a `attempt 1 · finished 14:00:20` sentence crammed
    # into Message; as columns they sort and scan, and Message is left to say the only
    # thing that varies in shape — the error. Laid out like the Tasks view, which
    # already answers the same "what ran, how did it go, how long" questions.
    # There is no separate `Time`: a step row's time *is* its start.
    "timeline-table-steps": (
        "Step",
        "Depends on",
        "Status",
        "Attempt",
        "Started",
        "Finished",
        "Duration",
        "Message",
    ),
    "timeline-table-artifacts": ("Artifact", "Produced by", "Disposition", "Type", "Size", "URI"),
}
#: Where the projects table's ticking countdown lives, and the width every value is padded
#: to. A cell that redraws once a second must not resize its column under the reader, so
#: the widest thing `format_countdown` produces ("in 364d 23h") sets the width once and
#: every shorter value is padded out to it.
NEXT_RUN_COLUMN = TABLE_COLUMNS["projects-table"].index("Next run")
COUNTDOWN_WIDTH = 11
#: How often that countdown redraws. Local arithmetic on rows already in hand — no request
#: is made, and nothing moves but the digits.
COUNTDOWN_TICK_SECONDS = 1.0
#: The background the three chrome rows share — the header, the chip strip under it and
#: the column header under that. They are one band of furniture above the rows, so they
#: are one colour: Textual would otherwise paint the header the app background, the
#: strips and the column header its own `$panel` blue-grey, and draw two seams across a
#: band that is a single thing. Grey rather than `$panel` because that blue appears
#: nowhere else in the palette.
CHROME_GRAY = "#232826"
#: The same band under the pointer, for the column headers that are also splitters.
CHROME_GRAY_HOVER = "#33403A"

#: The tab each target table belongs to, in the order `left`/`right` cycle them.
TARGET_TABS: tuple[tuple[str, str], ...] = (
    ("workflows-tab", "#workflows-table"),
    ("functions-tab", "#functions-table"),
)
#: The same for the workspace view's resource chips, in the order they are drawn.
RESOURCE_TABS: tuple[tuple[str, str], ...] = (
    ("projects-resource-tab", "#projects-table"),
    ("buckets-resource-tab", "#buckets-table"),
    ("secrets-resource-tab", "#secrets-table"),
)
#: The two tables a project's targets are drawn into, without their `#`.
TARGET_TABLE_IDS: tuple[str, ...] = ("workflows-table", "functions-table")
#: How often the screen refreshes itself. `rebase tui --refresh-interval 0` turns it off.
AUTO_REFRESH_SECONDS = 10.0
#: How long after a keypress to leave the screen alone, so rows do not move under a
#: cursor that is still being driven.
KEYPRESS_QUIET_SECONDS = 2.0
#: Consecutive silent failures before a refresh problem is worth interrupting for. One
#: flaky request on a timer is not news; three in a row is.
AUTO_REFRESH_FAILURE_LIMIT = 3
#: Run states still worth re-reading. Anything else has finished and cannot change, so
#: polling its timeline is pure cost — and that is exactly where a reader is scrolling.
LIVE_RUN_STATUSES = frozenset({"queued", "submitted", "accepted", "starting", "pending", "running"})
#: Run states that are over. Listed explicitly rather than taken as everything outside
#: `LIVE_RUN_STATUSES`, so a status this build has never heard of is treated as possibly
#: live: the cost of being wrong is de-emphasising a stage that really is still working.
TERMINAL_RUN_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
#: Tables whose place is put back after a repaint. Anything the reader can move a cursor
#: through, mark rows in, or scroll sideways — see `RebaseTuiApp._preserve_view`.
PRESERVED_TABLE_IDS: tuple[str, ...] = (
    "projects-table",
    "buckets-table",
    "secrets-table",
    *TARGET_TABLE_IDS,
    "runs-table",
    "timeline-table",
)
#: The project view's stacked boxes, top to bottom. One per reveal level. Each box below
#: the first resizes the one above it by its own column header — see `DragHeaderTable` —
#: and the chip strips are handles too — see `DragStrip`.
BOX_SELECTORS: tuple[str, ...] = ("#target-tabs", "#runs-table", "#timeline-pane")
#: Prefixes a synthetic row's key so it cannot collide with a target's uuid, and so the
#: selection handler can tell the two apart from the key alone.
EPHEMERAL_ROW_PREFIX = "ephemeral:"
#: The Origin column. A one-off run — `rebase run` against local source — registers no
#: target, so it has no row of its own and used to be invisible here. It is grouped under
#: the name it ran as and listed among the deployed targets, told apart by this column
#: rather than by a filter: both kinds answer "what has run as `collect`", and a toggle
#: would mean only ever seeing half the answer. Deploying the same name adds a `deployed`
#: row beside the `one-off` one, carrying the schedule and version it now has.
ORIGIN_DEPLOYED = "deployed"
ORIGIN_ONE_OFF = "one-off"
#: Version source modes whose deployed code is pinned to the connected GitHub repository.
#: `project_repo` predates workspace-level connections but describes the same provenance.
GITHUB_SOURCE_MODES = frozenset({"workspace_repo", "project_repo"})
#: Runs scanned when grouping one-off runs into rows, and the ceiling on how many of a
#: single group's runs the runs table then lists.
EPHEMERAL_SCAN_LIMIT = 200
#: What the run-detail chips filter down to. Execution children stay beside one another,
#: followed by the two observability views. Steps, tasks and artifacts are hidden for a
#: run that has none; the other views are always useful ways into the run's own account.
TIMELINE_FILTERS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("timeline-all", "[ Activity ]", ("event", "step", "task", "artifact", "log")),
    ("timeline-steps", "[ Steps ]", ("step",)),
    ("timeline-tasks", "[ Tasks ]", ("task",)),
    ("timeline-artifacts", "[ Artifacts ]", ("artifact",)),
    # Events and Logs deliberately overlap: the stages are the run's own account of
    # itself and belong in both "what happened" and "everything it said".
    ("timeline-logs", "[ Logs ]", ("event", "log")),
    ("timeline-events", "[ Events ]", ("event",)),
)
#: Log severities not worth naming on the line. A log row's severity is deliberately kept
#: out of the Status column — that column means lifecycle for an event and outcome for a
#: step, and `INFO` is neither — so the level leads the message instead. At these levels it
#: would only repeat what the `log` type already says on every row; anything else (warnings,
#: errors, whatever a runtime invents) still names itself, in its own colour.
ROUTINE_LOG_SEVERITIES = frozenset({"INFO", "DEBUG", "NOTSET", "-", ""})
#: What to say when a filter has nothing to show, rather than leaving a blank table.
TIMELINE_EMPTY: dict[str, str] = {
    "timeline-events": "No lifecycle events recorded for this run.",
    "timeline-tasks": "No tasks reported for this run.",
    "timeline-artifacts": "No artifacts registered for this run.",
    "timeline-logs": "No log output recorded for this run.",
    "timeline-all": "Nothing recorded for this run yet.",
}
#: The rows a box keeps whatever you do to it: its column header. For the tabbed box that
#: is two rows, not one — the chip strip above the table costs a row of its own.
MIN_TABLE_HEIGHT = 1
MIN_TARGET_BOX_HEIGHT = 2
#: How many rows `+`/`-` move a box.
BOX_STEP = 2
#: How wide to wrap an expanded timeline row when the pane's width cannot be read,
#: and the narrowest it is worth wrapping to.
TIMELINE_WRAP_FALLBACK = 80
TIMELINE_WRAP_MINIMUM = 30
#: Log lines fetched per run. The API answers an empty list — not an error — somewhere
#: above 200, so this is a ceiling to respect rather than one to raise on a hunch.
RUN_LOG_LIMIT = 200
#: Fields too long to belong in the details drawer, and what to say instead.
ELIDED_DETAIL_KEYS = ("source_code",)


@dataclass(frozen=True)
class TableView:
    """Where the reader was inside one table, so a repaint can put them back."""

    cursor_key: str | None
    marked: tuple[str, ...]
    scroll_x: float
    scroll_y: float


@dataclass(frozen=True)
class ProjectSummary:
    project: dict[str, Any]
    function_count: int
    workflow_count: int
    endpoint_count: int = 0
    cron_count: int = 0
    last_run: dict[str, Any] | None = None
    next_run_at: str | None = None
    #: Config-derived rollup of the project's cron states — "active", "paused" or
    #: "stopped" — with None meaning the project has no crons at all. Run
    #: outcomes never feed into this; those belong to `last_run`.
    cron_status: str | None = None
    #: Soonest automatic resume among the paused crons, when one is timed.
    paused_until: str | None = None


@dataclass(frozen=True)
class OverviewCounts:
    functions: dict[str, int]
    workflows: dict[str, int]
    endpoints: dict[str, int]
    crons: dict[str, int]
    last_runs: dict[str, dict[str, Any]] = field(default_factory=dict)
    next_runs: dict[str, str] = field(default_factory=dict)
    cron_statuses: dict[str, str] = field(default_factory=dict)
    paused_untils: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkspaceOverviewData:
    projects: list[dict[str, Any]]
    project_summaries: list[ProjectSummary]
    project_names: dict[str, str]
    environments: list[dict[str, Any]] = field(default_factory=list)
    buckets: list[dict[str, Any]] = field(default_factory=list)
    volumes: list[dict[str, Any]] = field(default_factory=list)
    secrets: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class WorkspaceResources:
    """The environment siblings of Projects: buckets, volumes, secrets, environments.

    `volumes` is read but no longer drawn: the feature is experimental and its table came
    out of the workspace view until it is settled. Kept on the model so putting the view
    back is the tab, the columns and the render, and nothing else.

    Kept apart from `WorkspaceOverviewData` because they answer a different question and
    degrade independently — none of them is needed to draw the project table. They are
    read alongside it rather than after it; see `load_workspace_overview`.
    """

    environments: list[dict[str, Any]] = field(default_factory=list)
    buckets: list[dict[str, Any]] = field(default_factory=list)
    volumes: list[dict[str, Any]] = field(default_factory=list)
    secrets: list[dict[str, Any]] = field(default_factory=list)


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
class EphemeralGroup:
    """The one-off runs of a single unregistered target, as one row.

    Grouped by the name the run declared rather than by run, because a name is what the
    author re-runs: five `rebase run …::collect` invocations are one thing tried five
    times, and listing them as five rows would bury the deployed targets they sit next to.
    """

    name: str
    target_type: str
    run_type: str
    runs: int
    last_run: str | None

    @property
    def row_key(self) -> str:
        return f"{EPHEMERAL_ROW_PREFIX}{self.target_type}:{self.name}"


@dataclass(frozen=True)
class ProjectTargetsData:
    project: dict[str, Any]
    functions: list[dict[str, Any]]
    workflows: list[dict[str, Any]]
    endpoints: list[dict[str, Any]]
    #: Every step of every workflow in the project, workflow by workflow.
    steps: tuple[WorkflowStep, ...] = ()
    #: The deployed current version by workflow id. Besides the graph used above, this
    #: carries the GitHub source mode and exact commit the workflow is pinned to.
    workflow_versions: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: When each function last executed, by function id. See `load_last_runs`.
    last_runs: dict[str, str] = field(default_factory=dict)
    #: One entry per name that has only ever run one-off. See `group_ephemeral_runs`.
    ephemeral: tuple[EphemeralGroup, ...] = ()

    def steps_by_function(self) -> dict[str, list[WorkflowStep]]:
        grouped: dict[str, list[WorkflowStep]] = {}
        for step in self.steps:
            grouped.setdefault(step.function_id, []).append(step)
        return grouped

    def ephemeral_by_type(self, target_type: str) -> tuple[EphemeralGroup, ...]:
        return tuple(group for group in self.ephemeral if group.target_type == target_type)


@dataclass(frozen=True)
class RunDetailData:
    run: dict[str, Any]
    events: list[dict[str, Any]]
    steps: list[dict[str, Any]]
    #: The fan-out inside those steps, one row per unit of work.
    tasks: list[dict[str, Any]] = field(default_factory=list)
    #: Durable output pointers emitted by the run and its tasks.
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    #: The runtime's own output, which the Logs chip shows alongside the events.
    logs: list[dict[str, Any]] = field(default_factory=list)
    #: The workflow version's compiled DAG, which is where step *edges* live. A step run
    #: records which node it is but not what it waited for, so the Steps view reads its
    #: upstreams from here. None for a run with no reachable version.
    step_graph: dict[str, Any] | None = None


@dataclass(frozen=True)
class TimelineRow:
    """One line of a run's activity, with its declared execution lineage intact."""

    at: datetime | None
    stage: str
    status: str
    message: str
    kind: Literal["event", "step", "task", "artifact", "log"]
    #: The owning step/task path, or ``run`` when this record has no narrower owner.
    scope: str = "run"
    #: The complete API record behind task/artifact detail drawers.
    record: dict[str, Any] = field(default_factory=dict)
    #: Original pointer plus its browser-safe destination, for artifact rows only.
    artifact_uri: str | None = None
    artifact_id: str | None = None
    artifact_run_id: str | None = None
    url: str | None = None


def _optional_entries(load: Callable[[], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Log output is supplementary: a run with unreadable logs is still a run."""
    try:
        return load()
    except Exception:
        return []


def _optional_list(load: Callable[[], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    try:
        return load()
    except RebaseWorkflowError:
        return []


def step_dependencies(step_graph: dict[str, Any] | None) -> dict[str, list[str]]:
    """Each node's upstream steps, keyed by `node_key` and named the way rows are.

    The graph addresses nodes by `node_key` (`count_to`) while a step row shows the
    node's `name` (`count-to`), so the keys are translated here rather than leaving the
    reader to match one spelling against the other. An upstream with no node of its own
    keeps its raw key: naming it wrongly would be worse than showing it unresolved.

    Separate from `workflow_steps`, which reads the same graph for the targets view:
    that one keys by function and drops nodes without one, which is right for asking
    "which workflow calls this function" and wrong for labelling a step run's edges.
    """
    nodes = (step_graph or {}).get("nodes")
    if not isinstance(nodes, list):
        return {}
    names = {
        str(node["node_key"]): str(node.get("name") or node["node_key"])
        for node in nodes
        if isinstance(node, dict) and node.get("node_key")
    }
    dependencies: dict[str, list[str]] = {}
    for node in nodes:
        if not isinstance(node, dict) or not node.get("node_key"):
            continue
        upstream = node.get("upstream_node_keys")
        if not isinstance(upstream, list):
            continue
        dependencies[str(node["node_key"])] = [names.get(str(key), str(key)) for key in upstream]
    return dependencies


def _ephemeral_identity(run: dict[str, Any]) -> tuple[str, str] | None:
    """The (target_type, name) a one-off run ran as, or None if it is not one.

    `is_ephemeral` is the authority rather than a null `target_id`: a run can be missing
    a target for other reasons, and inferring "one-off" from absence would sweep those in
    under a name that was never declared.
    """
    if not run.get("is_ephemeral"):
        return None
    target = run.get("ephemeral_target")
    if not isinstance(target, dict):
        return None
    name = target.get("name")
    if not isinstance(name, str) or not name:
        return None
    target_type = run.get("target_type")
    return (target_type if isinstance(target_type, str) and target_type else "workflow", name)


def deployed_identities(
    workflows: Iterable[dict[str, Any]],
    functions: Iterable[dict[str, Any]] = (),
) -> set[tuple[str, str]]:
    """The (target_type, name) of every registered target, in `_ephemeral_identity`'s shape.

    Both tables are read because a one-off run's name is matched against whichever kind
    it ran as, and the two namespaces are independent: a workflow and a function may
    share a name without being the same thing.
    """
    identities = {("workflow", str(item["name"])) for item in workflows if item.get("name")}
    identities |= {("function", str(item["name"])) for item in functions if item.get("name")}
    return identities


def target_ids_by_identity(
    workflows: Iterable[dict[str, Any]],
    functions: Iterable[dict[str, Any]] = (),
) -> dict[tuple[str, str], str]:
    """Registered target ids keyed by (target_type, name).

    The inverse of `deployed_identities`, for attributing a one-off run — which carries a
    name but no target_id — to the row that name belongs to.
    """
    mapping = {
        ("workflow", str(item["name"])): str(item["id"]) for item in workflows if item.get("name") and item.get("id")
    }
    mapping |= {
        ("function", str(item["name"])): str(item["id"]) for item in functions if item.get("name") and item.get("id")
    }
    return mapping


def group_ephemeral_runs(
    runs: list[dict[str, Any]],
    deployed: Collection[tuple[str, str]] = (),
) -> tuple[EphemeralGroup, ...]:
    """One row per name that has *only* run one-off, most recently run first.

    Ordered by recency rather than by name because these rows sit under the deployed
    ones, where the useful question is what you ran last, not what it was called.

    `deployed` is the (target_type, name) of everything the project has registered, and
    those names are left out. A one-off run of a deployed target is the same workflow —
    `rebase run …::sync` against the code behind the scheduled `sync` — so giving it a
    second row said there were two `sync` workflows when there is one. It is not lost:
    `load_last_runs` folds its time into the deployed row's Last run, and the runs table
    tells the two apart per run in its Trigger column (`schedule` against `api`), which
    is the level the distinction actually lives at.
    """
    known = set(deployed)
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for run in runs:
        identity = _ephemeral_identity(run)
        if identity is None or identity in known:
            continue
        when = run.get("started_at") or run.get("created_at")
        entry = grouped.setdefault(identity, {"runs": 0, "last_run": None, "run_type": format_execution(run)})
        entry["runs"] += 1
        if isinstance(when, str) and when > (entry["last_run"] or ""):
            entry["last_run"] = when
    return tuple(
        EphemeralGroup(
            name=name,
            target_type=target_type,
            run_type=str(entry["run_type"] or "-"),
            runs=int(entry["runs"]),
            last_run=entry["last_run"],
        )
        for (target_type, name), entry in sorted(
            grouped.items(), key=lambda item: (item[1]["last_run"] or "", item[0][1]), reverse=True
        )
    )


class RebaseTuiData:
    def __init__(self, client: Client | None = None, *, project: str | None = None, limit: int = 100) -> None:
        self.client = client or Client()
        self.project = project
        self.limit = limit

    @property
    def environment_name(self) -> str:
        value = getattr(self.client, "environment_name", None)
        return value if isinstance(value, str) and value else "dev"

    def use_environment(self, name: str) -> None:
        """Move every subsequent TUI request into one environment."""
        switch = getattr(self.client, "with_environment", None)
        if callable(switch):
            self.client = switch(name)

    def _optional_resource_list(self, method: str) -> list[dict[str, Any]]:
        load = getattr(self.client, method, None)
        if not callable(load):
            return []
        try:
            result = load()
        except RebaseWorkflowError:
            return []
        return result if isinstance(result, list) else []

    def load_workspace_base(self) -> WorkspaceOverviewData:
        """Projects and every count the project table draws, in one wave.

        `list_projects` used to run to completion before the counts were even requested,
        because `_overview_counts` took the project list as an argument and never read
        it. Nothing in the counts depends on the projects — every one of those routes is
        workspace-wide and keyed by `project_id` — so both go out together and the wait
        is the slower of the two rather than their sum.

        The `--project` guard still raises, and still after both are back: a
        `ThreadPoolExecutor` waits for everything it was given on the way out of the
        `with` block, and a running thread cannot be interrupted, so checking earlier
        would not return any sooner.
        """
        with ThreadPoolExecutor(max_workers=2) as executor:
            projects_future = executor.submit(self.client.list_projects)
            counts_future = executor.submit(self._overview_counts)
        projects = projects_future.result()
        if self.project is not None and not any(project.get("name") == self.project for project in projects):
            raise RebaseWorkflowError(f"project not found: {self.project}")
        counts = counts_future.result()
        return WorkspaceOverviewData(
            projects=projects,
            project_summaries=self._summaries(projects, counts),
            project_names={str(project.get("id", "")): str(project.get("name", "-")) for project in projects},
        )

    def offers_secrets(self) -> bool:
        """Whether asking for secrets could return anything at all.

        A client older than the route, or a test double, simply has no `list_secrets`.
        Knowing that here costs nothing; finding it out inside a worker thread costs a
        thread on every load that will never have anything to show for it.
        """
        return callable(getattr(self.client, "list_secrets", None))

    def load_workspace_secrets(self) -> list[dict[str, Any]]:
        """The secrets table, on its own timeline.

        Its own, because it is the only part of the workspace view that is not a database
        query: Secret Manager answers in ~0.25s where everything else together takes
        ~0.1s. Degrades to empty like every other supplementary column.
        """
        return self._optional_resource_list("list_secrets")

    def load_workspace_resources(self) -> WorkspaceResources:
        """The four environment-sibling reads, issued together.

        Older servers and lightweight test clients may not expose all four routes, so
        each column degrades on its own instead of taking the others down with it.
        """
        with ThreadPoolExecutor(max_workers=4) as executor:
            environments = executor.submit(self._optional_resource_list, "list_environments")
            buckets = executor.submit(self._optional_resource_list, "list_buckets")
            volumes = executor.submit(self._optional_resource_list, "list_volumes")
            secrets = executor.submit(self._optional_resource_list, "list_secrets")
        return WorkspaceResources(
            environments=environments.result() or [{"name": self.environment_name}],
            buckets=buckets.result(),
            volumes=volumes.result(),
            secrets=secrets.result(),
        )

    def _summaries(self, projects: list[dict[str, Any]], counts: OverviewCounts) -> list[ProjectSummary]:
        return [
            ProjectSummary(
                project=project,
                function_count=counts.functions.get(str(project["id"]), 0),
                workflow_count=counts.workflows.get(str(project["id"]), 0),
                endpoint_count=counts.endpoints.get(str(project["id"]), 0),
                cron_count=counts.crons.get(str(project["id"]), 0),
                last_run=counts.last_runs.get(str(project["id"])),
                next_run_at=counts.next_runs.get(str(project["id"])),
                cron_status=counts.cron_statuses.get(str(project["id"])),
                paused_until=counts.paused_untils.get(str(project["id"])),
            )
            for project in projects
        ]

    def load_workspace_composite(self) -> WorkspaceOverviewData | None:
        """The whole workspace view in one request, where the platform offers the route.

        `None` when this client has no such method (an older pairing, or a test double)
        or the platform has no such route, so the caller falls back to the fan-out. That
        is the common case against a platform behind the toolkit, not a rare one.

        The payload is the raw lists rather than per-project counts, and the counts come
        from the same `_counts_from_rows` the fan-out uses — the cron-status rollup is
        real logic and belongs in one place.
        """
        load = getattr(self.client, "get_workspace_overview", None)
        if not callable(load):
            return None
        payload = load()
        if payload is None:
            return None
        projects = payload.get("projects") or []
        if self.project is not None and not any(project.get("name") == self.project for project in projects):
            raise RebaseWorkflowError(f"project not found: {self.project}")
        counts = self._counts_from_rows(
            workflows=payload.get("workflows") or [],
            functions=payload.get("functions") or [],
            endpoints=payload.get("endpoints") or [],
            latest_runs=payload.get("latest_runs_by_project") or [],
        )
        return WorkspaceOverviewData(
            projects=projects,
            project_summaries=self._summaries(projects, counts),
            project_names={str(project.get("id", "")): str(project.get("name", "-")) for project in projects},
            environments=payload.get("environments") or [{"name": self.environment_name}],
            buckets=payload.get("buckets") or [],
            volumes=payload.get("volumes") or [],
            # Deliberately absent from the payload: secrets are a Secret Manager call
            # rather than a query, and waiting on them would hold the project table back
            # for the one box nobody opens the TUI to read. `load_workspace_secrets`
            # fills them in after the paint.
        )

    def load_workspace_overview(self) -> WorkspaceOverviewData:
        """Everything behind the workspace view: one request where the route exists.

        Falls back to all nine reads in flight at once where it does not.

        Unlike `load_project_base`/`load_project_detail`, where the second phase genuinely
        needs the first one's answers, these two halves read disjoint things and neither
        waits on the other, so there is no reason to stage them.

        Staging the two halves was measured and rejected. Against the live API the base
        costs ~1.17s on its own and the resources ~0.81s, but the two together still cost
        ~1.17s — the API absorbs the extra four requests. Painting the project table
        first would have bought it nothing and pushed the resource tables out to ~2.0s.
        """
        composite = self.load_workspace_composite()
        if composite is not None:
            return composite
        with ThreadPoolExecutor(max_workers=2) as executor:
            base_future = executor.submit(self.load_workspace_base)
            resources_future = executor.submit(self.load_workspace_resources)
        base = base_future.result()
        resources = resources_future.result()
        return replace(
            base,
            environments=resources.environments,
            buckets=resources.buckets,
            volumes=resources.volumes,
            secrets=resources.secrets,
        )

    def _overview_counts(self) -> OverviewCounts:
        """Every count the project table shows: four workspace-wide calls, in parallel.

        Every one of these objects carries its own `project_id`, so each column is one
        request for the whole workspace rather than one per project. Functions used to be
        the exception — no workspace-wide route existed, so the overview issued a request
        per project purely to fill a column — serially, inside the client — and opening the
        workspace view got slower with every project added. `GET /functions` closed that;
        the client keeps a per-project fallback for an older API, so a toolkit ahead of its
        platform loses the speed rather than the column.
        """
        with ThreadPoolExecutor(max_workers=OVERVIEW_FANOUT_WORKERS) as executor:
            workflows = executor.submit(self.client.list_workflows)
            # Endpoints are supplementary here, as they are in load_project_targets: an API
            # without the route should cost the column, not the whole overview.
            endpoints = executor.submit(lambda: _optional_list(self.client.list_endpoints))
            functions = executor.submit(self.client.list_functions)
            # One row per project from the server. Assembling this client-side from
            # `list_runs` does not work: runs come back newest-first across the
            # workspace, so a project on a 15-minute cron fills any page size and
            # the quiet projects — the ones worth checking — drop off the end.
            last_runs = executor.submit(self.client.list_latest_runs_by_project)
        return self._counts_from_rows(
            workflows=workflows.result(),
            functions=functions.result(),
            endpoints=endpoints.result(),
            latest_runs=last_runs.result(),
        )

    @classmethod
    def _counts_from_rows(
        cls,
        *,
        workflows: list[dict[str, Any]],
        functions: list[dict[str, Any]],
        endpoints: list[dict[str, Any]],
        latest_runs: list[dict[str, Any]],
    ) -> OverviewCounts:
        """The counts themselves, over rows someone else read.

        Separate from the reads so the composite route and the per-route fan-out produce
        the same table from the same arithmetic. The cron-status rollup in particular is
        real logic, and having it in one place is the point.
        """
        workflow_counts, cron_counts, next_runs, cron_statuses, paused_untils = cls._workflow_and_cron_counts(workflows)
        return OverviewCounts(
            functions=cls._counts_by_project(functions),
            workflows=workflow_counts,
            endpoints=cls._counts_by_project(endpoints),
            crons=cron_counts,
            last_runs={str(run["project_id"]): run for run in latest_runs if run.get("project_id")},
            next_runs=next_runs,
            cron_statuses=cron_statuses,
            paused_untils=paused_untils,
        )

    @staticmethod
    def _workflow_and_cron_counts(
        workflows: list[dict[str, Any]],
    ) -> tuple[dict[str, int], dict[str, int], dict[str, str], dict[str, str], dict[str, str]]:
        """Workflows per project, their cron count, and the config-level cron status.

        All of it comes out of the one workspace-wide read, so no column costs a
        request of its own. A workflow counts as a cron job whenever it has a cron
        schedule configured, in any state — a paused or stopped cron is still a cron
        job; the Status column carries its liveness. That column is the config-only
        rollup of `workflow_cron_state` (run outcomes stay with Last run): a project
        is "active" while any cron will fire, "paused" when the best of them is
        paused, "stopped" when every configured cron is switched off.
        """
        # The soonest fire time per project, out of the same read the counts come
        # from — so the column costs no request of its own. Earliest wins: with
        # several schedules in a project, the next thing to happen is the answer.
        next_runs: dict[str, str] = {}
        states: dict[str, list[str]] = {}
        resume_times: dict[str, list[str]] = {}
        for item in workflows:
            project_id = str(item.get("project_id") or "")
            if not project_id:
                continue
            next_run_at = item.get("next_run_at")
            if next_run_at:
                current = next_runs.get(project_id)
                if current is None or str(next_run_at) < current:
                    next_runs[project_id] = str(next_run_at)
            state = workflow_cron_state(item)
            if state is None:
                continue
            states.setdefault(project_id, []).append(state)
            if state == "paused" and item.get("paused_until"):
                resume_times.setdefault(project_id, []).append(str(item["paused_until"]))
        cron_statuses = {
            project_id: next(state for state in ("active", "paused", "stopped") if state in project_states)
            for project_id, project_states in states.items()
        }
        paused_untils = {
            project_id: min(times)
            for project_id, times in resume_times.items()
            if cron_statuses.get(project_id) == "paused"
        }
        return (
            Counter(str(item["project_id"]) for item in workflows if item.get("project_id")),
            {project_id: len(project_states) for project_id, project_states in states.items()},
            next_runs,
            cron_statuses,
            paused_untils,
        )

    @staticmethod
    def _counts_by_project(rows: list[dict[str, Any]]) -> dict[str, int]:
        return Counter(str(item["project_id"]) for item in rows if item.get("project_id"))

    def load_project_composite(self, project: dict[str, Any]) -> ProjectTargetsData | None:
        """Everything behind opening a project in one request, where the route exists.

        `None` when the client or the platform predates the route, so the caller falls
        back to the two-phase fan-out.

        This is where the composite route earns the most. Read separately, the current
        version of every workflow is a request each and the step rows behind Last run are
        a request per scanned run, so opening a project cost a round trip for every
        target in it.
        """
        load = getattr(self.client, "get_project_overview", None)
        if not callable(load):
            return None
        payload = load(str(project["id"]))
        if payload is None:
            return None
        workflows = payload.get("workflows") or []
        functions = payload.get("functions") or []
        runs = payload.get("runs") or []
        versions = {
            str(workflow_id): version
            for workflow_id, version in (payload.get("current_workflow_versions") or {}).items()
        }
        return ProjectTargetsData(
            project=payload.get("project") or project,
            functions=functions,
            workflows=workflows,
            endpoints=payload.get("endpoints") or [],
            steps=self._workflow_steps_from_versions(workflows, versions),
            workflow_versions=versions,
            last_runs=self.load_last_runs(
                str(project["id"]),
                runs,
                target_ids_by_identity(workflows, functions),
                step_runs=payload.get("step_runs") or [],
            ),
            ephemeral=group_ephemeral_runs(runs, deployed_identities(workflows, functions)),
        )

    def load_project_targets(self, project: dict[str, Any]) -> ProjectTargetsData:
        """Everything behind opening a project, for callers that want it in one piece."""
        composite = self.load_project_composite(project)
        if composite is not None:
            return composite
        base, runs = self.load_project_base(project)
        return self.load_project_detail(base, runs)

    def load_project_base(self, project: dict[str, Any]) -> tuple[ProjectTargetsData, list[dict[str, Any]]]:
        """The four reads that depend on nothing, issued together.

        These used to be four statements, which made them four round trips: the argument
        list of a constructor is evaluated in order, so `list_functions` waited on
        `list_runs` for no reason other than where it was written. Nothing here needs
        anything from the others, and the API answers concurrent reads in the time of the
        slowest one, so together they cost one round trip instead of four.

        Returns the runs alongside the targets because `load_project_detail` needs them
        and they are the most expensive read here — asking twice would give the second
        phase its own round trip and undo the point.
        """
        project_id = str(project["id"])
        with ThreadPoolExecutor(max_workers=4) as executor:
            workflows = executor.submit(self.client.list_workflows, project_id=project_id)
            functions = executor.submit(self.client.list_functions, project_id=project_id)
            # Supplementary data: never let it take down the function/workflow view.
            endpoints = executor.submit(lambda: _optional_list(lambda: self.client.list_project_endpoints(project_id)))
            runs = executor.submit(
                lambda: _optional_list(lambda: self.client.list_runs(project_id=project_id, limit=EPHEMERAL_SCAN_LIMIT))
            )
        run_rows = runs.result()
        workflow_rows = workflows.result()
        function_rows = functions.result()
        return (
            ProjectTargetsData(
                project=project,
                functions=function_rows,
                workflows=workflow_rows,
                endpoints=endpoints.result(),
                # Grouping one-off runs is local work on the runs already read, so the
                # rows it produces are there from the first paint. The deployed names go
                # in so a one-off run of a deployed target folds into that target's row
                # instead of becoming a second row with the same name.
                ephemeral=group_ephemeral_runs(run_rows, deployed_identities(workflow_rows, function_rows)),
            ),
            run_rows,
        )

    def load_project_detail(self, base: ProjectTargetsData, runs: list[dict[str, Any]]) -> ProjectTargetsData:
        """The two reads that needed the first phase's answers, also issued together.

        Step graphs need the workflows and last-run times need the runs, but neither
        needs the other.
        """
        with ThreadPoolExecutor(max_workers=2) as executor:
            versions = executor.submit(self.load_workflow_versions, base.workflows)
            last_runs = executor.submit(
                self.load_last_runs,
                str(base.project["id"]),
                runs,
                target_ids_by_identity(base.workflows, base.functions),
            )
        resolved_versions = versions.result()
        return replace(
            base,
            steps=self._workflow_steps_from_versions(base.workflows, resolved_versions),
            workflow_versions=resolved_versions,
            last_runs=last_runs.result(),
        )

    def load_ephemeral_runs(self, name: str, target_type: str, project_id: str) -> list[dict[str, Any]]:
        """The one-off runs that ran under *name*, newest first.

        Filtered here rather than by the API: `/runs` selects on `target_id`, which is
        exactly what these runs do not have. The name lives in `ephemeral_target`, a
        column the route does not filter on, so the project's runs are read and matched
        locally.
        """
        runs = self.client.list_runs(project_id=project_id, limit=EPHEMERAL_SCAN_LIMIT)
        return [run for run in runs if _ephemeral_identity(run) == (target_type, name)]

    def load_last_runs(
        self,
        project_id: str,
        runs: list[dict[str, Any]] | None = None,
        target_ids: dict[tuple[str, str], str] | None = None,
        step_runs: list[dict[str, Any]] | None = None,
    ) -> dict[str, str]:
        """When each workflow and function last executed, by target id.

        Keyed on the run's `target_id`, not `function_id` or `workflow_id`: those two
        come back null on the list route, so keying on them finds nothing. Ids do not
        collide across kinds, so one mapping serves both tables.

        Two sources, because a function is executed two different ways. A workflow or a
        standalone function gets its own run, so the project's run list answers for those
        in one request. A *step* never does — its executions live in `step_runs`, which
        the API exposes only per run — so this also reads the step rows of the most recent
        workflow runs, capped at `LAST_RUN_STEP_SCAN` requests.

        That cap is the honest limit of this column: a step that last ran longer ago than
        the scanned window shows no time rather than a wrong one. Supplementary like the
        endpoint list — a run history that will not load must not cost you the table.

        An ephemeral run carries a name instead of a target_id. Given `target_ids` it is
        attributed to the row of the same name, so `rebase run …::sync` updates the
        deployed `sync` row's Last run — it is that workflow being run, just triggered by
        hand rather than by the schedule. Without the map it is skipped, and a name that
        matches nothing deployed still is: that one gets its own row from
        `group_ephemeral_runs`.

        Takes `runs` when the caller has already read them, so the project view pays for
        the project's run list once rather than once per thing derived from it. Takes
        `step_runs` on the same terms: the composite route returns them with everything
        else, and reading them here would undo the point of asking once.
        """
        if runs is None:
            runs = _optional_list(lambda: self.client.list_runs(project_id=project_id, limit=LAST_RUN_SCAN_LIMIT))
        latest: dict[str, str] = {}

        def record(target_id: Any, when: Any) -> None:
            if not isinstance(target_id, str) or not isinstance(when, str) or not when:
                return
            if when > latest.get(target_id, ""):
                latest[target_id] = when

        workflow_run_ids: list[str] = []
        for run in runs:
            when = run.get("started_at") or run.get("created_at")
            record(run.get("target_id"), when)
            # Ephemeral runs have no target_id, so `record` above did nothing for them.
            # Route them to the deployed row of the same name when there is one.
            if run.get("is_ephemeral") and target_ids:
                identity = _ephemeral_identity(run)
                if identity is not None:
                    record(target_ids.get(identity), when)
            # Their steps are not registered functions either, so opening them spends one
            # of a capped number of requests to attribute a time to a row that does not
            # exist.
            if run.get("is_ephemeral"):
                continue
            if run.get("target_type") == "workflow" and isinstance(run.get("id"), str):
                workflow_run_ids.append(str(run["id"]))

        if step_runs is None:
            scanned = workflow_run_ids[:LAST_RUN_STEP_SCAN]
            step_runs = []
            if scanned:
                with ThreadPoolExecutor(max_workers=min(OVERVIEW_FANOUT_WORKERS, len(scanned))) as executor:
                    for steps in executor.map(
                        lambda run_id: _optional_list(lambda: self.client.list_run_steps(run_id)), scanned
                    ):
                        step_runs.extend(steps)
        for step in step_runs:
            record(step.get("function_id"), step.get("started_at") or step.get("created_at"))
        return latest

    def load_workflow_versions(self, workflows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """The current deployed version of every workflow, read concurrently.

        Both the step graph and source provenance live on the version rather than the
        workflow, so one response fills the function grouping and the workflow's Source
        and Commit columns. Like the endpoint list it is supplementary: an old API, a
        workflow with no current version, or one failed read costs those cells only.
        """
        versioned = [
            (str(workflow["id"]), str(workflow["current_version_id"]))
            for workflow in workflows
            if workflow.get("id") is not None and workflow.get("current_version_id")
        ]
        if not versioned:
            return {}

        def load(entry: tuple[str, str]) -> tuple[str, dict[str, Any] | None]:
            workflow_id, version_id = entry
            try:
                version = self.client.get_workflow_version(workflow_id, version_id)
            except Exception:
                return workflow_id, None
            return workflow_id, version

        with ThreadPoolExecutor(max_workers=min(STEP_GRAPH_FANOUT_WORKERS, len(versioned))) as executor:
            return {
                workflow_id: version for workflow_id, version in executor.map(load, versioned) if version is not None
            }

    def load_workflow_steps(self, workflows: list[dict[str, Any]]) -> tuple[WorkflowStep, ...]:
        """Compatibility helper for callers that only need the deployed step graphs."""
        return self._workflow_steps_from_versions(workflows, self.load_workflow_versions(workflows))

    @staticmethod
    def _workflow_steps_from_versions(
        workflows: list[dict[str, Any]], versions: dict[str, dict[str, Any]]
    ) -> tuple[WorkflowStep, ...]:
        steps: list[WorkflowStep] = []
        for workflow in workflows:
            workflow_id = str(workflow.get("id") or "")
            version = versions.get(workflow_id)
            if version is None:
                continue
            steps.extend(workflow_steps(workflow_id, str(workflow.get("name", "-")), version.get("step_graph")))
        return tuple(steps)

    def load_target_runs(
        self,
        target_type: TargetType,
        target_id: str,
        *,
        name: str | None = None,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Every run of this target, registered and one-off alike, newest first.

        `/runs` selects on target_id, which a one-off run does not have — it carries the
        name it ran as instead. So a workflow every one of whose runs was a `rebase run
        …::sync` matched nothing here and the table came up empty, while the Last run
        column beside it showed a time: the same runs, found by name there and by id
        here. Given a name and project, those runs are read and merged in.
        """
        if target_type == "function":
            registered = self.client.list_runs(function_id=target_id, target_type="function", limit=self.limit)
        else:
            registered = self.client.list_runs(workflow_id=target_id, target_type="workflow", limit=self.limit)
        if not name or not project_id:
            return registered

        # Supplementary, like every other by-name read: a target's own runs must not
        # disappear because the scan for one-off ones failed.
        ephemeral = _optional_list(lambda: self.load_ephemeral_runs(name, target_type, project_id))
        if not ephemeral:
            return registered

        merged = registered + ephemeral
        merged.sort(key=lambda run: str(run.get("created_at") or ""), reverse=True)
        return merged[: self.limit]

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
            # Logs used to be fetched only when asked for. They come with the run now, so
            # the Logs chip is instant -- and it costs nothing, being one more request
            # alongside four rather than one after them.
            logs = executor.submit(_optional_entries, lambda: self.load_run_logs(run_id))
            # Supplementary, like the endpoint list: an API without the route costs the
            # rows and not the run view.
            wanted_steps = target_type in (None, "workflow")
            steps = (
                executor.submit(_optional_list, lambda: self.client.list_run_steps(run_id)) if wanted_steps else None
            )
            tasks = executor.submit(_optional_list, lambda: self.client.list_run_tasks(run_id))
            artifacts = executor.submit(_optional_list, lambda: self.load_run_artifacts(run_id))
            resolved = run.result()
            is_workflow = resolved.get("target_type") == "workflow"
            return RunDetailData(
                run=resolved,
                events=events.result(),
                steps=steps.result() if steps is not None and is_workflow else [],
                tasks=tasks.result(),
                artifacts=artifacts.result(),
                logs=logs.result(),
                step_graph=self.load_step_graph(resolved) if is_workflow else None,
            )

    def load_step_graph(self, run: dict[str, Any]) -> dict[str, Any] | None:
        """The compiled DAG behind a workflow run, for naming each step's dependencies.

        Not part of the fan-out above, because it cannot be: the version id it needs is
        an answer from the run request itself. That makes it one extra hop when a
        workflow run is opened — and only then. Supplementary like the artifact list, so
        a server that cannot answer costs the `Depends on` column and not the run view.
        """
        workflow_id = run.get("target_id")
        version_id = run.get("target_version_id")
        load = getattr(self.client, "get_workflow_version", None)
        if not (workflow_id and version_id and callable(load)):
            return None
        try:
            version = load(str(workflow_id), str(version_id))
        except Exception:
            return None
        graph = version.get("step_graph") if isinstance(version, dict) else None
        return graph if isinstance(graph, dict) else None

    def load_run_logs(self, run_id: str) -> list[dict[str, Any]]:
        entries = self.client.get_run_logs(run_id, limit=RUN_LOG_LIMIT).get("entries")
        return entries if isinstance(entries, list) else []

    def load_run_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        """Compatibility seam for TUI test clients and pre-artifact client builds."""
        load = getattr(self.client, "list_run_artifacts", None)
        return load(run_id) if callable(load) else []


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


def artifact_browser_url(uri: str) -> str | None:
    """Turn a durable artifact URI into a safe destination for the user's browser."""
    value = uri.strip()
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value
    if parsed.scheme != "gs" or not parsed.netloc or not parsed.path.lstrip("/"):
        return None
    bucket = quote(parsed.netloc, safe="")
    object_name = quote(parsed.path.lstrip("/"), safe="/")
    return f"https://console.cloud.google.com/storage/browser/_details/{bucket}/{object_name}"


def bucket_console_url(bucket: dict[str, Any]) -> str | None:
    """Where `o` sends the browser for a bucket row.

    The API reports `console_url` outright, project query parameter and all.
    `uri` is the fallback for a server that predates that field: the bucket
    browser needs nothing but the name, and the console resolves the project
    against whichever one the reader had open.
    """
    reported = str(bucket.get("console_url") or "").strip()
    if reported.startswith("https://"):
        return reported
    parsed = urlsplit(str(bucket.get("uri") or "").strip())
    if parsed.scheme != "gs" or not parsed.netloc:
        return None
    return f"https://console.cloud.google.com/storage/browser/{quote(parsed.netloc, safe='')}"


def build_timeline(
    events: Sequence[dict[str, Any]],
    steps: Sequence[dict[str, Any]],
    logs: Sequence[dict[str, Any]] | None,
    tasks: Sequence[dict[str, Any]] = (),
    artifacts: Sequence[dict[str, Any]] = (),
) -> list[TimelineRow]:
    """Everything that happened during a run, in the order it happened.

    Events, steps, tasks, artifacts and log lines arrive from separate routes. Their
    timestamps put them into one readable account of the run. The explicit step/task
    foreign keys build a scope path; timestamps decide order, never parentage.
    """
    step_names = {
        str(step["id"]): str(step.get("name") or step.get("node_key") or step["id"])
        for step in steps
        if step.get("id") is not None
    }
    task_names = {
        str(task["id"]): str(task.get("name") or f"task {task.get('item_index', '-')}")
        for task in tasks
        if task.get("id") is not None
    }
    task_steps = {
        str(task["id"]): str(task["step_run_id"])
        for task in tasks
        if task.get("id") is not None and task.get("step_run_id") is not None
    }

    def task_scope(task: dict[str, Any]) -> str:
        step_id = task.get("step_run_id")
        return step_names.get(str(step_id), f"step {compact_id(step_id)}") if step_id is not None else "run"

    def artifact_scope(artifact: dict[str, Any]) -> str:
        task_id = artifact.get("task_id")
        step_id = artifact.get("step_run_id")
        if step_id is None and task_id is not None:
            step_id = task_steps.get(str(task_id))
        parts: list[str] = []
        if step_id is not None:
            parts.append(step_names.get(str(step_id), f"step {compact_id(step_id)}"))
        if task_id is not None:
            parts.append(task_names.get(str(task_id), f"task {compact_id(task_id)}"))
        return " › ".join(parts) or "run"

    rows = [
        TimelineRow(
            at=_parse_timestamp(event.get("created_at")),
            stage=str(event.get("stage", "-")),
            status=str(event.get("status", "-")),
            message=format_json_summary(event.get("message"), max_length=200),
            kind="event",
            record=dict(event),
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
                record=dict(step),
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
                stage=str(task.get("name") or f"task {task.get('item_index', '-')}"),
                status=str(task.get("status", "-")),
                message=f"{format_json_summary(task.get('parameters'), max_length=90)} -> {detail}",
                kind="task",
                scope=task_scope(task),
                record=dict(task),
            )
        )
    for artifact in artifacts:
        media_type = str(artifact.get("media_type") or "-")
        # An artifact points either at an absolute URI or at a logical bucket object.
        # The bucket form has no browsable location until the API resolves it, so it is
        # shown as its logical pointer and only turned into a destination on `a`.
        bucket = artifact.get("bucket")
        object_key = artifact.get("object_key")
        uri = (
            f"rb://bucket/{bucket}/{str(object_key).lstrip('/')}"
            if bucket and object_key
            else str(artifact.get("uri") or "-")
        )
        rows.append(
            TimelineRow(
                at=_parse_timestamp(artifact.get("created_at")),
                stage=str(artifact.get("name") or "artifact"),
                status=str(artifact.get("disposition") or "created"),
                message=f"{media_type} · {uri}",
                kind="artifact",
                scope=artifact_scope(artifact),
                record=dict(artifact),
                artifact_uri=uri,
                artifact_id=str(artifact.get("id")) if artifact.get("id") else None,
                artifact_run_id=str(artifact.get("workflow_run_id")) if artifact.get("workflow_run_id") else None,
                url=artifact_browser_url(uri),
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
                record=dict(entry),
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


def format_bytes(value: Any) -> str:
    """A compact artifact size, leaving absent or invalid values visibly unknown."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return "-"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "-"


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


def is_github_backed(version: dict[str, Any] | None) -> bool:
    """Whether a deployed version is pinned to source in the connected GitHub repo."""
    return isinstance(version, dict) and version.get("source_mode") in GITHUB_SOURCE_MODES


def format_workflow_source(version: dict[str, Any] | None) -> str:
    """The source-of-truth label for a deployed workflow version."""
    if is_github_backed(version):
        return "GitHub"
    if isinstance(version, dict) and version.get("source_mode") == "rebase_hosted":
        return "Rebase"
    return "-"


def format_workflow_commit(version: dict[str, Any] | None, *, compact: bool = True) -> str:
    """The Git commit a GitHub-backed workflow is deployed from."""
    if not is_github_backed(version):
        return "-"
    commit = version.get("git_commit_sha") if version is not None else None
    if not isinstance(commit, str) or not commit:
        return "-"
    return compact_id(commit) if compact else commit


def github_workflow_source_url(version: dict[str, Any] | None, *, line: int | None = None) -> str | None:
    """A GitHub blob URL pinned to the exact source commit of a workflow version."""
    if not is_github_backed(version):
        return None
    owner = version.get("repo_owner") if version is not None else None
    repo = version.get("repo_name") if version is not None else None
    commit = version.get("git_commit_sha") if version is not None else None
    source_path = version.get("source_path") if version is not None else None
    if not all(isinstance(value, str) and value for value in (owner, repo, commit, source_path)):
        return None
    normalized_path = str(source_path).replace("\\", "/").lstrip("/")
    if not normalized_path or ".." in normalized_path.split("/"):
        return None
    url = (
        f"https://github.com/{quote(str(owner), safe='')}/{quote(str(repo), safe='')}"
        f"/blob/{quote(str(commit), safe='')}/{quote(normalized_path, safe='/')}"
    )
    return f"{url}#L{line}" if isinstance(line, int) and line > 0 else url


def workflow_definition_line(source: str, entrypoint: str, deployed_source: str | None = None) -> int | None:
    """Locate an entrypoint in a complete source file without importing or executing it.

    When a name is defined more than once, the standalone source stored on the deployed
    version identifies which definition was actually shipped. With no unambiguous match,
    returning no line is safer than opening GitHub at the wrong function.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return None
    candidates = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == entrypoint
    ]
    if deployed_source:
        expected = textwrap.dedent(deployed_source).strip()
        matching = [
            node
            for node in candidates
            if (segment := ast.get_source_segment(source, node)) is not None
            and textwrap.dedent(segment).strip() == expected
        ]
        if len(matching) == 1:
            return matching[0].lineno
    return candidates[0].lineno if len(candidates) == 1 else None


def _git_source_at_commit(repo: Path, commit: str, source_path: str) -> str | None:
    try:
        completed = subprocess.run(  # noqa: S603
            ["git", "show", f"{commit}:{source_path}"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return completed.stdout


def workflow_definition_line_at_commit(version: dict[str, Any], roots: Iterable[Path]) -> int | None:
    """Resolve the definition line from a local checkout's exact committed Git object."""
    commit = version.get("git_commit_sha")
    source_path = version.get("source_path")
    entrypoint = version.get("entrypoint")
    if not all(isinstance(value, str) and value for value in (commit, source_path, entrypoint)):
        return None
    commit = str(commit)
    if not 7 <= len(commit) <= 64 or any(character not in "0123456789abcdefABCDEF" for character in commit):
        return None
    source_path = str(source_path).replace("\\", "/").lstrip("/")
    if not source_path or ".." in source_path.split("/"):
        return None

    seen: set[Path] = set()
    for root in roots:
        candidate = Path(root).expanduser()
        if not candidate.is_dir():
            continue
        repo = git_toplevel(candidate)
        if repo is None or repo in seen:
            continue
        seen.add(repo)
        source = _git_source_at_commit(repo, commit, source_path)
        if source is not None:
            deployed_source = version.get("source_code")
            return workflow_definition_line(
                source,
                str(entrypoint),
                deployed_source if isinstance(deployed_source, str) else None,
            )
    return None


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


def format_countdown(value: Any, now: datetime | None = None) -> str:
    """Render how long until *value*, as `in 2d 3h` / `in 3h 04m` / `in 4m 09s` / `in 12s`.

    A wall-clock "next run" is a date you have to subtract today's from before it means
    anything; the same fact as a countdown is read at a glance, and it ticks — see
    `RebaseTuiApp._tick_countdowns`. Two units at most: past the hour, the seconds are
    noise, and the whole thing has to stay inside `COUNTDOWN_WIDTH`.

    A time that has passed reads `due` rather than a negative number: the schedule says a
    run is owed, and the row will say so until the next refresh brings a later one.
    """
    if value in {None, ""}:
        return "-"
    if isinstance(value, datetime):
        target = value
    else:
        raw = str(value)
        try:
            target = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw
    # A timestamp with no offset is the API's UTC, the same assumption `_time` makes by
    # rendering it unconverted.
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)
    seconds = int((target - (now or datetime.now(UTC))).total_seconds())
    if seconds <= 0:
        return "due"
    minutes, secs = divmod(seconds, 60)
    hours, mins = divmod(minutes, 60)
    days, hrs = divmod(hours, 24)
    if days:
        return f"in {days}d {hrs}h"
    if hrs:
        return f"in {hrs}h {mins:02d}m"
    if mins:
        return f"in {mins}m {secs:02d}s"
    return f"in {secs}s"


def format_bool(value: Any) -> str:
    if value is True:
        return "enabled"
    if value is False:
        return "disabled"
    return "-"


def format_execution(value: dict[str, Any]) -> str:
    mode = value.get("mode")
    isolation = value.get("isolation")
    if mode is not None:
        return f"{mode}/{isolation}" if isolation is not None else str(mode)
    return str(value.get("run_type") or "-")


def format_schedule(value: Any, *, paused: bool = False) -> str:
    if not isinstance(value, dict):
        return "-"
    cron = str(value.get("cron") or "-")
    if not value.get("active", True) or paused:
        return f"{cron} ⏸"
    return cron


def _pause_in_effect(paused_until: Any) -> bool:
    """Whether a pause with this expiry still holds. No expiry means indefinite."""
    if not paused_until:
        return True
    try:
        until = datetime.fromisoformat(str(paused_until).replace("Z", "+00:00"))
    except ValueError:
        return True
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    return datetime.now(UTC) < until


def workflow_cron_state(workflow: dict[str, Any]) -> str | None:
    """One workflow's cron state as the user configured it, or None without a cron.

    Purely config-derived — "active", "paused" or "stopped" — so a cron that fires
    and crashes every time is still active: nobody has stopped it. Run outcomes
    belong to the Last run column, not here.
    """
    schedule = workflow.get("schedule")
    if not isinstance(schedule, dict) or schedule.get("type", "cron") != "cron":
        return None
    if workflow.get("enabled") is False or not schedule.get("active", True):
        return "stopped"
    if workflow.get("paused") and _pause_in_effect(workflow.get("paused_until")):
        return "paused"
    # Active flags but no computed fire time means the platform cannot run it —
    # a disabled version or an unusable cron expression: configured, not firing.
    return "active" if workflow.get("next_run_at") else "stopped"


def cron_status_text(status: str | None, paused_until: str | None = None) -> Text:
    if status is None:
        return Text("-")
    if status == "active":
        return Text("active", style=BRAND_MAIN_GREEN)
    if status == "paused":
        label = f"paused → {str(paused_until)[:10]}" if paused_until else "paused"
        return Text(label, style=BRAND_AMBER)
    return Text("stopped", style=BRAND_CORAL_RED)


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


def collapse_message(message: str) -> str:
    """One line standing in for a possibly many-line message.

    A row is one line high, so a multi-line message showed its first line and hid the
    rest — and a program whose output began with a newline got a row that was simply
    blank, which reads as "nothing was logged" rather than "press enter". A block of
    captured application output usually does begin with one.

    So the stand-in is the first line with something on it, and a count of what is
    waiting behind it. Expanding the row still shows the whole thing.
    """
    lines = message.splitlines()
    if len(lines) <= 1:
        return message
    first = next((line for line in lines if line.strip()), "")
    hidden = len([line for line in lines if line.strip()]) - 1
    return f"{first}  (+{hidden} more)" if hidden > 0 else first


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


class HeaderSafeDataTable(DataTable):
    """A DataTable that survives a click on the header of an empty table.

    Textual's header-click handler reads `self.ordered_columns[column_index]` without
    checking the list is not empty, so clicking the header of a table with no columns
    raises IndexError and takes the app down. It even records the click as
    `out_of_bounds` in its own metadata first, then indexes anyway.

    Every table here can legitimately be in that state: columns arrive with the rows (see
    `_fill_table`), so an unfilled table has none while still drawing a header row — the
    empty band above a box that has not loaded yet, or one that was cleared on the way out
    of a project. Clicking there is an ordinary thing to do, and it should do nothing.
    """

    #: Cells one wheel-left/right notch moves. Textual's own 4 is a mouse wheel's tilt
    #: switch, where notches arrive one at a time; a trackpad swipe arrives as a burst of
    #: them and at 4 cells each it throws the table end to end.
    HORIZONTAL_SCROLL_CELLS = 2

    def _on_click(self, event: events.Click) -> None:
        if self.ordered_columns:
            return
        # `prevent_default` rather than `stop`: Textual invokes `_on_click` for every class
        # in the MRO, so the base handler — the one that indexes — runs after this unless it
        # is suppressed. `stop` only ends the bubble to parent widgets, which is not where
        # the crash is. No `super()` call for the same reason: Textual makes it itself.
        event.prevent_default()
        event.stop()

    def _on_mouse_scroll_left(self, event: events.MouseScrollLeft) -> None:
        self._scroll_sideways(-self.HORIZONTAL_SCROLL_CELLS, event)

    def _on_mouse_scroll_right(self, event: events.MouseScrollRight) -> None:
        self._scroll_sideways(self.HORIZONTAL_SCROLL_CELLS, event)

    def _scroll_sideways(self, cells: float, event: events.MouseEvent) -> None:
        """Move a two-finger swipe (or a wheel tilt) along the table's own scrollbar.

        Textual scrolls horizontally on these events already, but animated and four cells
        a notch — fine for a wheel, and a slide that lags the fingers under a burst from a
        trackpad. This is the rule it uses for the vertical wheel instead: a small step,
        applied immediately. An event at the end of the travel is left to bubble, so the
        swipe carries on to whatever is behind the table rather than dying on it.
        """
        if not self.allow_horizontal_scroll:
            return
        # `prevent_default` for the same reason as `_on_click` above: Textual runs the
        # handler of every class in the MRO, so without it its own four-cell animated
        # scroll happens as well and a notch moves the table six cells, not two.
        event.prevent_default()
        target = max(0.0, min(float(self.max_scroll_x), self.scroll_target_x + cells))
        if target == self.scroll_target_x:
            return
        self.scroll_to(x=target, animate=False)
        event.stop()


class ChipSteppingTable(HeaderSafeDataTable):
    """A table sitting under a chip strip, whose arrows step between those chips.

    The gesture is the same wherever there are chips — `left`/`right` move the strip
    above whichever table holds the focus — so it lives on one class the tables under a
    strip share rather than being re-bound per table. Bound here rather than on the app
    because an app-level binding would have to be `priority` to beat DataTable's own
    inert cursor_left/cursor_right — and a priority binding on an arrow key takes it
    away from every Input in every dialog too.
    """

    BINDINGS = [
        Binding("left", "app.switch_chip_tab(-1)", "Previous tab", show=False),
        Binding("right", "app.switch_chip_tab(1)", "Next tab", show=False),
    ]


class DragHeaderTable(HeaderSafeDataTable):
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


class TimelineTable(DragHeaderTable):
    """The timeline: its header drags the boundary above, its arrows switch its chips.

    Same rule as the target tables — `left`/`right` step between the chips of whichever
    pane holds the focus — so the timeline inherits the gesture rather than inventing one.
    """

    BINDINGS = [
        Binding("left", "app.switch_timeline_filter(-1)", "Previous filter", show=False),
        Binding("right", "app.switch_timeline_filter(1)", "Next filter", show=False),
    ]


class DragStrip(Widget):
    """The mouse handlers that make a chip strip a splitter as well.

    The chip strips sit on the pane boundaries just as the `DragHeaderTable` headers
    under them do, so they take the same grab — anywhere along the row, chip or gap.
    The complication is that a strip is also there to be *clicked*: capturing the mouse
    on the press means the screen routes the release, and the `Click` Textual
    synthesises from it, back here rather than to the chip under the pointer. So the
    click is rebuilt by hand — a press that never left its row, released over the chip
    it started on, posts the `Tab.Clicked` the capture swallowed.
    """

    #: The box a drag on this strip resizes, as a selector for `begin_box_drag`.
    resizes: str = ""

    _pressed_tab: Tab | None = None
    _grab_row = 0
    _strip_dragged = False

    def on_mouse_down(self, event: events.MouseDown) -> None:
        # Mouse events bubble up from everything inside a `TabbedContent` — the tables
        # included — still carrying the child's own coordinates, so only the screen row
        # can say whether the press was on the strip itself.
        if self._screen_y(event) != self.region.y:
            return
        self._pressed_tab = self._tab_at(event)
        self._grab_row = self._screen_y(event)
        self._strip_dragged = False
        self.app.begin_box_drag(self.resizes, self._grab_row)  # type: ignore[attr-defined]
        self.capture_mouse()
        # The screen armed a text selection before this handler saw the press — the
        # strip's gaps, unlike a DataTable, allow selecting — and mid-drag that armed
        # selection auto-scrolls whichever table the pointer crosses. A grab on a
        # splitter is a resize, never a selection, so disarm it.
        self.screen.clear_selection()
        event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self.app.mouse_captured is not self:
            return
        y = self._screen_y(event)
        self._strip_dragged = self._strip_dragged or y != self._grab_row
        self.app.drag_box_to(y)  # type: ignore[attr-defined]
        event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self.app.mouse_captured is not self:
            return
        self.app.end_box_drag()  # type: ignore[attr-defined]
        self.release_mouse()
        event.stop()
        pressed, self._pressed_tab = self._pressed_tab, None
        if pressed is None or self._strip_dragged or pressed.disabled:
            return
        if self._tab_at(event) is pressed:
            pressed.post_message(Tab.Clicked(pressed))

    def _tab_at(self, event: events.MouseEvent) -> Tab | None:
        try:
            widget, _ = self.screen.get_widget_at(self._screen_x(event), self._screen_y(event))
        except NoWidget:
            return None
        return widget if isinstance(widget, Tab) else None

    def _screen_x(self, event: events.MouseEvent) -> int:
        return int(getattr(event, "screen_x", self.region.x + event.x))

    def _screen_y(self, event: events.MouseEvent) -> int:
        # screen_y is what survives the widget moving under the pointer mid-drag.
        return int(getattr(event, "screen_y", self.region.y + event.y))


class DragTabs(DragStrip, Tabs):
    """The timeline's chips: they share the runs/timeline boundary with the
    `TimelineTable` header below them, and drag the same box."""

    def __init__(self, *tabs: Tab, resizes: str, id: str) -> None:
        super().__init__(*tabs, id=id)
        self.resizes = resizes


class DragTabbedContent(DragStrip, TabbedContent):
    """The target chips: the top row of the top box, with nothing above to resize, so
    the drag stretches the box itself — down grows it, and the runs-table header on its
    far edge follows the pointer."""

    def __init__(self, *, initial: str, id: str, resizes: str) -> None:
        super().__init__(initial=initial, id=id)
        self.resizes = resizes


class SelectableDataTable(ChipSteppingTable):
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
        app = self.app
        if isinstance(app, RebaseTuiApp):
            app.note_interaction()
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

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        """Claim `escape` only while there are marks to clear.

        `escape` is also the app's Back key, and the focused table's binding would
        otherwise swallow it unconditionally. Declining the action when nothing is
        marked lets the key fall through to the app, so escape clears marks first
        and navigates back the press after — the same order `b` users expect.
        """
        if action == "clear_marks":
            return bool(self._marked)
        return True

    def restore_marks(self, keys: Iterable[str]) -> None:
        """Re-mark *keys*, ignoring any whose row is gone.

        A repaint rebuilds the rows, so marks cannot survive on their own. Rows that
        disappeared are dropped rather than remembered: a mark on a row that is no longer
        there would be a delete waiting to act on nothing. The anchor is reset for the
        same reason — its index no longer means anything after a rebuild.
        """
        present = {str(row.key.value) for row in self.ordered_rows if row.key.value is not None}
        self._anchor = None
        self._apply_marks({key for key in keys if key in present})

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


class EnvironmentChoiceScreen(ModalScreen[str | None]):
    """Pick the environment whose isolated resources the workspace view shows."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    CSS = f"""
    EnvironmentChoiceScreen {{
        align: center middle;
        background: #101412 70%;
    }}

    #environment-dialog {{
        width: 54;
        height: auto;
        max-height: 20;
        padding: 1 2;
        background: #101412;
        border: solid {BRAND_BRIGHT_GREEN};
    }}

    #environment-title {{
        color: {BRAND_BRIGHT_GREEN};
        text-style: bold;
        margin-bottom: 1;
    }}

    #environment-options {{
        height: auto;
        max-height: 12;
        background: #101412;
        border: none;
    }}

    #environment-hint {{
        color: {BRAND_MEDIUM_GRAY};
        margin-top: 1;
    }}
    """

    def __init__(self, environments: Sequence[str], *, current: str) -> None:
        super().__init__()
        self.environments = list(dict.fromkeys([current, *environments]))
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="environment-dialog"):
            yield Static(f"Environment — currently {self.current}", id="environment-title")
            yield OptionList(*self.environments, id="environment-options")
            yield Static("Enter selects. Escape cancels.", id="environment-hint")

    def on_mount(self) -> None:
        options = self.query_one("#environment-options", OptionList)
        options.highlighted = self.environments.index(self.current)
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.prompt))

    def action_cancel(self) -> None:
        self.dismiss(None)


class RebaseTuiApp(App[None]):
    TITLE = "Rebase TUI"
    SUB_TITLE = ""
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("b", "back", "Back"),
        # The same action `b` runs; hidden so the footer does not say Back twice.
        # Tables bind escape to clear marks, but only claim it while marks exist
        # (see SelectableDataTable.check_action), so it falls through to here.
        Binding("escape", "back", "Back", show=False),
        # Everything below stays out of the footer and lives in the key panel, which
        # lists `show=False` bindings too. Ten hints did not fit the width, so the four
        # you move around with kept their places and the rest went one keystroke away.
        Binding("d", "delete_selection", "Delete", show=False),
        Binding("o", "open_source", "Open source, or a bucket in the cloud console", show=False),
        Binding("g", "open_github", "Open deployed code on GitHub", show=False),
        Binding("w", "switch_workspace", "Switch workspace", show=False),
        Binding("v", "choose_environment", "Switch environment", show=False),
        Binding("a", "open_artifact", "Open artifact", show=False),
        Binding("s", "toggle_terminal_select", "Select text", show=False),
        Binding("c", "copy_row", "Copy ID", show=False),
        Binding("p", "show_details", "Details", show=False),
        Binding("l", "toggle_logs", "Logs", show=False),
        Binding("e", "toggle_events", "Events", show=False),
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

    RebaseHeader, Header {{
        background: {CHROME_GRAY};
        color: {BRAND_BRIGHT_GREEN};
    }}

    Footer {{
        background: #101412;
        color: {BRAND_BRIGHT_GREEN};
    }}

    /* Textual tints the clock a few percent lighter than the bar it sits in, which is
       a seam across the top row on its own. */
    RebaseClock {{
        background: transparent;
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

    /* Tables of fixed-width fields, sized to fit: they clip rather than scroll, and the
       row a bar would cost goes to the rows. */
    #buckets-table,
    #secrets-table,
    #runs-table,
    #timeline-table {{
        overflow-x: hidden;
        scrollbar-size-horizontal: 0;
        scrollbar-background: #101412;
        scrollbar-background-hover: #101412;
        scrollbar-background-active: #101412;
    }}

    /* The target tables outgrow their width: nine columns each, three of them
       timestamps. They keep a horizontal scrollbar rather than clipping, so "Last run"
       is reachable on a narrow terminal instead of merely absent. The projects table is
       the same story a level up — five counts, a status and two times — and the last
       column was falling off the right with no way back to it. A two-finger swipe drives
       the bar, as do `shift`+wheel and dragging the bar itself. */
    #projects-table,
    #functions-table,
    #workflows-table {{
        overflow-x: auto;
        scrollbar-size-horizontal: 1;
        scrollbar-background: #101412;
        scrollbar-background-hover: #101412;
        scrollbar-background-active: #101412;
    }}

    /* Green because Textual's default accent is a blue that appears nowhere else, and
       this bar sits under the first table anyone sees. */
    #projects-table,
    #workflows-table {{
        scrollbar-color: {BRAND_BRIGHT_GREEN};
        scrollbar-color-hover: {BRAND_BRIGHT_GREEN};
        scrollbar-color-active: {BRAND_MAIN_GREEN};
    }}

    #projects-table,
    #buckets-table,
    #secrets-table {{
        height: 1fr;
    }}

    #workspace-resource-tabs {{
        height: 1fr;
    }}

    #workspace-empty {{
        height: auto;
        padding: 0 1;
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
    #workspace-resource-tabs Tabs,
    #target-tabs Tabs,
    #timeline-tabs {{
        height: 1;
    }}

    #workspace-resource-tabs Underline,
    #target-tabs Underline,
    #timeline-tabs Underline {{
        display: none;
    }}

    /* Every strip wears the chrome band, so it reads as one piece with the column
       header row below it and the app header above — and, in the project view where a
       strip is a splitter as well as chips (see `DragStrip`), so the two rows you can
       grab look like the one band they are. */
    #workspace-resource-tabs Tabs,
    #target-tabs Tabs,
    #timeline-tabs {{
        background: {CHROME_GRAY};
    }}

    /* The chips sit centred in their strip rather than hugging the left edge. The
       chip list inside `Tabs` is auto-width but pinned to `min-width: 100%` for the
       sake of the underline bar these strips do not draw; freed of that, centring
       the full-width `#tabs-scroll` around it is all it takes. The arrow keys that
       step between chips live on the tables, untouched by layout. */
    #workspace-resource-tabs Tabs #tabs-scroll,
    #target-tabs Tabs #tabs-scroll,
    #timeline-tabs #tabs-scroll {{
        align-horizontal: center;
    }}

    #workspace-resource-tabs Tabs #tabs-list-bar,
    #workspace-resource-tabs Tabs #tabs-list,
    #target-tabs Tabs #tabs-list-bar,
    #target-tabs Tabs #tabs-list,
    #timeline-tabs #tabs-list-bar,
    #timeline-tabs #tabs-list {{
        width: auto;
        min-width: 0;
    }}

    #workspace-resource-tabs Tab,
    #target-tabs Tab,
    #timeline-tabs Tab {{
        padding: 0 1;
        margin: 0 1 0 0;
        color: {BRAND_MEDIUM_GRAY};
    }}

    #workspace-resource-tabs Tab:hover,
    #target-tabs Tab:hover,
    #timeline-tabs Tab:hover {{
        color: {BRAND_BRIGHT_GREEN};
    }}

    /* The `:focus` rule repeats the unfocused one because Textual's own
       `Tabs:focus .-active` would otherwise repaint it in the block-cursor colours. */
    #workspace-resource-tabs Tab.-active,
    #workspace-resource-tabs Tabs:focus Tab.-active,
    #target-tabs Tab.-active,
    #target-tabs Tabs:focus Tab.-active,
    #timeline-tabs Tab.-active,
    #timeline-tabs:focus Tab.-active {{
        background: {BRAND_BRIGHT_GREEN};
        color: #101412;
        text-style: bold;
    }}

    /* These headers are splitters. Lighting up under the pointer is the only affordance
       a terminal can offer for that — there is no cursor to change shape. */
    DragHeaderTable > .datatable--header-hover {{
        color: {BRAND_BRIGHT_GREEN};
        background: {CHROME_GRAY_HOVER};
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

    #timeline-pane {{
        height: 1fr;
    }}

    /* The one table whose content is prose. It is allowed to run off the right and be
       scrolled back — `^pgup`/`^pgdn`, a two-finger swipe, or the bar — where the tables
       of fixed-width fields are clipped, because a log line is not a column you can
       widen your way out of. */
    #timeline-table {{
        height: 1fr;
        overflow-x: auto;
        scrollbar-size-horizontal: 1;
        scrollbar-color: {BRAND_MEDIUM_GRAY};
        scrollbar-color-hover: {BRAND_BRIGHT_GREEN};
        scrollbar-color-active: {BRAND_BRIGHT_GREEN};
    }}

    DataTable {{
        background: #101412;
        scrollbar-size-vertical: 1;
    }}

    DataTable > .datatable--header {{
        background: {CHROME_GRAY};
        color: #E8F0ED;
    }}

    /* Textual tints the focused table 5% lighter, which turned the pane you were in a
       visibly different shade from the rest of the app — most obvious in the workspace
       view, where one table fills the screen and the whole background changed with it.
       The cursor row already says where the focus is, in colour rather than in wash. */
    DataTable:focus {{
        background-tint: transparent;
    }}

    /* Textual also tints the focused table's header 5% lighter, which drew a visible
       seam between a chip strip and the header row it shares its band with. */
    DataTable:focus > .datatable--header {{
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
        refresh_interval: float = AUTO_REFRESH_SECONDS,
    ) -> None:
        super().__init__()
        self.data = data or RebaseTuiData(client, project=project, limit=limit)
        self.project = project
        self.limit = limit
        #: Seconds between automatic refreshes; 0 disables the timer entirely.
        self._refresh_interval = max(0.0, refresh_interval)
        self._last_key_at = 0.0
        self._refresh_failures = 0
        #: How to re-read the open runs box, captured when a target was selected. A
        #: deployed target and a one-off group are read two different ways, and the tick
        #: should not have to re-derive which it is looking at.
        self._runs_reload: Callable[..., Awaitable[None]] | None = None
        self.profile_name = selected_profile_name()
        self.profile_data = load_profile(self.profile_name)
        self.workspace_overview: WorkspaceOverviewData | None = None
        self.environment_name = self.data.environment_name
        self.project_targets: ProjectTargetsData | None = None
        self.selected_project: dict[str, Any] | None = None
        self.selected_target_type: TargetType | None = None
        self.selected_target: dict[str, Any] | None = None
        #: One-off run groups by synthetic row key, alongside the real target rows.
        self._ephemeral_rows: dict[str, EphemeralGroup] = {}
        self.current_view: ViewName = "workspace"
        self.view_before_switcher: ViewName = "workspace"
        self._project_rows: dict[str, ProjectSummary] = {}
        self._workspace_resource_rows: dict[str, dict[str, dict[str, Any]]] = {
            "buckets-table": {},
            "secrets-table": {},
        }
        self._profile_rows: dict[str, dict[str, Any]] = {}
        self._function_rows: dict[str, dict[str, Any]] = {}
        self._workflow_rows: dict[str, dict[str, Any]] = {}
        self._steps_by_function: dict[str, list[WorkflowStep]] = {}
        self._endpoints_by_target: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._last_runs: dict[str, str] = {}
        self._run_rows: dict[str, dict[str, Any]] = {}
        self._run_detail: RunDetailData | None = None
        #: Which of the timeline's chips is showing. `l` jumps to the logs one.
        self._timeline_filter = TIMELINE_FILTERS[0][0]
        #: The activity record behind each currently rendered row, for lineage-aware
        #: task and artifact drawers, and so actions operate on the row under the
        #: cursor. Keys match the DataTable's row keys.
        self._timeline_rows: dict[str, TimelineRow] = {}
        #: Timeline rows opened out to their full text, by position in the current view.
        #: Position, not identity: changing filter or run reshuffles the list, and both
        #: clear this rather than leaving an expansion attached to some other line.
        self._expanded_timeline: set[int] = set()
        #: Log entries per run id, kept so toggling `l` back on costs no request.
        self._run_logs: dict[str, list[dict[str, Any]]] = {}
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
            with TabbedContent(initial="projects-resource-tab", id="workspace-resource-tabs"):
                with TabPane(Content("[ Projects ]"), id="projects-resource-tab"):
                    yield SelectableDataTable(id="projects-table")
                with TabPane(Content("[ Buckets ]"), id="buckets-resource-tab"):
                    # Selectable, unlike its sibling resource tables: `o` opens every
                    # marked bucket, so buckets need marks as well as a cursor.
                    yield SelectableDataTable(id="buckets-table")
                with TabPane(Content("[ Secrets ]"), id="secrets-resource-tab"):
                    yield ChipSteppingTable(id="secrets-table")
            yield Static("", id="workspace-empty")
        with Vertical(id="workspace-switcher-view"):
            yield HeaderSafeDataTable(id="workspace-profiles-table")
        with Vertical(id="project-view"):
            yield Static("", id="project-error", classes="panel")
            with DragTabbedContent(initial="workflows-tab", id="target-tabs", resizes="#target-tabs"):
                # The brackets are part of the label so an unselected chip still reads as
                # something you can press, with no colour to say so. `Text`, not `str`:
                # Textual parses a label as content markup and would read `[ Workflows ]`
                # as a style tag and render nothing at all; `Content` is the unparsed form.
                with TabPane(Content("[ Workflows ]"), id="workflows-tab"):
                    yield SelectableDataTable(id="workflows-table")
                with TabPane(Content("[ Functions ]"), id="functions-tab"):
                    yield SelectableDataTable(id="functions-table")
            yield DragHeaderTable(resizes="#target-tabs", id="runs-table")
            # A bare `Tabs` rather than a `TabbedContent`: four panes would mean four
            # tables holding slices of one run. One table, filtered by the chips.
            with Vertical(id="timeline-pane"):
                # Not focusable: `tab` walks panes, and a chip strip that could hold
                # the focus would be a stop on that walk with nothing to navigate.
                # The arrows on the table below drive it, and the mouse still clicks it.
                chips = DragTabs(
                    *(Tab(Content(label), id=tab_id) for tab_id, label, _ in TIMELINE_FILTERS),
                    resizes="#runs-table",
                    id="timeline-tabs",
                )
                chips.can_focus = False
                yield chips
                yield TimelineTable(resizes="#runs-table", id="timeline-table")
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
        if self._refresh_interval:
            self.set_interval(self._refresh_interval, self._refresh_tick)
        # Not behind `--refresh-interval`: that switches off *requests*, and a countdown
        # frozen at the value it was loaded with would be worse than a timestamp.
        self.set_interval(COUNTDOWN_TICK_SECONDS, self._tick_countdowns)

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
        # Membership of the visible set, not the widget's own `display`: a table inside a
        # hidden pane is still "displayed" and would keep a focus nothing can see.
        focused = self.focused
        if opening or (isinstance(focused, DataTable) and focused not in boxes):
            boxes[-1].focus()

    def _default_box_height(self, selector: str) -> int:
        """What a box is worth before anyone drags or `+`s it.

        Each level costs the ones above it some rows, so the box you just opened is the
        one with room to show something. Expanded logs are the exception that genuinely
        wants the whole screen, so both boxes above shrink to a header and a row or two.
        """
        if self._reveal_level == 2 and self._reading_logs:
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
        # Both chip-strip boxes spend a row on their chips before their table's header.
        return MIN_TABLE_HEIGHT if selector == "#runs-table" else MIN_TARGET_BOX_HEIGHT

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
        # A lone box already has the whole view: nothing to trade rows with. Reachable
        # by dragging the target chips before a run is open, where `+`/`-` warns instead.
        if selector not in selectors or rows == 0 or len(selectors) < 2:
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
        if focused_id == "#timeline-table":
            return "#timeline-pane"
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

    @contextmanager
    def _preserve_view(self) -> Iterator[None]:
        """Put the reader back where they were after the block repaints.

        A repaint rebuilds every row, so the cursor drops to the top, marked rows are
        forgotten and the horizontal scroll resets. That is a nuisance when *you* pressed
        `r` and unusable when a timer did, so every refresh path wraps its render in this.

        Restores by row key rather than index, because a repaint is also what reorders and
        removes rows. Focus is restored too: a repaint of the table that has it can leave
        the focus somewhere the keys no longer do what the reader expects.
        """
        snapshot = {
            table_id: view for table_id in PRESERVED_TABLE_IDS if (view := self._table_view(table_id)) is not None
        }
        focused_id = self.focused.id if self.focused is not None else None
        try:
            yield
        finally:
            for table_id, view in snapshot.items():
                self._restore_table_view(table_id, view)
            if focused_id is not None:
                with suppress(NoMatches):
                    self.query_one(f"#{focused_id}").focus()

    def _table_view(self, table_id: str) -> TableView | None:
        try:
            table = self.query_one(f"#{table_id}", DataTable)
        except NoMatches:
            return None
        marked = tuple(table.marked_keys) if isinstance(table, SelectableDataTable) else ()
        return TableView(
            cursor_key=self._cursor_key(table),
            marked=marked,
            scroll_x=table.scroll_x,
            scroll_y=table.scroll_y,
        )

    def _restore_table_view(self, table_id: str, view: TableView) -> None:
        try:
            table = self.query_one(f"#{table_id}", DataTable)
        except NoMatches:
            return
        if view.cursor_key is not None:
            # The row may be gone — a run finished and dropped off the page, a function was
            # deleted. Leaving the cursor at the top is the honest answer then.
            with suppress(Exception):
                table.move_cursor(row=table.get_row_index(view.cursor_key))
        if view.marked and isinstance(table, SelectableDataTable):
            table.restore_marks(view.marked)
        if view.scroll_x or view.scroll_y:
            table.scroll_to(x=view.scroll_x, y=view.scroll_y, animate=False)

    def _fill_table(self, table_id: str, *, widget: str | None = None) -> DataTable:
        """Empty a table and give it its header back, ready for rows.

        Headers and rows land in the same paint this way, so the columns are sized once
        against real data instead of snapping from header width to content width.
        """
        table = self.query_one(f"#{widget or table_id}", DataTable)
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

        for table_id in ("buckets-table", "secrets-table"):
            resource = self.query_one(f"#{table_id}", DataTable)
            resource.cursor_type = "row"
            resource.zebra_stripes = True

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
                self._load_project_targets(self.selected_project, preserve=True),
                name="project-targets",
                group="tui",
                exclusive=True,
            )
            return
        self.run_worker(self._load_workspace_overview(preserve=True), name="overview", group="tui", exclusive=True)

    def on_key(self, event: events.Key) -> None:
        """Note that the reader is driving, so the timer can leave them alone.

        Best effort: a key a focused widget consumes never reaches here, which is why the
        tables also report cursor movement through `note_interaction`.
        """
        self.note_interaction()

    def note_interaction(self) -> None:
        self._last_key_at = monotonic()

    def _refresh_tick(self) -> None:
        """Re-read whatever is on screen, quietly, on the timer.

        Nothing here is new work: it drives the same loaders `r` does, with `preserve` so
        the reader keeps their place and `announce=False` so a blip on a timer does not
        yank the view or raise a toast. `exclusive=True` on the shared worker group means a
        tick that arrives while the last one is still running is dropped, not queued.
        """
        if self._refresh_tick_paused():
            return
        if self.current_view == "workspace":
            self.run_worker(
                self._load_workspace_overview(preserve=True, announce=False),
                name="auto-refresh",
                group="tui",
                exclusive=True,
            )
            return
        if self.current_view == "project" and self.selected_project is not None:
            self.run_worker(self._refresh_tick_project(), name="auto-refresh", group="tui", exclusive=True)

    def _refresh_tick_paused(self) -> bool:
        """Whether now is a bad moment to move the screen.

        Each of these is a case where a repaint would take something away from the reader
        rather than give them something: a dialog whose row list is what they are about to
        act on, a text selection they are halfway through making, a delete already in
        flight, or a cursor they are still driving.
        """
        if len(self.screen_stack) > 1:
            return True
        if self._terminal_select:
            return True
        if self.screen.selections:
            return True
        if any(worker.group == "tui-delete" for worker in self.workers):
            return True
        return monotonic() - self._last_key_at < KEYPRESS_QUIET_SECONDS

    async def _refresh_tick_project(self) -> None:
        """The project view, top box to bottom, stopping where the screen stops.

        Only the boxes that are open are re-read, and a finished run's timeline is left
        alone: it cannot change, and it is the one place the reader is most likely to be
        scrolling through output.
        """
        if self.selected_project is None:
            return
        await self._load_project_targets(self.selected_project, preserve=True, announce=False)
        if self._reveal_level >= 1 and self._runs_reload is not None:
            await self._runs_reload(preserve=True, announce=False)
        if self._reveal_level >= 2 and self._live_run_id is not None:
            await self._load_run_detail(self._live_run_id, preserve=True, announce=False)

    @property
    def _live_run_id(self) -> str | None:
        """The selected run's id while it can still change, else None."""
        detail = self._run_detail
        if detail is None:
            return None
        run = detail.run
        if str(run.get("status") or "").lower() not in LIVE_RUN_STATUSES:
            return None
        run_id = run.get("id")
        return str(run_id) if run_id else None

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

    def action_switch_workspace(self) -> None:
        """Open the workspace picker from anywhere in the TUI."""
        if self.current_view != "workspace-switcher":
            self._open_workspace_switcher()

    def action_choose_environment(self) -> None:
        """Open the environment picker without leaving the current workspace."""
        environments = [
            str(item["name"])
            for item in (self.workspace_overview.environments if self.workspace_overview else [])
            if item.get("name")
        ]
        self.push_screen(
            EnvironmentChoiceScreen(environments or [self.environment_name], current=self.environment_name),
            self._on_environment_chosen,
        )

    def _on_environment_chosen(self, environment: str | None) -> None:
        if environment is None or environment == self.environment_name:
            return
        self.environment_name = environment
        self.data.use_environment(environment)
        workspace_id = getattr(self.data.client, "workspace_id", None)
        if isinstance(workspace_id, str) and workspace_id:
            set_active_environment(workspace_id, environment)
        self.workspace_overview = None
        self.selected_project = None
        self.project_targets = None
        self._clear_target_detail()
        self._show_workspace_view()
        self._update_workspace_title()
        self.action_refresh()

    def on_click(self, event: events.Click) -> None:
        if event.widget.__class__.__name__ == "HeaderTitle":
            event.stop()
            self._open_workspace_switcher()

    def _target_tab_order(self) -> list[tuple[str, str]]:
        return list(TARGET_TABS)

    def _visible_boxes(self) -> list[DataTable]:
        """The tables `tab` moves between, top to bottom, as the screen currently stands."""
        if self.current_view == "workspace":
            active = self.query_one("#workspace-resource-tabs", TabbedContent).active
            table = dict(RESOURCE_TABS).get(active, "#projects-table")
            return [self.query_one(table, DataTable)]
        if self.current_view == "workspace-switcher":
            return [self.query_one("#workspace-profiles-table", DataTable)]
        active = self.query_one("#target-tabs", TabbedContent).active
        # The target box holds whichever of its two tables the chip strip has open; the
        # other two boxes *are* their table. Pair them so visibility is read off the box.
        tables = [dict(self._target_tab_order()).get(active, "#workflows-table")]
        # A box is not always its table: the two with chip strips wrap one.
        tables.extend(
            "#timeline-table" if selector == "#timeline-pane" else selector
            for level in REVEAL_LEVELS[: self._reveal_level]
            for selector in level
        )
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

    def action_switch_chip_tab(self, delta: int) -> None:
        """Step between the chips above the table that has the focus.

        Which strip that is follows the view: the workspace view's
        Projects/Buckets/Secrets, the project view's Workflows/Functions. Bound
        to left/right on every table under a strip; see `ChipSteppingTable`.
        """
        if self.current_view == "workspace":
            self._step_tabs("#workspace-resource-tabs", list(RESOURCE_TABS), delta)
        elif self.current_view == "project":
            self._step_tabs("#target-tabs", self._target_tab_order(), delta)

    def _step_tabs(self, strip: str, tabs_and_tables: list[tuple[str, str]], delta: int) -> None:
        """Move one chip strip `delta` chips along, wrapping, and follow it with the focus."""
        tabs = self.query_one(strip, TabbedContent)
        order = [tab_id for tab_id, _ in tabs_and_tables]
        current = order.index(tabs.active) if tabs.active in order else 0
        tabs.active = order[(current + delta) % len(order)]
        # The focus follows, or `tab` would carry on from the box that is no longer there.
        self.query_one(dict(tabs_and_tables)[tabs.active], DataTable).focus()

    @property
    def display_tzinfo(self) -> tzinfo | None:
        """The zone every time in the TUI is rendered in, system local until changed."""
        return self._display_timezone or datetime.now().astimezone().tzinfo

    def _time(self, value: Any) -> str:
        return format_timestamp(value, self.display_tzinfo)

    @staticmethod
    def _countdown(value: Any) -> str:
        """A `Next run` cell: how long until it, padded so the column never moves."""
        return format_countdown(value).ljust(COUNTDOWN_WIDTH)

    def _tick_countdowns(self) -> None:
        """Redraw the projects table's `Next run` cells, once a second.

        One cell per row, in place, rather than a repaint: a repaint would drop the
        cursor and the marks a second after every keypress. `update_width=False` for the
        same reason the values are padded — the column was sized for the widest countdown
        when the rows landed and must not be resized from under the reader.
        """
        if self.current_view != "workspace" or not self._project_rows:
            return
        try:
            table = self.query_one("#projects-table", DataTable)
        except NoMatches:
            return
        for row_key, summary in self._project_rows.items():
            # A row the table no longer has — refreshed away between the two — is not an
            # error, it is just nothing to draw.
            with suppress(Exception):
                row = table.get_row_index(row_key)
                table.update_cell_at(Coordinate(row, NEXT_RUN_COLUMN), self._countdown(summary.next_run_at))

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

    def action_copy_row(self) -> None:
        """Copy the highlighted row's identifier — or the marked rows' — to the clipboard.

        Every table keys its rows on the thing's API id (or its name, for things the
        API identifies by name), which is exactly the handle to paste into a `rebase`
        command or hand to an agent. Copied as `workflow_id=...` rather than the bare
        value, so both the reader and whatever it is pasted into can tell what kind of
        identifier it is. The clipboard write goes through the terminal (OSC 52), the
        same channel Textual's own selection copy uses, so it works over SSH and
        inside tmux wherever that copy does.
        """
        table = self.focused
        if not isinstance(table, DataTable):
            self.notify("Select a row first — c copies its ID.", severity="warning")
            return
        keys = table.marked_keys if isinstance(table, SelectableDataTable) else []
        if not keys:
            keys = [key for key in (self._cursor_key(table),) if key is not None]
        table_id = str(table.id or "")
        ids = [value for key in keys if (value := self._row_identifier(table_id, key)) is not None]
        if not ids:
            self.notify("Select a row first — c copies its ID.", severity="warning")
            return
        self.copy_to_clipboard("\n".join(ids))
        self.notify(
            f"Copied {ids[0]} to the clipboard." if len(ids) == 1 else f"Copied {len(ids)} IDs to the clipboard."
        )

    def _row_identifier(self, table_id: str, key: str) -> str | None:
        """The copyable identity behind one row key, as a `label=value` pair.

        The value is almost always the key itself. The two synthetic keys are
        unwrapped: a one-off row registers no target, so the name it ran under is its
        identity; a timeline row is keyed by position, so its record's own id is the
        answer — and for the rows that have none (events, log lines), the run they
        belong to is. The label says what the value identifies, in the vocabulary an
        agent would search the API or codebase for — including whether a resource is
        being named by id or, where its API reports none, by name.
        """
        if key.startswith(EPHEMERAL_ROW_PREFIX):
            _, target_type, name = key.split(":", 2)
            return f"{target_type}_name={name}"
        if table_id == "timeline-table":
            row = self._timeline_rows.get(key)
            record_id = row.record.get("id") if row is not None else None
            if record_id and row is not None:
                return f"{row.kind}_id={record_id}"
            run_id = self._run_detail.run.get("id") if self._run_detail is not None else None
            return f"run_id={run_id}" if run_id else None
        if table_id in self._workspace_resource_rows:
            kind = table_id.removesuffix("-table").removesuffix("s")
            item = self._workspace_resource_rows[table_id].get(key, {})
            return f"{kind}_id={key}" if item.get("id") == key else f"{kind}_name={key}"
        label = {
            "projects-table": "project_id",
            "workflows-table": "workflow_id",
            "functions-table": "function_id",
            "runs-table": "run_id",
            "workspace-profiles-table": "profile",
        }.get(table_id, "id")
        return f"{label}={key}"

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
            # A one-off row is dropped by `_delete_label` along with anything else that
            # names no target, which is the safe outcome but a confusing thing to be told
            # "nothing selected" about when a row is plainly under the cursor.
            if keys and all(str(key).startswith(EPHEMERAL_ROW_PREFIX) for key in keys):
                self.notify(
                    "A one-off run registers no target, so there is nothing to delete.",
                    severity="warning",
                )
                return
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

    def _open_console_buckets(self) -> list[dict[str, Any]] | None:
        """The buckets `o` acts on: the marked rows, else the one under the cursor.

        None means `o` is not about buckets here at all, which is different from
        the empty list — that is the buckets tab with nothing chosen on it.
        """
        if self.current_view != "workspace":
            return None
        if self.query_one("#workspace-resource-tabs", TabbedContent).active != "buckets-resource-tab":
            return None
        table = self.query_one("#buckets-table", SelectableDataTable)
        keys = table.marked_keys or [key for key in (table.cursor_key,) if key is not None]
        rows = self._workspace_resource_rows.get("buckets-table", {})
        return [item for key in keys if (item := rows.get(key)) is not None]

    def _open_buckets_in_console(self, buckets: list[dict[str, Any]]) -> None:
        if not buckets:
            self.notify("No bucket selected.", severity="warning")
            return
        targets = [
            (str(bucket.get("name") or "-"), url)
            for bucket in buckets
            if (url := bucket_console_url(bucket)) is not None
        ]
        if not targets:
            self.notify(
                "This Rebase deployment does not report where its buckets are stored, so there is nothing to open.",
                severity="warning",
            )
            return
        self.run_worker(
            self._open_console(targets),
            name="open-bucket",
            group="tui-open",
            exclusive=True,
        )

    async def _open_console(self, targets: list[tuple[str, str]]) -> None:
        opened: list[str] = []
        for name, url in targets:
            try:
                launched = await asyncio.to_thread(webbrowser.open, url)
            except Exception as exc:
                self.notify(f"Could not open {name}: {exc}", severity="error")
                return
            if not launched:
                self.notify(f"Could not open a browser for {url}", severity="error")
                return
            opened.append(name)
        self.notify(f"Opened {', '.join(opened)} in the Google Cloud console.")

    def action_open_source(self) -> None:
        """Open whatever `o` means where the reader is standing.

        On the buckets tab that is the bucket's storage in the cloud console;
        everywhere else it stays the file that declares the selected project.
        """
        buckets = self._open_console_buckets()
        if buckets is not None:
            self._open_buckets_in_console(buckets)
            return
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

    def _selected_workflow_version(self) -> tuple[str, dict[str, Any]] | None:
        """The deployed workflow under the cursor, or behind the open run/timeline."""
        if self.current_view != "project" or self.project_targets is None:
            return None
        focused = self.focused
        table_id = str(focused.id) if isinstance(focused, DataTable) else ""
        workflow_id: str | None = None
        if table_id == "workflows-table":
            workflow_id = self._cursor_key(focused)
        elif table_id in {"runs-table", "timeline-table"} and self.selected_target_type == "workflow":
            target_id = (self.selected_target or {}).get("id")
            workflow_id = str(target_id) if target_id is not None else None
        if workflow_id is None or workflow_id.startswith(EPHEMERAL_ROW_PREFIX):
            return None
        workflow = self._workflow_rows.get(workflow_id)
        version = self.project_targets.workflow_versions.get(workflow_id)
        if workflow is None or version is None:
            return None
        return str(workflow.get("name") or workflow_id), version

    def action_open_github(self) -> None:
        """Open the selected workflow's exact deployed source revision on GitHub."""
        selected = self._selected_workflow_version()
        if selected is None:
            self.notify("Select a deployed workflow first — g opens its pinned GitHub source.", severity="warning")
            return
        workflow_name, version = selected
        if not is_github_backed(version):
            self.notify(f"{workflow_name} is stored by Rebase, not deployed from GitHub.", severity="warning")
            return
        if github_workflow_source_url(version) is None:
            self.notify(f"{workflow_name} has incomplete GitHub source metadata.", severity="warning")
            return
        roots = [Path.cwd(), *(Path(entry) for entry in search_paths(self._workspace_key()))]
        self.run_worker(
            self._open_github_workflow(workflow_name, dict(version), roots),
            name="open-github",
            group="tui-open",
            exclusive=True,
        )

    async def _open_github_workflow(self, workflow_name: str, version: dict[str, Any], roots: list[Path]) -> None:
        line = await asyncio.to_thread(workflow_definition_line_at_commit, version, roots)
        url = github_workflow_source_url(version, line=line)
        if url is None:
            self.notify(f"{workflow_name} has incomplete GitHub source metadata.", severity="warning")
            return
        try:
            opened = await asyncio.to_thread(webbrowser.open, url)
        except Exception as exc:
            self.notify(f"Could not open GitHub: {exc}", severity="error")
            return
        if not opened:
            self.notify(f"Could not open a browser for {url}", severity="error")
            return
        location = f" at line {line}" if line is not None else ""
        self.notify(f"Opened {workflow_name} at {format_workflow_commit(version)}{location} on GitHub.")

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

    def action_open_artifact(self) -> None:
        """Open the artifact under the timeline cursor at its durable location."""
        focused = self.focused
        key = self._cursor_key(focused) if getattr(focused, "id", None) == "timeline-table" else None
        row = self._timeline_rows.get(key or "")
        if row is None or row.kind != "artifact":
            self.notify("Select an artifact in the timeline first — a opens its location.", severity="warning")
            return
        if row.url is None and (row.artifact_id is None or row.artifact_run_id is None):
            self.notify(f"No browser destination is available for {row.artifact_uri or row.stage}.", severity="warning")
            return
        self.run_worker(
            self._open_artifact(row),
            name="open-artifact",
            group="tui-open",
            exclusive=True,
        )

    async def _open_artifact(self, row: TimelineRow) -> None:
        try:
            url = row.url
            if row.artifact_id is not None and row.artifact_run_id is not None:
                url = await asyncio.to_thread(
                    self.data.client.open_run_artifact,
                    row.artifact_run_id,
                    row.artifact_id,
                )
            if url is None:
                raise RebaseWorkflowError("artifact has no browser destination")
            opened = await asyncio.to_thread(webbrowser.open, url)
        except Exception as exc:
            self.notify(f"Could not open artifact: {exc}", severity="error")
            return
        if not opened:
            self.notify(f"Could not open a browser for {url}", severity="error")
            return
        self.notify(f"Opened {row.stage} in the browser.")

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
            if self.query_one("#workspace-resource-tabs", TabbedContent).active == "projects-resource-tab":
                return self.query_one("#projects-table", SelectableDataTable)
            return None
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

    async def _load_workspace_overview(self, *, preserve: bool = False, announce: bool = True) -> None:
        try:
            overview = await asyncio.to_thread(self.data.load_workspace_overview)
        except Exception as exc:
            self._set_error(exc, announce=announce)
            return
        self._refresh_failures = 0
        self.workspace_overview = overview
        self.project_targets = None
        self.selected_project = None
        self.selected_target = None
        self.selected_target_type = None
        with self._preserve_view() if preserve else nullcontext():
            self._render_workspace_overview(overview)
        self._clear_target_detail()
        self._show_workspace_view()
        # The composite route answers for everything except secrets, which cost more than
        # the rest of the view put together and are not what anyone opens the TUI to read.
        # The fan-out path already has them, and asking twice would undo the point.
        if not overview.secrets and self.data.offers_secrets():
            await self._load_workspace_secrets()

    async def _load_workspace_secrets(self) -> None:
        """Fill the secrets table once the rest of the screen is up.

        Supplementary in the strongest sense: a Secret Manager that is slow or unhappy
        costs this one table and nothing else, so a failure here is swallowed rather than
        replacing a working view with an error.
        """
        try:
            secrets = await asyncio.to_thread(self.data.load_workspace_secrets)
        except Exception:
            return
        if not secrets or self.current_view != "workspace" or self.workspace_overview is None:
            return
        self.workspace_overview = replace(self.workspace_overview, secrets=secrets)
        # Preserved: by now the reader has had the table for a moment and may have moved
        # the cursor, and a second paint must not take that away to deliver a detail.
        with self._preserve_view():
            self._render_secrets(secrets)

    async def _load_project_targets(
        self, project: dict[str, Any], *, preserve: bool = False, announce: bool = True
    ) -> None:
        """Load a project's targets, progressively on entry and atomically on refresh.

        Step graphs and last-run times take a second round trip, and holding the whole
        view back for them meant staring at an empty box for twice as long as the names
        actually took to arrive. The first paint is everything the first phase read; the
        second fills provenance, Last run, and the functions' workflow grouping.

        A refresh is different: a complete table is already on screen. Painting its base
        phase would temporarily replace those details with dashes and resize the columns,
        then reverse both changes when the version reads finished. Under *preserve*, keep
        the last complete frame until its complete replacement is ready.

        Where the platform has the composite route none of that applies: one request
        answers everything, so there is no second phase to stage and the table is painted
        complete the first time.
        """
        try:
            composite = await asyncio.to_thread(self.data.load_project_composite, project)
        except Exception as exc:
            self._set_error(exc, announce=announce)
            return
        if composite is not None:
            self._refresh_failures = 0
            self.project_targets = composite
            with self._preserve_view() if preserve else nullcontext():
                self._render_project_targets(composite, preserve=preserve)
            return

        try:
            base, runs = await asyncio.to_thread(self.data.load_project_base, project)
        except Exception as exc:
            self._set_error(exc, announce=announce)
            return
        self._refresh_failures = 0
        if not preserve:
            self.project_targets = base
            self._render_project_targets(base)

        try:
            targets = await asyncio.to_thread(self.data.load_project_detail, base, runs)
        except Exception as exc:
            # On entry, the names are already on screen and still usable. On refresh,
            # the previous complete frame is still there. In either case, say what is
            # missing rather than replacing a working table with an error.
            self._set_error(exc, announce=announce)
            return
        self.project_targets = targets
        # Preserved: by now the reader may have moved the cursor, or opened a target and
        # be looking at its runs. The second paint is a detail they did not ask for, and
        # it must not take a selection away to deliver one.
        with self._preserve_view():
            self._render_project_targets(targets, preserve=True)

    async def _load_runs(
        self,
        target_type: TargetType,
        target_id: str,
        *,
        name: str | None = None,
        preserve: bool = False,
        announce: bool = True,
    ) -> None:
        project_id = str(self.selected_project["id"]) if self.selected_project else None
        try:
            runs = await asyncio.to_thread(
                partial(
                    self.data.load_target_runs,
                    target_type,
                    target_id,
                    name=name,
                    project_id=project_id,
                )
            )
        except Exception as exc:
            self._set_error(exc, announce=announce)
            return
        with self._preserve_view() if preserve else nullcontext():
            self._render_runs(runs, preserve=preserve)

    def _select_ephemeral(self, row_key: str) -> None:
        """Open a one-off row's runs, the same way selecting a deployed target does."""
        group = self._ephemeral_rows.get(row_key)
        if group is None or self.selected_project is None:
            return
        self.selected_target_type = "workflow" if group.target_type == "workflow" else "function"
        # A dict standing in for the target row the drawer and the runs box expect. It
        # carries no id or version because there is no registered object behind it.
        self.selected_target = {
            "name": group.name,
            "origin": ORIGIN_ONE_OFF,
            "target_type": group.target_type,
            "run_type": group.run_type,
            "runs": group.runs,
            "last_run": group.last_run,
        }
        self._reveal(1)
        self._runs_reload = partial(self._load_ephemeral_runs, group, str(self.selected_project["id"]))
        self.run_worker(
            self._load_ephemeral_runs(group, str(self.selected_project["id"])),
            name="runs",
            group="tui",
            exclusive=True,
        )

    async def _load_ephemeral_runs(
        self, group: EphemeralGroup, project_id: str, *, preserve: bool = False, announce: bool = True
    ) -> None:
        try:
            runs = await asyncio.to_thread(self.data.load_ephemeral_runs, group.name, group.target_type, project_id)
        except Exception as exc:
            self._set_error(exc, announce=announce)
            return
        with self._preserve_view() if preserve else nullcontext():
            self._render_runs(runs, preserve=preserve)

    async def _load_run_detail(self, run_id: str, *, preserve: bool = False, announce: bool = True) -> None:
        target_type = (self._run_rows.get(run_id) or {}).get("target_type")
        try:
            detail = await asyncio.to_thread(self.data.load_run_detail, run_id, target_type=target_type)
        except Exception as exc:
            self._set_error(exc, announce=announce)
            return
        self._refresh_failures = 0
        self._run_detail = detail
        if not preserve:
            # Opening a different run starts folded. Re-reading the one already open must
            # not fold the rows the reader expanded to look at.
            self._expanded_timeline.clear()
        self._run_logs[run_id] = detail.logs
        with self._preserve_view() if preserve else nullcontext():
            self._render_timeline()

    @property
    def _reading_logs(self) -> bool:
        """Whether the timeline is showing log output, which wants the screen."""
        return self._timeline_filter == "timeline-logs"

    def action_toggle_logs(self) -> None:
        """Jump the timeline to its Logs chip, or back to Activity."""
        self._jump_to_timeline_filter("timeline-logs", "l shows everything the run said")

    def action_toggle_events(self) -> None:
        """Jump the timeline to its Events chip, or back to Activity."""
        self._jump_to_timeline_filter("timeline-events", "e shows the run's stages on their own")

    def _jump_to_timeline_filter(self, tab_id: str, hint: str) -> None:
        if self._run_detail is None:
            self.notify(f"Select a run first — {hint}.", severity="warning")
            return
        self._select_timeline_filter(TIMELINE_FILTERS[0][0] if self._timeline_filter == tab_id else tab_id)

    def _available_timeline_filters(self) -> list[str]:
        """The chips that describe something this run actually has.

        Events and Logs remain available even when empty because they are stable
        observability views. Execution children are structural: showing an empty Steps,
        Tasks or Artifacts branch implies the run has that shape when it does not.
        """
        detail = self._run_detail
        if detail is None:
            return [TIMELINE_FILTERS[0][0], "timeline-logs", "timeline-events"]
        present = {
            "timeline-steps": bool(detail.steps),
            "timeline-tasks": bool(detail.tasks),
            "timeline-artifacts": bool(detail.artifacts),
        }
        return [tab_id for tab_id, _, _ in TIMELINE_FILTERS if tab_id not in present or present[tab_id]]

    def _sync_timeline_tabs(self) -> None:
        """Hide absent execution branches and put useful counts on present ones."""
        detail = self._run_detail
        counts = {
            "timeline-steps": len(detail.steps) if detail is not None else 0,
            "timeline-tasks": len(detail.tasks) if detail is not None else 0,
            "timeline-artifacts": len(detail.artifacts) if detail is not None else 0,
        }
        available = set(self._available_timeline_filters())
        tabs = self.query_one("#timeline-tabs", Tabs)
        for tab_id, label, _ in TIMELINE_FILTERS:
            tab = tabs.query_one(f"#{tab_id}", Tab)
            tab.styles.display = "block" if tab_id in available else "none"
            count = counts.get(tab_id)
            rendered = label if count is None else f"[ {label[2:-2]} {count} ]"
            # Brackets are literal chip chrome, not Rich/Textual markup.
            tab.label = Content(rendered)
        if self._timeline_filter not in available:
            self._timeline_filter = TIMELINE_FILTERS[0][0]
        if tabs.active != self._timeline_filter:
            tabs.active = self._timeline_filter

    def _select_timeline_filter(self, tab_id: str) -> None:
        if tab_id not in self._available_timeline_filters():
            return
        self._timeline_filter = tab_id
        self._expanded_timeline.clear()
        tabs = self.query_one("#timeline-tabs", Tabs)
        if tabs.active != tab_id:
            tabs.active = tab_id
        self._render_timeline()

    def action_switch_timeline_filter(self, delta: int) -> None:
        """Step between the timeline's chips. Bound to left/right on its table."""
        order = self._available_timeline_filters()
        current = order.index(self._timeline_filter) if self._timeline_filter in order else 0
        self._select_timeline_filter(order[(current + delta) % len(order)])

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        if event.tabs.parent is not None and event.tabs.parent.id == "workspace-resource-tabs":
            boxes = self._visible_boxes()
            if boxes:
                boxes[0].focus()
            return
        if (
            event.tabs.id == "timeline-tabs"
            and event.tab.id in self._available_timeline_filters()
            and event.tab.id != self._timeline_filter
        ):
            self._timeline_filter = str(event.tab.id)
            self._expanded_timeline.clear()
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
            last_run = summary.last_run or {}
            # The outcome rides with the timestamp — "14:10:47 completed", coloured —
            # so Last run answers "what happened when it ran?" on its own, while the
            # Status column stays purely what the user configured.
            last_run_time = self._time(last_run.get("created_at"))
            last_run_cell = Text(
                f"{last_run_time} {last_run['status']}" if last_run.get("status") else last_run_time,
                style=status_style(last_run["status"]) if last_run.get("status") else "",
            )
            projects.add_row(
                str(project.get("name", "-")),
                str(summary.function_count),
                str(summary.workflow_count),
                str(summary.cron_count),
                str(summary.endpoint_count),
                cron_status_text(summary.cron_status, summary.paused_until),
                last_run_cell,
                self._countdown(summary.next_run_at),
                self._time(project.get("created_at")),
                key=project_id,
            )

        self._render_environment_resources(overview)

    def _render_environment_resources(self, overview: WorkspaceOverviewData) -> None:
        buckets = self._fill_table("buckets-table")
        bucket_rows = {str(item.get("id") or item.get("name")): item for item in overview.buckets if item.get("name")}
        self._workspace_resource_rows["buckets-table"] = bucket_rows
        for key, item in bucket_rows.items():
            buckets.add_row(
                str(item.get("name", "-")),
                str(item.get("uri") or "-"),
                self._time(item.get("created_at")),
                self._time(item.get("updated_at")),
                key=key,
            )

        self._render_secrets(overview.secrets)

    def _render_secrets(self, rows: list[dict[str, Any]]) -> None:
        """Just the secrets table, because it arrives after everything around it."""
        secrets = self._fill_table("secrets-table")
        secret_rows = {str(item.get("name")): item for item in rows if item.get("name")}
        self._workspace_resource_rows["secrets-table"] = secret_rows
        for key, item in secret_rows.items():
            keys = item.get("keys")
            secrets.add_row(
                str(item.get("name", "-")),
                ", ".join(str(value) for value in keys) if isinstance(keys, list) else "-",
                key=key,
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

    def _render_project_targets(self, targets: ProjectTargetsData, *, preserve: bool = False) -> None:
        """Draw both target tables. With *preserve*, keep where the reader already was.

        A repaint rebuilds the rows, which drops the cursor back to the top and would
        otherwise clear the runs box below. Under *preserve* the reader's place is put back
        by `_preserve_view` — cursor by key, marked rows, and scroll — and anything already
        open is left alone.
        """
        self._last_runs = targets.last_runs
        self._steps_by_function = targets.steps_by_function()
        self._function_rows = self._ordered_functions(targets)
        self._workflow_rows = {str(item["id"]): item for item in targets.workflows if item.get("id") is not None}
        self._endpoints_by_target = endpoints_by_target(targets.endpoints)
        self._ephemeral_rows = {group.row_key: group for group in targets.ephemeral}

        functions = self._fill_table("functions-table")
        for function_id, function in self._function_rows.items():
            steps = self._steps_by_function.get(function_id, [])
            functions.add_row(
                str(function.get("name", "-")),
                ORIGIN_DEPLOYED,
                format_step_workflows(steps),
                format_step_keys(steps),
                format_execution(function),
                format_bool(function.get("enabled")),
                format_endpoint(self._target_endpoints("function", function_id)),
                self._time(self._last_runs.get(function_id)),
                compact_id(function.get("current_version_id")),
                self._time(function.get("updated_at")),
                key=function_id,
            )
        for group in targets.ephemeral_by_type("function"):
            functions.add_row(
                group.name,
                ORIGIN_ONE_OFF,
                "-",
                "-",
                group.run_type,
                "-",
                "-",
                self._time(group.last_run),
                "-",
                "-",
                key=group.row_key,
            )

        workflows = self._fill_table("workflows-table")
        for workflow_id, workflow in self._workflow_rows.items():
            version = targets.workflow_versions.get(workflow_id)
            workflows.add_row(
                str(workflow.get("name", "-")),
                ORIGIN_DEPLOYED,
                format_workflow_source(version),
                format_workflow_commit(version),
                format_execution(workflow),
                format_bool(workflow.get("enabled")),
                format_endpoint(self._target_endpoints("workflow", workflow_id)),
                format_schedule(
                    workflow.get("schedule"),
                    paused=bool(workflow.get("paused")) and _pause_in_effect(workflow.get("paused_until")),
                ),
                self._time(workflow.get("next_run_at")),
                self._time(self._last_runs.get(workflow_id)),
                compact_id(workflow.get("current_version_id")),
                self._time(workflow.get("updated_at")),
                key=workflow_id,
            )
        for group in targets.ephemeral_by_type("workflow"):
            # State, endpoint, schedule, next run, version, updated: all deployment-time
            # facts, and a one-off has none of them. Dashes rather than blanks, so each
            # reads as "does not have one" rather than "failed to load".
            workflows.add_row(
                group.name,
                ORIGIN_ONE_OFF,
                "-",
                "-",
                group.run_type,
                "-",
                "-",
                "-",
                "-",
                self._time(group.last_run),
                "-",
                "-",
                key=group.row_key,
            )

        if not preserve:
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

    def _render_runs(self, runs: list[dict[str, Any]], *, preserve: bool = False) -> None:
        """Draw the runs box. With *preserve*, leave the timeline below it alone.

        Clearing the timeline is right when a different target was opened — what is under
        the runs box then describes a run of something else. It is wrong on a refresh: the
        reader is looking at that timeline, and a finished run is deliberately not re-read,
        so wiping it left an empty screen until a keypress redrew it from memory.
        """
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
        if not preserve:
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
        self._sync_timeline_tabs()
        run_id = str(detail.run.get("id", ""))
        logs = self._run_logs.get(run_id)
        # A finished run has nothing in flight, whatever its events still say. The platform
        # used to open stages it never closed — `step-graph` on every workflow run — leaving
        # a row reading `running` under a run that had succeeded minutes earlier. That is
        # fixed at the source, but events already written keep their status forever, and the
        # timeline should not be able to claim a finished run is still working regardless.
        run_finished = str(detail.run.get("status") or "") in TERMINAL_RUN_STATUSES
        kinds = dict((tab_id, kinds) for tab_id, _, kinds in TIMELINE_FILTERS)[self._timeline_filter]
        showing_all = self._timeline_filter == TIMELINE_FILTERS[0][0]
        table_name = {
            "timeline-steps": "timeline-table-steps",
            "timeline-tasks": "timeline-table-tasks",
            "timeline-artifacts": "timeline-table-artifacts",
        }.get(self._timeline_filter, "timeline-table-all" if showing_all else "timeline-table")
        dependencies = step_dependencies(detail.step_graph)
        table = self._fill_table(table_name, widget="timeline-table")
        message_width = self._timeline_message_width()
        self._timeline_rows = {}
        artifacts_by_task = Counter(
            str(artifact["task_id"]) for artifact in detail.artifacts if artifact.get("task_id") is not None
        )
        shown = 0
        for row in build_timeline(detail.events, detail.steps, logs, detail.tasks, detail.artifacts):
            if row.kind not in kinds:
                continue
            shown += 1
            # An expanded row keeps its full text, wrapped to what is on screen, and
            # grows to fit. Wrapped here rather than left to the column, because the
            # column is as wide as the longest *un*expanded line and that is the width
            # this row is trying to escape.
            expanded = shown - 1 in self._expanded_timeline
            message = textwrap.fill(row.message, message_width) if expanded else collapse_message(row.message)
            row_key = str(shown - 1)
            self._timeline_rows[row_key] = row

            if row.kind == "step" and self._timeline_filter == "timeline-steps":
                # A root step is shown with "-" rather than an empty cell: blank reads as
                # "not known", and having no dependencies is a fact about the DAG.
                step = row.record
                upstream = dependencies.get(str(step.get("node_key") or ""))
                depends_on = ", ".join(upstream) if upstream else "-"
                # The error alone, not the composed summary the Activity view shows:
                # attempt and finished have columns of their own here, and repeating
                # them in Message would spend the widest column saying it twice.
                outcome = format_json_summary(step.get("error"), max_length=160)
                outcome = textwrap.fill(outcome, message_width) if expanded else collapse_message(outcome)
                attempt = step.get("attempt")
                table.add_row(
                    Text(row.stage, style=MARK_STYLE),
                    Text(depends_on, style=BRAND_MEDIUM_GRAY),
                    status_text(row.status),
                    Text(str(attempt) if attempt not in {None, ""} else "-", style=BRAND_MEDIUM_GRAY),
                    self._time(step.get("started_at")),
                    self._time(step.get("finished_at")),
                    format_duration(step.get("started_at"), step.get("finished_at")),
                    Text(outcome, style=BRAND_MEDIUM_GRAY),
                    height=outcome.count("\n") + 1 if expanded else 1,
                    key=row_key,
                )
                continue

            if row.kind == "task" and self._timeline_filter == "timeline-tasks":
                task = row.record
                outcome = format_json_summary(task.get("error"), max_length=160)
                if outcome == "-":
                    outcome = format_json_summary(task.get("result"), max_length=160)
                outcome = textwrap.fill(outcome, message_width) if expanded else collapse_message(outcome)
                task_id = task.get("id")
                table.add_row(
                    row.stage,
                    row.scope if row.scope != "run" else "-",
                    str(task.get("kind") or "-"),
                    status_text(row.status),
                    self._time(task.get("started_at") or task.get("created_at")),
                    format_duration(task.get("started_at"), task.get("finished_at")),
                    str(artifacts_by_task.get(str(task_id), 0)) if task_id is not None else "0",
                    Text(outcome, style=BRAND_MEDIUM_GRAY),
                    height=outcome.count("\n") + 1 if expanded else 1,
                    key=row_key,
                )
                continue

            if row.kind == "artifact" and self._timeline_filter == "timeline-artifacts":
                artifact = row.record
                uri = str(artifact.get("uri") or "-")
                shown_uri = textwrap.fill(uri, message_width) if expanded else collapse_message(uri)
                table.add_row(
                    row.stage,
                    row.scope,
                    row.status,
                    str(artifact.get("media_type") or "-"),
                    format_bytes(artifact.get("size_bytes")),
                    Text(shown_uri, style=f"link {row.url}" if row.url else ""),
                    height=shown_uri.count("\n") + 1 if expanded else 1,
                    key=row_key,
                )
                continue

            cells: list[Any] = [self._time(row.at)]
            if showing_all:
                cells.append(Text(row.kind, style=BRAND_MEDIUM_GRAY))
                cells.append(Text(row.scope, style=BRAND_MEDIUM_GRAY))
            if row.kind == "log":
                # A log row is left flush with the messages around it: indenting it read as
                # ragged rather than as nesting, and the `log` type beside it already says
                # what the row is. Severity stays out of the Status column — that column
                # means lifecycle for an event and outcome for a step, and `INFO` is
                # neither — so a level worth naming leads the message instead.
                message_cell = Text()
                if row.status.strip().upper() not in ROUTINE_LOG_SEVERITIES:
                    message_cell.append(f"{row.status} ", style=status_style(row.status))
                message_cell.append(message, style=BRAND_MEDIUM_GRAY)
                cells.extend((Text(""), Text(""), message_cell))
            else:
                stale = run_finished and row.kind == "event" and row.status.strip().lower() == "running"
                item = row.stage
                if not showing_all and row.kind in {"task", "artifact"} and row.scope != "run":
                    item = f"{row.scope} › {item}"
                cells.extend(
                    (
                        Text(item, style=MARK_STYLE if row.kind == "step" else ""),
                        Text(row.status, style=BRAND_MEDIUM_GRAY) if stale else status_text(row.status),
                        Text(message, style=f"link {row.url}" if row.url else ""),
                    )
                )
            table.add_row(*cells, height=message.count("\n") + 1 if expanded else 1, key=row_key)
        # Activity, Events and Logs are stable views even before they have rows; an
        # explicit note distinguishes that state from a table that failed to paint.
        if not shown:
            note = TIMELINE_EMPTY.get(self._timeline_filter, "Nothing to show.")
            table.add_row(*self._timeline_note(showing_all, "empty", note))
        elif logs is not None and len(logs) >= RUN_LOG_LIMIT and "log" in kinds:
            table.add_row(
                *self._timeline_note(
                    showing_all, "truncated", f"Showing the first {RUN_LOG_LIMIT} log lines of this run."
                )
            )

    def _timeline_message_width(self) -> int:
        """How wide the run-detail table's final prose column can be.

        Everything to its left keeps its width; Summary, Result/Error or URI gets
        whatever the pane has left before a horizontal scrollbar is necessary.
        """
        table = self.query_one("#timeline-table", DataTable)
        columns = list(table.columns.values())
        visible = table.scrollable_content_region.width
        if len(columns) < 2 or visible <= 0:
            return TIMELINE_WRAP_FALLBACK
        spent = sum(column.get_render_width(table) for column in columns[:-1])
        return max(TIMELINE_WRAP_MINIMUM, visible - spent - 2)

    @staticmethod
    def _timeline_note(showing_all: bool, status: str, message: str) -> list[Any]:
        status_cell = Text(status, style=BRAND_AMBER)
        message_cell = Text(message, style=BRAND_MEDIUM_GRAY)
        return ["", "", "", "", status_cell, message_cell] if showing_all else ["", "", status_cell, message_cell]

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
        self._timeline_rows = {}
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
        self._visible_boxes()[0].focus()

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
        self.environment_name = self.data.environment_name
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
        # The structured diagnosis, when the server produced one: why the run
        # died and which knob to turn, in toolkit vocabulary.
        reason = run.get("failure_reason")
        if isinstance(reason, dict) and reason.get("message"):
            output["diagnosis"] = {key: reason[key] for key in ("message", "hint") if reason.get(key)}
        sections: list[Any] = [{"parameters": run.get("parameters") or {}}, output]
        status = str(run.get("status", "unknown"))
        fields = [
            DetailField("Run ID", str(run.get("id", "-"))),
            DetailField("Status", status, status_style(status)),
        ]
        timing = run_timing_summary(run)
        if timing:
            fields.append(DetailField("Timing", timing))
        return DetailDrawer(
            fields=fields,
            sections=sections,
        )

    @staticmethod
    def _task_drawer(row: TimelineRow) -> DetailDrawer:
        task = row.record
        output: dict[str, Any] = {"result": task.get("result")}
        if task.get("error"):
            output["error"] = task["error"]
        if task.get("error_type"):
            output["error_type"] = task["error_type"]
        status = str(task.get("status") or row.status or "unknown")
        return DetailDrawer(
            fields=[
                DetailField("Task", str(task.get("name") or row.stage)),
                DetailField("Status", status, status_style(status)),
                DetailField("Scope", row.scope),
                DetailField("Kind", str(task.get("kind") or "-")),
                DetailField("Task ID", str(task.get("id") or "-")),
            ],
            sections=[{"parameters": task.get("parameters") or {}}, output],
        )

    @staticmethod
    def _artifact_drawer(row: TimelineRow) -> DetailDrawer:
        artifact = row.record
        disposition = str(artifact.get("disposition") or row.status or "created")
        return DetailDrawer(
            fields=[
                DetailField("Artifact", str(artifact.get("name") or row.stage)),
                DetailField("Disposition", disposition),
                DetailField("Scope", row.scope),
                DetailField("URI", str(artifact.get("uri") or "-")),
                DetailField("Artifact ID", str(artifact.get("id") or "-")),
                DetailField("Producer run", str(artifact.get("producer_run_id") or "-")),
            ],
            sections=[detail_payload(artifact)],
        )

    def _selected_details(self) -> DetailDrawer | None:
        """The drawer for the focused table's row, or None when there is nothing to show."""
        focused = self.focused
        table_id = str(focused.id) if isinstance(focused, DataTable) else ""
        if table_id == "timeline-table":
            row_key = self._cursor_key(focused)
            row = self._timeline_rows.get(row_key or "")
            if row is not None and row.kind == "task":
                return self._task_drawer(row)
            if row is not None and row.kind == "artifact":
                return self._artifact_drawer(row)
            # Events, steps and log lines are observations within the run rather than
            # richer resources of their own, so their detail action remains the run.
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
        if table_id in self._workspace_resource_rows:
            item = self._workspace_resource_rows[table_id].get(key)
            if item is None:
                return None
            kind = table_id.removesuffix("-table").removesuffix("s").title()
            return DetailDrawer(
                fields=[DetailField(kind, str(item.get("name", "-")))],
                sections=[detail_payload(item, environment=self.environment_name)],
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
            version = self.project_targets.workflow_versions.get(key) if self.project_targets is not None else None
            own = sorted(
                (step for steps in self._steps_by_function.values() for step in steps if step.workflow_id == key),
                key=lambda step: step.order,
            )
            fields = [
                DetailField("Workflow", str(item.get("name", "-"))),
                DetailField("State", format_bool(item.get("enabled"))),
            ]
            source = format_workflow_source(version)
            if source != "-":
                fields.append(DetailField("Source", source))
            commit = format_workflow_commit(version, compact=False)
            if commit != "-":
                fields.append(DetailField("Commit", commit))
            return DetailDrawer(
                fields=fields,
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
        # Ahead of the two target branches, not after them: a one-off row lives in those
        # same tables, and either branch would look its synthetic key up among the real
        # targets, find nothing, and silently return.
        elif str(row_id).startswith(EPHEMERAL_ROW_PREFIX):
            self._select_ephemeral(str(row_id))
        elif event.data_table.id == "functions-table":
            target = self._function_rows.get(row_id)
            if target is None:
                return
            self.selected_target_type = "function"
            self.selected_target = target
            self._reveal(1)
            target_name = str(target.get("name") or "") or None
            self._runs_reload = partial(self._load_runs, "function", row_id, name=target_name)
            self.run_worker(
                self._load_runs("function", row_id, name=target_name),
                name="runs",
                group="tui",
                exclusive=True,
            )
        elif event.data_table.id == "workflows-table":
            target = self._workflow_rows.get(row_id)
            if target is None:
                return
            self.selected_target_type = "workflow"
            self.selected_target = target
            self._reveal(1)
            target_name = str(target.get("name") or "") or None
            self._runs_reload = partial(self._load_runs, "workflow", row_id, name=target_name)
            self.run_worker(
                self._load_runs("workflow", row_id, name=target_name),
                name="runs",
                group="tui",
                exclusive=True,
            )
        elif event.data_table.id == "timeline-table":
            # Enter, or a click, opens the row out to its full text and closes it again.
            # A log line is the one thing here that does not fit its row, and scrolling
            # sideways to read one sentence is worse than letting it wrap.
            position = int(row_id) if str(row_id).isdigit() else None
            if position is None:
                return
            self._expanded_timeline.symmetric_difference_update({position})
            self._render_timeline()
            self.query_one("#timeline-table", DataTable).move_cursor(row=position)
        elif event.data_table.id == "runs-table" and row_id in self._run_rows:
            self.run_worker(self._load_run_detail(row_id), name="run-detail", group="tui", exclusive=True)
        elif event.data_table.id == "workspace-profiles-table":
            self._select_workspace_profile(row_id)

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
        # The environment rides along in the title rather than owning a row of its
        # own: it is one short word, it qualifies the workspace rather than standing
        # beside it, and this way it stays on screen in the project view too.
        title = f"Rebase TUI - Workspace: {self._workspace_label()} ({self.environment_name})"
        if self.current_view == "project" and self.selected_project is not None:
            title = f"{title} / {self.selected_project.get('name', '-')}"
        if self._marked_count:
            title = f"{title} - {self._marked_count} marked"
        if self._terminal_select:
            title = f"{title} - select text (mouse off, s to resume)"
        self.title = title

    def _set_error(self, error: Exception, *, announce: bool = True) -> None:
        """Report a failed load. Quiet on a timer until it stops looking like a blip.

        A refresh the reader asked for should say what went wrong. One the timer asked for
        should not switch their view or raise a toast over a single dropped request — it
        keeps the last good data and counts. Three in a row is no longer a blip, and gets
        the loud treatment.
        """
        if not announce:
            self._refresh_failures += 1
            if self._refresh_failures < AUTO_REFRESH_FAILURE_LIMIT:
                return
        self._refresh_failures = 0
        self._show_project_view()
        self._set_project_error(f"The Rebase API request failed. Press r to retry.\nError: {error}")
        self.notify(f"Rebase API request failed: {error}", severity="error")


def run_tui(
    *,
    project: str | None = None,
    limit: int = 100,
    client: Client | None = None,
    refresh_interval: float = AUTO_REFRESH_SECONDS,
) -> None:
    RebaseTuiApp(client=client, project=project, limit=limit, refresh_interval=refresh_interval).run()
