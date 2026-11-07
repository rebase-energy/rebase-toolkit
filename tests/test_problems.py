"""Problems: spec extraction, registration, the project decorator and the CLI."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

import rebase as rb
from rebase.client import Problem, Project, problem_fingerprint, problem_spec


def _emflow_like_problem(name: str = "icing:se1", *, train_end: str = "2025-10-01") -> SimpleNamespace:
    """What an emflow Problem looks like to the duck-typed spec extractor."""
    return SimpleNamespace(
        name=name,
        description="Day-ahead SE1 wind power with icing features.",
        objective=SimpleNamespace(
            metric=SimpleNamespace(name="PinballLoss", aggregate="mean", quantiles=(0.1, 0.5, 0.9)),
            lower_is_better=True,
        ),
        schedule=SimpleNamespace(
            origin_freq="1D", cover_start="14h", cover_end="37h", target_freq="1h", origin_time="10:00"
        ),
        splits=SimpleNamespace(
            train_end=datetime.fromisoformat(train_end).replace(tzinfo=UTC),
            validation=(datetime(2025, 10, 1, tzinfo=UTC), datetime(2026, 1, 15, tzinfo=UTC)),
            holdout=(datetime(2026, 1, 15, tzinfo=UTC), datetime(2026, 4, 1, tzinfo=UTC)),
        ),
        reference_scores=[SimpleNamespace(rank=1, team="physics-qrf", score=12.5)],
        dataset=lambda: None,
    )


class FakeClient:
    def __init__(self) -> None:
        self.registered: list[dict[str, Any]] = []
        self.versions_by_fingerprint: dict[str, int] = {}

    def register_problem(self, name: str, **payload: Any) -> dict[str, Any]:
        self.registered.append({"name": name, **payload})
        fingerprint = problem_fingerprint(payload["spec"])
        created = fingerprint not in self.versions_by_fingerprint
        number = self.versions_by_fingerprint.setdefault(fingerprint, len(self.versions_by_fingerprint) + 1)
        return {
            "problem": {"id": "prob-1", "name": name},
            "version": {"id": f"ver-{number}", "version_number": number, "fingerprint": fingerprint},
            "created": created,
        }

    def get_problem(self, name: str) -> dict[str, Any]:
        return {"name": name, "latest_version": {"version_number": 2}}

    def list_problem_versions(self, name: str) -> list[dict[str, Any]]:
        return [{"version_number": 1}, {"version_number": 2}]

    def record_problem_score(self, name: str, **payload: Any) -> dict[str, Any]:
        self.recorded = getattr(self, "recorded", [])
        self.recorded.append({"name": name, **payload})
        return {"id": "score-1", "problem_name": name, "version_number": 1, **payload}

    def list_problem_scores(self, name: str, **params: Any) -> list[dict[str, Any]]:
        self.listed = {"name": name, **params}
        return [
            {"split": "validation", "model": "qrf-physics-v1", "score": 67.4859, "metric": "PinballLoss",
             "n_origins": 107, "version_number": 1, "stats": {"PersistenceSkill.skill": 0.739},
             "source": {"git_commit_sha": "f2b0f592abc", "git_dirty": True}, "created_at": "2026-11-07T12:00:00Z"},
            {"split": "validation", "model": "climatology", "score": 201.1, "metric": "PinballLoss",
             "n_origins": 107, "version_number": 1, "stats": {}, "source": {}, "created_at": "2026-11-07T11:00:00Z"},
        ]  # fmt: skip


class TestSpec:
    def test_spec_carries_what_defines_the_problem(self) -> None:
        spec = problem_spec(_emflow_like_problem())
        assert spec["name"] == "icing:se1"
        assert spec["objective"] == {
            "metric": "PinballLoss",
            "params": {"name": "PinballLoss", "aggregate": "mean", "quantiles": (0.1, 0.5, 0.9)},
            "aggregate": "mean",
            "lower_is_better": True,
        }
        assert spec["schedule"]["kind"] == "regular" and spec["schedule"]["cover_end"] == "37h"
        assert spec["splits"]["train_end"] == "2025-10-01T00:00:00+00:00"
        assert spec["splits"]["holdout"] == ["2026-01-15T00:00:00+00:00", "2026-04-01T00:00:00+00:00"]
        assert spec["reference_scores"] == [{"rank": 1, "team": "physics-qrf", "score": 12.5}]
        assert spec["dataset"] is None  # a lazy dataset loader is not loaded to name it

    def test_explicit_schedule_is_summarised_not_dumped(self) -> None:
        problem = _emflow_like_problem()
        problem.schedule = SimpleNamespace(
            _origins=[SimpleNamespace(asof=datetime(2026, 1, d, tzinfo=UTC)) for d in (1, 2, 3)]
        )
        schedule = problem_spec(problem)["schedule"]
        assert schedule == {
            "kind": "explicit",
            "n_origins": 3,
            "first_asof": "2026-01-01T00:00:00+00:00",
            "last_asof": "2026-01-03T00:00:00+00:00",
        }

    def test_fingerprint_ignores_description_but_not_splits(self) -> None:
        base = problem_spec(_emflow_like_problem())
        reworded = {**base, "description": "other words"}
        moved = problem_spec(_emflow_like_problem(train_end="2025-09-01"))
        assert problem_fingerprint(base) == problem_fingerprint(reworded)
        assert problem_fingerprint(base) != problem_fingerprint(moved)


class TestProblem:
    def test_requires_a_name_or_a_factory(self) -> None:
        with pytest.raises(ValueError, match="name or a factory"):
            Problem("  ")

    def test_name_falls_back_to_the_factory_problem(self) -> None:
        problem = Problem(factory=lambda: _emflow_like_problem("icing:se2"))
        assert problem.name == "icing:se2"

    def test_register_sends_spec_and_records_the_version(self) -> None:
        client = FakeClient()
        problem = Problem(factory=lambda: _emflow_like_problem(), description="Override")
        result = problem.register(client, project_id="proj-1", project_name="icing")  # type: ignore[arg-type]

        assert result["created"] is True
        assert problem.id == "prob-1" and problem.version_id == "ver-1" and problem.version_number == 1
        sent = client.registered[0]
        assert sent["name"] == "icing:se1" and sent["description"] == "Override" and sent["project_id"] == "proj-1"
        assert sent["source"]["project"] == "icing"
        assert sent["source"]["factory"].endswith("<lambda>")

    def test_description_defaults_to_the_problem_description(self) -> None:
        client = FakeClient()
        Problem(factory=lambda: _emflow_like_problem()).register(client)  # type: ignore[arg-type]
        assert client.registered[0]["description"] == "Day-ahead SE1 wind power with icing features."

    def test_re_registering_an_unchanged_spec_is_not_a_new_version(self) -> None:
        client = FakeClient()
        first = Problem(factory=lambda: _emflow_like_problem()).register(client)  # type: ignore[arg-type]
        second = Problem(factory=lambda: _emflow_like_problem()).register(client)  # type: ignore[arg-type]
        assert first["created"] is True and second["created"] is False
        assert second["version"]["version_number"] == 1

    def test_from_name_reads_through_the_client(self) -> None:
        problem = rb.Problem.from_name("icing:se1", client=FakeClient())  # type: ignore[arg-type]
        assert problem.get()["latest_version"]["version_number"] == 2
        assert [v["version_number"] for v in problem.versions()] == [1, 2]


class TestProjectDecorator:
    def test_decorator_attaches_a_problem_to_the_project(self) -> None:
        project = Project("icing")

        @project.problem(name="icing:se1", description="SE1")
        def icing_se1() -> Any:
            return _emflow_like_problem()

        assert isinstance(icing_se1, Problem)
        assert project._problems == [icing_se1]
        assert icing_se1.name == "icing:se1" and icing_se1.description == "SE1"
        assert icing_se1.spec()["objective"]["metric"] == "PinballLoss"


class TestDeployHooks:
    def test_project_deploy_registers_problems_with_provenance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("rebase.client.preflight_datasets", lambda client=None, **kw: None)
        fake = FakeClient()
        monkeypatch.setattr(Project, "_client", property(lambda self: fake), raising=False)
        monkeypatch.setattr(FakeClient, "ensure_project", lambda self, name, **kw: {"id": "proj-icing"}, raising=False)
        project = rb.Project("icing")

        @project.problem(name="icing:se1")
        def icing_se1() -> Any:
            return _emflow_like_problem()

        project.deploy()
        assert icing_se1.version_number == 1 and icing_se1.version_id == "ver-1"
        sent = fake.registered[0]
        assert sent["project_id"] == "proj-icing" and sent["source"]["project"] == "icing"

    def test_deploy_file_lists_problems_and_rejects_orphans(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        from rebase.cli import deploy_file
        from rebase.client import RebaseWorkflowError

        monkeypatch.setattr("rebase.client.preflight_datasets", lambda client=None, **kw: None)
        fake = FakeClient()
        monkeypatch.setattr(Project, "_client", property(lambda self: fake), raising=False)
        monkeypatch.setattr(FakeClient, "ensure_project", lambda self, name, **kw: {"id": "proj-icing"}, raising=False)

        attached = tmp_path / "deploy_icing.py"
        attached.write_text(
            "from types import SimpleNamespace\n"
            "from datetime import UTC, datetime\n"
            "import rebase as rb\n"
            "project = rb.project('icing')\n"
            "def _p():\n"
            "    return SimpleNamespace(name='icing:se1', description=None, objective=None, schedule=None,\n"
            "        splits=None, reference_scores=[], dataset=None)\n"
            "@project.problem(name='icing:se1')\n"
            "def icing_se1():\n"
            "    return _p()\n"
        )
        rows = deploy_file(attached)
        assert ("problem", "icing:se1 v1", "ver-1") in rows

        orphan = tmp_path / "deploy_orphan.py"
        orphan.write_text(
            "import rebase as rb\n"
            "project = rb.project('icing')\n"
            "loose = rb.Problem('icing:se2', factory=lambda: None)\n"
        )
        with pytest.raises(RebaseWorkflowError, match="loose is not attached"):
            deploy_file(orphan)


class TestCli:
    SPEC = {
        "objective": {"metric": "PinballLoss"},
        "schedule": {"kind": "regular", "origin_freq": "1D", "cover_end": "37h"},
        "splits": {
            "train_end": "2025-10-01T00:00:00+00:00",
            "validation": ["2025-10-01T00:00:00+00:00", "2026-01-15T00:00:00+00:00"],
            "holdout": ["2026-01-15T00:00:00+00:00", "2026-04-01T00:00:00+00:00"],
        },
    }

    def _client(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        deleted: list[str] = []
        spec = self.SPEC

        class _Client:
            def list_problems(self) -> list[dict[str, Any]]:
                return [
                    {
                        "name": "icing:se1",
                        "latest_version": {"version_number": 2, "spec": spec},
                        "updated_at": "2026-11-07T09:00:00Z",
                    },
                    {"name": "icing:se2", "latest_version": None},
                ]

            def get_problem(self, name: str) -> dict[str, Any]:
                return {
                    "name": name,
                    "id": "prob-1",
                    "version_count": 2,
                    "latest_version": {
                        "version_number": 2,
                        "fingerprint": "abcdef0123456789",
                        "spec": spec,
                        "source": {"project": "icing", "git_commit_sha": "deadbeef"},
                    },
                }

            def list_problem_versions(self, name: str) -> list[dict[str, Any]]:
                return [
                    {
                        "version_number": 1,
                        "fingerprint": "0123456789abcdef",
                        "spec": spec,
                        "source": {"project": "icing"},
                    }
                ]

            def delete_problem(self, name: str) -> dict[str, Any]:
                deleted.append(name)
                return {"deleted": name}

        monkeypatch.setattr("rebase.cli.Client", lambda *a, **kw: _Client())
        return deleted

    def test_list_get_versions_delete(self, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        from rebase.cli import main

        deleted = self._client(monkeypatch)
        assert main(["problem", "list"]) == 0
        out = capsys.readouterr().out
        assert "icing:se1" in out and "v2" in out and "PinballLoss" in out and "1D -> 37h" in out
        assert "icing:se2" in out

        assert main(["problem", "get", "icing:se1"]) == 0
        out = capsys.readouterr().out
        assert "abcdef0123456789" in out and "deadbeef" in out and "2026-01-15 .. 2026-04-01" in out

        assert main(["problem", "versions", "icing:se1"]) == 0
        assert "v1" in capsys.readouterr().out
        assert main(["problem", "versions", "icing:se1", "--json"]) == 0
        assert "0123456789abcdef" in capsys.readouterr().out

        assert main(["problem", "delete", "icing:se1", "--yes"]) == 0
        assert deleted == ["icing:se1"] and "Deleted problem" in capsys.readouterr().out


def _emflow_like_result(**overrides: Any) -> SimpleNamespace:
    """What an emflow Result looks like to the duck-typed payload extractor."""
    result = SimpleNamespace(
        problem="icing:se1",
        model="qrf-physics-v1",
        objective="PinballLoss",
        lower_is_better=True,
        split="validation",
        score=67.48591210664405,
        n_origins=107,
        n_scored=2544,
        stats=lambda: {"PersistenceSkill.skill": 0.739, "QuantileCalibration.coverage_q50": float("nan"), "n": 3},
    )
    for key, value in overrides.items():
        setattr(result, key, value)
    return result


class TestScores:
    def test_payload_reads_the_result_and_drops_nan(self) -> None:
        from rebase.client import problem_score_payload

        payload = problem_score_payload(_emflow_like_result())
        assert payload["model"] == "qrf-physics-v1" and payload["split"] == "validation"
        assert payload["metric"] == "PinballLoss" and payload["lower_is_better"] is True
        assert payload["score"] == pytest.approx(67.4859, abs=1e-4)
        assert payload["n_origins"] == 107 and payload["n_scored"] == 2544
        assert payload["stats"] == {"PersistenceSkill.skill": 0.739, "QuantileCalibration.coverage_q50": None, "n": 3}

    def test_model_name_can_be_overridden(self) -> None:
        from rebase.client import problem_score_payload

        assert problem_score_payload(_emflow_like_result(), model="physics-v2")["model"] == "physics-v2"

    def test_record_pins_the_fingerprint_of_the_loaded_spec(self) -> None:
        client = FakeClient()
        problem = Problem(factory=_emflow_like_problem, client=client)  # type: ignore[arg-type]
        response = problem.record(_emflow_like_result(), source={"notes": "trees=500"})
        sent = client.recorded[0]
        assert sent["name"] == "icing:se1"
        assert sent["fingerprint"] == problem.fingerprint()
        assert sent["score"] == pytest.approx(67.4859, abs=1e-4)
        assert sent["source"]["notes"] == "trees=500"
        assert "git_commit_sha" in sent["source"], "factory git metadata travels with the score"
        assert response["version_number"] == 1

    def test_record_by_name_carries_the_running_repo_commit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No factory to read git metadata from: the recording script's repo is the provenance."""
        client = FakeClient()
        problem = Problem.from_name("icing:se1", client=client)
        monkeypatch.setattr(problem, "fingerprint", lambda: "abc")
        monkeypatch.setattr(sys.modules["__main__"], "__file__", __file__, raising=False)
        problem.record(_emflow_like_result())
        source = client.recorded[0]["source"]
        assert source["git_commit_sha"] and source["repo_name"] == "rebase-toolkit"

    def test_scores_forwards_filters(self) -> None:
        client = FakeClient()
        problem = Problem.from_name("icing:se1", client=client)
        problem.scores(split="holdout", best=True)
        assert client.listed == {"name": "icing:se1", "version": None, "split": "holdout", "model": None, "best": True}


