from __future__ import annotations

import tomllib
from pathlib import Path
from uuid import UUID

import typer

from rebase import client_identity
from rebase.cli import app, run_app
from rebase.client import Client
from rebase.version import __version__


def test_version_matches_pyproject() -> None:
    pyproject = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    assert __version__ == pyproject["project"]["version"]


def test_every_request_names_the_client(monkeypatch) -> None:
    monkeypatch.delenv("REBASE_RUN_ID", raising=False)
    # Process-global: an earlier test that went through the CLI's main() set it.
    monkeypatch.setattr(client_identity, "_client", "sdk")
    headers = Client(api_key="rb_test", workspace_id="ws")._request_headers(auth=False)
    assert headers["User-Agent"].startswith(f"rebase-toolkit/{__version__} ")
    assert headers["X-Rebase-Client"] == "sdk"
    assert headers["X-Rebase-Client-Version"] == __version__
    assert headers["X-Rebase-Origin"] == "local"
    UUID(headers["X-Rebase-Invocation"])
    # One id per process: a deploy's several requests group into one deploy.
    assert headers["X-Rebase-Invocation"] == client_identity.identity_headers()["X-Rebase-Invocation"]


def test_inside_a_run_the_origin_is_the_run(monkeypatch) -> None:
    monkeypatch.setenv("REBASE_RUN_ID", "8a7a54a1-1f5b-4f0e-9d53-0c6f0d9e3c11")
    assert client_identity.identity_headers()["X-Rebase-Origin"] == "run"


def test_command_path_keeps_command_names_and_nothing_else() -> None:
    root = typer.main.get_command(app)
    assert client_identity.command_path(root, ["run", "secret/path.py::target", "--wait"]) == "run"
    assert client_identity.command_path(root, ["deploy", "flows.py"]) == "deploy"
    assert client_identity.command_path(root, ["-W", "my-workspace", "tui"]) == "tui"
    assert client_identity.command_path(root, ["secret", "create", "DB_PASSWORD", "hunter2"]) == "secret create"
    assert client_identity.command_path(root, ["workflow", "schedule", "--help"]) == "workflow schedule"
    assert client_identity.command_path(root, ["not-a-command"]) is None
    inspection = typer.main.get_command(run_app)
    assert client_identity.command_path(inspection, ["logs", "some-run-id"], prefix="run") == "run logs"


def test_the_cli_sets_the_command(monkeypatch) -> None:
    monkeypatch.setattr(client_identity, "_client", "sdk")
    monkeypatch.setattr(client_identity, "_command", None)
    client_identity.set_client("cli", command="deploy")
    headers = client_identity.identity_headers()
    assert (headers["X-Rebase-Client"], headers["X-Rebase-Command"]) == ("cli", "deploy")
