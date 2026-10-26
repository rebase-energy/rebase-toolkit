from __future__ import annotations

from contextlib import suppress
from pathlib import Path

import pytest

from rebase.client import _dataset_registry

REPO_ROOT = Path(__file__).resolve().parent.parent
_USER_CONFIG = Path.home() / ".rebase" / "config.json"


@pytest.fixture(autouse=True)
def _isolate_user_home(monkeypatch, tmp_path):
    """Never read or overwrite the developer's profile or login session."""
    home = tmp_path / "isolated-user-home"
    home.mkdir()
    # tmp_path can itself live under the real home. Hide its global profile
    # from workspace-marker discovery after HOME has moved into the sandbox.
    is_file = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda path: path != _USER_CONFIG and is_file(path))
    monkeypatch.setenv("HOME", str(home))
    # pathlib/expanduser use USERPROFILE on Windows, not HOME.
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    for name in ("REBASE_CONFIG_PATH", "REBASE_AUTH_FILE", "REBASE_WORKFLOWS_AUTH_FILE"):
        monkeypatch.delenv(name, raising=False)


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
