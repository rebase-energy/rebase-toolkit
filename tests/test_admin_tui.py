"""`rebase admin`: the pure helpers, and the app driven through Textual's pilot."""

from __future__ import annotations

import asyncio
from typing import Any, cast

from textual.coordinate import Coordinate
from textual.widgets import DataTable, OptionList

from rebase.admin_format import (
    choices_for,
    credit_change_blocks,
    format_cpu,
    format_field_value,
    format_memory,
    format_money,
    workspace_row,
)
from rebase.admin_tui import (
    QUOTA_TABLE_ID,
    WORKSPACES_TABLE_ID,
    AdminTuiData,
    CreditBlockConfirmScreen,
    QuotaChoiceScreen,
    QuotaFieldScreen,
    QuotaValueScreen,
    RebaseAdminApp,
    WorkspaceDetail,
)
from rebase.client import Client, RebaseWorkflowError


def _policy(workspace_id: str, **overrides: Any) -> dict[str, Any]:
    policy = {
        "workspace_id": workspace_id,
        "currency": "EUR",
        "monthly_credit_cents": 2000,
        "cloud_run_enabled": True,
        "gpu_allowed": False,
        "max_concurrent_cloud_run_runs": 6,
        "max_run_timeout_seconds": 300,
        "max_cloud_run_min_instances": 0,
        "max_cloud_run_concurrency": 1,
        "max_cloud_run_instances": 2,
        "max_cloud_run_cpu_milli": 1000,
        "max_cloud_run_memory_mib": 512,
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-01T00:00:00Z",
    }
    policy.update(overrides)
    return policy


def _member(email: str, role: str = "Owner") -> dict[str, Any]:
    return {
        "profile_id": "22222222-2222-2222-2222-222222222222",
        "email": email,
        "github_username": None,
        "display_name": None,
        "role": role,
        "enabled": True,
    }


class FakeAdminClient:
    """Duck-types the four admin methods; records every write."""

    def __init__(self, *, refuse: str | None = None) -> None:
        self.refuse = refuse
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.workspaces: list[dict[str, Any]] = [
            {
                "id": "agent-work",
                "name": "agent-work",
                "created_at": "2026-08-09T00:00:00Z",
                "members": [_member("sebastian@rebase.energy"), _member("colleague@rebase.energy", "Viewer")],
                "policy": _policy("agent-work", max_cloud_run_memory_mib=1024),
                "policy_defaulted": False,
            },
            {
                "id": "fresh",
                "name": None,
                "created_at": "2026-09-01T00:00:00Z",
                "members": [],
                "policy": _policy("fresh"),
                "policy_defaulted": True,
            },
        ]
        self.usage: dict[str, dict[str, Any]] = {
            "agent-work": {
                "workspace_id": "agent-work",
                "currency": "EUR",
                "monthly_credit_cents": 2000,
                "finalized_spend_cents": 1500,
                "active_reservation_cents": 100,
                "remaining_cents": 400,
                "compute_blocked": False,
            },
            "fresh": {
                "workspace_id": "fresh",
                "currency": "EUR",
                "monthly_credit_cents": 2000,
                "finalized_spend_cents": 0,
                "active_reservation_cents": 0,
                "remaining_cents": 2000,
                "compute_blocked": False,
            },
        }

    def list_admin_workspaces(self, *, limit: int = 200) -> list[dict[str, Any]]:
        return [dict(workspace) for workspace in self.workspaces]

    def get_admin_workspace_usage(self, workspace_id: str) -> dict[str, Any]:
        return dict(self.usage[workspace_id])

    def update_admin_compute_policy(self, workspace_id: str, **limits: int) -> dict[str, Any]:
        self.calls.append(("policy", workspace_id, dict(limits)))
        if self.refuse:
            raise RebaseWorkflowError(self.refuse)
        for workspace in self.workspaces:
            if workspace["id"] == workspace_id:
                workspace["policy"].update(limits)
                workspace["policy_defaulted"] = False
                return dict(workspace["policy"])
        raise AssertionError(workspace_id)

    def update_admin_credit_grant(self, workspace_id: str, *, monthly_credit_cents: int) -> dict[str, Any]:
        self.calls.append(("credit", workspace_id, {"monthly_credit_cents": monthly_credit_cents}))
        usage = dict(self.usage[workspace_id])
        spent = usage["finalized_spend_cents"] + usage["active_reservation_cents"]
        usage["monthly_credit_cents"] = monthly_credit_cents
        usage["remaining_cents"] = max(monthly_credit_cents - spent, 0)
        usage["compute_blocked"] = usage["remaining_cents"] <= 0
        self.usage[workspace_id] = usage
        return usage


