"""`rebase admin`: every workspace on the platform, its members, and its quota.

A separate app rather than a tab in `RebaseTuiApp`. That one is framed around a
single workspace -- workspace switcher, environment switcher, project tabs -- and
cross-tenant administration is a different mental model under a different
credential. Adding it as a tab would leave every existing view asking "which
workspace am I in?". So this borrows the idioms (place-keeping across repaints,
the quiet auto-refresh, the drawer and modal shapes) and none of the frame.

The API it talks to is gated by the *profile*-level superadmin check, which
means a session credential: an API key is refused, and so is an address that is
not both in `SUPERADMIN_EMAILS` and under the company domain.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, nullcontext, suppress
from dataclasses import dataclass
from time import monotonic
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, OptionList, Static

from rebase.admin_format import (
    CREDIT_FIELD,
    EDITABLE_FIELDS,
    FIELD_LABELS,
    FIELD_UNITS,
    WORKSPACE_COLUMNS,
    credit_change_blocks,
    format_money,
    workspace_row,
)
from rebase.brand import BRAND_AMBER, BRAND_BRIGHT_GREEN, BRAND_CORAL_RED, BRAND_MEDIUM_GRAY
from rebase.client import Client
from rebase.tui import (
    AUTO_REFRESH_FAILURE_LIMIT,
    AUTO_REFRESH_SECONDS,
    KEYPRESS_QUIET_SECONDS,
    HeaderSafeDataTable,
    RebaseHeader,
    TableView,
)

WORKSPACES_TABLE_ID = "admin-workspaces-table"
_BACKGROUND = "#101412"


# --- data layer (no Textual) -----------------------------------------------------


class AdminTuiData:
    """Blocking client calls, kept out of the app so tests can duck-type the client."""

    def __init__(self, client: Client, *, limit: int = 200) -> None:
        self.client = client
        self.limit = limit

    def load(self) -> list[dict[str, Any]]:
        return self.client.list_admin_workspaces(limit=self.limit)

    def usage(self, workspace_id: str) -> dict[str, Any]:
        return self.client.get_admin_workspace_usage(workspace_id)

    def set_limit(self, workspace_id: str, field: str, value: int) -> dict[str, Any]:
        return self.client.update_admin_compute_policy(workspace_id, **{field: value})

    def set_credit(self, workspace_id: str, monthly_credit_cents: int) -> dict[str, Any]:
        return self.client.update_admin_credit_grant(workspace_id, monthly_credit_cents=monthly_credit_cents)


# --- modals ----------------------------------------------------------------------


class QuotaFieldScreen(ModalScreen[str | None]):
    """Which of the seven editable numbers to change."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    CSS = f"""
    QuotaFieldScreen {{
        align: center middle;
        background: {_BACKGROUND} 70%;
    }}

    #quota-field-dialog {{
        width: 60;
        height: auto;
        padding: 1 2;
        background: {_BACKGROUND};
        border: solid {BRAND_BRIGHT_GREEN};
    }}

    #quota-field-title {{
        color: {BRAND_BRIGHT_GREEN};
        text-style: bold;
        margin-bottom: 1;
    }}

    #quota-field-options {{
        height: auto;
        max-height: 12;
        background: {_BACKGROUND};
        border: none;
    }}

    #quota-field-hint {{
        color: {BRAND_MEDIUM_GRAY};
        margin-top: 1;
    }}
    """

    def __init__(self, workspace: dict[str, Any]) -> None:
        super().__init__()
        self.workspace = workspace

    def compose(self) -> ComposeResult:
        policy = self.workspace["policy"]
        with Vertical(id="quota-field-dialog"):
            yield Static(f"Edit quota — {self.workspace.get('name') or self.workspace['id']}", id="quota-field-title")
            yield OptionList(
                *(f"{FIELD_LABELS[key]}  ({policy[key]} {FIELD_UNITS[key]})" for key, _, _ in EDITABLE_FIELDS),
                id="quota-field-options",
            )
            yield Static("Enter picks a field. Escape cancels.", id="quota-field-hint")

    def on_mount(self) -> None:
        options = self.query_one("#quota-field-options", OptionList)
        options.highlighted = 0
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(EDITABLE_FIELDS[event.option_index][0])

    def action_cancel(self) -> None:
        self.dismiss(None)


class QuotaValueScreen(ModalScreen[int | None]):
    """Type the new value. Only the shape is checked here; the server is the authority.

    A non-negative integer is all this insists on -- the workspace's `max_grantable_*`
    ceiling and Cloud Run's cpu/memory coupling are the server's to enforce, and its
    409 text is surfaced verbatim rather than second-guessed.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    CSS = f"""
    QuotaValueScreen {{
        align: center middle;
        background: {_BACKGROUND} 70%;
    }}

    #quota-value-dialog {{
        width: 60;
        height: auto;
        padding: 1 2;
        background: {_BACKGROUND};
        border: solid {BRAND_BRIGHT_GREEN};
    }}

    #quota-value-title {{
        color: {BRAND_BRIGHT_GREEN};
        text-style: bold;
        margin-bottom: 1;
    }}

    #quota-value-input {{
        background: {_BACKGROUND};
        border: solid {BRAND_MEDIUM_GRAY};
    }}

    #quota-value-hint {{
        color: {BRAND_MEDIUM_GRAY};
        margin-top: 1;
    }}
    """

    def __init__(self, workspace: dict[str, Any], field: str) -> None:
        super().__init__()
        self.workspace = workspace
        self.field = field
        self.current = int(workspace["policy"][field])

    def compose(self) -> ComposeResult:
        name = self.workspace.get("name") or self.workspace["id"]
        with Vertical(id="quota-value-dialog"):
            yield Static(f"{FIELD_LABELS[self.field]} for {name} — currently {self.current}", id="quota-value-title")
            yield Input(placeholder=f"new value in {FIELD_UNITS[self.field]}", id="quota-value-input")
            yield Static("Enter applies. Escape cancels.", id="quota-value-hint")

    def on_mount(self) -> None:
        self.query_one("#quota-value-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip().replace("_", "").replace(",", "")
        if not value.isdigit():
            event.input.value = ""
            self.query_one("#quota-value-hint", Static).update(
                Text("A whole number of " + FIELD_UNITS[self.field] + ", or escape.", style=BRAND_CORAL_RED)
            )
            return
        self.dismiss(int(value))

    def action_cancel(self) -> None:
        self.dismiss(None)


class CreditBlockConfirmScreen(ModalScreen[bool]):
    """The one edit with an immediate, visible consequence gets a second Enter.

    Lowering a grant below what the workspace has already spent this month sets
    `compute_blocked` at once -- every new run is refused until the 1st. That is
    sometimes exactly the intent, so it is allowed; but never by accident.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "confirm", "Block", show=False),
    ]
    CSS = f"""
    CreditBlockConfirmScreen {{
        align: center middle;
        background: {_BACKGROUND} 70%;
    }}

    #credit-block-dialog {{
        width: 72;
        height: auto;
        padding: 1 2;
        background: {_BACKGROUND};
        border: solid {BRAND_CORAL_RED};
    }}

    #credit-block-title {{
        color: {BRAND_CORAL_RED};
        text-style: bold;
        padding-bottom: 1;
    }}

    #credit-block-hint {{
        color: {BRAND_MEDIUM_GRAY};
        padding-top: 1;
    }}
    """

    def __init__(self, workspace: dict[str, Any], *, new_monthly_cents: int, usage: dict[str, Any]) -> None:
        super().__init__()
        self.workspace = workspace
        self.new_monthly_cents = new_monthly_cents
        self.usage = usage

    def compose(self) -> ComposeResult:
        name = self.workspace.get("name") or self.workspace["id"]
        currency = str(self.usage.get("currency", "EUR"))
        spent = int(self.usage.get("finalized_spend_cents", 0)) + int(self.usage.get("active_reservation_cents", 0))
        with Vertical(id="credit-block-dialog"):
            yield Static(f"This blocks compute in {name} immediately", id="credit-block-title")
            yield Static(
                f"{name} has already used {format_money(spent, currency)} this month. A grant of "
                f"{format_money(self.new_monthly_cents, currency)} leaves nothing, so every new run "
                "will be refused until the 1st.",
                id="credit-block-body",
            )
            yield Static("Enter blocks it anyway. Escape cancels.", id="credit-block-hint")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class WorkspaceDrawer(ModalScreen[None]):
    """One workspace in full: members, policy, and this month's usage."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("p", "close", "Close"),
        Binding("q", "close", "Close"),
    ]
    CSS = f"""
    WorkspaceDrawer {{
        align: right top;
        background: {_BACKGROUND} 40%;
    }}

    #admin-drawer {{
        width: 66%;
        height: 100%;
        padding: 1 2;
        background: {_BACKGROUND};
        border-left: solid {BRAND_BRIGHT_GREEN};
    }}

    .admin-drawer-heading {{
        color: {BRAND_BRIGHT_GREEN};
        text-style: bold;
        margin-top: 1;
    }}

    #admin-drawer-body {{
        height: 1fr;
        background: {_BACKGROUND};
        scrollbar-size-vertical: 1;
        scrollbar-color: {BRAND_BRIGHT_GREEN};
    }}

    #admin-drawer-members {{
        height: auto;
        max-height: 12;
    }}

    #admin-drawer-hint {{
        color: {BRAND_MEDIUM_GRAY};
        margin-top: 1;
    }}
    """

    def __init__(self, workspace: dict[str, Any], *, usage: dict[str, Any] | None, usage_error: str | None) -> None:
        super().__init__()
        self.workspace = workspace
        self.usage = usage
        self.usage_error = usage_error

    def compose(self) -> ComposeResult:
        workspace = self.workspace
        policy = workspace["policy"]
        currency = str(policy.get("currency", "EUR"))
        with Vertical(id="admin-drawer"):
            yield Static(
                Text.assemble(
                    (str(workspace.get("name") or workspace["id"]), f"bold {BRAND_BRIGHT_GREEN}"),
                    (f"  {workspace['id']}", BRAND_MEDIUM_GRAY),
                )
            )
            with VerticalScroll(id="admin-drawer-body"):
                yield Static(f"Members ({len(workspace.get('members', []))})", classes="admin-drawer-heading")
                members = HeaderSafeDataTable(id="admin-drawer-members", cursor_type="none")
                yield members
                yield Static("Quota", classes="admin-drawer-heading")
                flagged = "  (table defaults — no policy row yet)" if workspace.get("policy_defaulted") else ""
                yield Static(
                    "\n".join(
                        f"{FIELD_LABELS[key] + ':':<26} {policy[key]} {FIELD_UNITS[key]}"
                        for key, _, _ in EDITABLE_FIELDS
                    )
                    + flagged
                )
                yield Static("This month", classes="admin-drawer-heading")
                yield Static(self._usage_text(currency), id="admin-drawer-usage")
            yield Static("e edits a quota field. Arrow keys scroll. p or escape closes.", id="admin-drawer-hint")

    def on_mount(self) -> None:
        table = self.query_one("#admin-drawer-members", HeaderSafeDataTable)
        table.add_columns("Email", "Role", "GitHub", "Enabled")
        for member in self.workspace.get("members", []):
            table.add_row(
                str(member.get("email") or "-"),
                str(member.get("role", "-")),
                str(member.get("github_username") or "-"),
                "yes" if member.get("enabled", True) else "no",
            )
        self.query_one("#admin-drawer-body", VerticalScroll).focus()

    def _usage_text(self, currency: str) -> Text:
        if self.usage is None:
            return Text(f"usage unavailable — {self.usage_error or 'not loaded'}", style=BRAND_AMBER)
        usage = self.usage
        lines = [
            f"{'Grant:':<26} {format_money(int(usage['monthly_credit_cents']), currency)}",
            f"{'Spent:':<26} {format_money(int(usage['finalized_spend_cents']), currency)}",
            f"{'Reserved:':<26} {format_money(int(usage['active_reservation_cents']), currency)}",
            f"{'Remaining:':<26} {format_money(int(usage['remaining_cents']), currency)}",
        ]
        text = Text("\n".join(lines))
        if usage.get("compute_blocked"):
            text.append("\n\ncompute is BLOCKED — the grant is exhausted", style=f"bold {BRAND_CORAL_RED}")
        return text

    def action_close(self) -> None:
        self.dismiss(None)


