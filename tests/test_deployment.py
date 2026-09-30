from __future__ import annotations

import importlib
import json
from collections import Counter
from typing import Any

import pytest
import requests
from http_stub import patch_client_http

import rebase as rb
from rebase.deployment import _deployment_scope, _report_context

client_module = importlib.import_module("rebase.client")
cli = importlib.import_module("rebase.cli")


def response(payload: Any, status: int = 200, **headers: str) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode()
    result.headers.update(headers)
    return result


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(client_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(rb.Workflow, "_source_metadata_for_deploy", lambda *args: {})
    monkeypatch.setattr(rb.Function, "_source_metadata_for_deploy", lambda *args: {})
    monkeypatch.setattr(rb.Client, "_http_request", lambda *args, **kwargs: pytest.fail("unexpected HTTP request"))
    return rb.Client(api_key="test", api_url="https://example.invalid", workspace_id="test", environment_name="dev")


def workflow(client: rb.Client, name: str) -> rb.Workflow:
    target = rb.Workflow(name=name, project="fleet", client=client)
    target.source_code = "def run(): return 1"
    target.entrypoint = "run"
    return target


def test_workflow_create_and_update_use_deployment_timeout(client, monkeypatch) -> None:
    calls = []

    def http(method, url, **kwargs):
        calls.append((method, kwargs["timeout"]))
        return response({"id": "w"})

    patch_client_http(monkeypatch, http)
    client.register_workflow(name="w", source_code="def w(): pass")
    client.update_workflow("w", source_code="def w(): pass")
    assert calls == [("POST", 300), ("PATCH", 300)]


@pytest.mark.parametrize("method", ["POST", "PATCH"])
def test_workflow_write_retries_connect_timeout_before_request_is_sent(client, monkeypatch, method) -> None:
    calls = []

    def http(verb, url, **kwargs):
        calls.append((verb, kwargs["json"].copy()))
        if len(calls) < 3:
            raise requests.ConnectTimeout("connection not established")
        return response({"id": "w"})

    patch_client_http(monkeypatch, http)
    if method == "POST":
        result = client.register_workflow(name="w", source_code="new source")
    else:
        result = client.update_workflow("w", source_code="new source")
    assert result["id"] == "w"
    assert [verb for verb, _ in calls] == [method] * 3
    assert calls[0][1] == calls[1][1] == calls[2][1]


@pytest.mark.parametrize("method", ["POST", "PATCH"])
@pytest.mark.parametrize("failure", ["timeout", "connection", "server"])
def test_uncertain_workflow_write_is_reconciled_without_replaying(client, monkeypatch, method, failure) -> None:
    calls = Counter()
    stored = {}

    def http(verb, url, **kwargs):
        path = url.removeprefix(client.api_url)
        calls[(verb, path)] += 1
        if verb == method:
            stored.update(kwargs["json"], id="w")
            if failure == "server":
                return response({"detail": "lost response"}, 500)
            error = requests.ReadTimeout if failure == "timeout" else requests.ConnectionError
            raise error("response lost after commit")
        if path == "/workflows":
            return response([{"id": "w", "name": "w"}])
        assert verb == "GET" and path == "/workflows/w"
        # Exercise eventual visibility after the first probe.
        return response(stored if calls[(verb, path)] > 1 else {"id": "w", "source_code": "old"})

    patch_client_http(monkeypatch, http)
    if method == "POST":
        result = client.register_workflow(name="w", source_code="new")
    else:
        result = client.update_workflow("w", source_code="new")
    assert result == stored
    assert sum(count for (verb, _), count in calls.items() if verb == method) == 1
    assert calls[("GET", "/workflows/w")] == 2


@pytest.mark.parametrize("readback", ["absent", "different", "missing_field", "duplicate", "unavailable", "build"])
def test_unconfirmed_create_is_uncertain_and_never_reposted(client, monkeypatch, readback) -> None:
    calls = Counter()
    stored = {}

    def http(method, url, **kwargs):
        calls[method] += 1
        if method == "POST":
            stored.update(kwargs["json"], id="w")
            raise requests.ReadTimeout("response lost")
        if readback == "unavailable":
            return response({"detail": "unavailable"}, 503)
        if url.endswith("/workflows"):
            if readback == "absent":
                return response([])
            matches = [{"id": "w", "name": "w"}]
            return response(matches * (2 if readback == "duplicate" else 1))
        current = stored.copy()
        if readback == "different":
            current["source_code"] = "old"
        elif readback == "missing_field":
            current.pop("git_dirty")
        return response(current)

    patch_client_http(monkeypatch, http)
    with pytest.raises(rb.RebaseWorkflowError, match="write outcome uncertain"):
        client.register_workflow(name="w", source_code="new", build={"id": "build"} if readback == "build" else None)
    assert calls["POST"] == 1
    assert calls["GET"] >= 3


def test_exhausted_connect_timeout_is_bounded_and_not_uncertain(client, monkeypatch) -> None:
    calls = []

    def http(method, url, **kwargs):
        calls.append(method)
        raise requests.ConnectTimeout("unreachable")

    patch_client_http(monkeypatch, http)
    with pytest.raises(rb.RebaseWorkflowError) as caught:
        client.update_workflow("w", source_code="new")
    assert "uncertain" not in str(caught.value)
    assert calls == ["PATCH"] * 3


@pytest.mark.parametrize("failure", ["timeout", "connection", "throttle", "server", "gateway"])
def test_deployment_reads_retry_transient_failures(client, monkeypatch, failure) -> None:
    calls = []
    delays = []

    def http(method, url, **kwargs):
        calls.append(method)
        if len(calls) < 3:
            if failure == "timeout":
                raise requests.ReadTimeout("temporary")
            if failure == "connection":
                raise requests.ConnectionError("temporary")
            status = {"throttle": 429, "server": 500, "gateway": 503}[failure]
            return response({"detail": "temporary"}, status, **{"Retry-After": "999"})
        return response([])

    patch_client_http(monkeypatch, http)
    monkeypatch.setattr(client_module.time, "sleep", delays.append)
    with _deployment_scope():
        assert client.list_workflows() == []
    assert calls == ["GET"] * 3
    assert len(delays) == 2 and all(0 < delay <= 10 for delay in delays)


def test_deployment_read_does_not_retry_auth_or_validation_failure(client, monkeypatch) -> None:
    calls = []

    def http(method, url, **kwargs):
        calls.append(method)
        return response({"detail": "forbidden"}, 403)

    patch_client_http(monkeypatch, http)
    with _deployment_scope(), pytest.raises(rb.RebaseWorkflowError) as caught:
        client.list_workflows()
    assert caught.value.status_code == 403
    assert calls == ["GET"]


@pytest.mark.parametrize("method,path", [("POST", "/runs/ephemeral"), ("GET", "/workflows")])
def test_non_deployment_requests_keep_existing_retry_behavior(client, monkeypatch, method, path) -> None:
    calls = []

    def http(verb, url, **kwargs):
        calls.append(verb)
        raise requests.ConnectTimeout("temporary")

    patch_client_http(monkeypatch, http)
    with pytest.raises(requests.ConnectTimeout):
        client.request(method, path)
    assert calls == [method]


def fleet(client, monkeypatch, *, failure_at="wf-2", failure="validation"):
    project = rb.Project("fleet", client=client)
    project._workflows = [workflow(client, f"wf-{i}") for i in range(4)]
    calls = Counter()

    def http(method, url, **kwargs):
        path = url.removeprefix(client.api_url)
        calls[(method, path)] += 1
        if method == "GET":
            if path == "/projects":
                return response([{"id": "p", "name": "fleet"}])
            if path == "/projects/p/workflows":
                return response([{"id": target.name, "name": target.name} for target in project._workflows])
            return response({"id": path.rsplit("/", 1)[-1], "source_code": "old"})
        assert method == "PATCH"
        name = path.rsplit("/", 1)[-1]
        if name == failure_at:
            if failure == "timeout":
                raise requests.ReadTimeout("injected")
            if failure == "interrupt":
                raise KeyboardInterrupt
            return response({"detail": "injected validation error"}, 400)
        return response({"id": name, **kwargs["json"]})

    patch_client_http(monkeypatch, http)
    return project, calls


@pytest.mark.parametrize("failure,status", [("validation", "failed"), ("timeout", "uncertain")])
def test_project_reports_successful_prefix_failure_and_unattempted_suffix(client, monkeypatch, failure, status) -> None:
    project, calls = fleet(client, monkeypatch, failure=failure)
    with pytest.raises(rb.DeploymentError) as caught:
        project.deploy()
    report = caught.value.report
    assert report is project.deployment_report
    assert [(r.name, r.status) for r in report.results] == [
        ("wf-0", "succeeded"),
        ("wf-1", "succeeded"),
        ("wf-2", status),
        ("wf-3", "unattempted"),
    ]
    assert report.results[0].id == "wf-0"
    assert report.results[2].error
    assert report.results[3].id is None
    assert calls[("PATCH", "/workflows/wf-2")] == 1
    assert calls[("PATCH", "/workflows/wf-3")] == 0
    assert report.counts["succeeded"] == 2
    assert report.counts["unattempted"] == 1
    assert _report_context.get() is None


def test_project_preflight_failure_reports_all_targets_unattempted(client, monkeypatch) -> None:
    project, calls = fleet(client, monkeypatch)

    def fail(*args):
        raise rb.RebaseWorkflowError("preflight failed")

    monkeypatch.setattr(client_module, "preflight_datasets", fail)
    with pytest.raises(rb.DeploymentError) as caught:
        project.deploy()
    assert caught.value.report.counts["unattempted"] == 4
    assert caught.value.report.error == "preflight failed"
    assert not calls


def test_shared_step_failure_is_distinguished_from_workflows_that_never_wrote(client, monkeypatch) -> None:
    project = rb.Project("fleet", client=client)

    @project.step(name="shared")
    def shared():
        return 1

    @project.workflow(name="first")
    def first():
        return shared()

    @project.workflow(name="second")
    def second():
        return shared()

    def http(method, url, **kwargs):
        if method == "GET" and url.endswith("/projects"):
            return response([{"id": "p", "name": "fleet"}])
        if method == "GET" and url.endswith("/functions"):
            return response([{"id": "s", "name": "shared"}])
        if method == "GET" and url.endswith("/functions/s"):
            return response({"id": "s", "source_code": "old"})
        assert method == "PATCH" and url.endswith("/functions/s")
        raise requests.ReadTimeout("step response lost")

    patch_client_http(monkeypatch, http)
    with pytest.raises(rb.DeploymentError) as caught:
        project.deploy()
    results = caught.value.report.results
    assert [(r.name, r.status) for r in results] == [
        ("shared", "uncertain"),
        ("first", "failed"),
        ("second", "unattempted"),
    ]
    assert results[0].target_type == "step"


def test_public_deploy_reports_later_targets_and_returns_original_targets_on_success(client, monkeypatch) -> None:
    project, calls = fleet(client, monkeypatch)
    with pytest.raises(rb.DeploymentError) as caught:
        rb.deploy(*project._workflows)
    assert caught.value.report.counts == {"succeeded": 2, "failed": 1, "uncertain": 0, "unattempted": 1}
    old_report = caught.value.report
    project, calls = fleet(client, monkeypatch, failure_at=None)
    assert rb.deploy(*project._workflows) == project._workflows
    assert project._workflows[0].deployment_report is not old_report
    assert old_report.counts["failed"] == 1
    assert _report_context.get() is None


@pytest.mark.parametrize("failure,exit_code", [("validation", 1), ("timeout", 1), ("interrupt", 130), ("none", 0)])
def test_cli_always_prints_deployment_outcomes(client, monkeypatch, capsys, failure, exit_code) -> None:
    from types import SimpleNamespace

    project, calls = fleet(client, monkeypatch, failure=failure, failure_at=None if failure == "none" else "wf-2")
    monkeypatch.setattr(cli, "Client", lambda: client)
    monkeypatch.setattr(cli, "_environment_policy", lambda *args: {})
    monkeypatch.setattr(cli, "_policy_requires_gitops", lambda *args: False)
    monkeypatch.setattr(cli, "_load_module", lambda path: SimpleNamespace(project=project))
    assert cli.main(["deploy", "unused.py"]) == exit_code
    output = capsys.readouterr()
    combined = output.out + output.err
    assert "Deployment results" in combined
    assert "wf-0" in combined and "wf-3" in combined
    assert ("4 succeeded" if failure == "none" else "2 succeeded") in combined
    if failure in {"timeout", "interrupt"}:
        assert "1 uncertain" in combined
    assert _report_context.get() is None


def test_retry_after_deployment_failure_gets_a_fresh_report(client, monkeypatch) -> None:
    project, _ = fleet(client, monkeypatch)
    with pytest.raises(rb.DeploymentError) as caught:
        project.deploy()
    old_report = caught.value.report
    monkeypatch.setattr(
        client, "_write_definition_uncached", lambda method, path, payload, **kwargs: {"id": path.rsplit("/", 1)[-1]}
    )
    assert project.deploy() is project
    assert project.deployment_report is not old_report
    assert project.deployment_report.counts == {"succeeded": 4, "failed": 0, "uncertain": 0, "unattempted": 0}
    assert old_report.counts["failed"] == 1


def test_reconciled_workflow_is_reported_as_succeeded(client, monkeypatch) -> None:
    target = workflow(client, "w")
    stored = {}

    def http(method, url, **kwargs):
        if method == "PATCH":
            stored.update(kwargs["json"], id="w")
            raise requests.ReadTimeout("response lost")
        if url.endswith("/projects"):
            return response([{"id": "p", "name": "fleet"}])
        if url.endswith("/projects/p/workflows"):
            return response([{"id": "w", "name": "w"}])
        return response(stored)

    patch_client_http(monkeypatch, http)
    assert target.deploy(environment="staging") is target
    assert target.deployment_report.counts == {"succeeded": 1, "failed": 0, "uncertain": 0, "unattempted": 0}
    assert target.deployment_report.results[0].error is None
    assert target.deployment_report.error is None


def test_cli_includes_targets_in_projects_after_the_failed_project(client, monkeypatch, capsys) -> None:
    from types import SimpleNamespace

    first, _ = fleet(client, monkeypatch)
    second = rb.Project("other", client=client)
    second._workflows = [workflow(client, "later")]
    monkeypatch.setattr(cli, "_load_module", lambda path: SimpleNamespace(first=first, second=second))
    with pytest.raises(rb.DeploymentError) as caught:
        cli.deploy_file("unused.py")
    assert [(r.name, r.status) for r in caught.value.report.results][-1] == ("later", "unattempted")
    assert "later" in capsys.readouterr().err


def test_deployment_retry_and_readback_keep_environment_headers(client, monkeypatch) -> None:
    target = workflow(client, "w")
    observed = []
    stored = {}
    lookup_calls = 0

    def http(method, url, **kwargs):
        nonlocal lookup_calls
        observed.append(kwargs["headers"]["X-Rebase-Environment"])
        if url.endswith("/projects"):
            lookup_calls += 1
            if lookup_calls == 1:
                raise requests.ConnectionError("temporary lookup failure")
            return response([{"id": "p", "name": "fleet"}])
        if url.endswith("/projects/p/workflows"):
            return response([{"id": "w", "name": "w"}])
        if method == "PATCH":
            stored.update(kwargs["json"], id="w")
            raise requests.ReadTimeout("response lost")
        return response(stored)

    patch_client_http(monkeypatch, http)
    target.deploy(environment="staging")
    assert len(observed) == 5
    assert set(observed) == {"staging"}
    assert client.environment_name == "dev"


@pytest.mark.parametrize("method", ["POST", "PATCH"])
def test_reconciliation_reads_version_fields_omitted_by_workflow_endpoint(client, monkeypatch, method):
    payload = {
        "name": "w",
        "source_code": "new",
        "step_graph": {"nodes": []},
        "required_parameters": [],
        "git_commit_sha": "abc",
        "git_dirty": False,
        "environment": "dev",
    }
    resource = {
        "id": "w",
        "name": "w",
        "source_code": "new",
        "current_version_id": "v",
    }
    version = {
        "id": "v",
        "workflow_id": "w",
        "step_graph": {"nodes": []},
        "required_parameters": [],
        "git_commit_sha": "abc",
        "git_dirty": False,
    }
    calls = []

    def http(verb, url, **kwargs):
        calls.append((verb, url))
        if verb == method:
            raise requests.ReadTimeout("response lost after commit")
        if url.endswith("/workflows"):
            return response([resource])
        if url.endswith("/versions/v"):
            return response(version)
        return response(resource)

    patch_client_http(monkeypatch, http)
    result = client._write_definition(
        method, "/workflows" if method == "POST" else "/workflows/w", payload, target_type="workflow"
    )
    assert result == resource
    assert sum(verb == method for verb, _ in calls) == 1
    assert sum(url.endswith("/versions/v") for _, url in calls) == 1
    assert sum(verb == "GET" and url.endswith("/workflows/w") for verb, url in calls) == 2


@pytest.mark.parametrize("mismatch", ["version_id", "workflow_id", "concurrent_deploy", "missing_field", "environment"])
def test_reconciliation_rejects_inconsistent_or_incomplete_versions(client, monkeypatch, mismatch):
    resource = {"id": "w", "current_version_id": "v", "source_code": "new"}
    version = {"id": "v", "workflow_id": "w", "step_graph": {"nodes": []}}
    payload = {"source_code": "new", "step_graph": {"nodes": []}, "environment": "dev"}
    if mismatch == "environment":
        payload["environment"] = "staging"
    if mismatch in {"version_id", "workflow_id"}:
        version["id" if mismatch == "version_id" else "workflow_id"] = "other"
    if mismatch == "missing_field":
        version.pop("step_graph")

    def http(verb, url, **kwargs):
        assert verb == "GET"
        if url.endswith("/versions/v"):
            return response(version)
        return response({**resource, "current_version_id": "newer" if mismatch == "concurrent_deploy" else "v"})

    patch_client_http(monkeypatch, http)
    assert client._confirmed_definition(resource, payload, target_type="workflow") is None


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", [requests.ReadTimeout, requests.ConnectionError])
def test_shared_step_lost_response_is_reconciled_before_workflow_deploy(client, monkeypatch, existing, failure):
    project = rb.Project("fleet", client=client)

    @project.step(name="shared")
    def shared():
        return 1

    @project.workflow(name="first")
    def first():
        return shared()

    stored = {}
    version = {}
    writes = []

    def http(method, url, **kwargs):
        path = url.removeprefix(client.api_url)
        if method == "GET":
            if path == "/projects":
                return response([{"id": "p", "name": "fleet"}])
            if path == "/projects/p/functions":
                return response([stored or {"id": "s", "name": "shared"}] if existing or stored else [])
            if path == "/projects/p/workflows":
                return response([])
            if path == "/functions/s/versions/v":
                return response(version)
            if path == "/functions/s":
                return response(stored)
        writes.append((method, path))
        assert kwargs["timeout"] == 300
        if path in {"/functions/s", "/projects/p/functions"}:
            payload = kwargs["json"]
            stored.update(
                {k: v for k, v in payload.items() if not k.startswith("git_")}, id="s", current_version_id="v"
            )
            stored.pop("environment", None)
            version.update({k: v for k, v in payload.items() if k.startswith("git_")}, id="v", function_id="s")
            raise failure("shared-step response lost after commit")
        assert path == "/projects/p/workflows"
        return response({"id": "w", **kwargs["json"]})

    patch_client_http(monkeypatch, http)
    assert project.deploy() is project
    assert writes == [
        ("PATCH", "/functions/s") if existing else ("POST", "/projects/p/functions"),
        ("POST", "/projects/p/workflows"),
    ]
    assert project.deployment_report.counts == {"succeeded": 2, "failed": 0, "uncertain": 0, "unattempted": 0}


@pytest.mark.parametrize(
    "change,confirmed", [(None, True), ("python_version", False), ("runtime", False), ("extra", False)]
)
def test_function_reconciliation_accepts_only_known_python_image_default(client, change, confirmed):
    image = {"kind": "python", "python_version": "3.13", "uv_pip_packages": ["requests==2.32.5"], "uv_version": None}
    persisted = {**image, "runtime": "python"}
    if change:
        persisted[change] = "different"
    resource = {"id": "f", "image_spec": persisted}
    assert (
        client._confirmed_definition(resource, {"image_spec": image}, target_type="function") is not None
    ) == confirmed


def test_function_reconciliation_checks_explicit_image_runtime(client):
    resource = {"id": "f", "image_spec": {"kind": "python", "runtime": "python"}}
    assert (
        client._confirmed_definition(
            resource, {"image_spec": {"kind": "python", "runtime": "other"}}, target_type="function"
        )
        is None
    )


@pytest.mark.parametrize(
    "mode,isolation,run_type,confirmed",
    [
        ("job", None, "long", True),
        ("interactive", None, "quick_shared", False),
        ("job", "dedicated", "long", False),
        ("job", None, "quick", False),
    ],
)
def test_reconciliation_accepts_stored_job_isolation_default_only(client, mode, isolation, run_type, confirmed):
    resource = {"id": "w", "mode": mode, "isolation": isolation, "run_type": run_type}
    payload = {"mode": mode, "isolation": "shared"}
    assert (client._confirmed_definition(resource, payload, target_type="workflow") is not None) == confirmed


def test_reconciliation_rejects_missing_job_isolation(client):
    resource = {"id": "w", "mode": "job", "run_type": "long"}
    assert (
        client._confirmed_definition(resource, {"mode": "job", "isolation": "shared"}, target_type="workflow") is None
    )


@pytest.fixture
def cached_fleet(client, monkeypatch):
    monkeypatch.setattr(rb.Function, "_source_metadata_for_deploy", lambda *args: {"git_dirty": False})
    project = rb.Project("fleet", client=client)

    @project.step(name="shared")
    def shared():
        return 1

    @project.workflow(name="first")
    def first():
        return shared()

    @project.workflow(name="second")
    def second():
        return shared()

    calls = Counter()
    stored = {"functions": {}, "workflows": {}}

    def http(method, url, **kwargs):
        path = url.removeprefix(client.api_url)
        calls[(method, path)] += 1
        if path == "/projects":
            return response([{"id": "p", "name": "fleet"}])
        if path.startswith("/secrets/"):
            return response({"secret_refs": {"KEY": "reference"}})
        parts = path.strip("/").split("/")
        kind = parts[-1] if parts[0] == "projects" else parts[0]
        if method == "GET":
            return response(list(stored[kind].values()) if parts[0] == "projects" else stored[kind][parts[1]])
        payload = kwargs["json"]
        name = payload["name"] if method == "POST" else parts[1]
        resource = stored[kind].setdefault(name, {"id": name, "name": name})
        resource.update(payload, current_version_id=name + "-version")
        return response(resource)

    patch_client_http(monkeypatch, http)
    return project, shared, calls, stored


def test_grid_shaped_fleet_reuses_steps_and_lookups_only_within_invocation(cached_fleet, client):
    project, shared, calls, stored = cached_fleet
    shared.secrets = ["bundle"]
    for target in project._workflows:
        target.secrets = ["bundle"]
    project.deploy()
    assert calls[("POST", "/projects/p/functions")] == 1
    assert calls[("PATCH", "/functions/shared")] == 0
    assert calls[("GET", "/projects")] == 1
    assert calls[("GET", "/projects/p/functions")] == 1
    assert calls[("GET", "/projects/p/workflows")] == 1
    assert calls[("GET", "/secrets/bundle")] == 1
    assert project.deployment_report.counts == {"succeeded": 3, "failed": 0, "uncertain": 0, "unattempted": 0}
    # A later invocation must refresh every lookup and perform one new step write.
    project.deploy()
    assert calls[("PATCH", "/functions/shared")] == 1
    assert calls[("GET", "/projects")] == 2
    assert calls[("GET", "/projects/p/functions")] == 2
    assert calls[("GET", "/projects/p/workflows")] == 2
    assert calls[("GET", "/secrets/bundle")] == 2


@pytest.mark.parametrize(
    "attribute,value",
    [
        ("source_code", "def shared(): return 2"),
        ("image_spec", {"kind": "python", "python_version": "3.13"}),
        ("env", {"FLAG": "changed"}),
        ("secrets", {"KEY": "different-reference"}),
        ("cloud_run_cpu", "2"),
        ("default_parameters", {"value": 2}),
        ("enabled", False),
    ],
)
def test_changed_step_definition_is_written_including_return_to_previous_value(cached_fleet, attribute, value):
    from copy import deepcopy

    project, shared, calls, stored = cached_fleet
    original = deepcopy(getattr(shared, attribute))
    with _deployment_scope():
        rb.Function.deploy(shared)
        setattr(shared, attribute, value)
        rb.Function.deploy(shared)
        setattr(shared, attribute, original)
        rb.Function.deploy(shared)
        rb.Function.deploy(shared)
    assert calls[("POST", "/projects/p/functions")] == 1
    assert calls[("PATCH", "/functions/shared")] == 2


def test_intervening_function_write_invalidates_step_reuse(cached_fleet, client):
    _, shared, calls, stored = cached_fleet
    with _deployment_scope():
        rb.Function.deploy(shared)
        client.update_function(shared.id, source_code="def shared(): return 999")
        rb.Function.deploy(shared)
    assert calls[("PATCH", "/functions/shared")] == 2
    assert stored["functions"]["shared"]["source_code"] == shared.source_code


def test_lookup_cache_isolated_by_environment_workspace_and_client(client, monkeypatch):
    calls = []

    def http(method, url, **kwargs):
        headers = kwargs["headers"]
        calls.append(headers.copy())
        return response([{"id": str(len(calls)), "name": "fleet"}])

    patch_client_http(monkeypatch, http)
    with _deployment_scope():
        assert client.find_project("fleet")["id"] == "1"
        assert client.find_project("fleet")["id"] == "1"
        assert client.find_project("fleet", environment_name="staging")["id"] == "2"
        assert client.find_project("fleet", environment_name="staging")["id"] == "2"
        client.workspace_id = "other"
        assert client.find_project("fleet")["id"] == "3"
        other = rb.Client(api_key="other", api_url=client.api_url, workspace_id="other")
        found = other.find_project("fleet")
        assert found is not None and found["id"] == "4"
    assert len(calls) == 4


def test_failed_invocation_drops_cache_and_returned_data_cannot_poison_it(cached_fleet, client):
    _, _, calls, _ = cached_fleet
    with pytest.raises(RuntimeError), _deployment_scope():
        item = client.find_project("fleet")
        item["name"] = "mutated"
        assert client.find_project("fleet")["name"] == "fleet"
        raise RuntimeError("interrupted deployment")
    with _deployment_scope():
        assert client.find_project("fleet")["name"] == "fleet"
    assert calls[("GET", "/projects")] == 2


def test_secret_mutation_invalidates_cached_reference(client, monkeypatch):
    current = {"secret_refs": {"KEY": "old"}}
    calls = Counter()

    def http(method, url, **kwargs):
        calls[method] += 1
        if method == "PUT":
            current["secret_refs"]["KEY"] = "new"
        return response(current)

    patch_client_http(monkeypatch, http)
    with _deployment_scope():
        assert client.get_secret("bundle")["secret_refs"]["KEY"] == "old"
        client.set_secret("bundle", {"KEY": "value"})
        assert client.get_secret("bundle")["secret_refs"]["KEY"] == "new"
    assert calls["GET"] == 2


def test_tests_use_temporary_home_for_profiles_and_auth(tmp_path):
    from pathlib import Path

    from rebase.auth import auth_file_path
    from rebase.config import config_path, write_profile

    assert Path.home().is_relative_to(tmp_path)
    assert Path("~").expanduser() == Path.home()
    assert config_path().is_relative_to(tmp_path)
    assert auth_file_path().is_relative_to(tmp_path)
    write_profile(profile="isolated", api_url="https://example.invalid", workspace={"id": "test"})
    assert config_path().is_file()


def test_cached_inventory_never_confirms_a_lost_create_response(cached_fleet, client, monkeypatch):
    project, shared, calls, stored = cached_fleet
    original_http = rb.Client._http_request
    discarded = False

    def lose_reply(self, method, path, **kwargs):
        nonlocal discarded
        reply = original_http(self, method, path, **kwargs)
        if method == "POST" and path == "/projects/p/functions" and not discarded:
            discarded = True
            raise requests.ReadTimeout("lost committed create reply")
        return reply

    monkeypatch.setattr(rb.Client, "_http_request", lose_reply)
    project.deploy()
    assert discarded
    assert calls[("POST", "/projects/p/functions")] == 1
    assert calls[("GET", "/projects/p/functions")] == 2  # inventory + fresh reconciliation
    assert calls[("GET", "/functions/shared")] == 1
    assert calls[("PATCH", "/functions/shared")] == 0
    assert project.deployment_report.counts["succeeded"] == 3


def test_cached_step_response_cannot_be_mutated_through_a_target(cached_fleet):
    _, shared, calls, _ = cached_fleet
    with _deployment_scope():
        rb.Function.deploy(shared)
        shared.data["current_version_id"] = "corrupted"
        rb.Function.deploy(shared)
        assert shared.data["current_version_id"] == "shared-version"
    assert calls[("POST", "/projects/p/functions")] == 1
    assert calls[("PATCH", "/functions/shared")] == 0


def test_project_only_selects_workflow_and_required_steps(client, monkeypatch):
    project = rb.Project("fleet", client=client)
    project._workflows = [workflow(client, "first"), workflow(client, "second")]
    calls = []
    monkeypatch.setattr(client, "ensure_project", lambda *a, **kw: {"id": "p"})
    monkeypatch.setattr(client, "find_workflow", lambda *a, **kw: None)
    monkeypatch.setattr(
        client,
        "_write_definition_uncached",
        lambda method, path, payload, **kw: calls.append(payload["name"]) or {"id": "w"},
    )
    project.deploy(only=["second"])
    assert calls == ["second"]
    assert project.deployment_report is not None
    assert [r.name for r in project.deployment_report.results] == ["second"]


@pytest.mark.parametrize("names", [["missing"], []])
def test_project_only_rejects_invalid_selection_before_writes(client, names):
    project = rb.Project("fleet", client=client)
    project._workflows = [workflow(client, "first")]
    with pytest.raises(rb.RebaseWorkflowError):
        project.deploy(only=names)


def test_cli_only_requires_project_disambiguation(client, monkeypatch):
    from types import SimpleNamespace

    first = rb.Project("first", client=client)
    second = rb.Project("second", client=client)
    monkeypatch.setattr(cli, "_load_module", lambda path: SimpleNamespace(first=first, second=second))
    with pytest.raises(rb.RebaseWorkflowError, match="exactly one project"):
        cli.deploy_file("unused.py", only=["workflow"])
