from __future__ import annotations

from typing import Any

from rebase._optional import optional_module


def _energydatamodel():
    return optional_module("energydatamodel", "data")


def __getattr__(name: str) -> Any:
    return getattr(_energydatamodel(), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_energydatamodel())))