def _app(client: FakeAdminClient) -> RebaseAdminApp:
    return RebaseAdminApp(data=AdminTuiData(cast(Client, client)), refresh_interval=0)


# --- pure helpers ----------------------------------------------------------------


def test_admin_format_helpers() -> None:
    assert format_memory(512) == "512 MiB"
    assert format_memory(2048) == "2 GiB"
    assert format_memory(1536) == "1.5 GiB"
    assert format_memory(1100) == "1100 MiB"
    assert format_cpu(1000) == "1 vCPU"
    assert format_cpu(2500) == "2.5 vCPU"
    assert format_money(2000, "EUR") == "€20.00"
    assert format_money(123456, "USD") == "$1,234.56"


def test_credit_change_blocks_mirrors_the_server() -> None:
    usage = {"finalized_spend_cents": 1500, "active_reservation_cents": 100}
    assert credit_change_blocks(usage, 1600) is True, "remaining == 0 blocks, as the server's <= 0 does"
    assert credit_change_blocks(usage, 1601) is False
    assert credit_change_blocks(usage, 0) is True


def test_workspace_row_flags_a_defaulted_policy() -> None:
    client = FakeAdminClient()
    busy, fresh = (workspace_row(workspace) for workspace in client.workspaces)
    assert busy[0] == "agent-work" and busy[2] == "2" and busy[4] == "1 GiB" and busy[-1] == ""
    assert fresh[0] == "fresh", "a nameless workspace shows its id"
    assert fresh[-1] == "defaults"


# --- the app -----------------------------------------------------------------------


def test_field_choices_fold_in_the_current_value_and_leave_continuous_fields_free() -> None:
    assert choices_for("max_cloud_run_memory_mib", 512) == (512, 1024, 2048, 4096, 8192, 16384, 32768)
    assert choices_for("max_cloud_run_memory_mib", 1536) == (512, 1024, 1536, 2048, 4096, 8192, 16384, 32768)
    assert choices_for("max_cloud_run_cpu_milli", 1000) is None, "milli-vCPU is continuous"
    assert choices_for("monthly_credit_cents", 2000) is None, "money is continuous"
    assert format_field_value("max_run_timeout_seconds", 300) == "5 min"
    assert format_field_value("max_run_timeout_seconds", 90) == "90 s"
    assert format_field_value("monthly_credit_cents", 2000) == "€20.00  (2000 cents)"


def test_admin_tui_lists_every_workspace_and_flags_defaults() -> None:
    async def scenario() -> None:
        app = _app(FakeAdminClient())
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.3)
            table = app.query_one(f"#{WORKSPACES_TABLE_ID}", DataTable)
            assert table.row_count == 2
            assert str(table.get_cell_at(Coordinate(0, 0))) == "agent-work"
            assert str(table.get_cell_at(Coordinate(1, 8))) == "defaults"
            assert app.sub_title == "2 workspaces"
            # The same brand theme as the main TUI: green key hints, not orange.
            assert app.theme == "rebase"
            # Nothing selected yet: the pane below invites rather than shows.
            assert app.query_one("#admin-detail-members", DataTable).display is False

    asyncio.run(scenario())


