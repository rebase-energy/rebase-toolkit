"""Which sequential-decision framework the SDK builds on: sdflow, or emflow before it.

sdflow is emflow's domain-neutral successor with the same shapes (``Problem``,
``Predictor``, ``load_problem``, ``evaluate``). The SDK uses whichever is installed,
preferring sdflow, and records which one in the provenance of problems and scores.
emflow stays accepted for one release so existing installs keep working.
"""

from __future__ import annotations

import importlib
from types import ModuleType

FRAMEWORKS = ("sdflow", "emflow")
MODELING_EXTRA = "modeling"


def framework_module() -> ModuleType:
    """The installed framework, sdflow first; an ImportError naming the extra otherwise."""
    for name in FRAMEWORKS:
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    raise ImportError(
        f"No sequential-decision framework is installed ({' or '.join(FRAMEWORKS)}). "
        f'Install one with: pip install "rebase-toolkit[{MODELING_EXTRA}]"'
    )


def framework_models() -> ModuleType | None:
    """The framework's ``models`` module, or None when neither framework is installed."""
    for name in FRAMEWORKS:
        try:
            return importlib.import_module(f"{name}.models")
        except ImportError:
            continue
    return None


def installs_framework(package: str) -> bool:
    """Does this dependency spec install sdflow or emflow (by name or by git URL)?"""
    normalized = package.strip().lower()
    for name in FRAMEWORKS:
        if (
            normalized == name
            or normalized.startswith(f"{name} ")
            or normalized.startswith(f"{name}=")
            or normalized.startswith(f"{name}>")
            or normalized.startswith(f"{name}<")
            or normalized.startswith(f"{name}[")
            or f"github.com/rebase-energy/{name}" in normalized
        ):
            return True
    return False
