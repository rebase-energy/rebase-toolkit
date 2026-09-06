from __future__ import annotations

from contextlib import suppress
from pathlib import Path

import pytest

from rebase.client import _dataset_registry

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _pin_the_hosted_platform(monkeypatch):
    """A developer's laptop may carry a `~/.rebase/platform` pointer; the suite must
    not follow it. Tests of the pointer itself unset this and point HOME at tmp_path."""
    monkeypatch.setenv("REBASE_PLATFORM", "production")


@pytest.fixture(autouse=True)
def _isolate_dataset_registry():
    """Datasets declared with a contract/freshness register process-wide (deploy
    preflight relies on it); tests must not leak registrations across modules."""
    _dataset_registry.clear()
    yield
    _dataset_registry.clear()


@pytest.fixture(autouse=True)
def _no_workspace_marker_in_repo():
    """Fail the test that writes a `.rebase/` marker into this checkout.

    `rebase setup` anchors the marker at the git root, so a test exercising it
    without chdir'ing into tmp_path lands one here. Every later test then resolves
    its profile through that marker instead of the global config, failing far from
    the cause — so catch it at the source and clean up.
    """
    marker = REPO_ROOT / ".rebase" / "config.json"
    existed = marker.exists()
    yield
    if marker.exists() and not existed:
        marker.unlink()
        with suppress(OSError):
            marker.parent.rmdir()
        raise AssertionError(
            f"test wrote a workspace marker to {marker}; chdir into tmp_path before running setup helpers"
        )
