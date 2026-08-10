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
