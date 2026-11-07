from __future__ import annotations

import copy
import importlib
import json
import threading
import time
from collections import Counter

import pytest
import requests

import rebase as rb

client_module = importlib.import_module("rebase.client")


def response(payload, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode()
    return result


class Platform:
    """Transport fixture for fresh reconciliation and bounded submission."""

    def __init__(self):
        self.resources = {}
        self.calls = Counter()
        self.writes = []
        self.delay = 0
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()
        self.fail = None
        self.lose = None
        self.unsupported = False
        self.sessions = set()

    def request(self, client, method, path, **kwargs):
        with self.lock:
            self.calls[method, path] += 1
        payload = kwargs.get("json", {})
        if path == "/projects":
            return response([{"id": "p", "name": "fleet"}])
        if path == "/projects/p/workflows" and method == "GET":
            return response(list(self.resources.values()))
        if path == "/workflows/compare-deployments":
            if self.unsupported:
                return response({"detail": "Not Found"}, 404)
            rows = []
            for item in payload["items"]:
                stored = self.resources[item["id"]]
                unchanged = all(stored.get(k) == v for k, v in item["definition"].items())
                rows.append({"id": item["id"], "unchanged": unchanged, "resource": stored if unchanged else None})
            return response({"items": rows})
        if path.startswith("/workflows/") and method == "GET":
            return response(self.resources[path.rsplit("/", 1)[1]])
        if method not in {"POST", "PATCH"}:
            raise AssertionError((method, path))
        name = payload.get("name", path.rsplit("/", 1)[1])
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.sessions.add(id(client._http_session()))
        try:
            if self.delay:
                time.sleep(self.delay)
            if name == self.fail:
                return response({"detail": "rejected"}, 400)
            stored = {
                **self.resources.get(name, {}),
                **copy.deepcopy(payload),
                "id": name,
                "name": name,
                "current_version_id": f"{name}-{len(self.writes)}",
            }
            with self.lock:
                self.resources[name] = stored
                self.writes.append(name)
            if name == self.lose:
                self.lose = None
                raise requests.ReadTimeout("response lost after commit")
            return response(stored)
        finally:
            with self.lock:
                self.active -= 1


@pytest.fixture
def fleet(monkeypatch):
    platform = Platform()
    client = rb.Client(api_key="test", api_url="https://example.invalid", workspace_id="test", environment_name="dev")
    monkeypatch.setattr(
        rb.Client, "_http_request", lambda self, method, path, **kw: platform.request(self, method, path, **kw)
    )
    monkeypatch.setattr(rb.Workflow, "_source_metadata_for_deploy", lambda *a: {})
    project = rb.Project("fleet", client=client)
    for i in range(12):
        workflow = rb.Workflow(name=f"w{i:03}", project="fleet", client=client)
        workflow.source_code = "def run(): return 1"
        workflow.entrypoint = "run"
        project._workflows.append(workflow)
    return project, platform


def test_redeploy_skips_every_definition_and_one_change_writes_once(fleet):
    project, platform = fleet
    project.deploy()
    assert len(platform.writes) == 12
    platform.writes.clear()
    project.deploy()
    assert platform.writes == []
    assert project.deployment_report.counts["unchanged"] == 12
    project._workflows[3].default_parameters = {"value": 2}
    project.deploy()
    assert platform.writes == ["w003"]
    assert project.deployment_report.counts["unchanged"] == 11
    assert platform.calls["GET", "/projects/p/workflows"] == 3


def test_old_server_falls_back_once_per_invocation(fleet):
    project, platform = fleet
    project.deploy()
    platform.unsupported = True
    project.deploy()
    assert len(platform.writes) == 24
    assert platform.calls["POST", "/workflows/compare-deployments"] == 1
    assert project.deployment_report.counts["succeeded"] == 12


def test_failure_then_rerun_reconciles_and_only_writes_remainder(fleet):
    project, platform = fleet
    platform.fail = "w004"
    with pytest.raises(rb.DeploymentError) as caught:
        project.deploy()
    assert caught.value.report.counts == {"succeeded": 4, "failed": 1, "uncertain": 0, "unattempted": 7}
    platform.fail = None
    platform.writes.clear()
    project.deploy()
    assert platform.writes == [f"w{i:03}" for i in range(4, 12)]
    assert project.deployment_report.counts["unchanged"] == 4


def test_parallel_writes_are_bounded_and_use_separate_sessions(fleet):
    project, platform = fleet
    platform.delay = 0.015
    project.deploy(jobs=3)
    assert 1 < platform.peak <= 3
    assert len(platform.writes) == 12
    assert 1 < len(platform.sessions) <= 3
    assert project.deployment_report.counts["succeeded"] == 12


def test_parallel_failure_drains_inflight_and_leaves_suffix_unattempted(fleet):
    project, platform = fleet
    platform.delay = 0.02
    platform.fail = "w000"
    with pytest.raises(rb.DeploymentError) as caught:
        project.deploy(jobs=3)
    assert platform.active == 0
    assert caught.value.report.counts["failed"] == 1
    assert caught.value.report.counts["unattempted"] >= 7
    assert caught.value.report.counts["succeeded"] == len(platform.writes)
    assert sum(caught.value.report.counts.values()) == 12


def test_parallel_lost_response_is_confirmed_without_replay(fleet):
    project, platform = fleet
    platform.lose = "w001"
    project.deploy(jobs=3)
    assert Counter(platform.writes)["w001"] == 1
    assert project.deployment_report.counts["succeeded"] == 12


def test_only_does_not_touch_other_workflows(fleet):
    project, platform = fleet
    project.deploy(only=["w008", "w002"], jobs=2)
    assert set(platform.writes) == {"w002", "w008"}
    assert len(project.deployment_report.results) == 2


@pytest.mark.parametrize("jobs", [0, 17, True, 2.5])
def test_invalid_concurrency_does_not_write(fleet, jobs):
    project, platform = fleet
    with pytest.raises(rb.RebaseWorkflowError, match="jobs"):
        project.deploy(jobs=jobs)
    assert not platform.calls


def test_unchanged_suffix_is_reported_even_when_first_write_fails(fleet):
    project, platform = fleet
    project.deploy()
    project._workflows[0].default_parameters = {"changed": True}
    platform.fail = "w000"
    with pytest.raises(rb.DeploymentError) as caught:
        project.deploy()
    assert caught.value.report.counts["unchanged"] == 11
    assert caught.value.report.counts["failed"] == 1
    assert caught.value.report.counts["unattempted"] == 0


def test_interrupt_drains_inflight_and_preserves_report(fleet, monkeypatch):
    project, platform = fleet
    platform.delay = 0.03

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(client_module, "wait", interrupt)
    with pytest.raises(KeyboardInterrupt):
        project.deploy(jobs=3)
    assert platform.active == 0
    assert project.deployment_report.counts["succeeded"] == len(platform.writes)
    assert project.deployment_report.counts["unattempted"] >= 9
    assert sum(project.deployment_report.counts.values()) == 12


def test_corrupt_compare_response_does_not_allow_skipping(fleet, monkeypatch):
    project, platform = fleet
    project.deploy()
    original = platform.request

    def corrupt(client, method, path, **kwargs):
        if path.endswith("/compare-deployments"):
            return response({"items": []})
        return original(client, method, path, **kwargs)

    monkeypatch.setattr(platform, "request", corrupt)
    platform.writes.clear()
    with pytest.raises(rb.DeploymentError, match="Incomplete"):
        project.deploy()
    assert not platform.writes


def test_duplicate_workflow_names_are_rejected_before_parallel_writes(fleet):
    project, platform = fleet
    project._workflows[1].name = project._workflows[0].name
    with pytest.raises(rb.RebaseWorkflowError, match="unique"):
        project.deploy(jobs=4)
    assert not platform.calls


def test_cli_forwards_selection_and_concurrency(monkeypatch):
    cli = importlib.import_module("rebase.cli")
    calls = []
    monkeypatch.setattr(cli, "_environment_policy", lambda *a: {"protected": False})
    monkeypatch.setattr(cli, "deploy_file", lambda file, **kw: calls.append(kw) or [])
    assert cli.main(["deploy", "unused.py", "--only", "a", "--only", "b", "--jobs", "3"]) == 0
    assert calls[0]["only"] == ["a", "b"]
    assert calls[0]["jobs"] == 3


def test_cli_does_not_silently_drop_selection_for_gitops(monkeypatch):
    cli = importlib.import_module("rebase.cli")
    monkeypatch.setattr(cli, "_environment_policy", lambda *a: {"protected": True})
    monkeypatch.setattr(cli, "_create_gitops_intent_for_deploy", lambda *a, **kw: pytest.fail("selection lost"))
    assert cli.main(["deploy", "unused.py", "--only", "a"]) == 1