def test_admin_tui_enter_fills_the_pane_beneath_the_list() -> None:
    """Details go under the table, not over it -- the list stays readable."""

    async def scenario() -> None:
        app = _app(FakeAdminClient())
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("enter")
            await pilot.pause(0.5)

            assert len(app.screen_stack) == 1, "no modal was pushed"
            assert app.detail_workspace_id == "agent-work"
            members = app.query_one("#admin-detail-members", DataTable)
            assert members.display is True and members.row_count == 2
            assert str(members.get_cell_at(Coordinate(1, 1))) == "Viewer"
            quota = app.query_one(f"#{QUOTA_TABLE_ID}", DataTable)
            assert quota.row_count == 7
            assert str(quota.get_cell_at(Coordinate(0, 1))) == "1 GiB"
            assert "€20.00" in str(quota.get_cell_at(Coordinate(6, 1)))
            usage = str(app.query_one("#admin-detail-usage").render())
            assert "€15.00" in usage and "€4.00" in usage
            # The cursor followed enter down into the quota table.
            assert quota.has_focus and quota.cursor_row == 0

            # b closes the pane and hands the cursor back to the list.
            await pilot.press("b")
            await pilot.pause(0.1)
            assert app.detail_workspace_id is None
            assert app.query_one("#admin-detail-members", DataTable).display is False
            table = app.query_one(f"#{WORKSPACES_TABLE_ID}", DataTable)
            assert table.has_focus

            # The list still drives the pane.
            table.move_cursor(row=1)
            await pilot.press("enter")
            await pilot.pause(0.5)
            assert app.detail_workspace_id == "fresh"
            assert app.query_one("#admin-detail-members", DataTable).display is False, "no members to show"
            assert "no policy row yet" in str(app.query_one("#admin-detail-quota-heading").render())

            # escape does the same as b; with nothing open, both are no-ops.
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert app.detail_workspace_id is None and table.has_focus
            await pilot.press("escape", "b")
            await pilot.pause(0.1)
            assert len(app.screen_stack) == 1 and app.is_running

    asyncio.run(scenario())


def test_admin_tui_memory_is_picked_from_cloud_run_tiers_and_the_cursor_survives() -> None:
    async def scenario() -> None:
        client = FakeAdminClient()
        app = _app(client)
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.3)
            table = app.query_one(f"#{WORKSPACES_TABLE_ID}", DataTable)
            table.move_cursor(row=1)  # fresh, currently 512 MiB
            await pilot.press("e")
            await pilot.pause(0.1)
            assert isinstance(app.screen, QuotaFieldScreen)
            await pilot.press("enter")  # first field: memory ceiling
            await pilot.pause(0.1)
            assert isinstance(app.screen, QuotaChoiceScreen), "memory is a pick-list, not free text"
            options = app.screen.query_one("#quota-choice-options", OptionList)
            assert options.highlighted == 0, "the current value (512) starts highlighted"
            await pilot.press("down", "down", "down", "enter")  # 4096
            await pilot.pause(0.5)

            assert client.calls == [("policy", "fresh", {"max_cloud_run_memory_mib": 4096})]
            assert str(table.get_cell_at(Coordinate(1, 4))) == "4 GiB"
            assert str(table.get_cell_at(Coordinate(1, 8))) == "", "a written row is no longer on defaults"
            assert table.cursor_row == 1, "the repaint after the edit must not lose the reader's place"

    asyncio.run(scenario())


def test_admin_tui_custom_drops_from_the_list_to_free_text() -> None:
    async def scenario() -> None:
        client = FakeAdminClient()
        app = _app(client)
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("e")
            await pilot.pause(0.1)
            await pilot.press("enter")  # memory
            await pilot.pause(0.1)
            assert isinstance(app.screen, QuotaChoiceScreen)
            await pilot.press("end", "enter")  # last entry: Custom…
            await pilot.pause(0.1)
            assert isinstance(app.screen, QuotaValueScreen)
            await pilot.press(*"1536", "enter")
            await pilot.pause(0.5)

            assert client.calls == [("policy", "agent-work", {"max_cloud_run_memory_mib": 1536})]

    asyncio.run(scenario())


def test_admin_tui_vcpu_and_credit_are_free_text() -> None:
    async def scenario() -> None:
        client = FakeAdminClient()
        app = _app(client)
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("e")
            await pilot.pause(0.1)
            await pilot.press("down", "enter")  # second field: vCPU ceiling
            await pilot.pause(0.1)
            assert isinstance(app.screen, QuotaValueScreen), "milli-vCPU is continuous"
            await pilot.press(*"2500", "enter")
            await pilot.pause(0.5)
            assert client.calls == [("policy", "agent-work", {"max_cloud_run_cpu_milli": 2500})]

            await pilot.press("e")
            await pilot.pause(0.1)
            await pilot.press(*(["down"] * 6), "enter")  # seventh field: monthly credit
            await pilot.pause(0.1)
            assert isinstance(app.screen, QuotaValueScreen), "money is continuous"
            await pilot.press("escape")
            await pilot.pause(0.1)

    asyncio.run(scenario())


