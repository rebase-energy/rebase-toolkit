"""hosted_search inside the job: the hillclimb dir it lays out, the engine
entry points it calls (one search in-process, a fleet otherwise), and the
result it returns. The engine is a fake package shaped like the real one:
`hillclimb.api` is a submodule, not an attribute of the package."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from rebase import hillclimb


class FakeConfig:
    def __init__(self, runs_dir: Path):
        self.holdout = SimpleNamespace(enabled=True)
        self.paths = SimpleNamespace(runs_dir=runs_dir)


def _fake_engine(monkeypatch, tmp_path: Path, calls: dict):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("HILLCLIMB_DIR", raising=False)
    monkeypatch.delenv("REBASE_HILLCLIMB_ARTIFACTS_BUCKET", raising=False)
    monkeypatch.setenv("REBASE_HILLCLIMB_MAX_COST_USD", "7.5")
    runs_dir = home / "hillclimb" / "runs"

    def run_search(target, **kwargs):
        calls["run_search"] = {"target": target, **kwargs}
        run_dir = runs_dir / "20260909-120000-solar"
        search = run_dir / "searches" / "solar"
        search.mkdir(parents=True)
        (run_dir / "run.yaml").write_text("run_id: x\n")
        (search / "status.json").write_text(
            json.dumps({"state": "done", "cost_usd": 1.5, "selected": {"candidate_id": "c003", "val_score": 0.4}})
        )
        return SimpleNamespace(run_dir=run_dir, search_dir=search, error=None)

    class FakeFleet:
        def __init__(self, run_dir):
            self.run_dir, self.run_id = run_dir, run_dir.name

        def wait(self, *, poll_s, deadline_s):
            calls["wait"] = {"poll_s": poll_s, "deadline_s": deadline_s}
            return {1001: 0, 1002: 2}

        def alive(self):
            return []

    def run_fleet(target, *, config, parallel_searches, **kwargs):
        calls["run_fleet"] = {"target": target, "parallel_searches": parallel_searches, **kwargs}
        run_dir = runs_dir / "20260909-120000-solar"
        for name, state in (("solar", "done"), ("solar-2", "parked")):
            search = run_dir / "searches" / name
            search.mkdir(parents=True)
            (search / "status.json").write_text(json.dumps({"state": state, "cost_usd": 2.0}))
        (run_dir / "run.yaml").write_text("run_id: x\n")
        return FakeFleet(run_dir)

    package = types.ModuleType("hillclimb")
    package.run_search = run_search  # the package re-exports run_search only
    api = types.ModuleType("hillclimb.api")
    api.run_fleet = run_fleet
    config = types.ModuleType("hillclimb.config")
    config.Config = SimpleNamespace(load=lambda: FakeConfig(runs_dir))
    for name, module in (("hillclimb", package), ("hillclimb.api", api), ("hillclimb.config", config)):
        monkeypatch.setitem(sys.modules, name, module)
    return home


def test_hosted_search_single_search_runs_in_process(tmp_path: Path, monkeypatch) -> None:
    calls: dict = {}
    home = _fake_engine(monkeypatch, tmp_path, calls)

    result = hillclimb.hosted_search(
        "emflow://x", budget_s=600, name="s", sync_id="abc", backend="dummy", holdout=False
    )

    config = (home / "hillclimb" / "config.yaml").read_text()
    assert "parallel_operators: 3" in config and "machine_max_operators: 0" in config
    assert "max_cost_usd: 7.5" in config and "backend_auth: api-key" in config and "backend: dummy" in config
    assert calls["run_search"]["budget_s"] == 600 and calls["run_search"]["holdout"] is False
    assert "run_fleet" not in calls
    assert result["state"] == "done" and result["cost_usd"] == 1.5
    assert result["selected"]["candidate_id"] == "c003"
    assert result["sync_id"] == "abc" and result["gcs_prefix"] is None  # no bucket in the env


def test_hosted_search_fleet_uses_the_api_submodule(tmp_path: Path, monkeypatch) -> None:
    calls: dict = {}
    _fake_engine(monkeypatch, tmp_path, calls)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")

    result = hillclimb.hosted_search(
        "emflow://x",
        budget_s=300,
        name="fleet",
        sync_id="abc",
        parallel_searches=2,
        parallel_operators=1,
        policy="gepa",
    )

    fleet = calls["run_fleet"]
    assert fleet["parallel_searches"] == 2 and fleet["parallel_operators"] == 1 and fleet["policy"] == "gepa"
    assert fleet["budget"] == 300 and fleet["holdout"] is True
    assert calls["wait"]["deadline_s"] == 300 + hillclimb.DEFAULT_AGENT_TIMEOUT_S + hillclimb.FLEET_GRACE_S
    assert "run_search" not in calls
    assert result["state"] == "parked" and result["cost_usd"] == 4.0
    assert [s["state"] for s in result["searches"]] == ["done", "parked"]


def test_hosted_search_requires_the_engine(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "hillclimb", None)
    with pytest.raises(RuntimeError, match="hillclimb extra"):
        hillclimb.hosted_search("emflow://x", budget_s=1)
