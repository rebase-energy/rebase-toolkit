"""Pure helpers shared by `rebase admin` (the TUI) and `rebase admin workspaces` (the table).

Kept apart from `admin_tui` so the headless command does not import textual: pulling
it in costs every other CLI command startup time, which is the same reason `tui_command`
defers its import.
"""

from __future__ import annotations

from typing import Any

#: The seven things a superadmin may change, in the order the edit picker offers
#: them. The six ceilings go through the compute-policy route; the credit grant
#: has its own, because it is billing rather than capacity and touches two tables.
EDITABLE_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("max_cloud_run_memory_mib", "Memory ceiling", "MiB"),
    ("max_cloud_run_cpu_milli", "vCPU ceiling", "milli-vCPU (1000 = 1 vCPU)"),
    ("max_run_timeout_seconds", "Run timeout", "seconds"),
    ("max_concurrent_cloud_run_runs", "Concurrent runs", "runs in flight"),
    ("max_cloud_run_instances", "Max instances", "instances"),
    ("max_cloud_run_concurrency", "Per-instance concurrency", "requests"),
    ("monthly_credit_cents", "Monthly credit", "cents"),
)
CREDIT_FIELD = "monthly_credit_cents"
FIELD_LABELS = {key: label for key, label, _ in EDITABLE_FIELDS}
FIELD_UNITS = {key: unit for key, _, unit in EDITABLE_FIELDS}

WORKSPACE_COLUMNS: tuple[str, ...] = (
    "Name",
    "ID",
    "Members",
    "vCPU",
    "Memory",
    "Timeout",
    "Concurrent",
    "Credit / mo",
    "Policy",
)


def format_memory(mib: int) -> str:
    return f"{mib / 1024:g} GiB" if mib >= 1024 and mib % 256 == 0 else f"{mib} MiB"


def format_cpu(milli: int) -> str:
    return f"{milli / 1000:g} vCPU"


def format_money(cents: int, currency: str) -> str:
    symbol = {"EUR": "€", "USD": "$", "SEK": "kr"}.get(currency, f"{currency} ")
    return f"{symbol}{cents / 100:,.2f}"


def credit_change_blocks(usage: dict[str, Any], new_monthly_cents: int) -> bool:
    """Whether lowering the grant to `new_monthly_cents` blocks compute right now.

    Mirrors the server's own arithmetic -- remaining = grant - finalized - reserved,
    and `compute_blocked` is remaining <= 0 -- so the warning can be shown *before*
    the write rather than discovered after it.
    """
    spent = int(usage.get("finalized_spend_cents", 0)) + int(usage.get("active_reservation_cents", 0))
    return new_monthly_cents - spent <= 0


def workspace_row(workspace: dict[str, Any]) -> tuple[str, ...]:
    """One table row, in `WORKSPACE_COLUMNS` order; the last cell is empty unless flagged."""
    policy = workspace["policy"]
    return (
        str(workspace.get("name") or workspace["id"]),
        str(workspace["id"]),
        str(len(workspace.get("members", []))),
        format_cpu(int(policy["max_cloud_run_cpu_milli"])),
        format_memory(int(policy["max_cloud_run_memory_mib"])),
        f"{policy['max_run_timeout_seconds']}s",
        str(policy["max_concurrent_cloud_run_runs"]),
        format_money(int(policy["monthly_credit_cents"]), str(policy.get("currency", "EUR"))),
        "defaults" if workspace.get("policy_defaulted") else "",
    )