def test_admin_tui_asks_twice_before_a_credit_change_blocks_compute() -> None:
    async def scenario() -> None:
        client = FakeAdminClient()
        app = _app(client)
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.3)
            # agent-work has spent 1600 this month; 1000 would block it at once.
            await pilot.press("e")
            await pilot.pause(0.1)
            await pilot.press(*(["down"] * 6), "enter")
            await pilot.pause(0.1)
            await pilot.press(*"1000", "enter")
            await pilot.pause(0.5)

            assert isinstance(app.screen, CreditBlockConfirmScreen), "must warn before writing"
            assert not [call for call in client.calls if call[0] == "credit"]
            await pilot.press("escape")
            await pilot.pause(0.3)
            assert not [call for call in client.calls if call[0] == "credit"], "escape must not write"

            await pilot.press("e")
            await pilot.pause(0.1)
            await pilot.press(*(["down"] * 6), "enter")
            await pilot.pause(0.1)
            await pilot.press(*"1000", "enter")
            await pilot.pause(0.5)
            assert isinstance(app.screen, CreditBlockConfirmScreen)
            await pilot.press("enter")
            await pilot.pause(0.5)

            assert ("credit", "agent-work", {"monthly_credit_cents": 1000}) in client.calls
            assert client.usage["agent-work"]["compute_blocked"] is True

    asyncio.run(scenario())


def test_admin_tui_edit_refreshes_the_open_pane() -> None:
    """An edit to the workspace on show must be visible below without pressing enter again."""

    async def scenario() -> None:
        client = FakeAdminClient()
        app = _app(client)
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("enter")
            await pilot.pause(0.5)
            quota = app.query_one(f"#{QUOTA_TABLE_ID}", DataTable)
            assert str(quota.get_cell_at(Coordinate(0, 1))) == "1 GiB"

            # The cursor is on the memory row: e goes straight to its pick-list.
            await pilot.press("e")
            await pilot.pause(0.1)
            assert isinstance(app.screen, QuotaChoiceScreen), "no field picker when a setting is highlighted"
            assert app.screen.field == "max_cloud_run_memory_mib"
            await pilot.press("down", "down", "enter")  # 1024 -> 4096
            await pilot.pause(0.5)

            assert client.calls == [("policy", "agent-work", {"max_cloud_run_memory_mib": 4096})]
            assert str(quota.get_cell_at(Coordinate(0, 1))) == "4 GiB", "the pane repainted without another enter"
            assert quota.has_focus and quota.cursor_row == 0, "and kept the cursor on the setting just changed"
            assert isinstance(app.query_one("#admin-detail"), WorkspaceDetail)

            # Any row: two down is the run timeout, and enter edits it too.
            await pilot.press("down", "down", "enter")
            await pilot.pause(0.1)
            assert isinstance(app.screen, QuotaChoiceScreen)
            assert app.screen.field == "max_run_timeout_seconds"
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert quota.cursor_row == 2

    asyncio.run(scenario())


def test_admin_tui_shows_the_servers_refusal_verbatim() -> None:
    async def scenario() -> None:
        refusal = "max_cloud_run_memory_mib cannot exceed 32768 MiB, the platform maximum"
        client = FakeAdminClient(refuse=refusal)
        app = _app(client)
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("e")
            await pilot.pause(0.1)
            await pilot.press("enter")  # memory
            await pilot.pause(0.1)
            await pilot.press("end", "up", "enter")  # 32 GiB, the last real tier
            await pilot.pause(0.5)

            # run_test disables toast widgets, so read the notifications themselves.
            messages = [notification.message for notification in app._notifications]
            assert any(refusal in message for message in messages), messages
            table = app.query_one(f"#{WORKSPACES_TABLE_ID}", DataTable)
            assert str(table.get_cell_at(Coordinate(0, 4))) == "1 GiB", "a refused edit changes nothing on screen"

    asyncio.run(scenario())
