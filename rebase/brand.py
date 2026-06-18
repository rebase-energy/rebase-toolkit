from __future__ import annotations

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
