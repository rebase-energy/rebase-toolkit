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


#: Which fields offer a pick-list and which take free text. A tuple is the list of
#: values to offer (the current value is added if missing, and every list ends with
#: a "Custom…" entry that falls back to free text). None means the value is genuinely
#: continuous -- vCPU in milli-units, money in cents -- and a list would only get in
#: the way. Memory follows Cloud Run's own tiers; the small integer caps have a
#: handful of values anyone actually sets.
FIELD_CHOICES: dict[str, tuple[int, ...] | None] = {
    "max_cloud_run_memory_mib": (512, 1024, 2048, 4096, 8192, 16384, 32768),
    "max_cloud_run_cpu_milli": None,
    "max_run_timeout_seconds": (60, 300, 600, 900, 1800, 3600),
    "max_concurrent_cloud_run_runs": (1, 2, 4, 6, 8, 10, 20),
    "max_cloud_run_instances": (1, 2, 3, 5, 10),
    "max_cloud_run_concurrency": (1, 10, 20, 40, 80),
    "monthly_credit_cents": None,
}


def choices_for(field: str, current: int) -> tuple[int, ...] | None:
    """The pick-list for a field with the current value folded in, or None for free text."""
    choices = FIELD_CHOICES.get(field)
    if choices is None:
        return None
    return tuple(sorted(set(choices) | {int(current)}))


def format_field_value(field: str, value: int, currency: str = "EUR") -> str:
    """A quota value the way a person reads it, not the way the API stores it."""
    value = int(value)
    if field == "max_cloud_run_memory_mib":
        return format_memory(value)
    if field == "max_cloud_run_cpu_milli":
        return f"{format_cpu(value)}  ({value} milli-vCPU)"
    if field == "max_run_timeout_seconds":
        return f"{value // 60} min" if value >= 60 and value % 60 == 0 else f"{value} s"
    if field == "max_concurrent_cloud_run_runs":
        return f"{value} run{'s' if value != 1 else ''} in flight"
    if field == "max_cloud_run_instances":
        return f"{value} instance{'s' if value != 1 else ''}"
    if field == "max_cloud_run_concurrency":
        return f"{value} request{'s' if value != 1 else ''} per instance"
    if field == CREDIT_FIELD:
        return f"{format_money(value, currency)}  ({value} cents)"
    return f"{value} {FIELD_UNITS.get(field, '')}".strip()
