from __future__ import annotations

from typing import Any

from rebase._optional import optional_module


def _emflow():
    return optional_module("emflow", "modeling")


def __getattr__(name: str) -> Any:
    return getattr(_emflow(), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_emflow())))
