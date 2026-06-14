from __future__ import annotations

import importlib
from types import ModuleType


def optional_module(module_name: str, extra: str) -> ModuleType:
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        raise ImportError(
            f"Optional Rebase module {module_name!r} is not installed. "
            f'Install it with: pip install "rebase-toolkit[{extra}]"'
        ) from exc
