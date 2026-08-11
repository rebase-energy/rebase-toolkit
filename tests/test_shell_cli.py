from typing import Any

from rebase import cli as rebase_cli
from rebase.cli import main
from rebase.client import Client
from rebase.shell import ShellResult, close_message


def test_close_message_maps_relay_codes() -> None:
    assert close_message(ShellResult(close_code=1000, close_reason="")) is None
    assert close_message(ShellResult(close_code=None, close_reason="")) is None
    assert "another client" in close_message(ShellResult(close_code=4409, close_reason=""))
    assert "expired" in close_message(ShellResult(close_code=4403, close_reason=""))
    unexpected = close_message(ShellResult(close_code=1011, close_reason="server error"))
    assert "1011" in unexpected and "server error" in unexpected


def _patch_function_lookup(monkeypatch) -> None:
    monkeypatch.setattr(Client, "list_projects", lambda self: [{"id": "project-id", "name": "energy"}])
    monkeypatch.setattr(
        Client,
        "list_functions",
        lambda self, *, project=None, project_id=None: [{"id": "function-id", "name": "add"}],
    )


def test_shell_command_bridges_and_cleans_up(monkeypatch, capsys) -> None:
    _patch_function_lookup(monkeypatch)
    observed: dict[str, Any] = {"deleted": []}

    def fake_create(self, payload):
        observed["payload"] = payload
        return {
            "id": "session-id",
            "status": "starting",
            "relay_ws_url": "wss://relay.example.com/shell/client",
            "client_token": "client-token",
        }

    def fake_bridge(relay_ws_url, session_id, client_token, *, on_waiting=None, on_ready=None):
        observed["bridge"] = (relay_ws_url, session_id, client_token)
        if on_waiting:
            on_waiting()
        if on_ready:
            on_ready()
        return ShellResult(close_code=1000, close_reason="")

    monkeypatch.setattr(Client, "create_shell_session", fake_create)
    monkeypatch.setattr(Client, "delete_shell_session", lambda self, sid: observed["deleted"].append(sid))
    monkeypatch.setattr(rebase_cli, "_run_shell_bridge", fake_bridge)

    assert main(["shell", "add", "--project", "energy"]) == 0

    assert observed["payload"] == {"function_id": "function-id"}
    assert observed["bridge"] == ("wss://relay.example.com/shell/client", "session-id", "client-token")
    assert observed["deleted"] == ["session-id"]
    output = capsys.readouterr().out
    assert "Session ended." in output


def test_shell_command_workflow_flag_targets_workflow(monkeypatch) -> None:
    monkeypatch.setattr(Client, "list_projects", lambda self: [{"id": "project-id", "name": "energy"}])
    monkeypatch.setattr(
        Client,
        "list_workflows",
        lambda self, *, project=None, project_id=None: [{"id": "workflow-id", "name": "flow"}],
    )
    observed: dict[str, Any] = {}

    def fake_create(self, payload):
        observed["payload"] = payload
        return {
            "id": "session-id",
            "status": "starting",
            "relay_ws_url": "wss://relay.example.com/shell/client",
            "client_token": "client-token",
        }

    monkeypatch.setattr(Client, "create_shell_session", fake_create)
    monkeypatch.setattr(Client, "delete_shell_session", lambda self, sid: None)
    monkeypatch.setattr(
        rebase_cli,
        "_run_shell_bridge",
        lambda *args, **kwargs: ShellResult(close_code=1000, close_reason=""),
    )

    assert main(["shell", "flow", "--project", "energy", "--workflow", "--ttl", "900"]) == 0
    assert observed["payload"] == {"workflow_id": "workflow-id", "ttl_seconds": 900}


def test_shell_command_reports_abnormal_close(monkeypatch, capsys) -> None:
    _patch_function_lookup(monkeypatch)
    monkeypatch.setattr(
        Client,
        "create_shell_session",
        lambda self, payload: {
            "id": "session-id",
            "status": "starting",
            "relay_ws_url": "wss://relay.example.com/shell/client",
            "client_token": "client-token",
        },
    )
    monkeypatch.setattr(Client, "delete_shell_session", lambda self, sid: None)
    monkeypatch.setattr(
        rebase_cli,
        "_run_shell_bridge",
        lambda *args, **kwargs: ShellResult(close_code=4409, close_reason=""),
    )

    assert main(["shell", "add", "--project", "energy"]) == 1
    assert "another client" in capsys.readouterr().err


def test_shell_command_requires_project_for_name_lookup(capsys) -> None:
    assert main(["shell", "add"]) == 1
    assert "--project is required" in capsys.readouterr().err
