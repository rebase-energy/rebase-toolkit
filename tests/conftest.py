from __future__ import annotations

import pytest

from rebase.client import _dataset_registry


@pytest.fixture(autouse=True)
def _isolate_dataset_registry():
    """Datasets declared with a contract/freshness register process-wide (deploy
    preflight relies on it); tests must not leak registrations across modules."""
    _dataset_registry.clear()
    yield
    _dataset_registry.clear()
