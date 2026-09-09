from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from rebase.cli import main


def test_hillclimb_init_creates_rebase_local_workspace(tmp_path: Path, capsys) -> None:
    assert main(["hillclimb", "init", str(tmp_path)]) == 0

    marker = tmp_path / "hillclimb" / "config.yaml"
    assert marker.exists()
    assert "rebase hillclimb" in marker.read_text()
    assert (tmp_path / "hillclimb" / "knowledge" / ".gitkeep").exists()
    assert (tmp_path / "hillclimb" / "problems" / ".gitkeep").exists()
    assert (tmp_path / "hillclimb" / "specs" / ".gitkeep").exists()
    assert (tmp_path / "hillclimb" / "runs" / ".gitkeep").exists()
    assert "hillclimb/runs/" in (tmp_path / ".gitignore").read_text().splitlines()
    assert "rebase hillclimb problems gefcom2014" in capsys.readouterr().out

    assert main(["hillclimb", "init", str(tmp_path)]) == 1
    assert "already inside the Hillclimb workspace" in capsys.readouterr().err


def test_hillclimb_problems_lists_targets(monkeypatch, capsys) -> None:
    from rebase import hillclimb

    monkeypatch.setattr(
        hillclimb,
        "discover_emflow_problems",
        lambda family: [
            {
                "target": "emflow://gefcom2014:solar",
                "family": "gefcom2014",
                "track": "solar",
            }
        ],
    )

    assert main(["hillclimb", "problems", "gefcom2014"]) == 0
    output = capsys.readouterr().out
    assert "Hillclimb Problems" in output
    assert "emflow://gefcom2014:solar" in output


def test_hillclimb_problems_supports_json(monkeypatch, capsys) -> None:
    from rebase import hillclimb

    expected = [{"target": "emflow://gefcom2014:wind", "family": "gefcom2014", "track": "wind"}]
    monkeypatch.setattr(hillclimb, "discover_emflow_problems", lambda family: expected)

    assert main(["hillclimb", "problems", "gefcom2014", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == expected


def test_hillclimb_start_can_disable_holdout_for_local_smoke(monkeypatch, capsys) -> None:
    from rebase import hillclimb

    observed = {}

    def fake_run_local_search(target, **kwargs):
        observed.update(target=target, **kwargs)
        return SimpleNamespace(state="done", ref="run/search", selected=None)

    monkeypatch.setattr(hillclimb, "run_local_search", fake_run_local_search)

    assert (
        main(
            [
                "hillclimb",
                "start",
                "emflow://gefcom2014:solar",
                "--local",
                "--backend",
                "dummy",
                "--no-holdout",
            ]
        )
        == 0
    )
    assert observed["target"] == "emflow://gefcom2014:solar"
    assert observed["backend"] == "dummy"
    assert observed["holdout"] is False
    assert "done run/search" in capsys.readouterr().out


def test_hosted_hillclimb_passes_holdout_as_run_parameter() -> None:
    from rebase.hillclimb import start_hosted_search

    class FakeClient:
        def run_ephemeral(self, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(id="run-id")

    client = FakeClient()
    start_hosted_search(client, "emflow://gefcom2014:solar", budget_s=600, holdout=False)

    assert client.kwargs["parameters"]["holdout"] is False
    assert "holdout: bool = True" in client.kwargs["source_code"]
    assert client.kwargs["mode"] == "job" and client.kwargs["image_spec"] == {"runtime": "hillclimb"}
    # the platform sizes the job from the search shape in the env
    assert client.kwargs["env"] == {
        "REBASE_HILLCLIMB_PARALLEL_SEARCHES": "1",
        "REBASE_HILLCLIMB_PARALLEL_OPERATORS": "3",
    }
    assert client.kwargs["secrets"] == {}


def test_hosted_hillclimb_start_forwards_fleet_shape_and_secrets(monkeypatch, capsys) -> None:
    from rebase import cli, hillclimb

    class FakeClient:
        kwargs: dict = {}

        def list_secrets(self):
            return [{"name": "other"}, {"name": "hillclimb"}]

        def get_secret(self, name):
            assert name == "hillclimb"
            return {"secret_refs": {"CLAUDE_CODE_OAUTH_TOKEN": "ws-claude:3", "UNRELATED": "x"}}

        def run_ephemeral(self, **kwargs):
            FakeClient.kwargs = kwargs
            return SimpleNamespace(id="run-id")

    monkeypatch.setattr(cli, "Client", FakeClient)
    assert (
        main(
            [
                "hillclimb",
                "start",
                "emflow://gefcom2014:solar",
                "--budget",
                "15m",
                "--parallel-searches",
                "2",
                "--parallel-operators",
                "2",
                "--policy",
                "gepa",
            ]
        )
        == 0
    )
    kwargs = FakeClient.kwargs
    assert kwargs["parameters"]["budget_s"] == 900
    assert kwargs["parameters"]["parallel_searches"] == 2
    assert kwargs["parameters"]["parallel_operators"] == 2
    assert kwargs["parameters"]["policy"] == "gepa"
    assert kwargs["env"] == {"REBASE_HILLCLIMB_PARALLEL_SEARCHES": "2", "REBASE_HILLCLIMB_PARALLEL_OPERATORS": "2"}
    assert kwargs["secrets"] == {"CLAUDE_CODE_OAUTH_TOKEN": "ws-claude:3"}
    out = capsys.readouterr().out
    assert "agents bill workspace secret hillclimb" in out
    assert "rebase hillclimb watch run-id" in out
    assert hillclimb.RUN_NAME_PREFIX in kwargs["name"]


def test_resolve_agent_secrets_falls_back_to_deployment_credentials() -> None:
    from rebase.hillclimb import resolve_agent_secrets

    class NoSecrets:
        def list_secrets(self):
            return [{"name": "other"}]

    assert resolve_agent_secrets(NoSecrets(), None) == (None, {})

    class Wrong:
        def get_secret(self, name):
            return {"secret_refs": {"UNRELATED": "x"}}

    try:
        resolve_agent_secrets(Wrong(), "wrong")
    except RuntimeError as exc:
        assert "CLAUDE_CODE_OAUTH_TOKEN" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected a RuntimeError")


def test_hosted_status_and_stop_go_through_the_api(monkeypatch, capsys) -> None:
    from rebase import cli

    class FakeClient:
        controls: list = []

        def get_run(self, run_id):
            return {"id": run_id, "status": "running", "parameters": {"sync_id": "abc"}}

        def list_hillclimb_objects(self, run_id):
            return {
                "objects": [{"path": "run-1/searches/solar/status.json", "generation": "5"}, {"path": "run-1/run.yaml"}]
            }

        def get_hillclimb_object(self, run_id, path, *, etag=None):
            body = json.dumps(
                {
                    "state": "running",
                    "candidates": {"total": 4, "ok": 3},
                    "budget": {"remaining_s": 120},
                    "cost_usd": 0.5,
                }
            )
            return body.encode(), '"5"'

        def send_hillclimb_control(self, run_id, *, action, candidate_id=None, reason="", source="cli"):
            FakeClient.controls.append(action)
            return {"path": "control/x-stop.json"}

    monkeypatch.setattr(cli, "Client", FakeClient)
    assert main(["hillclimb", "status", "run-1"]) == 0
    out = capsys.readouterr().out
    assert "platform run: running" in out and "run-1/solar: running" in out and "candidates=4 (3 ok)" in out
    assert main(["hillclimb", "stop", "run-1"]) == 0
    assert FakeClient.controls == ["stop"]
