import sys
from types import ModuleType

import pytest

from rebase._optional import optional_module


def test_optional_module_returns_installed_module(monkeypatch) -> None:
    module = ModuleType("energydatamodel")
    monkeypatch.setitem(sys.modules, "energydatamodel", module)

    assert optional_module("energydatamodel", "data") is module


def test_optional_module_error_mentions_extra() -> None:
    with pytest.raises(ImportError, match='pip install "rebase-toolkit\\[data\\]"'):
        optional_module("definitely_missing_rebase_optional_module", "data")
