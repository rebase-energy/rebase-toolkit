import importlib
import sys
from types import ModuleType

import pytest

from rebase._optional import optional_module
from rebase.tui_graph_pane import load_plotui


def test_optional_module_returns_installed_module(monkeypatch) -> None:
    module = ModuleType("energydatamodel")
    monkeypatch.setitem(sys.modules, "energydatamodel", module)

    assert optional_module("energydatamodel", "data") is module


def test_optional_module_error_mentions_extra() -> None:
    with pytest.raises(ImportError, match='pip install "rebase-toolkit\\[data\\]"'):
        optional_module("definitely_missing_rebase_optional_module", "data")


def test_tui_does_not_import_plotui_at_module_scope(monkeypatch) -> None:
    """plotui is the graph pane's optional extra; `rebase tui` must open without it."""
    monkeypatch.setitem(sys.modules, "plotui", None)
    for name in ("rebase.tui", "rebase.tui_graph", "rebase.tui_graph_pane"):
        importlib.reload(importlib.import_module(name))
    assert sys.modules.get("plotui") is None


def test_graph_pane_names_the_graph_extra(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "plotui", None)
    with pytest.raises(ImportError, match=r"rebase-toolkit\[graph\]"):
        load_plotui()