class TestScoresCli:
    def test_scores_table_and_json(self, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        from rebase import cli

        client = FakeClient()
        monkeypatch.setattr(cli, "Client", lambda *a, **k: client)
        assert cli.main(["problem", "scores", "icing:se1"]) == 0
        out = capsys.readouterr().out
        assert "best per model and split" in out and "--all" in out
        assert client.listed["best"] is True  # the table wraps at 80 columns; the JSON form checks the content

        assert cli.main(["problem", "scores", "icing:se1", "--all", "--split", "validation", "--json"]) == 0
        assert client.listed["best"] is False and client.listed["split"] == "validation"
        assert '"climatology"' in capsys.readouterr().out


class TestFramework:
    def test_load_prefers_sdflow_and_records_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import types

        from rebase import client as client_module

        fake = types.ModuleType("sdflow")
        fake.__version__ = "0.1.0"
        fake.load_problem = lambda name: _emflow_like_problem(name)  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "sdflow", fake)
        monkeypatch.delitem(sys.modules, "emflow", raising=False)

        problem = Problem.from_name("icing:se1", client=FakeClient())
        assert problem.load().name == "icing:se1"
        source = problem._source("icing")
        assert source["framework"] == "sdflow" and source["framework_version"] == "0.1.0"
        assert client_module.decision_framework() is fake

    def test_load_falls_back_to_emflow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import types

        from rebase import _framework
        from rebase import client as client_module

        monkeypatch.delitem(sys.modules, "sdflow", raising=False)
        fake = types.ModuleType("emflow")
        fake.__version__ = "0.3.1"
        fake.load_problem = lambda name: _emflow_like_problem(name)  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "emflow", fake)
        monkeypatch.setattr(
            _framework.importlib,
            "import_module",
            lambda n: (_ for _ in ()).throw(ImportError()) if n == "sdflow" else fake,
        )
        assert client_module.decision_framework() is fake


class TestInstallsFramework:
    def test_either_framework_counts(self) -> None:
        from rebase._framework import installs_framework

        assert installs_framework("sdflow>=0.1")
        assert installs_framework("sdflow[energy]==0.1.0")
        assert installs_framework("emflow @ git+https://github.com/rebase-energy/emflow.git@abc")
        assert not installs_framework("pandas>=2")
        assert not installs_framework("sdflowers")

    def test_models_default_to_sdflow_from_pypi(self) -> None:
        from rebase.client import DEFAULT_MODEL_DEPENDENCY, _model_dependencies

        assert DEFAULT_MODEL_DEPENDENCY.startswith("sdflow")
        assert _model_dependencies(["pandas>=2"]) == ["pandas>=2", DEFAULT_MODEL_DEPENDENCY]
        assert _model_dependencies(["emflow>=0.3"]) == ["emflow>=0.3"], "a named framework is left alone"