# --- the app ---------------------------------------------------------------------


@dataclass(frozen=True)
class AdminLoad:
    workspaces: list[dict[str, Any]]


class RebaseAdminApp(App[None]):
    TITLE = "Rebase — admin"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("p", "open_drawer", "Details"),
        Binding("e", "edit_quota", "Edit quota"),
    ]
    CSS = f"""
    Screen {{
        background: {_BACKGROUND};
        color: #E8F0ED;
    }}

    #{WORKSPACES_TABLE_ID} {{
        height: 1fr;
        background: {_BACKGROUND};
        scrollbar-size-vertical: 1;
        scrollbar-color: {BRAND_BRIGHT_GREEN};
    }}

    #admin-error {{
        color: {BRAND_CORAL_RED};
        padding: 0 1;
        display: none;
    }}

    #admin-error.visible {{
        display: block;
    }}
    """

    def __init__(
        self,
        *,
        client: Client | None = None,
        data: AdminTuiData | None = None,
        limit: int = 200,
        refresh_interval: float = AUTO_REFRESH_SECONDS,
    ) -> None:
        super().__init__()
        self.data = data or AdminTuiData(client or Client(), limit=limit)
        self.workspaces: list[dict[str, Any]] = []
        self._usage_cache: dict[str, dict[str, Any]] = {}
        self._refresh_interval = refresh_interval
        self._refresh_failures = 0
        self._last_key_at = 0.0

    def compose(self) -> ComposeResult:
        yield RebaseHeader(show_clock=True, icon="• Admin")
        yield Static("", id="admin-error")
        yield HeaderSafeDataTable(id=WORKSPACES_TABLE_ID, cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self.run_worker(self._load(preserve=False, announce=True), name="admin-load", group="admin", exclusive=True)
        if self._refresh_interval:
            self.set_interval(self._refresh_interval, self._refresh_tick)

    # -- loading & rendering ------------------------------------------------------

    async def _load(self, *, preserve: bool, announce: bool) -> None:
        try:
            workspaces = await asyncio.to_thread(self.data.load)
        except Exception as exc:
            self._set_error(exc, announce=announce)
            return
        self._refresh_failures = 0
        self._clear_error()
        self.workspaces = workspaces
        with self._preserve_view() if preserve else nullcontext():
            self._render(workspaces)

    def _render(self, workspaces: Sequence[dict[str, Any]]) -> None:
        table = self.query_one(f"#{WORKSPACES_TABLE_ID}", DataTable)
        table.clear(columns=True)
        table.add_columns(*WORKSPACE_COLUMNS)
        for workspace in workspaces:
            cells = list(workspace_row(workspace))
            if cells[-1]:
                cells[-1] = Text(cells[-1], style=BRAND_AMBER)  # type: ignore[call-overload]
            table.add_row(*cells, key=str(workspace["id"]))
        self.sub_title = f"{len(workspaces)} workspace{'s' if len(workspaces) != 1 else ''}"

    def _set_error(self, error: Exception, *, announce: bool) -> None:
        """Quiet on a timer until it stops looking like a blip -- same rule as the main TUI."""
        if not announce:
            self._refresh_failures += 1
            if self._refresh_failures < AUTO_REFRESH_FAILURE_LIMIT:
                return
        self._refresh_failures = 0
        label = self.query_one("#admin-error", Static)
        label.update(f"The Rebase API request failed. Press r to retry.\nError: {error}")
        label.add_class("visible")
        self.notify(f"Rebase API request failed: {error}", severity="error")

    def _clear_error(self) -> None:
        with suppress(NoMatches):
            self.query_one("#admin-error", Static).remove_class("visible")

    # -- place-keeping ------------------------------------------------------------

    @contextmanager
    def _preserve_view(self) -> Iterator[None]:
        """Put the reader back on the same workspace after a repaint reorders the rows."""
        view = self._table_view()
        try:
            yield
        finally:
            if view is not None:
                self._restore_table_view(view)

    def _table_view(self) -> TableView | None:
        try:
            table = self.query_one(f"#{WORKSPACES_TABLE_ID}", DataTable)
        except NoMatches:
            return None
        return TableView(
            cursor_key=self._cursor_key(table), marked=(), scroll_x=table.scroll_x, scroll_y=table.scroll_y
        )

    def _restore_table_view(self, view: TableView) -> None:
        table = self.query_one(f"#{WORKSPACES_TABLE_ID}", DataTable)
        if view.cursor_key is not None:
            with suppress(Exception):
                table.move_cursor(row=table.get_row_index(view.cursor_key))
        if view.scroll_x or view.scroll_y:
            table.scroll_to(x=view.scroll_x, y=view.scroll_y, animate=False)

    @staticmethod
    def _cursor_key(table: DataTable) -> str | None:
        if not 0 <= table.cursor_row < len(table.ordered_rows):
            return None
        value = table.ordered_rows[table.cursor_row].key.value
        return None if value is None else str(value)

    def _selected_workspace(self) -> dict[str, Any] | None:
        key = self._cursor_key(self.query_one(f"#{WORKSPACES_TABLE_ID}", DataTable))
        return next((workspace for workspace in self.workspaces if str(workspace["id"]) == key), None)

    # -- auto-refresh ---------------------------------------------------------------

    def on_key(self) -> None:
        self._last_key_at = monotonic()

    def _refresh_tick(self) -> None:
        if self._refresh_tick_paused():
            return
        self.run_worker(self._load(preserve=True, announce=False), name="admin-refresh", group="admin", exclusive=True)

    def _refresh_tick_paused(self) -> bool:
        """A repaint must not take something away: a dialog, an edit in flight, a hand on the keys."""
        if len(self.screen_stack) > 1:
            return True
        if any(worker.group == "admin-edit" for worker in self.workers):
            return True
        return monotonic() - self._last_key_at < KEYPRESS_QUIET_SECONDS

    # -- actions ----------------------------------------------------------------------

    def action_refresh(self) -> None:
        self.run_worker(self._load(preserve=True, announce=True), name="admin-load", group="admin", exclusive=True)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_open_drawer()

    def action_open_drawer(self) -> None:
        workspace = self._selected_workspace()
        if workspace is None:
            return
        self.run_worker(self._open_drawer(workspace), name="admin-drawer", group="admin-edit", exclusive=True)

    async def _open_drawer(self, workspace: dict[str, Any]) -> None:
        usage, error = await self._usage_for(str(workspace["id"]))
        self.push_screen(WorkspaceDrawer(workspace, usage=usage, usage_error=error))

    async def _usage_for(self, workspace_id: str) -> tuple[dict[str, Any] | None, str | None]:
        try:
            usage = await asyncio.to_thread(self.data.usage, workspace_id)
        except Exception as exc:
            return None, str(exc)
        self._usage_cache[workspace_id] = usage
        return usage, None

    def action_edit_quota(self) -> None:
        workspace = self._selected_workspace()
        if workspace is None:
            self.notify("Select a workspace first.", severity="warning")
            return
        self.push_screen(QuotaFieldScreen(workspace), lambda field: self._on_field_chosen(workspace, field))

    def _on_field_chosen(self, workspace: dict[str, Any], field: str | None) -> None:
        if field is None:
            return
        self.push_screen(
            QuotaValueScreen(workspace, field), lambda value: self._on_value_entered(workspace, field, value)
        )

    def _on_value_entered(self, workspace: dict[str, Any], field: str, value: int | None) -> None:
        if value is None:
            return
        coroutine = (
            self._apply_credit(workspace, value)
            if field == CREDIT_FIELD
            else self._apply_limit(workspace, field, value)
        )
        self.run_worker(coroutine, name="admin-edit", group="admin-edit", exclusive=True)

    async def _apply_limit(self, workspace: dict[str, Any], field: str, value: int) -> None:
        name = workspace.get("name") or workspace["id"]
        try:
            await asyncio.to_thread(self.data.set_limit, str(workspace["id"]), field, value)
        except Exception as exc:
            # The server's 409 is the authority; show its words, not ours.
            self.notify(str(exc), severity="error")
            return
        self.notify(f"{FIELD_LABELS[field]} for {name} is now {value} {FIELD_UNITS[field]}.")
        await self._load(preserve=True, announce=False)

    async def _apply_credit(self, workspace: dict[str, Any], monthly_credit_cents: int) -> None:
        workspace_id = str(workspace["id"])
        name = workspace.get("name") or workspace_id
        usage, _ = await self._usage_for(workspace_id)
        if usage is not None and credit_change_blocks(usage, monthly_credit_cents):
            confirmed = await self.push_screen_wait(
                CreditBlockConfirmScreen(workspace, new_monthly_cents=monthly_credit_cents, usage=usage)
            )
            if not confirmed:
                return
        try:
            result = await asyncio.to_thread(self.data.set_credit, workspace_id, monthly_credit_cents)
        except Exception as exc:
            self.notify(str(exc), severity="error")
            return
        self._usage_cache[workspace_id] = result
        currency = str(result.get("currency", "EUR"))
        suffix = " Compute is now blocked." if result.get("compute_blocked") else ""
        self.notify(
            f"Monthly credit for {name} is now {format_money(monthly_credit_cents, currency)}, this month included."
            + suffix,
            severity="warning" if result.get("compute_blocked") else "information",
        )
        await self._load(preserve=True, announce=False)


def run_admin_tui(
    *,
    client: Client | None = None,
    limit: int = 200,
    refresh_interval: float = AUTO_REFRESH_SECONDS,
) -> None:
    RebaseAdminApp(client=client, limit=limit, refresh_interval=refresh_interval).run()
