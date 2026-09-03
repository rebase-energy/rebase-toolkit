from __future__ import annotations

from typing import Any

from rich.theme import Theme

BRAND_MAIN_GREEN = "#0D9373"
BRAND_BRIGHT_GREEN = "#03C497"
BRAND_MEDIUM_GRAY = "#656565"
BRAND_CORAL_RED = "#E46962"
BRAND_AMBER = "#FBAE40"
BRAND_SLATE_BLUE = "#3F6E91"

REBASE_THEME = Theme(
    {
        "rebase.active": f"bold {BRAND_BRIGHT_GREEN}",
        "rebase.border": f"dim {BRAND_MEDIUM_GRAY}",
        "rebase.error": BRAND_CORAL_RED,
        "rebase.info": BRAND_SLATE_BLUE,
        "rebase.muted": BRAND_MEDIUM_GRAY,
        "rebase.success": BRAND_MAIN_GREEN,
        "rebase.title": f"bold {BRAND_MAIN_GREEN}",
        "rebase.value": f"bold {BRAND_BRIGHT_GREEN}",
        "rebase.warning": BRAND_AMBER,
    }
)


def status_colour(status: Any) -> str:
    """The brand colour a run, step or task status is shown in, anywhere it is shown."""
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
