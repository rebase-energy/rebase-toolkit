from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, available_timezones

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult, RenderResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import Vertical
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

# Recomposing the header means naming its pieces, and Textual only exports the container.
# `test_tui_header_parts_still_exist` fails loudly if these move.
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
#: Header text per table, added when the rows are and never before. See `_setup_tables`.
TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "projects-table": ("Project", "Functions", "Workflows", "Cron jobs", "Endpoints"),
    "workspace-profiles-table": ("Active", "Profile", "Workspace", "Workspace ID", "API URL"),
    "functions-table": ("Name", "Run type", "State", "Endpoint", "Version", "Updated"),
    "workflows-table": ("Name", "Run type", "State", "Endpoint", "Schedule", "Next run", "Version", "Updated"),
    "asgi-apps-table": ("Name", "Base path", "Auth", "State", "URL path", "Updated"),
    "runs-table": ("Run", "Status", "Backend", "Created", "Finished"),
    "events-table": ("Time", "Stage", "Status", "Message"),
    "steps-table": ("Step", "Status", "Attempt", "Started", "Finished", "Error"),
}


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


def format_timestamp(value: Any, tz: tzinfo | None = None) -> str:
    """Render an API timestamp, converted to *tz* when it carries an offset to convert from."""
    if value in {None, ""}:
        return "-"
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
        ("d", "delete_selection", "Delete"),
        ("o", "open_source", "Open source"),
        ("s", "toggle_terminal_select", "Select text"),
        # priority: the screen's default `tab` -> focus_next otherwise shadows this.
        Binding("tab", "toggle_target_tab", "Switch target", priority=True),
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
        self._marked_count = 0
        self._terminal_select = False
        self._display_timezone: ZoneInfo | None = None

    def compose(self) -> ComposeResult:
        yield RebaseHeader(show_clock=True, icon="• Commands")
        with Vertical(id="workspace-view"):
            yield SelectableDataTable(id="projects-table")
        with Vertical(id="workspace-switcher-view"):
            yield DataTable(id="workspace-profiles-table")
        with Vertical(id="project-view"):
            yield Static("Select a project.", id="project-detail", classes="panel")
            with TabbedContent(initial="workflows-tab", id="target-tabs"):
                with TabPane("Workflows", id="workflows-tab"):
                    yield SelectableDataTable(id="workflows-table")
                with TabPane("Functions", id="functions-tab"):
                    yield SelectableDataTable(id="functions-table")
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

        asgi_apps = self.query_one("#asgi-apps-table", DataTable)
        asgi_apps.cursor_type = "row"
        asgi_apps.zebra_stripes = True

        runs = self.query_one("#runs-table", DataTable)
        runs.cursor_type = "row"
        runs.zebra_stripes = True

        events = self.query_one("#events-table", DataTable)
        events.zebra_stripes = True

        steps = self.query_one("#steps-table", DataTable)
        steps.zebra_stripes = True

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
        # Every rendered timestamp is now in the wrong zone; the reload repaints the
        # tables, and the project panel is redrawn here because nothing else will.
        if self.selected_project is not None:
            summary = self._project_rows.get(str(self.selected_project.get("id", "")))
            if summary is not None:
                self._render_project_detail(summary)
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
                "Nothing here can be deleted. ASGI apps, runs and workspaces have no delete endpoint.",
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
        self.query_one("#run-detail", Static).update(f"Loading latest {target_type} runs...")
        try:
            runs = await asyncio.to_thread(self.data.load_target_runs, target_type, target_id)
        except Exception as exc:
            self._set_error(exc)
            return
        self._render_runs(runs)

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

        projects = self._fill_table("projects-table")
        for project_id, summary in self._project_rows.items():
            project = summary.project
            projects.add_row(
                str(project.get("name", "-")),
                str(summary.workflow_count),
                str(summary.cron_count),
                str(summary.function_count),
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
        self._function_rows = {str(item["id"]): item for item in targets.functions if item.get("id") is not None}
        self._workflow_rows = {str(item["id"]): item for item in targets.workflows if item.get("id") is not None}
        self._asgi_app_rows = {str(item["id"]): item for item in targets.asgi_apps if item.get("id") is not None}
        self._endpoints_by_target = endpoints_by_target(targets.endpoints)

        functions = self._fill_table("functions-table")
        for function_id, function in self._function_rows.items():
            functions.add_row(
                str(function.get("name", "-")),
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

        asgi_apps = self._fill_table("asgi-apps-table")
        for asgi_app_id, asgi_app in self._asgi_app_rows.items():
            asgi_apps.add_row(
                str(asgi_app.get("name", "-")),
                str(asgi_app.get("base_path") or "-"),
                str(asgi_app.get("auth") or "-"),
                format_bool(asgi_app.get("enabled")),
                str(asgi_app.get("url_path") or "-"),
                self._time(asgi_app.get("updated_at")),
                key=asgi_app_id,
            )

        self._clear_target_detail(clear_project=False)
        self.query_one("#target-detail", Static).update("Select a workflow, function, or ASGI app.")

    def _target_endpoints(self, target_type: str, target_id: str) -> list[dict[str, Any]]:
        return self._endpoints_by_target.get((target_type, target_id), [])

    def _render_runs(self, runs: list[dict[str, Any]]) -> None:
        self._run_rows = {str(item["id"]): item for item in runs if item.get("id") is not None}
        table = self._fill_table("runs-table")
        for run_id, run in self._run_rows.items():
            table.add_row(
                compact_id(run_id),
                status_text(run.get("status")),
                str(run.get("execution_backend", "-")),
                self._time(run.get("created_at")),
                self._time(run.get("finished_at")),
                key=run_id,
            )
        if not runs:
            self.query_one("#run-detail", Static).update("No runs found for the selected target.")
        else:
            self.query_one("#run-detail", Static).update("Select a run.")
        self.query_one("#events-table", DataTable).clear(columns=True)
        self.query_one("#steps-table", DataTable).clear(columns=True)

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

        events = self._fill_table("events-table")
        for event in detail.events:
            events.add_row(
                self._time(event.get("created_at")),
                str(event.get("stage", "-")),
                status_text(event.get("status")),
                format_json_summary(event.get("message"), max_length=120),
            )

        steps = self._fill_table("steps-table")
        for step in detail.steps:
            steps.add_row(
                str(step.get("name") or step.get("node_key") or "-"),
                status_text(step.get("status")),
                str(step.get("attempt", "-")),
                self._time(step.get("started_at")),
                self._time(step.get("finished_at")),
                format_json_summary(step.get("error"), max_length=80),
            )

    def _clear_target_detail(self, *, clear_project: bool = True) -> None:
        if clear_project:
            self.query_one("#project-detail", Static).update("Select a project.")
            self.query_one("#functions-table", DataTable).clear(columns=True)
            self.query_one("#workflows-table", DataTable).clear(columns=True)
            self.query_one("#asgi-apps-table", DataTable).clear(columns=True)
            self._function_rows = {}
            self._workflow_rows = {}
            self._asgi_app_rows = {}
            self._endpoints_by_target = {}
        self.query_one("#target-detail", Static).update("Select a workflow, function, or ASGI app.")
        self.query_one("#runs-table", DataTable).clear(columns=True)
        self.query_one("#run-detail", Static).update("Select a run.")
        self.query_one("#events-table", DataTable).clear(columns=True)
        self.query_one("#steps-table", DataTable).clear(columns=True)
        self._run_rows = {}

    def _render_project_detail(self, summary: ProjectSummary) -> None:
        project = summary.project
        description = format_json_summary(project.get("description"), max_length=110)
        lines = [
            f"Project {project.get('name', '-')}",
            f"Functions: {summary.function_count} | Workflows: {summary.workflow_count} | "
            f"Cron jobs: {summary.cron_count} | Endpoints: {summary.endpoint_count}",
            f"Updated: {self._time(project.get('updated_at'))} | ID: {project.get('id', '-')}",
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
        title = f"Rebase TUI - Workspace: {self._workspace_label()}"
        if self._marked_count:
            title = f"{title} - {self._marked_count} marked"
        if self._terminal_select:
            title = f"{title} - select text (mouse off, s to resume)"
        self.title = title

    def _set_error(self, error: Exception) -> None:
        # The summary bar used to carry the error text; the detail panel is now the only
        # place a failed load can say what went wrong, so it gets the message itself.
        self._show_project_view()
        self.query_one("#project-detail", Static).update(
            f"The Rebase API request failed. Press r to retry.\nError: {error}"
        )
        self.notify(f"Rebase API request failed: {error}", severity="error")


def run_tui(*, project: str | None = None, limit: int = 25, client: Client | None = None) -> None:
    RebaseTuiApp(client=client, project=project, limit=limit).run()
