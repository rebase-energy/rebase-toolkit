"""``rebase.modeling``: the installed sequential-decision framework, re-exported.

sdflow when it is installed, else emflow (see :mod:`rebase._framework`). Attribute
access is forwarded, so ``rebase.modeling.Predictor`` is the framework's ``Predictor``.
"""

from __future__ import annotations

from typing import Any

from rebase._framework import framework_module


def __getattr__(name: str) -> Any:
    return getattr(framework_module(), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(framework_module())))
