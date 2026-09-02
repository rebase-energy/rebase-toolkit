"""`rebase admin`: the pure helpers, and the app driven through Textual's pilot."""

from __future__ import annotations

import asyncio
from typing import Any, cast

from textual.coordinate import Coordinate
from textual.widgets import DataTable

from rebase.admin_format import (
    credit_change_blocks,
    format_cpu,
    format_memory,
    format_money,
    workspace_row,
)
from rebase.admin_tui import (
    WORKSPACES_TABLE_ID,
    AdminTuiData,
    CreditBlockConfirmScreen,
    QuotaFieldScreen,
    QuotaValueScreen,
    RebaseAdminApp,
    WorkspaceDrawer,
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


def test_admin_tui_lists_every_workspace_and_flags_defaults() -> None:
    async def scenario() -> None:
        app = _app(FakeAdminClient())
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause(0.3)
            table = app.query_one(f"#{WORKSPACES_TABLE_ID}", DataTable)
            assert table.row_count == 2
            assert str(table.get_cell_at(Coordinate(0, 0))) == "agent-work"
            assert str(table.get_cell_at(Coordinate(1, 8))) == "defaults"
            assert app.sub_title == "2 workspaces"

    asyncio.run(scenario())


def test_admin_tui_edits_a_ceiling_and_keeps_the_cursor() -> None:
    async def scenario() -> None:
        client = FakeAdminClient()
        app = _app(client)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause(0.3)
            table = app.query_one(f"#{WORKSPACES_TABLE_ID}", DataTable)
            table.move_cursor(row=1)
            await pilot.press("e")
            await pilot.pause(0.1)
            assert isinstance(app.screen, QuotaFieldScreen)
            await pilot.press("enter")  # first field: memory ceiling
            await pilot.pause(0.1)
            assert isinstance(app.screen, QuotaValueScreen)
            await pilot.press(*"4096", "enter")
            await pilot.pause(0.5)

            assert client.calls == [("policy", "fresh", {"max_cloud_run_memory_mib": 4096})]
            assert str(table.get_cell_at(Coordinate(1, 4))) == "4 GiB"
            assert str(table.get_cell_at(Coordinate(1, 8))) == "", "a written row is no longer on defaults"
            assert table.cursor_row == 1, "the repaint after the edit must not lose the reader's place"

    asyncio.run(scenario())


def test_admin_tui_asks_twice_before_a_credit_change_blocks_compute() -> None:
    async def scenario() -> None:
        client = FakeAdminClient()
        app = _app(client)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause(0.3)
            # agent-work has spent 1600 this month; 1000 would block it at once.
            await pilot.press("e")
            await pilot.pause(0.1)
            await pilot.press(*(["down"] * 6), "enter")  # seventh field: monthly credit
            await pilot.pause(0.1)
            assert isinstance(app.screen, QuotaValueScreen)
            await pilot.press(*"1000", "enter")
            await pilot.pause(0.5)

            assert isinstance(app.screen, CreditBlockConfirmScreen), "must warn before writing"
            assert not [call for call in client.calls if call[0] == "credit"]
            await pilot.press("escape")
            await pilot.pause(0.3)
            assert not [call for call in client.calls if call[0] == "credit"], "escape must not write"

            # Same edit, confirmed with enter this time.
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


def test_admin_tui_raising_credit_needs_no_confirmation() -> None:
    async def scenario() -> None:
        client = FakeAdminClient()
        app = _app(client)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("e")
            await pilot.pause(0.1)
            await pilot.press(*(["down"] * 6), "enter")
            await pilot.pause(0.1)
            await pilot.press(*"5000", "enter")
            await pilot.pause(0.5)

            assert client.calls == [("credit", "agent-work", {"monthly_credit_cents": 5000})]
            assert not isinstance(app.screen, CreditBlockConfirmScreen)

    asyncio.run(scenario())


def test_admin_tui_shows_the_servers_refusal_verbatim() -> None:
    async def scenario() -> None:
        refusal = "max_cloud_run_memory_mib cannot exceed 32768 MiB, the platform maximum"
        client = FakeAdminClient(refuse=refusal)
        app = _app(client)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("e")
            await pilot.pause(0.1)
            await pilot.press("enter")
            await pilot.pause(0.1)
            await pilot.press(*"99999", "enter")
            await pilot.pause(0.5)

            # run_test disables toast widgets, so read the notifications themselves.
            messages = [notification.message for notification in app._notifications]
            assert any(refusal in message for message in messages), messages
            table = app.query_one(f"#{WORKSPACES_TABLE_ID}", DataTable)
            assert str(table.get_cell_at(Coordinate(0, 4))) == "1 GiB", "a refused edit changes nothing on screen"

    asyncio.run(scenario())


def test_admin_tui_drawer_shows_members_and_usage() -> None:
    async def scenario() -> None:
        app = _app(FakeAdminClient())
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause(0.3)
            await pilot.press("p")
            await pilot.pause(0.5)
            assert isinstance(app.screen, WorkspaceDrawer)
            members = app.screen.query_one("#admin-drawer-members", DataTable)
            assert members.row_count == 2
            assert str(members.get_cell_at(Coordinate(1, 1))) == "Viewer"
            usage = str(app.screen.query_one("#admin-drawer-usage").render())
            assert "€15.00" in usage and "€4.00" in usage
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert not isinstance(app.screen, WorkspaceDrawer)

    asyncio.run(scenario())
