import json
import sys
from datetime import UTC, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from http_stub import patch_client_http

import rebase as rb
from rebase.config import DEFAULT_API_URL, DEFAULT_SERVER_URL, set_active_environment, write_profile


class FakeResponse:
    def __init__(self, payload: dict[str, Any] | list[dict[str, Any]]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any] | list[dict[str, Any]]:
        return self._payload


class FakeErrorResponse(FakeResponse):
    text = '{"detail":"Rebase Workflows is invite-only."}'
    status_code = 403

    def raise_for_status(self) -> None:
        raise requests.HTTPError("403")


class FakeStreamResponse:
    text = ""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.closed = False

    def raise_for_status(self) -> None:
        return None

    def iter_lines(self, *, decode_unicode: bool = False):
        yield from self._lines

    def close(self) -> None:
        self.closed = True


def _map_square(x: int) -> dict[str, int]:
    return {"value": x * x}


def test_client_sends_bearer_token(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["method"] = method
        observed["url"] = url
        observed["headers"] = kwargs["headers"]
        return FakeResponse([])

    patch_client_http(monkeypatch, fake_request)

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    assert client.list_workflows() == []

    assert observed == {
        "method": "GET",
        "url": "https://workflows.example.com/workflows",
        "headers": {"Authorization": "Bearer rbw_test", "X-Rebase-Environment": "dev"},
    }


def test_client_request_accepts_custom_timeout(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["method"] = method
        observed["url"] = url
        observed["timeout"] = kwargs["timeout"]
        return FakeResponse({"ok": True})

    patch_client_http(monkeypatch, fake_request)

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    assert client.request("POST", "/slow", timeout=123, json={}) == {"ok": True}

    assert observed == {
        "method": "POST",
        "url": "https://workflows.example.com/slow",
        "timeout": 123,
    }


def test_stream_request_parses_ndjson(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    response = FakeStreamResponse(
        [
            '{"type":"item","index":0,"status":"succeeded","result":{"value":1}}',
            "",
            '{"type":"summary","status":"succeeded"}',
        ]
    )

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeStreamResponse:
        observed["method"] = method
        observed["url"] = url
        observed["headers"] = kwargs["headers"]
        observed["stream"] = kwargs["stream"]
        observed["timeout"] = kwargs["timeout"]
        return response

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    events = list(client.stream_request("POST", "/functions/fn/map", json={"items": [1]}))

    assert events == [
        {"type": "item", "index": 0, "status": "succeeded", "result": {"value": 1}},
        {"type": "summary", "status": "succeeded"},
    ]
    assert response.closed is True
    assert observed == {
        "method": "POST",
        "url": "https://workflows.example.com/functions/fn/map",
        "headers": {"Authorization": "Bearer rbw_test", "X-Rebase-Environment": "dev"},
        "stream": True,
        "timeout": None,
    }


def test_client_run_function_map_posts_payload(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_stream_request(method: str, path: str, **kwargs: Any):
        observed["method"] = method
        observed["path"] = path
        observed["json"] = kwargs["json"]
        observed["timeout"] = kwargs["timeout"]
        yield {"type": "summary", "status": "succeeded"}

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "stream_request", fake_stream_request)

    assert list(
        client.run_function_map(
            "function-id",
            items=[1, 2],
            parameter="x",
            kwargs={"scale": 10},
            max_concurrency=4,
            timeout=30,
        )
    ) == [{"type": "summary", "status": "succeeded"}]
    assert observed == {
        "method": "POST",
        "path": "/functions/function-id/map",
        "json": {
            "items": [1, 2],
            "parameter": "x",
            "kwargs": {"scale": 10},
            "ordered": True,
            "return_exceptions": False,
            "max_concurrency": 4,
            "timeout_seconds": 30,
        },
        "timeout": None,
    }


def test_client_run_function_map_attributes_the_batch_to_the_running_step(monkeypatch) -> None:
    """A map issued inside a step is that step's tasks, and only this call can say so."""
    observed: dict[str, Any] = {}

    def fake_stream_request(method: str, path: str, **kwargs: Any):
        observed.clear()
        observed.update(kwargs["json"])
        yield {"type": "summary", "status": "succeeded"}

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "stream_request", fake_stream_request)
    monkeypatch.setenv("REBASE_RUN_ID", "run-1")
    monkeypatch.setenv("REBASE_STEP_RUN_ID", "step-1")

    list(client.run_function_map("function-id", items=[1], parameter="x"))
    assert observed["workflow_run_id"] == "run-1"
    assert observed["step_run_id"] == "step-1"

    # Outside any step but inside a run, the batch still belongs to the run.
    monkeypatch.delenv("REBASE_STEP_RUN_ID")
    list(client.run_function_map("function-id", items=[1], parameter="x"))
    assert observed["workflow_run_id"] == "run-1"
    assert "step_run_id" not in observed

    # And a map from a laptop belongs to nothing, which the platform accepts.
    monkeypatch.delenv("REBASE_RUN_ID")
    list(client.run_function_map("function-id", items=[1], parameter="x"))
    assert "workflow_run_id" not in observed


def _http_error(message: str, status_code: int) -> rb.RebaseWorkflowError:
    """`status_code` is set on the instance by `request`, not passed to the constructor."""
    error = rb.RebaseWorkflowError(message)
    error.status_code = status_code
    return error


def test_client_list_run_tasks_tolerates_an_api_without_the_route(monkeypatch) -> None:
    calls: list[tuple[str, str, Any]] = []

    def fake_request(method: str, path: str, params: Any = None) -> Any:
        calls.append((method, path, params))
        return [{"item_index": 0, "status": "succeeded"}]

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "request", fake_request)
    assert client.list_run_tasks("run-1", step_run_id="step-1") == [{"item_index": 0, "status": "succeeded"}]
    assert calls == [("GET", "/runs/run-1/tasks", {"step_run_id": "step-1"})]

    # 404/405 mean the platform is older than this toolkit: no tasks, not a failure.
    def absent(method: str, path: str, params: Any = None) -> Any:
        raise _http_error("Method Not Allowed", 405)

    monkeypatch.setattr(client, "request", absent)
    assert client.list_run_tasks("run-1") == []

    def broken(method: str, path: str, params: Any = None) -> Any:
        raise _http_error("boom", 500)

    monkeypatch.setattr(client, "request", broken)
    with pytest.raises(rb.RebaseWorkflowError, match="boom"):
        client.list_run_tasks("run-1")


def test_client_lists_and_creates_run_artifacts(monkeypatch) -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def fake_request(method: str, path: str, **kwargs: Any) -> Any:
        calls.append((method, path, kwargs))
        return [{"id": "artifact-1"}] if method == "GET" else {"id": "artifact-1"}

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "request", fake_request)

    assert client.list_run_artifacts("run-1", step_run_id="step-1", task_id="task-1") == [{"id": "artifact-1"}]
    assert client.create_run_artifact("run-1", {"uri": "gs://bucket/a.json"}) == {"id": "artifact-1"}
    assert calls == [
        (
            "GET",
            "/runs/run-1/artifacts",
            {"params": {"step_run_id": "step-1", "task_id": "task-1"}},
        ),
        ("POST", "/runs/run-1/artifacts", {"json": {"uri": "gs://bucket/a.json"}}),
    ]


def test_client_list_run_artifacts_tolerates_an_api_without_the_route(monkeypatch) -> None:
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    def absent(*args: Any, **kwargs: Any) -> Any:
        raise _http_error("Not Found", 404)

    monkeypatch.setattr(client, "request", absent)
    assert client.list_run_artifacts("run-1") == []


def test_client_list_runs_filters_on_target_id_not_workflow_id(monkeypatch) -> None:
    """`/runs` has no `workflow_id` parameter, and FastAPI drops unknown ones in silence.

    Sending it looked like a filter and was not: every run in the workspace came back,
    so one project's runs appeared under another project's workflow.
    """
    seen: list[dict[str, Any]] = []

    def fake_request(method: str, path: str, params: Any = None) -> Any:
        seen.append(params)
        return []

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "request", fake_request)

    client.list_runs(workflow_id="wf-1", target_type="workflow", limit=5)
    assert seen[-1] == {"target_id": "wf-1", "target_type": "workflow", "limit": 5}
    assert "workflow_id" not in seen[-1]

    # The kind is inferred when only the id is given.
    client.list_runs(function_id="fn-1", limit=5)
    assert seen[-1] == {"target_id": "fn-1", "target_type": "function", "limit": 5}
    client.list_runs(model_id="m-1", limit=5)
    assert seen[-1] == {"target_id": "m-1", "target_type": "model", "limit": 5}

    # And target_id still works on its own, for callers that already speak the API's shape.
    client.list_runs(target_id="wf-1", target_type="workflow", limit=5)
    assert seen[-1] == {"target_id": "wf-1", "target_type": "workflow", "limit": 5}

    # An unfiltered listing stays unfiltered.
    client.list_runs(limit=5)
    assert seen[-1] == {"limit": 5}


def test_client_list_runs_rejects_contradictory_targets(monkeypatch) -> None:
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "request", lambda *a, **k: [])

    with pytest.raises(rb.RebaseWorkflowError, match="one target at a time"):
        client.list_runs(workflow_id="wf-1", function_id="fn-1")
    with pytest.raises(rb.RebaseWorkflowError, match="target_id=other"):
        client.list_runs(workflow_id="wf-1", target_id="other")
    with pytest.raises(rb.RebaseWorkflowError, match="target_type='function'"):
        client.list_runs(workflow_id="wf-1", target_type="function")


def test_current_run_reads_the_runner_s_environment(monkeypatch) -> None:
    """Read per call, not cached: the steps of one run share a process."""
    monkeypatch.delenv("REBASE_RUN_ID", raising=False)
    monkeypatch.delenv("REBASE_STEP_RUN_ID", raising=False)
    assert rb.current_run() is None

    monkeypatch.setenv("REBASE_RUN_ID", "run-1")
    assert rb.current_run() == rb.RunContext(run_id="run-1", step_run_id=None)

    monkeypatch.setenv("REBASE_STEP_RUN_ID", "step-1")
    assert rb.current_run() == rb.RunContext(run_id="run-1", step_run_id="step-1")

    monkeypatch.setenv("REBASE_STEP_RUN_ID", "step-2")
    assert rb.current_run().step_run_id == "step-2"


def test_function_map_buffers_ordered_results(monkeypatch) -> None:
    def fake_run_function_map(*args: Any, **kwargs: Any):
        yield {"type": "item", "index": 1, "status": "succeeded", "result": {"value": 4}}
        yield {"type": "item", "index": 0, "status": "succeeded", "result": {"value": 1}}
        yield {"type": "summary", "status": "succeeded"}

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "run_function_map", fake_run_function_map)
    function = rb.Function(
        project="default",
        name="square",
        client=client,
        function_id="function-id",
        data={"name": "square"},
    )

    assert list(function.map([{"x": 1}, {"x": 2}])) == [{"value": 1}, {"value": 4}]


def test_function_map_infers_single_required_parameter(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_run_function_map(function_id: str, **kwargs: Any):
        observed["function_id"] = function_id
        observed.update(kwargs)
        yield {"type": "item", "index": 0, "status": "succeeded", "result": {"value": 1}}

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "run_function_map", fake_run_function_map)
    function = rb.Function(_map_square, project="default", client=client)
    function.id = "function-id"

    assert list(function.map([1])) == [{"value": 1}]
    assert observed["function_id"] == "function-id"
    assert observed["items"] == [1]
    assert observed["parameter"] == "x"


def test_function_map_raises_or_returns_item_errors(monkeypatch) -> None:
    def fake_run_function_map(*args: Any, **kwargs: Any):
        yield {"type": "item", "index": 0, "status": "failed", "error": "boom"}

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "run_function_map", fake_run_function_map)
    function = rb.Function(
        project="default",
        name="square",
        client=client,
        function_id="function-id",
        data={"name": "square"},
    )

    with pytest.raises(rb.RebaseWorkflowError, match="boom"):
        list(function.map([{"x": 1}]))

    result = list(function.map([{"x": 1}], return_exceptions=True))
    assert len(result) == 1
    assert isinstance(result[0], rb.RebaseWorkflowError)
    assert str(result[0]) == "boom"


def test_create_github_starter_workflow_posts_path(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["method"] = method
        observed["url"] = url
        observed["json"] = kwargs["json"]
        return FakeResponse(
            {
                "repo_owner": "rebase",
                "repo_name": "platform",
                "path": ".rebase/starter_workflow.py",
                "html_url": "https://github.com/rebase/platform/blob/main/.rebase/starter_workflow.py",
                "commit_sha": "abc123",
            }
        )

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    response = client.create_github_starter_workflow("connection-id")

    assert response["commit_sha"] == "abc123"
    assert observed == {
        "method": "POST",
        "url": "https://workflows.example.com/integrations/github/repo-connections/connection-id/starter-workflow",
        "json": {"path": ".rebase/starter_workflow.py"},
    }


def test_find_github_repository_installation_gets_repo_full_name(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["method"] = method
        observed["url"] = url
        observed["params"] = kwargs["params"]
        return FakeResponse(
            {
                "installation_id": 123,
                "repository": {
                    "id": 456,
                    "owner": "rebase",
                    "name": "platform",
                    "full_name": "rebase/platform",
                    "private": True,
                },
            }
        )

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    response = client.find_github_repository_installation("rebase/platform")

    assert response["installation_id"] == 123
    assert observed == {
        "method": "GET",
        "url": "https://workflows.example.com/integrations/github/repository-installation",
        "params": {"repo_full_name": "rebase/platform"},
    }


def test_list_github_repo_connections_gets_workspace_connections(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["method"] = method
        observed["url"] = url
        observed["params"] = kwargs["params"]
        return FakeResponse([{"repo_owner": "rebase", "repo_name": "platform", "scope": "workspace"}])

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    response = client.list_github_repo_connections()

    assert response == [{"repo_owner": "rebase", "repo_name": "platform", "scope": "workspace"}]
    assert observed == {
        "method": "GET",
        "url": "https://workflows.example.com/integrations/github/repo-connections",
        "params": {},
    }


def test_client_uses_fastapi_detail_for_http_errors(monkeypatch) -> None:
    patch_client_http(
        monkeypatch,
        lambda *args, **kwargs: FakeErrorResponse({"detail": "Rebase Workflows is invite-only."}),
    )
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    with pytest.raises(rb.RebaseWorkflowError) as exc_info:
        client.list_my_workspaces()

    assert str(exc_info.value) == "Rebase Workflows is invite-only."


def test_client_formats_credit_exhaustion_errors(monkeypatch) -> None:
    class CreditErrorResponse(FakeErrorResponse):
        text = '{"detail":{"code":"workspace_credits_exhausted"}}'

        def json(self) -> dict[str, Any]:
            return {
                "detail": {
                    "code": "workspace_credits_exhausted",
                    "message": "workspace monthly compute credits are exhausted",
                    "remaining_cents": 25,
                    "required_reservation_cents": 50,
                }
            }

    patch_client_http(monkeypatch, lambda *args, **kwargs: CreditErrorResponse({}))
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    with pytest.raises(rb.RebaseWorkflowError) as exc_info:
        client.list_my_workspaces()

    assert str(exc_info.value) == (
        "workspace monthly compute credits are exhausted. Remaining: 0.25 EUR; required reservation: 0.50 EUR."
    )


def test_get_workspace_usage_calls_active_workspace_endpoint(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["method"] = method
        observed["url"] = url
        return FakeResponse({"workspace_id": "beta-team", "monthly_credit_cents": 2000})

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    assert client.get_workspace_usage()["monthly_credit_cents"] == 2000
    assert observed == {
        "method": "GET",
        "url": "https://workflows.example.com/workspace/usage",
    }


def test_create_platform_invite_posts_email(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["method"] = method
        observed["url"] = url
        observed["json"] = kwargs["json"]
        return FakeResponse({"id": "invite-id", "email": "new@example.com", "status": "pending"})

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    response = client.create_platform_invite("new@example.com", workspace_creation_limit=3)

    assert response["email"] == "new@example.com"
    assert observed == {
        "method": "POST",
        "url": "https://workflows.example.com/platform/invites",
        "json": {"email": "new@example.com", "expires_at": None, "workspace_creation_limit": 3},
    }


def test_create_platform_invite_posts_null_workspace_limit(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["method"] = method
        observed["url"] = url
        observed["json"] = kwargs["json"]
        return FakeResponse({"id": "invite-id", "email": "new@example.com", "status": "pending"})

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    response = client.create_platform_invite("new@example.com", workspace_creation_limit=None)

    assert response["email"] == "new@example.com"
    assert observed == {
        "method": "POST",
        "url": "https://workflows.example.com/platform/invites",
        "json": {"email": "new@example.com", "expires_at": None, "workspace_creation_limit": None},
    }


def test_create_workspace_invite_posts_identity_and_role(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["method"] = method
        observed["url"] = url
        observed["json"] = kwargs["json"]
        return FakeResponse(
            {
                "id": "invite-id",
                "email": "new@example.com",
                "github_username": None,
                "role": "Developer",
                "status": "pending",
            }
        )

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    response = client.create_workspace_invite(email="new@example.com", role="Developer")

    assert response["email"] == "new@example.com"
    assert observed == {
        "method": "POST",
        "url": "https://workflows.example.com/workspace/invites",
        "json": {
            "email": "new@example.com",
            "github_username": None,
            "role": "Developer",
            "expires_at": None,
        },
    }


def test_list_workspace_members_requests_members_endpoint(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["method"] = method
        observed["url"] = url
        return FakeResponse(
            [
                {
                    "email": "owner@example.com",
                    "github_username": "owner-gh",
                    "role": "Owner",
                    "enabled": True,
                }
            ]
        )

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    response = client.list_workspace_members()

    assert response[0]["role"] == "Owner"
    assert observed == {
        "method": "GET",
        "url": "https://workflows.example.com/workspace/members",
    }


def test_list_api_keys_requests_workspace_endpoint(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["method"] = method
        observed["url"] = url
        return FakeResponse([{"id": "key-id", "name": "agent"}])

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    response = client.list_api_keys()

    assert response == [{"id": "key-id", "name": "agent"}]
    assert observed == {
        "method": "GET",
        "url": "https://workflows.example.com/workspace/api-keys",
    }


def test_create_api_key_posts_payload(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["method"] = method
        observed["url"] = url
        observed["json"] = kwargs["json"]
        return FakeResponse({"id": "key-id", "name": "agent", "api_key": "rb_secret"})

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    response = client.create_api_key(
        "agent",
        project_id="project-id",
        permissions=["runs:read"],
        expires_at="2026-07-01T00:00:00Z",
    )

    assert response["api_key"] == "rb_secret"
    assert observed == {
        "method": "POST",
        "url": "https://workflows.example.com/workspace/api-keys",
        "json": {
            "name": "agent",
            "project_id": "project-id",
            "permissions": ["runs:read"],
            "expires_at": "2026-07-01T00:00:00Z",
        },
    }


def test_create_api_key_defaults_to_agent_permissions(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["json"] = kwargs["json"]
        return FakeResponse({"id": "key-id", "name": "agent", "api_key": "rb_secret"})

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    client.create_api_key("agent")

    assert observed["json"]["permissions"] == [
        "workspace:read",
        "projects:read",
        "endpoints:read",
        "endpoints:execute",
        "functions:read",
        "workflows:read",
        "models:read",
        "runs:read",
    ]


def test_revoke_api_key_deletes_workspace_key(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["method"] = method
        observed["url"] = url
        return FakeResponse({"id": "key-id", "enabled": False})

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    response = client.revoke_api_key("key-id")

    assert response == {"id": "key-id", "enabled": False}
    assert observed == {
        "method": "DELETE",
        "url": "https://workflows.example.com/workspace/api-keys/key-id",
    }


def test_client_get_project_requests_project_endpoint(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["method"] = method
        observed["url"] = url
        observed["headers"] = kwargs["headers"]
        return FakeResponse({"id": "project-id", "name": "energy"})

    patch_client_http(monkeypatch, fake_request)

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    assert client.get_project("project-id") == {"id": "project-id", "name": "energy"}

    assert observed == {
        "method": "GET",
        "url": "https://workflows.example.com/projects/project-id",
        "headers": {"Authorization": "Bearer rbw_test", "X-Rebase-Environment": "dev"},
    }


def test_client_lists_run_events_from_events_endpoint(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["method"] = method
        observed["url"] = url
        observed["headers"] = kwargs["headers"]
        return FakeResponse([{"id": "event-id", "message": "Accepted run request."}])

    patch_client_http(monkeypatch, fake_request)

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    assert client.list_run_events("run-id") == [{"id": "event-id", "message": "Accepted run request."}]

    assert observed == {
        "method": "GET",
        "url": "https://workflows.example.com/runs/run-id/events",
        "headers": {"Authorization": "Bearer rbw_test", "X-Rebase-Environment": "dev"},
    }


def test_client_lists_runs_with_filters(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["method"] = method
        observed["url"] = url
        observed["headers"] = kwargs["headers"]
        observed["params"] = kwargs["params"]
        return FakeResponse([{"id": "run-id", "status": "succeeded"}])

    patch_client_http(monkeypatch, fake_request)

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    assert client.list_runs(project_id="project-id", target_type="workflow", limit=25) == [
        {"id": "run-id", "status": "succeeded"}
    ]

    assert observed == {
        "method": "GET",
        "url": "https://workflows.example.com/runs",
        "headers": {"Authorization": "Bearer rbw_test", "X-Rebase-Environment": "dev"},
        "params": {
            "project_id": "project-id",
            "target_type": "workflow",
            "limit": 25,
        },
    }


def test_client_lists_runs_serializes_time_and_source_filters(monkeypatch) -> None:
    from datetime import UTC, datetime

    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["params"] = kwargs["params"]
        return FakeResponse([])

    patch_client_http(monkeypatch, fake_request)

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    client.list_runs(
        workflow_id="workflow-id",
        since=datetime(2026, 7, 4, 9, tzinfo=UTC),
        until="2026-07-11T09:00:00+00:00",
        status="succeeded",
        trigger_source="schedule",
        limit=500,
    )

    assert observed["params"] == {
        "target_id": "workflow-id",
        "target_type": "workflow",
        "since": "2026-07-04T09:00:00+00:00",
        "until": "2026-07-11T09:00:00+00:00",
        "status": "succeeded",
        "trigger_source": "schedule",
        "limit": 500,
    }

    # None filters are omitted entirely
    client.list_runs()
    assert observed["params"] == {"limit": 100}


def _fake_replay_endpoint(monkeypatch, observed: dict[str, Any]) -> None:
    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["method"] = method
        observed["url"] = url
        observed["json"] = kwargs["json"]
        return FakeResponse(
            {"id": "replay-run-id", "status": "queued", "replay_of": "run-id", "trigger_source": "replay"}
        )

    patch_client_http(monkeypatch, fake_request)


def test_client_replay_run_defaults_to_original_version(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    _fake_replay_endpoint(monkeypatch, observed)

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    run = client.replay_run("run-id")

    assert observed["method"] == "POST"
    assert observed["url"] == "https://workflows.example.com/runs/run-id/replay"
    assert observed["json"] == {"parameters": {}}
    assert run.id == "replay-run-id"
    assert run.data["replay_of"] == "run-id"


def test_client_replay_run_latest_sets_use_current_version(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    _fake_replay_endpoint(monkeypatch, observed)

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    client.replay_run("run-id", version="latest")
    assert observed["json"] == {"parameters": {}, "use_current_version": True}

    client.replay_run("run-id", version="current")
    assert observed["json"] == {"parameters": {}, "use_current_version": True}


def test_client_replay_run_pins_explicit_version_and_parameters(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    _fake_replay_endpoint(monkeypatch, observed)

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    client.replay_run("run-id", version="version-uuid", parameters={"horizon": 48})

    assert observed["json"] == {"parameters": {"horizon": 48}, "target_version_id": "version-uuid"}


def test_run_replay_delegates_to_client(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    _fake_replay_endpoint(monkeypatch, observed)

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    run = rb.Run("run-id", client=client)
    replay = run.replay(version="latest", parameters={"a": 1})

    assert observed["url"] == "https://workflows.example.com/runs/run-id/replay"
    assert observed["json"] == {"parameters": {"a": 1}, "use_current_version": True}
    assert replay.id == "replay-run-id"


def test_trigger_context_parses_replay_fields() -> None:
    payload = {
        "reason": "api",
        "is_replay": True,
        "replay": {"of_run_id": "run-id", "knowledge_time": "2026-07-10T09:00:00+00:00", "code": "original"},
    }
    ctx = rb.TriggerContext.from_payload(payload)

    assert ctx.is_replay is True
    assert ctx.replay["of_run_id"] == "run-id"
    assert ctx.replay["code"] == "original"
    assert ctx.raw == payload


def test_trigger_context_replay_fields_default_when_absent() -> None:
    ctx = rb.TriggerContext.from_payload({"reason": "datasets_ready"})

    assert ctx.is_replay is False
    assert ctx.replay == {}


def test_client_uses_hosted_api_url_by_default(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(tmp_path / "missing-config.json"))

    client = rb.Client()

    assert client.api_key is None
    assert client.api_url == DEFAULT_API_URL
    assert client.api_url == DEFAULT_SERVER_URL
    assert rb.DEFAULT_SERVER_URL == DEFAULT_SERVER_URL


def test_client_reads_api_key_from_local_profile(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))
    write_profile(api_key="rbw_profile", profile="default", path=config_path)

    client = rb.Client()

    assert client.api_key == "rbw_profile"
    assert client.api_url == DEFAULT_API_URL


def test_client_reads_api_url_from_local_profile(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))
    write_profile(api_key="rbw_profile", api_url="http://127.0.0.1:8080", profile="default", path=config_path)

    client = rb.Client()

    assert client.api_key == "rbw_profile"
    assert client.api_url == "http://127.0.0.1:8080"


def test_client_sends_workspace_header_from_local_profile(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))
    write_profile(
        api_key="rbw_profile",
        api_url="http://127.0.0.1:8080",
        profile="default",
        workspace={"id": "workspace-id", "name": "ACME"},
        path=config_path,
    )
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["headers"] = kwargs["headers"]
        return FakeResponse([])

    patch_client_http(monkeypatch, fake_request)

    client = rb.Client()
    assert client.list_workflows() == []

    assert observed["headers"] == {
        "Authorization": "Bearer rbw_profile",
        "X-Rebase-Environment": "dev",
        "X-Rebase-Workspace": "workspace-id",
    }


def test_client_prefers_explicit_access_token_over_profile_api_key(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))
    write_profile(
        api_key="rbw_old",
        api_url="http://127.0.0.1:8080",
        profile="default",
        workspace={"id": "workspace-id", "name": "ACME"},
        path=config_path,
    )
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["headers"] = kwargs["headers"]
        return FakeResponse([])

    patch_client_http(monkeypatch, fake_request)

    client = rb.Client(access_token="supabase-token", profile="default")
    assert client.list_my_workspaces() == []

    assert observed["headers"] == {
        "Authorization": "Bearer supabase-token",
        "X-Rebase-Environment": "dev",
        "X-Rebase-Workspace": "workspace-id",
    }


def test_client_can_select_named_profile(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config_path))
    write_profile(api_key="rbw_default", profile="default", path=config_path)
    write_profile(api_key="rbw_prod", profile="prod", path=config_path)

    client = rb.Client(profile="prod")

    assert client.api_key == "rbw_prod"
    assert client.api_url == DEFAULT_API_URL


def test_list_functions_with_project_name_does_not_create_project(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["method"] = method
        observed["url"] = url
        return FakeResponse([])

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "find_project", lambda name: {"id": "project-id", "name": name})
    monkeypatch.setattr(client, "ensure_project", lambda name, **kwargs: (_ for _ in ()).throw(AssertionError()))

    assert client.list_functions(project="energy") == []
    assert observed["method"] == "GET"
    assert observed["url"] == "https://workflows.example.com/projects/project-id/functions"


def test_workflow_deploy_updates_existing_workflow_version(monkeypatch) -> None:
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(
        client,
        "list_workflows",
        lambda: [
            {
                "id": "workflow-id",
                "name": "forecast",
                "flow_ref": "app.flows.energy:forecast",
            }
        ],
    )
    observed: dict[str, Any] = {}

    def fake_update_workflow(workflow_id: str, **kwargs: Any) -> dict[str, Any]:
        observed["workflow_id"] = workflow_id
        observed.update(kwargs)
        return {"id": workflow_id, "name": "forecast", "current_version_id": "version-id"}

    monkeypatch.setattr(client, "update_workflow", fake_update_workflow)

    def forecast(site_id: str) -> dict:
        return {"site_id": site_id}

    workflow = rb.Workflow(forecast, client=client).deploy()

    assert workflow.id == "workflow-id"
    assert observed["workflow_id"] == "workflow-id"
    assert observed["entrypoint"] == "forecast"
    assert observed["source_code"].startswith("def forecast")
    assert observed["step_graph"] is None
    assert rb.DEFAULT_MODE == "interactive"
    assert rb.DEFAULT_ISOLATION == "shared"
    assert observed["mode"] == "interactive"
    assert observed["isolation"] == "shared"


def test_update_workflow_omits_step_graph_unless_explicit(monkeypatch) -> None:
    observed_payloads: list[dict[str, Any]] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed_payloads.append(kwargs["json"])
        return FakeResponse({"id": "workflow-id", "name": "forecast"})

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    client.update_workflow("workflow-id", description="Updated")
    client.update_workflow("workflow-id", step_graph=None)
    client.update_workflow("workflow-id", schedule=None)

    assert observed_payloads[0] == {"description": "Updated"}
    assert observed_payloads[1] == {"step_graph": None}
    assert observed_payloads[2] == {"schedule": None}


def test_workflow_deploy_registers_function_source(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "list_workflows", lambda: [])

    def fake_register_workflow(**kwargs: Any) -> dict[str, Any]:
        observed.update(kwargs)
        return {"id": "workflow-id"}

    monkeypatch.setattr(client, "register_workflow", fake_register_workflow)

    def add(left: float = 0, right: float = 0) -> dict:
        return {"sum": left + right}

    workflow = rb.Workflow(add, name="add-numbers", client=client).deploy()

    assert workflow.id == "workflow-id"
    assert observed["name"] == "add-numbers"
    assert observed["flow_ref"] is None
    assert observed["entrypoint"] == "add"
    assert "def add(left: float = 0, right: float = 0) -> dict:" in observed["source_code"]
    assert observed["default_parameters"] == {"left": 0, "right": 0}
    assert observed["mode"] == "interactive"
    assert observed["isolation"] == "shared"
    assert workflow.run_type == "quick"


def test_workflow_can_use_long_run_type(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "list_workflows", lambda: [])
    monkeypatch.setattr(client, "register_workflow", lambda **kwargs: observed.update(kwargs) or {"id": "workflow-id"})

    def forecast(site_id: str = "site-001") -> dict:
        return {"site_id": site_id}

    workflow = rb.Workflow(
        forecast,
        name="cloud-run-forecast",
        run_type="long",
        client=client,
    ).deploy()

    assert workflow.run_type == "long"
    assert observed["mode"] == "job"
    assert observed["isolation"] == "shared"


def test_workflow_accepts_quick_and_long_run_types(monkeypatch) -> None:
    observed: list[dict[str, Any]] = []
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "list_workflows", lambda: [])
    monkeypatch.setattr(client, "register_workflow", lambda **kwargs: observed.append(kwargs) or {"id": "workflow-id"})

    def quick_forecast() -> dict:
        return {"status": "ok"}

    def long_forecast() -> dict:
        return {"status": "ok"}

    quick = rb.Workflow(
        quick_forecast,
        name="quick-forecast",
        run_type="quick",
        client=client,
    ).deploy()
    long = rb.Workflow(
        long_forecast,
        name="long-forecast",
        run_type="long",
        client=client,
    ).deploy()

    assert quick.run_type == "quick"
    assert long.run_type == "long"
    assert [(payload["mode"], payload["isolation"]) for payload in observed] == [
        ("interactive", "shared"),
        ("job", "shared"),
    ]


def test_workflow_rejects_unknown_run_type() -> None:
    def forecast() -> dict:
        return {"status": "ok"}

    with pytest.raises(ValueError, match="run_type must be"):
        rb.Workflow(forecast, project="energy", run_type="unknown")


def test_workflow_rejects_quick_shared_run_type() -> None:
    def forecast() -> dict:
        return {"status": "ok"}

    with pytest.raises(ValueError, match="run_type must be"):
        rb.Workflow(forecast, project="energy", run_type="quick_shared")


def test_workflow_rejects_legacy_backend_parameter() -> None:
    def forecast() -> dict:
        return {"status": "ok"}

    with pytest.raises(ValueError, match="'backend' parameter was removed"):
        rb.Workflow(forecast, project="energy", backend="interactive")


def test_workflow_explicit_defaults_override_function_defaults(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "list_workflows", lambda: [])
    monkeypatch.setattr(client, "register_workflow", lambda **kwargs: observed.update(kwargs) or {"id": "workflow-id"})

    def forecast(site_id: str, horizon_hours: int = 24) -> dict:
        return {"site_id": site_id, "horizon_hours": horizon_hours}

    rb.Workflow(forecast, default_parameters={"horizon_hours": 48}, client=client).deploy()

    assert observed["default_parameters"] == {"horizon_hours": 48}


def test_project_deploy_registers_function_source(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "ensure_project", lambda name, **kwargs: {"id": "project-id", "name": name})
    monkeypatch.setattr(client, "find_function", lambda name, *, project: None)

    def fake_register_function(**kwargs: Any) -> dict[str, Any]:
        observed.update(kwargs)
        return {"id": "function-id", "name": kwargs["name"]}

    monkeypatch.setattr(client, "register_function", fake_register_function)

    project = rb.Project("energy-forecasting", client=client)

    @project.function(name="normalize-weather")
    def normalize_weather(site_id: str, horizon_hours: int = 24) -> dict:
        return {"site_id": site_id, "horizon_hours": horizon_hours}

    project.deploy()

    assert normalize_weather.id == "function-id"
    assert observed["project"] == "energy-forecasting"
    assert observed["name"] == "normalize-weather"
    assert observed["entrypoint"] == "normalize_weather"
    assert observed["source_code"].startswith("def normalize_weather")
    assert "@project.function" not in observed["source_code"]
    assert "def normalize_weather(site_id: str, horizon_hours: int = 24) -> dict:" in observed["source_code"]
    assert observed["default_parameters"] == {"horizon_hours": 24}
    assert observed["mode"] == "interactive"
    assert observed["isolation"] == "shared"
    assert observed["source_mode"] == "rebase_hosted"
    assert rb.DEFAULT_ISOLATION == "shared"
    assert normalize_weather.run_type == "quick_shared"


def test_project_deploy_registers_asgi_app_source(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "ensure_project", lambda name, **kwargs: {"id": "project-id", "name": name})
    monkeypatch.setattr(client, "find_asgi_app", lambda name, *, project: None)

    def fake_register_asgi_app(**kwargs: Any) -> dict[str, Any]:
        observed.update(kwargs)
        return {"id": "asgi-app-id", "name": kwargs["name"], "url_path": "/e/default/grid/api"}

    monkeypatch.setattr(client, "register_asgi_app", fake_register_asgi_app)

    project = rb.Project("grid", client=client)

    @project.asgi_app(
        name="grid-api",
        base_path="api",
        auth="public",
        dependencies=["fastapi==0.115.0"],
        env={"GRID_ENV": "prod"},
        secrets={"GRID_TOKEN": "grid-token:latest"},
        max_instances=2,
        concurrency=80,
        timeout_seconds=60,
        cpu="1",
        memory="1Gi",
    )
    def grid_api() -> object:
        return object()

    project.deploy()

    assert grid_api.id == "asgi-app-id"
    assert observed["project"] == "grid"
    assert observed["name"] == "grid-api"
    assert observed["entrypoint"] == "grid_api"
    assert observed["base_path"] == "/api"
    assert observed["auth"] == "public"
    assert observed["source_code"].startswith("def grid_api")
    assert "@project.asgi_app" not in observed["source_code"]
    assert observed["image_spec"]["uv_pip_packages"] == ["fastapi==0.115.0"]
    assert observed["env"] == {"GRID_ENV": "prod"}
    assert observed["secrets"] == {"GRID_TOKEN": "grid-token:latest"}
    assert observed["cloud_run_max_instances"] == 2
    assert observed["cloud_run_concurrency"] == 80
    assert observed["cloud_run_timeout_seconds"] == 60
    assert observed["cloud_run_cpu"] == "1"
    assert observed["cloud_run_memory"] == "1Gi"
    assert observed["source_mode"] == "rebase_hosted"


def test_asgi_app_create_and_update_use_deploy_timeout(monkeypatch) -> None:
    observed: list[dict[str, Any]] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed.append({"method": method, "url": url, "timeout": kwargs["timeout"], "json": kwargs["json"]})
        return FakeResponse({"id": "asgi-app-id", "name": "grid-api"})

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "ensure_project", lambda name: {"id": "project-id", "name": name})

    client.register_asgi_app(project="grid", name="grid-api", source_code="def app(): pass", entrypoint="app")
    client.update_asgi_app("asgi-app-id", source_code="def app(): pass")

    assert observed == [
        {
            "method": "POST",
            "url": "https://workflows.example.com/projects/project-id/asgi-apps",
            "timeout": 300,
            "json": {
                "name": "grid-api",
                "description": None,
                "source_code": "def app(): pass",
                "entrypoint": "app",
                "base_path": "/",
                "auth": "api_key",
                "image_spec": None,
                "env": {},
                "secrets": {},
                "volumes": [],
                "cloud_run_min_instances": None,
                "cloud_run_max_instances": None,
                "cloud_run_concurrency": None,
                "cloud_run_timeout_seconds": None,
                "cloud_run_cpu": None,
                "cloud_run_memory": None,
                "enabled": True,
                "source_mode": None,
                "repo_owner": None,
                "repo_name": None,
                "repo_path": None,
                "source_path": None,
                "git_commit_sha": None,
                "git_branch": None,
                "git_tag": None,
                "git_dirty": False,
            },
        },
        {
            "method": "PATCH",
            "url": "https://workflows.example.com/asgi-apps/asgi-app-id",
            "timeout": 300,
            "json": {"source_code": "def app(): pass"},
        },
    ]


def test_asgi_app_deploy_updates_existing_app(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "find_asgi_app", lambda name, *, project: {"id": "existing-id", "name": name})
    monkeypatch.setattr(client, "register_asgi_app", lambda **kwargs: pytest.fail("existing ASGI app should update"))
    monkeypatch.setattr(
        client,
        "update_asgi_app",
        lambda asgi_app_id, **kwargs: (
            observed.update({"asgi_app_id": asgi_app_id, **kwargs}) or {"id": asgi_app_id, "name": "grid-api"}
        ),
    )

    @rb.asgi_app(project="grid", name="grid-api", base_path="/api")
    def grid_api() -> object:
        return object()

    grid_api.client = client
    grid_api.deploy()

    assert grid_api.id == "existing-id"
    assert observed["asgi_app_id"] == "existing-id"
    assert observed["entrypoint"] == "grid_api"
    assert observed["base_path"] == "/api"


def test_function_endpoint_deploy_sends_endpoint_config(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "ensure_project", lambda name, **kwargs: {"id": "project-id", "name": name})
    monkeypatch.setattr(client, "find_function", lambda name, *, project: None)
    monkeypatch.setattr(client, "register_function", lambda **kwargs: observed.update(kwargs) or {"id": "function-id"})

    @rb.endpoint(method="GET", path="forecast", auth="public", mode="sync", timeout=12)
    def forecast(site_id: str) -> dict:
        return {"site_id": site_id}

    rb.Function(forecast, project="energy", client=client).deploy()

    assert observed["endpoint"].to_payload() == {
        "name": None,
        "method": "GET",
        "path": "/forecast",
        "auth": "public",
        "mode": "sync",
        "timeout_seconds": 12,
        "docs": False,
        "enabled": True,
    }


def test_model_endpoint_constructor_sends_endpoint_config(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "find_model", lambda name, *, project: None)
    monkeypatch.setattr(client, "ensure_project", lambda name, **kwargs: {"id": "project-id", "name": name})
    monkeypatch.setattr(client, "register_model", lambda **kwargs: observed.update(kwargs) or {"id": "model-id"})

    class PriceForecastPredictor(rb.Predictor):
        name = "price-forecast"

        def predict(self, zone: str = "SE3") -> dict:
            return {"zone": zone}

    PriceForecastPredictor(
        project="models",
        endpoint=rb.endpoint(method="POST", path="/predict"),
        client=client,
    ).deploy()

    assert observed["endpoint"].to_payload()["method"] == "POST"
    assert observed["endpoint"].to_payload()["path"] == "/predict"
    assert observed["endpoint"].to_payload()["auth"] == "api_key"


def test_function_deploy_github_source_uses_project_repo_metadata(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "find_project", lambda name: {"id": "project-id", "source_mode": "project_repo"})
    monkeypatch.setattr(client, "find_function", lambda name, *, project: None)
    monkeypatch.setattr(client, "register_function", lambda **kwargs: observed.update(kwargs) or {"id": "function-id"})

    function = rb.Function(project="energy-forecasting", name="forecast", deploy_source="github", client=client)
    function.source_code = "def forecast() -> dict:\n    return {}\n"
    function.entrypoint = "forecast"
    function.source_metadata = {
        "repo_owner": "rebase-energy",
        "repo_name": "platform",
        "source_path": "workflows/forecast.py",
        "git_commit_sha": "abc123",
        "git_branch": "main",
        "git_dirty": False,
    }

    function.deploy()

    assert observed["source_mode"] == "project_repo"
    assert observed["repo_owner"] == "rebase-energy"
    assert observed["repo_name"] == "platform"
    assert observed["source_path"] == "workflows/forecast.py"
    assert observed["git_commit_sha"] == "abc123"
    assert observed["git_dirty"] is False


def test_function_deploy_github_source_rejects_dirty_source_file(monkeypatch) -> None:
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "find_project", lambda name: {"id": "project-id", "source_mode": "workspace_repo"})

    function = rb.Function(project="energy-forecasting", name="forecast", deploy_source="github", client=client)
    function.source_code = "def forecast() -> dict:\n    return {}\n"
    function.entrypoint = "forecast"
    function.source_metadata = {
        "repo_owner": "rebase-energy",
        "repo_name": "platform",
        "source_path": "workflows/forecast.py",
        "git_commit_sha": "abc123",
        "git_dirty": True,
    }

    with pytest.raises(rb.RebaseWorkflowError, match="source file to be committed"):
        function.deploy()


def test_project_deploy_preserves_target_deploy_source_override(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "ensure_project", lambda name, **kwargs: {"id": "project-id", "name": name})
    monkeypatch.setattr(client, "find_project", lambda name: {"id": "project-id", "source_mode": "project_repo"})
    monkeypatch.setattr(client, "find_function", lambda name, *, project: None)
    monkeypatch.setattr(client, "register_function", lambda **kwargs: observed.update(kwargs) or {"id": "function-id"})

    project = rb.Project("energy-forecasting", deploy_source="rebase", client=client)

    @project.function(name="forecast", deploy_source="github")
    def forecast() -> dict:
        return {}

    forecast.source_metadata = {
        "repo_owner": "rebase-energy",
        "repo_name": "platform",
        "source_path": "workflows/forecast.py",
        "git_commit_sha": "abc123",
        "git_dirty": False,
    }

    project.deploy()

    assert observed["source_mode"] == "project_repo"
    assert observed["git_commit_sha"] == "abc123"


def test_project_function_can_use_quick_run_type(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "ensure_project", lambda name, **kwargs: {"id": "project-id", "name": name})
    monkeypatch.setattr(client, "find_function", lambda name, *, project: None)
    monkeypatch.setattr(client, "register_function", lambda **kwargs: observed.update(kwargs) or {"id": "function-id"})

    project = rb.Project("energy-forecasting", client=client)

    @project.function(name="quick-function", run_type="quick")
    def quick_function() -> dict:
        return {"status": "ok"}

    project.deploy()

    assert quick_function.run_type == "quick"
    assert observed["mode"] == "interactive"
    assert observed["isolation"] == "dedicated"


def test_project_function_can_use_quick_shared_run_type(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "ensure_project", lambda name, **kwargs: {"id": "project-id", "name": name})
    monkeypatch.setattr(client, "find_function", lambda name, *, project: None)
    monkeypatch.setattr(client, "register_function", lambda **kwargs: observed.update(kwargs) or {"id": "function-id"})

    project = rb.Project("energy-forecasting", client=client)

    @project.function(name="quick-shared-function", run_type="quick_shared")
    def quick_shared_function() -> dict:
        return {"status": "ok"}

    project.deploy()

    assert quick_shared_function.run_type == "quick_shared"
    assert observed["mode"] == "interactive"
    assert observed["isolation"] == "shared"


def test_project_function_can_use_long_run_type(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "ensure_project", lambda name, **kwargs: {"id": "project-id", "name": name})
    monkeypatch.setattr(client, "find_function", lambda name, *, project: None)
    monkeypatch.setattr(client, "register_function", lambda **kwargs: observed.update(kwargs) or {"id": "function-id"})

    project = rb.Project("energy-forecasting", client=client)

    @project.function(name="long-function", run_type="long")
    def long_function() -> dict:
        return {"status": "ok"}

    project.deploy()

    assert long_function.run_type == "long"
    assert observed["mode"] == "job"
    assert observed["isolation"] == "shared"


def test_project_function_defaults_to_shared_and_accepts_dedicated_isolation() -> None:
    project = rb.Project("energy-forecasting")

    @project.function(name="shared-function")
    def shared_function() -> dict:
        return {"status": "ok"}

    @project.function(name="dedicated-function", isolation="dedicated")
    def dedicated_function() -> dict:
        return {"status": "ok"}

    assert (shared_function.mode, shared_function.isolation) == ("interactive", "shared")
    assert (dedicated_function.mode, dedicated_function.isolation) == ("interactive", "dedicated")


def test_function_rejects_mixed_new_and_deprecated_execution_settings() -> None:
    def execute() -> dict:
        return {"status": "ok"}

    with pytest.raises(ValueError, match="either mode/isolation"):
        rb.Function(execute, project="energy", isolation="shared", run_type="quick")


def test_project_function_rejects_legacy_backend_parameter() -> None:
    project = rb.Project("energy-forecasting")

    with pytest.raises(ValueError, match="'backend' parameter was removed"):

        @project.function(name="legacy-function", backend="cloud_run")
        def legacy_function() -> dict:
            return {"status": "ok"}


def test_project_function_sends_cloud_run_isolation_settings(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "ensure_project", lambda name, **kwargs: {"id": "project-id", "name": name})
    monkeypatch.setattr(client, "find_function", lambda name, *, project: None)
    monkeypatch.setattr(client, "register_function", lambda **kwargs: observed.update(kwargs) or {"id": "function-id"})

    project = rb.Project("energy-forecasting", client=client)

    @project.function(name="cloud-run-function", run_type="quick", min_instances=1, concurrency=1)
    def cloud_run_function() -> dict:
        return {"status": "ok"}

    project.deploy()

    assert observed["cloud_run_min_instances"] == 1
    assert observed["cloud_run_concurrency"] == 1


def test_project_function_rejects_unknown_run_type() -> None:
    project = rb.Project("energy-forecasting")

    with pytest.raises(ValueError, match="run_type must be"):

        @project.function(run_type="unknown")
        def bad_run_type() -> dict:
            return {"status": "ok"}


def test_project_function_serializes_dependencies(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "ensure_project", lambda name, **kwargs: {"id": "project-id", "name": name})
    monkeypatch.setattr(client, "find_function", lambda name, *, project: None)
    monkeypatch.setattr(client, "register_function", lambda **kwargs: observed.update(kwargs) or {"id": "function-id"})

    project = rb.Project("energy-forecasting", client=client)

    @project.function(name="numpy-function", dependencies=["numpy==2.3.0"])
    def numpy_function() -> dict:
        return {"status": "ok"}

    project.deploy()

    assert observed["image_spec"] == {
        "kind": "python",
        "python_version": "3.13",
        "uv_pip_packages": ["numpy==2.3.0"],
        "uv_version": None,
    }


def test_function_deploy_sends_default_image_spec_when_dependencies_removed(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(
        client,
        "find_function",
        lambda name, *, project: {"id": "function-id", "name": name, "project": project},
    )

    def fake_update_function(function_id: str, **kwargs: Any) -> dict[str, Any]:
        observed["function_id"] = function_id
        observed.update(kwargs)
        return {"id": function_id, "name": "add"}

    monkeypatch.setattr(client, "update_function", fake_update_function)

    def add(a: int = 0, b: int = 0) -> dict[str, int]:
        return {"sum": a + b}

    rb.Function(add, project="math", run_type="quick", client=client).deploy()

    assert observed["image_spec"] == {
        "kind": "python",
        "python_version": "3.13",
        "uv_pip_packages": [],
        "uv_version": None,
    }


def test_project_function_serializes_image_builder(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "ensure_project", lambda name, **kwargs: {"id": "project-id", "name": name})
    monkeypatch.setattr(client, "find_function", lambda name, *, project: None)
    monkeypatch.setattr(client, "register_function", lambda **kwargs: observed.update(kwargs) or {"id": "function-id"})

    image = rb.Image.python("3.13").uv_pip_install("pandas==2.3.0", uv_version="0.9.0")
    project = rb.Project("energy-forecasting", client=client)

    @project.function(name="pandas-function", image=image)
    def pandas_function() -> dict:
        return {"status": "ok"}

    project.deploy()

    assert observed["image_spec"] == {
        "kind": "python",
        "python_version": "3.13",
        "uv_pip_packages": ["pandas==2.3.0"],
        "uv_version": "0.9.0",
    }


def test_built_function_deploy_sends_pinned_artifact_contract(monkeypatch, tmp_path) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    image = rb.Image.python("3.13").add_local_dir(tmp_path, "/workspace/source")
    build = {
        "image_build_id": "11111111-1111-1111-1111-111111111111",
        "image_digest": "sha256:" + "a" * 64,
        "image_recipe": image.to_dict(),
        "source_bundle_id": "22222222-2222-2222-2222-222222222222",
        "source_bundle_digest": "sha256:" + "b" * 64,
        "entrypoint_module": "test_client",
        "entrypoint_qualname": "run_built",
    }
    monkeypatch.setattr(client, "find_function", lambda name, *, project: None)
    monkeypatch.setattr(client, "prepare_built_image", lambda fn, declared_image: build)
    monkeypatch.setattr(client, "register_function", lambda **kwargs: observed.update(kwargs) or {"id": "fn"})

    def run_built() -> dict[str, bool]:
        return {"ok": True}

    rb.Function(run_built, project="energy", image=image, mode="job", client=client).deploy()

    assert observed["build"] == build
    assert observed["mode"] == "job"
    assert observed["image_spec"] == image.legacy_spec()


def test_built_function_rejects_shared_interactive_runner(tmp_path) -> None:
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    image = rb.Image.python("3.13").add_local_dir(tmp_path, "/workspace/source")

    def shared_built() -> None:
        return None

    function = rb.Function(shared_built, project="energy", image=image, client=client)
    with pytest.raises(rb.RebaseWorkflowError, match="shared runner"):
        function.deploy()


def test_project_deploy_registers_step_workflow_graph(monkeypatch) -> None:
    observed_functions: list[dict[str, Any]] = []
    observed_workflow: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "ensure_project", lambda name, **kwargs: {"id": "project-id", "name": name})
    monkeypatch.setattr(client, "find_function", lambda name, *, project: None)
    monkeypatch.setattr(client, "find_workflow", lambda name, *, project=None: None)

    def fake_register_function(**kwargs: Any) -> dict[str, Any]:
        observed_functions.append(kwargs)
        return {
            "id": f"{kwargs['name']}-id",
            "name": kwargs["name"],
            "current_version_id": f"{kwargs['name']}-version-id",
        }

    def fake_register_workflow(**kwargs: Any) -> dict[str, Any]:
        observed_workflow.update(kwargs)
        return {"id": "workflow-id", "name": kwargs["name"], "current_version_id": "workflow-version-id"}

    monkeypatch.setattr(client, "register_function", fake_register_function)
    monkeypatch.setattr(client, "register_workflow", fake_register_workflow)

    project = rb.Project("energy-forecasting", client=client)

    @project.step(name="load-weather", retries=2, timeout_seconds=30)
    def load_weather(site_id: str) -> dict:
        return {"site_id": site_id}

    @project.step(name="build-forecast")
    def build_forecast(weather: dict, horizon_hours: int = 24) -> dict:
        return {"weather": weather, "horizon_hours": horizon_hours}

    @project.workflow(name="site-forecast")
    def site_forecast(site_id: str, horizon_hours: int = 24) -> dict:
        weather = load_weather(site_id)
        forecast = build_forecast(weather, horizon_hours=horizon_hours)
        return {"forecast": forecast}

    project.deploy()

    assert [item["name"] for item in observed_functions] == ["load-weather", "build-forecast"]
    assert all(item["mode"] == "interactive" and item["isolation"] == "shared" for item in observed_functions)
    assert observed_workflow["mode"] == "interactive"
    assert observed_workflow["isolation"] == "shared"
    graph = observed_workflow["step_graph"]
    assert graph["schema_version"] == 1
    assert graph["engine"] == "prefect"
    assert graph["return_binding"] == {
        "type": "dict",
        "items": {"forecast": {"type": "node_output", "node_key": "build_forecast"}},
    }

    load_node, build_node = graph["nodes"]
    assert load_node["node_key"] == "load_weather"
    assert load_node["name"] == "load-weather"
    assert load_node["function_id"] == "load-weather-id"
    assert load_node["function_version_id"] == "load-weather-version-id"
    assert load_node["input_bindings"] == {"site_id": {"type": "parameter", "name": "site_id"}}
    assert load_node["retry_policy"] == {"retries": 2}
    assert load_node["timeout_seconds"] == 30

    assert build_node["node_key"] == "build_forecast"
    assert build_node["function_version_id"] == "build-forecast-version-id"
    assert build_node["upstream_node_keys"] == ["load_weather"]
    assert build_node["input_bindings"] == {
        "weather": {"type": "node_output", "node_key": "load_weather"},
        "horizon_hours": {"type": "parameter", "name": "horizon_hours"},
    }


def test_cron_window_resolves_bounds_in_the_cron_timezone() -> None:
    from datetime import date, datetime

    schedule = rb.Cron("*/5 * * * *", timezone="Europe/Stockholm", start="2099-09-16", end="2099-10-14").to_dict()
    # A bare date is the start of that day in the cron's timezone (CEST here).
    assert schedule["start"] == "2099-09-16T00:00:00+02:00"
    assert schedule["end"] == "2099-10-14T00:00:00+02:00"

    # date and datetime objects work too; an aware datetime is kept as given.
    assert rb.Cron("0 * * * *", end=date(2099, 1, 1)).to_dict()["end"] == "2099-01-01T00:00:00+00:00"
    aware = datetime(2099, 1, 1, 8, 0, tzinfo=UTC)
    assert rb.Cron("0 * * * *", timezone="Europe/Stockholm", end=aware).to_dict()["end"] == "2099-01-01T08:00:00+00:00"
    assert rb.Cron("0 * * * *", end="2099-01-01T08:00:00Z").to_dict()["end"] == "2099-01-01T08:00:00+00:00"

    # Without a window the payload is unchanged, so older servers see what they always saw.
    assert "start" not in rb.Cron("0 * * * *").to_dict()
    assert "end" not in rb.Cron("0 * * * *").to_dict()


def test_cron_window_rejects_what_can_never_fire() -> None:
    with pytest.raises(ValueError, match="in the past"):
        rb.Cron("0 * * * *", end="2020-01-01")
    with pytest.raises(ValueError, match="start .* must be before end"):
        rb.Cron("0 * * * *", start="2099-02-01", end="2099-01-01")
    with pytest.raises(ValueError, match="ISO 8601"):
        rb.Cron("0 * * * *", end="someday")
    with pytest.raises(TypeError):
        rb.Cron("0 * * * *", end=5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown timezone"):
        rb.Cron("0 * * * *", timezone="Nope/Nope", start="2099-01-01")
    # A past start is fine: it means "already started", and a redeploy must not break.
    assert rb.Cron("0 * * * *", start="2020-01-01").to_dict()["start"] == "2020-01-01T00:00:00+00:00"


def test_project_workflow_deploy_sends_cron_schedule(monkeypatch) -> None:
    observed_workflow: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "ensure_project", lambda name, **kwargs: {"id": "project-id", "name": name})
    monkeypatch.setattr(client, "find_workflow", lambda name, *, project=None: None)
    monkeypatch.setattr(client, "register_workflow", lambda **kwargs: observed_workflow.update(kwargs) or {"id": "id"})

    project = rb.Project("energy-forecasting", client=client)

    @project.workflow(
        name="scheduled-forecast",
        schedule=rb.Cron("0 6 * * *", timezone="Europe/Stockholm"),
    )
    def scheduled_forecast(site_id: str = "site-001", zone: str = "SE3") -> dict:
        return {"site_id": site_id, "zone": zone}

    project.deploy()

    assert scheduled_forecast.schedule == {
        "type": "cron",
        "cron": "0 6 * * *",
        "timezone": "Europe/Stockholm",
        "day_or": True,
        "active": True,
    }
    assert observed_workflow["schedule"] == scheduled_forecast.schedule
    assert observed_workflow["default_parameters"] == {"site_id": "site-001", "zone": "SE3"}
    assert observed_workflow["required_parameters"] == []


def test_scheduled_workflow_requires_defaults(monkeypatch) -> None:
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        raise AssertionError("scheduled workflow validation should run before API calls")

    patch_client_http(monkeypatch, fake_request)

    project = rb.Project("energy-forecasting", client=client)

    @project.workflow(
        name="scheduled-forecast",
        schedule=rb.Cron("0 6 * * *", timezone="Europe/Stockholm"),
    )
    def scheduled_forecast(site_id: str, zone: str = "SE3") -> dict:
        return {"site_id": site_id, "zone": zone}

    with pytest.raises(rb.RebaseWorkflowError, match="Missing defaults: site_id"):
        project.deploy()


def test_on_workflow_to_dict_and_validation() -> None:
    trigger = rb.OnWorkflow("energy/ingest-prices", on="failure", active=False)

    assert trigger.to_dict() == {
        "type": "on_workflow",
        "source": "energy/ingest-prices",
        "on": "failure",
        "active": False,
    }
    with pytest.raises(ValueError, match="non-empty"):
        rb.OnWorkflow("   ")
    with pytest.raises(ValueError, match="on must be"):
        rb.OnWorkflow("energy/ingest-prices", on="crashed")


def test_on_update_to_dict_and_validation() -> None:
    trigger = rb.OnUpdate(
        ["nordpool/prices", rb.Dataset.from_name("weather/ecmwf")],
        require="any",
        at_most_every="15m",
        deadline=rb.Cron("0 9 * * *", timezone="Europe/Stockholm"),
    )

    assert trigger.to_dict() == {
        "type": "on_update",
        "datasets": ["nordpool/prices", "weather/ecmwf"],
        "require": "any",
        "at_most_every": "15m",
        "deadline": {
            "type": "cron",
            "cron": "0 9 * * *",
            "timezone": "Europe/Stockholm",
            "day_or": True,
            "active": True,
        },
        "active": True,
    }
    assert rb.OnUpdate(["a"], at_most_every=timedelta(minutes=15)).to_dict()["at_most_every"] == "900s"
    assert rb.OnUpdate(["a"], at_most_every=900).to_dict()["at_most_every"] == "900s"
    assert "at_most_every" not in rb.OnUpdate(["a"]).to_dict()
    assert "deadline" not in rb.OnUpdate(["a"]).to_dict()
    with pytest.raises(ValueError, match="at least one dataset"):
        rb.OnUpdate([])
    with pytest.raises(ValueError, match="unique"):
        rb.OnUpdate(["a", "a"])
    with pytest.raises(ValueError, match="require must be"):
        rb.OnUpdate(["a"], require="most")
    with pytest.raises(TypeError, match="dataset names or rebase.Dataset"):
        rb.OnUpdate([42])
    with pytest.raises(TypeError, match="deadline must be"):
        rb.OnUpdate(["a"], deadline="0 9 * * *")


def test_workflow_trigger_dict_rejects_parameters() -> None:
    def forecast(site_id: str = "site-001") -> dict:
        return {"site_id": site_id}

    with pytest.raises(TypeError, match="Triggers do not accept parameters"):
        rb.Workflow(forecast, trigger={"type": "on_workflow", "source": "energy/ingest", "parameters": {}})


def test_project_workflow_deploy_sends_trigger(monkeypatch) -> None:
    observed_workflow: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "ensure_project", lambda name, **kwargs: {"id": "project-id", "name": name})
    monkeypatch.setattr(client, "find_workflow", lambda name, *, project=None: None)
    monkeypatch.setattr(client, "register_workflow", lambda **kwargs: observed_workflow.update(kwargs) or {"id": "id"})

    project = rb.Project("energy-forecasting", client=client)

    @project.workflow(
        name="triggered-forecast",
        trigger=rb.OnUpdate(["nordpool/prices"], at_most_every="15m"),
    )
    def triggered_forecast(ctx=None, site_id: str = "site-001") -> dict:
        return {"ctx": ctx, "site_id": site_id}

    project.deploy()

    assert triggered_forecast.trigger == {
        "type": "on_update",
        "datasets": ["nordpool/prices"],
        "require": "all",
        "at_most_every": "15m",
        "active": True,
    }
    assert observed_workflow["trigger"] == triggered_forecast.trigger
    assert observed_workflow["default_parameters"] == {"site_id": "site-001"}
    assert observed_workflow["required_parameters"] == []


def test_triggered_workflow_requires_defaults(monkeypatch) -> None:
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        raise AssertionError("triggered workflow validation should run before API calls")

    patch_client_http(monkeypatch, fake_request)

    project = rb.Project("energy-forecasting", client=client)

    @project.workflow(
        name="triggered-forecast",
        trigger=rb.OnWorkflow("energy/ingest-prices"),
    )
    def triggered_forecast(site_id: str, ctx=None) -> dict:
        return {"site_id": site_id, "ctx": ctx}

    with pytest.raises(rb.RebaseWorkflowError, match="Triggered workflows require defaults.*site_id"):
        project.deploy()


def test_workflow_deploy_updates_existing_workflow_with_trigger(monkeypatch) -> None:
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "list_workflows", lambda: [{"id": "workflow-id", "name": "forecast"}])
    observed: dict[str, Any] = {}

    def fake_update_workflow(workflow_id: str, **kwargs: Any) -> dict[str, Any]:
        observed["workflow_id"] = workflow_id
        observed.update(kwargs)
        return {"id": workflow_id, "name": "forecast", "current_version_id": "version-id"}

    monkeypatch.setattr(client, "update_workflow", fake_update_workflow)

    def forecast(ctx=None, site_id: str = "site-001") -> dict:
        return {"site_id": site_id, "ctx": ctx}

    workflow = rb.Workflow(forecast, trigger=rb.OnWorkflow("energy/ingest-prices"), client=client).deploy()

    assert workflow.id == "workflow-id"
    assert observed["trigger"] == {
        "type": "on_workflow",
        "source": "energy/ingest-prices",
        "on": "success",
        "active": True,
    }
    assert observed["default_parameters"] == {"site_id": "site-001"}
    assert observed["required_parameters"] == []


def test_update_workflow_omits_trigger_unless_explicit(monkeypatch) -> None:
    observed_payloads: list[dict[str, Any]] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed_payloads.append(kwargs["json"])
        return FakeResponse({"id": "workflow-id", "name": "forecast"})

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    client.update_workflow("workflow-id", description="Updated")
    client.update_workflow("workflow-id", trigger=None)
    client.update_workflow("workflow-id", trigger={"type": "on_workflow", "source": "energy/ingest"})

    assert observed_payloads[0] == {"description": "Updated"}
    assert observed_payloads[1] == {"trigger": None}
    assert observed_payloads[2] == {"trigger": {"type": "on_workflow", "source": "energy/ingest"}}


def test_dataset_client_methods_hit_expected_endpoints(monkeypatch) -> None:
    observed: list[dict[str, Any]] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed.append({"method": method, "url": url, "json": kwargs.get("json")})
        if url.endswith("/listeners"):
            return FakeResponse(["energy/forecast"])
        if method == "GET" and url.endswith("/datasets"):
            return FakeResponse([])
        if url.endswith("/signal"):
            return FakeResponse({"dataset": "nordpool/prices", "fired": ["run-id"]})
        if method == "DELETE":
            return FakeResponse({"deleted": "nordpool/prices"})
        return FakeResponse({"id": "dataset-id", "name": "nordpool/prices"})

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    client.create_dataset("nordpool/prices", description="Day-ahead prices")
    assert client.list_datasets() == []
    client.get_dataset("nordpool/prices")
    signalled = client.signal_dataset("nordpool/prices", watermark="2026-07-11T09:00:00Z", run_id="run-id")
    assert client.list_dataset_listeners("nordpool/prices") == ["energy/forecast"]
    client.delete_dataset("nordpool/prices")
    client.get_workflow_trigger("workflow-id")

    assert signalled == {"dataset": "nordpool/prices", "fired": ["run-id"]}
    assert [(item["method"], item["url"]) for item in observed] == [
        ("POST", "https://workflows.example.com/datasets"),
        ("GET", "https://workflows.example.com/datasets"),
        ("GET", "https://workflows.example.com/datasets/nordpool/prices"),
        ("POST", "https://workflows.example.com/datasets/nordpool/prices/signal"),
        ("GET", "https://workflows.example.com/datasets/nordpool/prices/listeners"),
        ("DELETE", "https://workflows.example.com/datasets/nordpool/prices"),
        ("GET", "https://workflows.example.com/workflows/workflow-id/trigger"),
    ]
    assert observed[0]["json"] == {"name": "nordpool/prices", "description": "Day-ahead prices"}
    assert observed[3]["json"] == {"watermark": "2026-07-11T09:00:00Z", "source": "sdk", "run_id": "run-id"}


def test_dataset_mark_updated_signals(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["method"] = method
        observed["url"] = url
        observed["json"] = kwargs.get("json")
        return FakeResponse({"dataset": "nordpool/prices", "fired": []})

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    dataset = rb.Dataset("nordpool/prices", client=client)
    result = dataset.mark_updated(watermark={"as_of": "2026-07-11"})

    assert result == {"dataset": "nordpool/prices", "fired": []}
    assert observed["method"] == "POST"
    assert observed["url"] == "https://workflows.example.com/datasets/nordpool/prices/signal"
    assert observed["json"] == {"watermark": {"as_of": "2026-07-11"}, "source": "sdk", "run_id": None}


def test_trigger_context_from_payload() -> None:
    context = rb.TriggerContext.from_payload(None)

    assert context.reason == "api"
    assert context.fired_at is None
    assert context.source_run_id is None
    assert context.source_workflow is None
    assert context.since == {}
    assert context.latest == {}
    assert context.missing == []
    assert context.deadline is None
    assert context.raw == {}

    payload = {
        "reason": "dataset_update",
        "fired_at": "2026-07-11T09:00:00Z",
        "source_workflow": "energy/ingest-prices",
        "since": {"nordpool/prices": "w-41"},
        "latest": {"nordpool/prices": "w-42"},
        "missing": ["weather/ecmwf"],
        "deadline": "2026-07-11T09:00:00Z",
    }
    context = rb.TriggerContext.from_payload(payload)

    assert context.reason == "dataset_update"
    assert context.fired_at == "2026-07-11T09:00:00Z"
    assert context.source_workflow == "energy/ingest-prices"
    assert context.since == {"nordpool/prices": "w-41"}
    assert context.latest == {"nordpool/prices": "w-42"}
    assert context.missing == ["weather/ecmwf"]
    assert context.deadline == "2026-07-11T09:00:00Z"
    assert context.raw == payload


def test_project_deploy_sends_source_settings(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    def fake_ensure_project(name: str, **kwargs: Any) -> dict[str, Any]:
        observed["name"] = name
        observed.update(kwargs)
        return {"id": "project-id", "name": name}

    monkeypatch.setattr(client, "ensure_project", fake_ensure_project)

    project = rb.Project(
        "energy-forecasting",
        source_mode="workspace_repo",
        repo_path="projects/energy-forecasting",
        client=client,
    )
    project.deploy()

    assert observed == {
        "name": "energy-forecasting",
        "description": None,
        "source_mode": "workspace_repo",
        "repo_owner": None,
        "repo_name": None,
        "repo_path": "projects/energy-forecasting",
        "environment_name": "dev",
    }


def test_function_from_name_spawns_remote_run(monkeypatch) -> None:
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(
        client,
        "find_function",
        lambda name, *, project: {"id": "function-id", "name": name, "project": project},
    )

    observed: dict[str, Any] = {}

    def fake_run_function(function_id: str, parameters: dict[str, Any] | None = None) -> rb.Run:
        observed["function_id"] = function_id
        observed["parameters"] = parameters
        return rb.Run("run-id", client=client, data={"id": "run-id", "status": "submitted"})

    monkeypatch.setattr(client, "run_function", fake_run_function)

    function = rb.Function.from_name("shared-energy-utils", "normalize-weather", client=client)
    run = function.spawn(site_id="site-001")

    assert run.id == "run-id"
    assert observed == {
        "function_id": "function-id",
        "parameters": {"site_id": "site-001"},
    }


def test_run_lists_step_runs(monkeypatch) -> None:
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(
        client,
        "list_run_steps",
        lambda run_id: [{"id": "step-run-id", "workflow_run_id": run_id, "node_key": "load_weather"}],
    )

    run = rb.Run("run-id", client=client)

    assert run.steps() == [{"id": "step-run-id", "workflow_run_id": "run-id", "node_key": "load_weather"}]


def test_decorated_targets_call_local_python_without_cloud(monkeypatch) -> None:
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "run_ephemeral", lambda **kwargs: pytest.fail("local calls should not use cloud"))
    project = rb.Project("hello", client=client)

    @project.function()
    def add(a: int, b: int) -> int:
        return a + b

    @project.step()
    def load_name(name: str) -> dict:
        return {"name": name}

    @project.step()
    def package(payload: dict) -> dict:
        return {"message": f"Hello, {payload['name']}!"}

    @project.workflow()
    def hello(name: str) -> dict:
        return package(load_name(name))

    assert add(1, 2) == 3
    assert load_name("Rebase") == {"name": "Rebase"}
    assert hello("Rebase") == {"message": "Hello, Rebase!"}


def test_function_ephemeral_run_sends_source_without_deploy(monkeypatch) -> None:
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    observed: dict[str, Any] = {}

    def fake_run_ephemeral(**kwargs: Any) -> rb.Run:
        observed.update(kwargs)
        return rb.Run("run-id", client=client, data={"id": "run-id", "status": "submitted"})

    monkeypatch.setattr(client, "run_ephemeral", fake_run_ephemeral)
    monkeypatch.setattr(rb.Function, "deploy", lambda self, **kwargs: pytest.fail("ephemeral run should not deploy"))

    def add(a: int, b: int) -> dict:
        return {"sum": a + b}

    function = rb.Function(add, project="math", name="add", client=client)
    run = function.ephemeral_run(a=1, b=2)

    assert run.id == "run-id"
    assert observed["target_type"] == "function"
    assert observed["project"] == "math"
    assert observed["name"] == "add"
    assert observed["entrypoint"] == "add"
    assert observed["parameters"] == {"a": 1, "b": 2}
    assert observed["mode"] == "interactive"
    assert observed["isolation"] == "shared"
    assert "def add(a: int, b: int) -> dict:" in observed["source_code"]


def test_predictor_as_function_generates_predict_wrapper() -> None:
    class PriceForecastPredictor(rb.Predictor):
        name = "price-forecast"

        def predict(self, zone: str = "SE3", horizon_hours: int = 24) -> dict:
            return {"zone": zone, "horizon_hours": horizon_hours}

    model = PriceForecastPredictor(project="models", dependencies=["boltons==25.0.0"])
    function = model.as_function()

    assert function.project == "models"
    assert function.name == "price-forecast"
    assert function.entrypoint == "predict"
    assert function.default_parameters == {"zone": "SE3", "horizon_hours": 24}
    assert function.mode == "interactive"
    assert function.isolation == "shared"
    assert function.run_type == "quick_shared"
    assert function.image_spec is not None
    assert "boltons==25.0.0" in function.image_spec["uv_pip_packages"]
    assert any("emflow" in package for package in function.image_spec["uv_pip_packages"])
    assert "class PriceForecastPredictor(rb.Predictor):" in str(function.source_code)
    assert "def predict(zone: str = 'SE3', horizon_hours: int = 24) -> dict:" in str(function.source_code)
    assert "model = PriceForecastPredictor()" in str(function.source_code)
    assert "return model.predict(zone=zone, horizon_hours=horizon_hours)" in str(function.source_code)


def test_predictor_deploy_registers_model(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "find_model", lambda name, *, project: None)

    def fake_register_model(**kwargs: Any) -> dict[str, Any]:
        observed.update(kwargs)
        return {"id": "model-id", "name": kwargs["name"], "current_version_id": "version-id"}

    monkeypatch.setattr(client, "register_model", fake_register_model)

    class PriceForecastPredictor(rb.Predictor):
        name = "price-forecast"

        def predict(self, zone: str = "SE3") -> dict:
            return {"zone": zone}

    model = PriceForecastPredictor(project="models", client=client).deploy()

    assert model.id == "model-id"
    assert observed["project"] == "models"
    assert observed["name"] == "price-forecast"
    assert observed["kind"] == "predictor"
    assert observed["operation_name"] == "predict"
    assert observed["environment"] == "dev"
    assert observed["default_parameters"] == {"zone": "SE3"}
    assert observed["mode"] == "interactive"
    assert observed["isolation"] == "shared"
    assert any("emflow" in package for package in observed["image_spec"]["uv_pip_packages"])


def test_client_records_model_publication(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["method"] = method
        observed["url"] = url
        observed["json"] = kwargs["json"]
        return FakeResponse({"id": "publication-id", **kwargs["json"]})

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    publication = client.record_model_publication(
        "model-id",
        model_version_id="version-id",
        repo_id="rebase/price-forecast",
        repo_type="model",
        visibility="public",
        revision="main",
        provider_commit_sha="hf-commit",
        source_git_commit_sha="git-commit",
        publication_metadata={"hub_commit_url": "https://huggingface.co/rebase/price-forecast/commit/hf-commit"},
    )

    assert publication["id"] == "publication-id"
    assert observed == {
        "method": "POST",
        "url": "https://workflows.example.com/models/model-id/publications",
        "json": {
            "model_version_id": "version-id",
            "provider": "huggingface",
            "repo_type": "model",
            "repo_id": "rebase/price-forecast",
            "visibility": "public",
            "path_in_repo": None,
            "revision": "main",
            "provider_commit_sha": "hf-commit",
            "source_git_commit_sha": "git-commit",
            "publication_metadata": {"hub_commit_url": "https://huggingface.co/rebase/price-forecast/commit/hf-commit"},
        },
    }


def test_predictor_deploy_can_publish_to_huggingface(monkeypatch) -> None:
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "find_model", lambda name, *, project: None)
    monkeypatch.setattr(
        client,
        "register_model",
        lambda **kwargs: {"id": "model-id", "name": kwargs["name"], "current_version_id": "version-id"},
    )
    monkeypatch.setattr(
        client,
        "get_model_version",
        lambda model_id, version_id: {
            "id": version_id,
            "model_id": model_id,
            "version_number": 1,
            "git_commit_sha": "git-commit",
            "git_dirty": False,
            "source_code": "def predict():\n    return {}\n",
        },
    )

    def fake_publish(self: rb.Model, *, version: dict[str, Any], config: rb.HuggingFacePublishConfig) -> dict[str, Any]:
        observed["model_id"] = self.id
        observed["version"] = version
        observed["repo_id"] = config.repo_id
        observed["private"] = config.private
        return {"id": "publication-id", "repo_id": config.repo_id}

    monkeypatch.setattr(rb.Model, "_publish_to_huggingface", fake_publish)

    class PriceForecastPredictor(rb.Predictor):
        name = "price-forecast"

        def predict(self, zone: str = "SE3") -> dict:
            return {"zone": zone}

    model = PriceForecastPredictor(project="models", client=client).deploy(
        huggingface=rb.HuggingFacePublishConfig("rebase/price-forecast", private=False),
    )

    assert model.data["huggingface_publication"] == {
        "id": "publication-id",
        "repo_id": "rebase/price-forecast",
    }
    assert observed["model_id"] == "model-id"
    assert observed["version"]["id"] == "version-id"
    assert observed["repo_id"] == "rebase/price-forecast"
    assert observed["private"] is False


def test_publish_to_huggingface_does_not_require_git_metadata(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    class FakeHfApi:
        def __init__(self, *, token: str | None = None) -> None:
            observed["token"] = token

        def create_repo(self, **kwargs: Any) -> None:
            observed["create_repo"] = kwargs

        def list_repo_commits(self, **kwargs: Any) -> list[Any]:
            observed["list_repo_commits"] = kwargs
            return []

        def upload_folder(self, **kwargs: Any) -> SimpleNamespace:
            folder_path = kwargs["folder_path"]
            with open(f"{folder_path}/rebase_model.json", encoding="utf-8") as provenance_file:
                observed["provenance"] = json.load(provenance_file)
            observed["upload_folder"] = kwargs
            return SimpleNamespace(
                oid="hf-commit",
                commit_url="https://huggingface.co/rebase/price-forecast/commit/hf-commit",
            )

    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(HfApi=FakeHfApi))

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    def fake_record_model_publication(model_id: str, **kwargs: Any) -> dict[str, Any]:
        observed["record_model_id"] = model_id
        observed["record"] = kwargs
        return {"id": "publication-id", **kwargs}

    monkeypatch.setattr(client, "record_model_publication", fake_record_model_publication)

    class PriceForecastPredictor(rb.Predictor):
        name = "price-forecast"

        def predict(self) -> dict:
            return {}

    model = PriceForecastPredictor(client=client)
    model.id = "model-id"
    model.data = {"id": "model-id"}

    publication = model._publish_to_huggingface(
        version={
            "id": "version-id",
            "model_id": "model-id",
            "version_number": 1,
            "fingerprint": "fp-123",
            "git_commit_sha": None,
            "git_dirty": False,
            "source_hash": "source-hash",
            "source_code": "def predict():\n    return {}\n",
        },
        config=rb.HuggingFacePublishConfig("rebase/price-forecast", private=False),
    )

    assert publication["id"] == "publication-id"
    assert observed["record_model_id"] == "model-id"
    assert observed["record"]["provider_commit_sha"] == "hf-commit"
    assert observed["record"]["source_git_commit_sha"] is None
    assert observed["record"]["publication_metadata"]["source_git_sync"] is False
    assert observed["provenance"]["git_commit_sha"] is None
    assert observed["provenance"]["source_hash"] == "source-hash"


def test_publish_to_huggingface_can_disable_source_git_sync(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    class FakeHfApi:
        def __init__(self, *, token: str | None = None) -> None:
            observed["token"] = token

        def create_repo(self, **kwargs: Any) -> None:
            observed["create_repo"] = kwargs

        def list_repo_commits(self, **kwargs: Any) -> list[Any]:
            observed["list_repo_commits"] = kwargs
            return []

        def upload_folder(self, **kwargs: Any) -> SimpleNamespace:
            folder_path = kwargs["folder_path"]
            with open(f"{folder_path}/rebase_model.json", encoding="utf-8") as provenance_file:
                observed["provenance"] = json.load(provenance_file)
            return SimpleNamespace(
                oid="hf-commit",
                commit_url="https://huggingface.co/rebase/price-forecast/commit/hf-commit",
            )

    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(HfApi=FakeHfApi))

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    def fake_record_model_publication(model_id: str, **kwargs: Any) -> dict[str, Any]:
        observed["record_model_id"] = model_id
        observed["record"] = kwargs
        return {"id": "publication-id", **kwargs}

    monkeypatch.setattr(client, "record_model_publication", fake_record_model_publication)

    class PriceForecastPredictor(rb.Predictor):
        name = "price-forecast"

        def predict(self) -> dict:
            return {}

    model = PriceForecastPredictor(client=client)
    model.id = "model-id"
    model.data = {"id": "model-id"}

    model._publish_to_huggingface(
        version={
            "id": "version-id",
            "model_id": "model-id",
            "version_number": 1,
            "fingerprint": "fp-123",
            "git_commit_sha": "git-commit",
            "git_dirty": False,
            "source_hash": "source-hash",
        },
        config=rb.HuggingFacePublishConfig(
            "rebase/price-forecast",
            private=False,
            sync_source_git=False,
        ),
    )

    assert observed["record"]["source_git_commit_sha"] is None
    assert observed["record"]["publication_metadata"]["source_git_sync"] is False
    assert observed["provenance"]["git_commit_sha"] == "git-commit"


def test_predictor_ephemeral_run_sends_model_payload(monkeypatch) -> None:
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    observed: dict[str, Any] = {}

    def fake_run_ephemeral(**kwargs: Any) -> rb.Run:
        observed.update(kwargs)
        return rb.Run("run-id", client=client, data={"id": "run-id", "status": "submitted"})

    monkeypatch.setattr(client, "run_ephemeral", fake_run_ephemeral)
    monkeypatch.setattr(
        rb.Function,
        "deploy",
        lambda self, **kwargs: pytest.fail("ephemeral model run should not deploy"),
    )

    class PriceForecastPredictor(rb.Predictor):
        name = "price-forecast"

        def predict(self, zone: str = "SE3") -> dict:
            return {"zone": zone}

    run = PriceForecastPredictor(project="models", client=client).ephemeral_run(zone="SE4")

    assert run.id == "run-id"
    assert observed["target_type"] == "model"
    assert observed["project"] == "models"
    assert observed["name"] == "price-forecast"
    assert observed["entrypoint"] == "predict"
    assert observed["parameters"] == {"zone": "SE4"}
    assert observed["default_parameters"] == {"zone": "SE3"}
    assert observed["mode"] == "interactive"
    assert observed["isolation"] == "shared"


def test_predictor_handle_runs_model(monkeypatch) -> None:
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(
        client,
        "find_model",
        lambda name, *, project: {
            "id": "model-id",
            "name": name,
            "operation_name": "predict",
            "project_id": "project-id",
        },
    )
    observed: dict[str, Any] = {}

    def fake_run_model(model_id: str, parameters: dict[str, Any] | None = None, *, environment: str = "dev") -> rb.Run:
        observed["model_id"] = model_id
        observed["parameters"] = parameters
        observed["environment"] = environment
        return rb.Run(
            "run-id",
            client=client,
            data={"id": "run-id", "status": "succeeded", "result": {"zone": "SE4"}},
        )

    monkeypatch.setattr(client, "run_model", fake_run_model)
    monkeypatch.setattr(
        client,
        "get_run",
        lambda run_id: {"id": run_id, "status": "succeeded", "result": {"zone": "SE4"}},
    )

    result = rb.Predictor.from_name("models", "price-forecast", client=client).predict.remote(
        environment="staging",
        zone="SE4",
    )

    assert result == {"zone": "SE4"}
    assert observed == {
        "model_id": "model-id",
        "parameters": {"zone": "SE4"},
        "environment": "staging",
    }


def test_optimizer_as_function_generates_optimize_wrapper() -> None:
    class DispatchOptimizer(rb.Optimizer):
        name = "dispatch-optimizer"

        def optimize(self, site_id: str, horizon_hours: int = 24) -> dict:
            return {"site_id": site_id, "horizon_hours": horizon_hours}

    function = DispatchOptimizer(project="models").as_function()

    assert function.name == "dispatch-optimizer"
    assert function.entrypoint == "optimize"
    assert function.default_parameters == {"horizon_hours": 24}
    assert "class DispatchOptimizer(rb.Optimizer):" in str(function.source_code)
    assert "def optimize(site_id: str, horizon_hours: int = 24) -> dict:" in str(function.source_code)
    assert "return model.optimize(site_id=site_id, horizon_hours=horizon_hours)" in str(function.source_code)


def test_agent_as_function_generates_act_wrapper() -> None:
    class BatteryAgent(rb.Agent):
        name = "battery-agent"

        def act(self, state: dict) -> dict:
            return {"action": "hold", "state": state}

    function = BatteryAgent(project="models").as_function()

    assert function.name == "battery-agent"
    assert function.entrypoint == "act"
    assert function.default_parameters == {}
    assert "class BatteryAgent(rb.Agent):" in str(function.source_code)
    assert "def act(state: dict) -> dict:" in str(function.source_code)
    assert "return model.act(state=state)" in str(function.source_code)


def test_plain_model_is_not_directly_deployable() -> None:
    class BaseEnergyModel(rb.Model):
        name = "base-energy-model"

    with pytest.raises(rb.RebaseWorkflowError, match="rebase.Model is not directly deployable"):
        BaseEnergyModel().as_function()


def test_simulator_is_local_only_for_cloud_deploy() -> None:
    class BatterySimulator(rb.Simulator):
        name = "battery-simulator"

        def _transition_function(self, state, action):
            return state

        def _gather_info(self):
            return {}

        def reset(self):
            return {}

        def step(self, action=None):
            return {}, {}

    with pytest.raises(rb.RebaseWorkflowError, match="Simulator cloud deployment is not supported"):
        BatterySimulator().deploy()


def test_workflow_ephemeral_run_embeds_step_sources_without_deploy(monkeypatch) -> None:
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    observed: dict[str, Any] = {}

    def fake_run_ephemeral(**kwargs: Any) -> rb.Run:
        observed.update(kwargs)
        return rb.Run("run-id", client=client, data={"id": "run-id", "status": "submitted"})

    monkeypatch.setattr(client, "run_ephemeral", fake_run_ephemeral)
    monkeypatch.setattr(rb.Workflow, "deploy", lambda self, **kwargs: pytest.fail("ephemeral run should not deploy"))
    project = rb.Project("hello", client=client)

    @project.step()
    def load_name(name: str = "World") -> dict:
        return {"name": name}

    @project.step()
    def package(payload: dict) -> dict:
        return {"message": f"Hello, {payload['name']}!"}

    @project.workflow(name="hello-workflow")
    def hello(name: str = "World") -> dict:
        return package(load_name(name))

    run = hello.ephemeral_run(name="Rebase")

    assert run.id == "run-id"
    assert observed["target_type"] == "workflow"
    assert observed["project"] == "hello"
    assert observed["name"] == "hello-workflow"
    assert observed["parameters"] == {"name": "Rebase"}
    assert observed["mode"] == "interactive"
    assert observed["isolation"] == "shared"
    nodes = observed["step_graph"]["nodes"]
    assert [node["name"] for node in nodes] == ["load-name", "package"]
    assert all(node["function_version_id"] is None for node in nodes)
    assert all("source_code" in node and node["source_code"] for node in nodes)


def test_get_workflow_schedule(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["method"] = method
        observed["url"] = url
        return FakeResponse(
            {
                "workflow_id": "workflow-id",
                "version_id": "version-id",
                "schedule": {"type": "cron", "cron": "0 * * * *"},
                "active": True,
                "next_run_at": "2026-07-11T11:00:00Z",
            }
        )

    patch_client_http(monkeypatch, fake_request)

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    schedule = client.get_workflow_schedule("workflow-id")

    assert observed["method"] == "GET"
    assert observed["url"] == "https://workflows.example.com/workflows/workflow-id/schedule"
    assert schedule["schedule"]["cron"] == "0 * * * *"
    assert schedule["next_run_at"] == "2026-07-11T11:00:00Z"


def test_cancel_run_posts_to_cancel_endpoint(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["method"] = method
        observed["url"] = url
        return FakeResponse({"id": "run-id", "status": "cancelled"})

    patch_client_http(monkeypatch, fake_request)

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    cancelled = client.cancel_run("run-id")

    assert observed["method"] == "POST"
    assert observed["url"] == "https://workflows.example.com/runs/run-id/cancel"
    assert cancelled["status"] == "cancelled"


def test_run_cancel_helper_updates_data(monkeypatch) -> None:
    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        return FakeResponse({"id": "run-id", "status": "cancelled"})

    patch_client_http(monkeypatch, fake_request)

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    run = rb.Run("run-id", client=client, data={"id": "run-id", "status": "running"})
    run.cancel()
    assert run.status == "cancelled"


def test_get_run_logs_passes_cursor_params(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["method"] = method
        observed["url"] = url
        observed["params"] = kwargs.get("params")
        return FakeResponse(
            {
                "run_id": "run-id",
                "source": "cloud_logging",
                "entries": [{"timestamp": "2026-07-11T10:00:01Z", "severity": "INFO", "message": "line"}],
                "next_since": "2026-07-11T10:00:01Z",
            }
        )

    patch_client_http(monkeypatch, fake_request)

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    logs = client.get_run_logs("run-id", since="2026-07-11T10:00:00Z", limit=100)

    assert observed["method"] == "GET"
    assert observed["url"] == "https://workflows.example.com/runs/run-id/logs"
    assert observed["params"] == {"since": "2026-07-11T10:00:00Z", "limit": 100}
    assert logs["entries"][0]["message"] == "line"


def test_workspace_notifications_roundtrip(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["method"] = method
        observed["url"] = url
        observed["json"] = kwargs.get("json")
        return FakeResponse(
            {
                "workspace_id": "default",
                "notify_on_failure": True,
                "webhook_url": "https://hooks.example.com/rebase",
                "has_webhook_secret": True,
            }
        )

    patch_client_http(monkeypatch, fake_request)

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    client.get_workspace_notifications()
    assert observed["method"] == "GET"
    assert observed["url"] == "https://workflows.example.com/workspace/notifications"

    client.update_workspace_notifications(
        notify_on_failure=True,
        webhook_url="https://hooks.example.com/rebase",
        webhook_secret="s3cret",
    )
    assert observed["method"] == "PATCH"
    assert observed["json"] == {
        "notify_on_failure": True,
        "webhook_url": "https://hooks.example.com/rebase",
        "webhook_secret": "s3cret",
    }

    client.update_workspace_notifications(webhook_url=None, webhook_secret=None)
    assert observed["json"] == {"webhook_url": "", "webhook_secret": ""}


def test_volume_rpcs_and_file_ops(monkeypatch, tmp_path) -> None:
    observed: list[dict[str, Any]] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed.append({"method": method, "url": url, "json": kwargs.get("json"), "params": kwargs.get("params")})
        if url.endswith("/upload-url") or url.endswith("/download-url"):
            return FakeResponse({"url": "https://signed.example.com/x", "method": "PUT", "expires_seconds": 3600})
        if url.endswith("/volumes"):
            return FakeResponse({"name": "models", "provider": "gcs", "bucket": "b", "prefix": "models/"})
        if url.endswith("/objects") and method == "GET":
            return FakeResponse([{"path": "model.pkl", "size": 3, "updated": None}])
        return FakeResponse({"deleted": "x"})

    transferred: dict[str, Any] = {}

    class FakeTransferResponse:
        status_code = 200
        text = ""
        content = b"model-bytes"

    def fake_put(url: str, data: Any = None, timeout: int | None = None) -> FakeTransferResponse:
        transferred["put_url"] = url
        transferred["body"] = data.read()
        return FakeTransferResponse()

    def fake_get(url: str, timeout: int | None = None) -> FakeTransferResponse:
        transferred["get_url"] = url
        return FakeTransferResponse()

    patch_client_http(monkeypatch, fake_request)
    monkeypatch.setattr("requests.put", fake_put)
    monkeypatch.setattr("requests.get", fake_get)

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    volume = rb.Volume.from_name("models", create_if_missing=True)
    volume._client = client

    local = tmp_path / "model.pkl"
    local.write_bytes(b"weights")
    remote = volume.put_file(local, "nested/model.pkl")
    assert remote == "nested/model.pkl"
    assert transferred["body"] == b"weights"
    # create_if_missing triggered POST /volumes before the upload URL request
    assert observed[0]["url"].endswith("/volumes")
    assert observed[1]["json"] == {"path": "nested/model.pkl"}

    assert volume.read_file("nested/model.pkl") == b"model-bytes"
    assert volume.listdir() == [{"path": "model.pkl", "size": 3, "updated": None}]
    volume.remove_file("/nested/model.pkl")
    assert observed[-1]["method"] == "DELETE"
    assert observed[-1]["params"] == {"path": "nested/model.pkl"}
    volume.commit()  # no-ops must exist for Modal compatibility
    volume.reload()


def test_resolve_volumes_payload_from_mapping(monkeypatch) -> None:
    from rebase.client import _resolve_volumes_payload

    created: list[str] = []

    class FakeClient:
        def create_volume(self, name: str) -> dict[str, Any]:
            created.append(name)
            return {"name": name}

    from typing import cast

    from rebase.client import Client as _Client

    payload = _resolve_volumes_payload(
        {
            "/models": rb.Volume.from_name("model-cache", create_if_missing=True),
            "/features": "feature-store",
        },
        cast(_Client, FakeClient()),
    )
    assert payload == [
        {"volume": "feature-store", "mount_path": "/features", "read_only": False},
        {"volume": "model-cache", "mount_path": "/models", "read_only": False},
    ]
    assert created == ["model-cache"]


def test_function_deploy_sends_volume_attachments(monkeypatch, tmp_path) -> None:
    from rebase.client import Client

    observed: dict[str, Any] = {}

    def fake_find_function(self: Client, name: str, project: str | None = None) -> None:
        return None

    def fake_register_function(self: Client, **kwargs: Any) -> dict[str, Any]:
        observed.update(kwargs)
        return {"id": "function-id"}

    monkeypatch.setattr(Client, "find_function", fake_find_function)
    monkeypatch.setattr(Client, "register_function", fake_register_function)
    monkeypatch.setattr(Client, "ensure_project", lambda self, name: {"id": "project-id"})

    import rebase as rb_module

    @rb_module.function(project="ml", name="train", volumes={"/models": "model-cache"})
    def train() -> dict:
        return {}

    train.client = Client(api_key="rbw_test", api_url="https://workflows.example.com")
    train.deploy()

    assert observed["volumes"] == [{"volume": "model-cache", "mount_path": "/models", "read_only": False}]


def test_update_dataset_sends_only_provided_keys(monkeypatch) -> None:
    observed: list[dict[str, Any]] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed.append({"method": method, "url": url, "json": kwargs["json"]})
        return FakeResponse({"id": "dataset-id", "name": "nordpool/prices"})

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    contract = {"$schema": "rebase/contract-v1", "properties": {}, "required": [], "x-rebase": {}}
    client.update_dataset("nordpool/prices", contract=contract)
    client.update_dataset("nordpool/prices", freshness={"max_age": "45m"}, description="prices")
    client.update_dataset("nordpool/prices", contract=None, freshness=None)  # explicit null clears

    assert all(item["method"] == "PATCH" for item in observed)
    assert all(item["url"] == "https://workflows.example.com/datasets/nordpool/prices" for item in observed)
    assert observed[0]["json"] == {"contract": contract}
    assert observed[1]["json"] == {"freshness": {"max_age": "45m"}, "description": "prices"}
    assert observed[2]["json"] == {"contract": None, "freshness": None}


def test_signal_dataset_includes_validation_when_given(monkeypatch) -> None:
    observed: list[dict[str, Any]] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed.append(kwargs["json"])
        return FakeResponse({"dataset": "nordpool/prices", "fired": []})

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    client.signal_dataset("nordpool/prices", watermark="w-1")
    report = {"passed": True, "checks": 4, "row_count": 10, "failures": []}
    client.signal_dataset("nordpool/prices", watermark="w-1", validation=report, source="source_write")

    assert "validation" not in observed[0]
    assert observed[1] == {"watermark": "w-1", "source": "source_write", "run_id": None, "validation": report}


def test_dataset_sync_config_publishes_absent_and_warns_on_drift(monkeypatch) -> None:
    contract = {"$schema": "rebase/contract-v1", "properties": {}, "required": [], "x-rebase": {}}
    stored: dict[str, Any] = {
        "name": "nordpool/prices",
        "contract": {"$schema": "rebase/contract-v1", "properties": {"old": {"type": "number"}}},
        "freshness": None,
    }
    calls: dict[str, int] = {"get": 0, "patch": 0}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        if method == "GET":
            calls["get"] += 1
            return FakeResponse(stored)
        if method == "PATCH":
            calls["patch"] += 1
            return FakeResponse({**stored, **kwargs["json"]})
        return FakeResponse({"dataset": "nordpool/prices", "fired": []})

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    dataset = rb.Dataset(
        "nordpool/prices",
        client=client,
        contract=contract,
        freshness={"max_age": "45m"},
    )
    # Drifted contract: warned, never overwritten. Absent freshness: first
    # publication, PATCHed once. Both evaluated once per instance.
    with pytest.warns(UserWarning, match="rebase dataset sync"):
        dataset.mark_updated(watermark="w-1")
    dataset.mark_updated(watermark="w-2")

    assert calls["patch"] == 1
    assert calls["get"] == 1


def test_dataset_sync_config_skips_patch_when_stored_matches(monkeypatch) -> None:
    contract = {"$schema": "rebase/contract-v1", "properties": {}, "required": [], "x-rebase": {}}
    observed_methods: list[str] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed_methods.append(method)
        if method == "GET":
            return FakeResponse({"name": "nordpool/prices", "contract": contract, "freshness": None})
        return FakeResponse({"dataset": "nordpool/prices", "fired": []})

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    dataset = rb.Dataset("nordpool/prices", client=client, contract=contract)
    dataset.mark_updated()

    assert "PATCH" not in observed_methods


def test_dataset_sync_config_survives_get_failure(monkeypatch) -> None:
    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        if method == "GET":
            raise requests.ConnectionError("api down")
        return FakeResponse({"dataset": "nordpool/prices", "fired": []})

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    dataset = rb.Dataset("nordpool/prices", client=client, freshness={"max_age": "45m"})
    with pytest.warns(UserWarning, match="skipping sync"):
        result = dataset.mark_updated()

    assert result == {"dataset": "nordpool/prices", "fired": []}


def test_on_update_only_valid_serialization() -> None:
    default = rb.OnUpdate(["nordpool/prices"])
    assert "only_valid" not in default.to_dict()

    strict = rb.OnUpdate(["nordpool/prices"], only_valid=True)
    assert strict.to_dict()["only_valid"] is True

    with pytest.raises(TypeError, match="only_valid"):
        rb.OnUpdate(["nordpool/prices"], only_valid="yes")


def _mark(directory, workspace_id: str) -> None:
    from rebase.config import write_local_config

    write_local_config(directory, workspace_id=workspace_id)


def test_directory_marker_overrides_the_active_workspace(tmp_path, monkeypatch) -> None:
    """A repo's workspace beats the globally active one, on the active credentials.

    The workspace travels as one header and an API key reaches every workspace the user
    belongs to, so pinning a directory needs neither a second profile nor another
    sign-in — which is the whole point of the marker.
    """
    config = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config))
    write_profile(api_key="rbw_key", workspace={"id": "agent-work"}, path=config)

    repo = tmp_path / "repo"
    (repo / "deploy").mkdir(parents=True)
    monkeypatch.chdir(repo)
    assert rb.Client().workspace_id == "agent-work"

    _mark(repo, "rebase-grid")
    client = rb.Client()
    assert client.workspace_id == "rebase-grid"
    assert client.api_key == "rbw_key"
    assert client._request_headers(auth=True)["X-Rebase-Workspace"] == "rebase-grid"

    # Still the repo's workspace from a subdirectory, the way git resolves config.
    monkeypatch.chdir(repo / "deploy")
    assert rb.Client().workspace_id == "rebase-grid"


def test_explicit_workspace_beats_the_directory_marker(tmp_path, monkeypatch) -> None:
    """Switching workspace by hand has to survive being inside a marked repo."""
    config = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config))
    write_profile(api_key="rbw_key", workspace={"id": "agent-work"}, path=config)

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    _mark(repo, "rebase-grid")

    assert rb.Client(workspace_id="forecast-dev").workspace_id == "forecast-dev"


def test_unmarked_directory_keeps_the_profile_workspace(tmp_path, monkeypatch) -> None:
    config = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config))
    write_profile(api_key="rbw_key", workspace={"id": "agent-work"}, path=config)

    monkeypatch.chdir(tmp_path)
    assert rb.Client().workspace_id == "agent-work"


def _compiled_graph(monkeypatch, build) -> dict[str, Any]:
    """Deploy a project against a fake API and return the workflow's compiled step graph."""
    observed: dict[str, Any] = {}
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "ensure_project", lambda name, **kwargs: {"id": "project-id", "name": name})
    monkeypatch.setattr(client, "find_function", lambda name, *, project: None)
    monkeypatch.setattr(client, "find_workflow", lambda name, *, project=None: None)
    monkeypatch.setattr(
        client,
        "register_function",
        lambda **kwargs: {
            "id": f"{kwargs['name']}-id",
            "name": kwargs["name"],
            "current_version_id": f"{kwargs['name']}-version-id",
        },
    )
    monkeypatch.setattr(
        client,
        "register_workflow",
        lambda **kwargs: observed.update(kwargs) or {"id": "workflow-id", "current_version_id": "v"},
    )

    project = rb.Project("energy-forecasting", client=client)
    build(project)
    project.deploy()
    return observed["step_graph"]


def test_steps_that_ignore_each_other_still_compile_to_a_chain(monkeypatch) -> None:
    """Steps are sequential by contract, so the graph is a line even without data flow.

    Two steps that pass nothing between them would otherwise be independent roots, and
    "which step failed" stops having one answer the moment the graph forks.
    """

    def build(project: Any) -> None:
        @project.step(name="capture-dk")
        def capture_dk() -> dict:
            return {"area": "DK"}

        @project.step(name="capture-se")
        def capture_se() -> dict:
            return {"area": "SE"}

        @project.workflow(name="capture-all")
        def capture_all() -> dict:
            return {"dk": capture_dk(), "se": capture_se()}

    nodes = {node["node_key"]: node for node in _compiled_graph(monkeypatch, build)["nodes"]}

    assert nodes["capture_dk"]["upstream_node_keys"] == []
    assert nodes["capture_se"]["upstream_node_keys"] == ["capture_dk"]
    # Sequence only: nothing is passed between them, so no binding claims a value is.
    assert nodes["capture_se"]["input_bindings"] == {}


def test_the_ordering_edge_is_added_to_the_data_edges_not_instead_of_them(monkeypatch) -> None:
    """A step reaching back past its predecessor keeps both edges, and stays ordered."""

    def build(project: Any) -> None:
        @project.step(name="first")
        def first() -> dict:
            return {}

        @project.step(name="second")
        def second() -> dict:
            return {}

        @project.step(name="third")
        def third(from_first: dict) -> dict:
            return from_first

        @project.workflow(name="reaches-back")
        def reaches_back() -> dict:
            one = first()
            second()
            return {"third": third(one)}

    nodes = {node["node_key"]: node for node in _compiled_graph(monkeypatch, build)["nodes"]}

    # "first" carries the value, "second" only the sequence — both are upstream.
    assert nodes["third"]["upstream_node_keys"] == ["first", "second"]
    assert nodes["third"]["input_bindings"] == {"from_first": {"type": "node_output", "node_key": "first"}}


def test_graph_node_order_follows_the_body(monkeypatch) -> None:
    def build(project: Any) -> None:
        @project.step(name="a")
        def step_a() -> dict:
            return {}

        @project.step(name="b")
        def step_b() -> dict:
            return {}

        @project.step(name="c")
        def step_c() -> dict:
            return {}

        @project.workflow(name="ordered")
        def ordered() -> dict:
            step_a()
            step_b()
            step_c()
            return {}

    graph = _compiled_graph(monkeypatch, build)
    # The node key comes from the step's name, not the Python function's.
    assert [node["node_key"] for node in graph["nodes"]] == ["a", "b", "c"]
    assert [node["upstream_node_keys"] for node in graph["nodes"]] == [[], ["a"], ["b"]]


def test_function_ephemeral_run_forwards_env_and_secrets(monkeypatch) -> None:
    """An ephemeral run must carry the same env/secrets a deployed one gets.

    Regression: these were dropped entirely, so user code that read
    os.environ["..."] died with KeyError on every ephemeral run while the
    identical deployed function worked.
    """
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    observed: dict[str, Any] = {}

    def fake_run_ephemeral(**kwargs: Any) -> rb.Run:
        observed.update(kwargs)
        return rb.Run("run-id", client=client, data={"id": "run-id", "status": "submitted"})

    monkeypatch.setattr(client, "run_ephemeral", fake_run_ephemeral)

    def add(a: int, b: int) -> dict:
        return {"sum": a + b}

    function = rb.Function(
        add,
        project="math",
        name="add",
        client=client,
        env={"PLAIN": "value"},
        secrets={"API_KEY": "rbw-ws-bundle--API_KEY"},
    )
    function.ephemeral_run(a=1, b=2)

    assert observed["env"] == {"PLAIN": "value"}
    assert observed["secrets"] == {"API_KEY": "rbw-ws-bundle--API_KEY"}


def test_workflow_ephemeral_run_forwards_image_env_and_secrets(monkeypatch) -> None:
    """Workflows additionally dropped image_spec, silently ignoring uv_pip_install()."""
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    observed: dict[str, Any] = {}

    def fake_run_ephemeral(**kwargs: Any) -> rb.Run:
        observed.update(kwargs)
        return rb.Run("run-id", client=client, data={"id": "run-id", "status": "submitted"})

    monkeypatch.setattr(client, "run_ephemeral", fake_run_ephemeral)

    def flow() -> str:
        return "done"

    workflow = rb.Workflow(
        flow,
        project="etl",
        name="flow",
        client=client,
        env={"PLAIN": "value"},
        secrets={"API_KEY": "rbw-ws-bundle--API_KEY"},
        image=rb.Image.python("3.12").uv_pip_install("httpx"),
    )
    workflow.ephemeral_run()

    assert observed["env"] == {"PLAIN": "value"}
    assert observed["secrets"] == {"API_KEY": "rbw-ws-bundle--API_KEY"}
    assert observed["image_spec"] is not None
    assert "httpx" in str(observed["image_spec"])


def test_run_ephemeral_posts_env_and_secrets(monkeypatch) -> None:
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    sent: dict[str, Any] = {}

    def fake_request(method: str, path: str, **kwargs: Any) -> dict:
        sent["json"] = kwargs.get("json")
        return {"id": "run-id", "status": "submitted"}

    monkeypatch.setattr(client, "request", fake_request)

    client.run_ephemeral(
        target_type="function",
        project="math",
        name="add",
        source_code="def add():\n    return 1\n",
        entrypoint="add",
        run_type="quick",
        env={"PLAIN": "value"},
        secrets={"API_KEY": "rbw-ws-bundle--API_KEY"},
    )

    assert sent["json"]["env"] == {"PLAIN": "value"}
    assert sent["json"]["secrets"] == {"API_KEY": "rbw-ws-bundle--API_KEY"}


def test_run_ephemeral_omits_empty_env_and_secrets(monkeypatch) -> None:
    """Empty dicts are left out so an older API, which forbids unknown fields,
    still accepts ordinary ephemeral runs."""
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    sent: dict[str, Any] = {}

    def fake_request(method: str, path: str, **kwargs: Any) -> dict:
        sent["json"] = kwargs.get("json")
        return {"id": "run-id", "status": "submitted"}

    monkeypatch.setattr(client, "request", fake_request)

    client.run_ephemeral(
        target_type="function",
        project="math",
        name="add",
        source_code="def add():\n    return 1\n",
        entrypoint="add",
        run_type="quick",
    )

    assert "env" not in sent["json"]
    assert "secrets" not in sent["json"]


class FakeMissingRouteResponse:
    """A 404 from a platform that predates the route being asked for."""

    text = '{"detail":"Not Found"}'
    status_code = 404

    def raise_for_status(self) -> None:
        raise requests.HTTPError("404")

    def json(self) -> dict[str, Any]:
        return {"detail": "Not Found"}


def test_unfiltered_list_functions_makes_one_request(monkeypatch) -> None:
    """It used to walk the projects and ask for each one's functions in turn.

    That serial request-per-project is what made the TUI's workspace view slow in
    proportion to the size of the workspace, and every other caller inherited it.
    """
    paths: list[str] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        paths.append(url.split("workflows.example.com")[-1])
        return FakeResponse([{"id": "fn-1", "project_id": "p1"}, {"id": "fn-2", "project_id": "p2"}])

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    functions = client.list_functions()

    assert [item["id"] for item in functions] == ["fn-1", "fn-2"]
    assert paths == ["/functions"]


def test_unfiltered_list_functions_falls_back_when_the_route_is_missing(monkeypatch) -> None:
    """A toolkit ahead of its platform should lose the speed, not the answer."""
    paths: list[str] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> Any:
        path = url.split("workflows.example.com")[-1]
        paths.append(path)
        if path == "/functions":
            return FakeMissingRouteResponse()
        if path == "/projects":
            return FakeResponse([{"id": "p1", "name": "one"}, {"id": "p2", "name": "two"}])
        return FakeResponse([{"id": f"fn-{path[-11]}", "project_id": path.split("/")[2]}])

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    functions = client.list_functions()

    assert len(functions) == 2
    assert paths == ["/functions", "/projects", "/projects/p1/functions", "/projects/p2/functions"]


class FakeMethodNotAllowedResponse(FakeMissingRouteResponse):
    """A 405, which is how an absent route can present when a prefix already matches."""

    status_code = 405

    def raise_for_status(self) -> None:
        raise requests.HTTPError("405")


def test_get_workspace_overview_returns_the_payload(monkeypatch) -> None:
    paths: list[str] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> Any:
        paths.append(url.split("workflows.example.com")[-1])
        return FakeResponse({"environment": "dev", "projects": [{"id": "p1", "name": "one"}]})

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    overview = client.get_workspace_overview()

    assert overview is not None
    assert [project["name"] for project in overview["projects"]] == ["one"]
    assert paths == ["/workspace/overview"]


@pytest.mark.parametrize("response", [FakeMissingRouteResponse, FakeMethodNotAllowedResponse])
def test_get_workspace_overview_reports_an_absent_route_as_none(monkeypatch, response) -> None:
    """A toolkit ahead of its platform should lose the speed, not the answer."""
    paths: list[str] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> Any:
        paths.append(url.split("workflows.example.com")[-1])
        return response()

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    assert client.get_workspace_overview() is None
    # The client only detects the route; assembling the fallback is the caller's job.
    assert paths == ["/workspace/overview"]


def test_get_workspace_overview_reraises_a_real_error(monkeypatch) -> None:
    def fake_request(method: str, url: str, **kwargs: Any) -> Any:
        return FakeErrorResponse({"detail": "nope"})

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    with pytest.raises(rb.RebaseWorkflowError):
        client.get_workspace_overview()


def test_an_absent_overview_route_is_asked_for_once(monkeypatch) -> None:
    """The TUI re-reads its view every few seconds; a wasted probe each time adds up."""
    paths: list[str] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> Any:
        paths.append(url.split("workflows.example.com")[-1])
        return FakeMissingRouteResponse()

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    assert client.get_workspace_overview() is None
    assert client.get_workspace_overview() is None
    assert client.get_workspace_overview() is None

    assert paths == ["/workspace/overview"]


def test_an_absent_project_overview_route_is_asked_for_once_across_projects(monkeypatch) -> None:
    """The route is absent, not that project's copy of it, so one 404 answers for all."""
    paths: list[str] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> Any:
        paths.append(url.split("workflows.example.com")[-1])
        return FakeMissingRouteResponse()

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    assert client.get_project_overview("p1") is None
    assert client.get_project_overview("p2") is None

    assert paths == ["/projects/p1/overview"]


def test_get_project_overview_reports_an_absent_route_as_none(monkeypatch) -> None:
    paths: list[str] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> Any:
        paths.append(url.split("workflows.example.com")[-1])
        return FakeMissingRouteResponse()

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    assert client.get_project_overview("p1") is None
    assert paths == ["/projects/p1/overview"]


# --- inline-result contract & transport (ephemeral fast path) -----------------


def test_ephemeral_run_terminal_response_needs_no_refetch(monkeypatch) -> None:
    """The submit response already carries the terminal record; `.result()` must
    answer from it without a single follow-up GET."""
    calls: list[str] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        calls.append(f"{method} {url.split('workflows.example.com')[-1]}")
        return FakeResponse({"id": "run-1", "status": "succeeded", "result": {"value": 7}})

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    run = client.run_ephemeral(
        target_type="function",
        project="demo",
        name="fn",
        source_code="def fn():\n    return 7\n",
        entrypoint="fn",
        run_type="quick_shared",
    )

    assert run.result() == {"value": 7}
    assert calls == ["POST /runs/ephemeral"]


def test_run_result_unwraps_a_marked_non_dict_return(monkeypatch) -> None:
    """remote() hands back what the function returned: the server marks the
    {"value": ...} it stores for a list return, and the marker is unwrapped."""

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        return FakeResponse(
            {"id": "run-1", "status": "succeeded", "result": {"value": ["A"], "__rebase_wrapped__": True}}
        )

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    run = client.run_ephemeral(
        target_type="function",
        project="demo",
        name="fn",
        source_code="def fn():\n    return ['A']\n",
        entrypoint="fn",
        run_type="quick_shared",
    )

    assert run.result() == ["A"]


def test_run_result_passes_a_dict_return_and_an_unmarked_wrapper_through() -> None:
    assert rb.client.unwrap_result({"value": ["A"]}) == {"value": ["A"]}
    assert rb.client.unwrap_result({"status": "ok", "value": 1}) == {"status": "ok", "value": 1}
    assert rb.client.unwrap_result({"value": None, "__rebase_wrapped__": True}) is None
    assert rb.client.unwrap_result({}) == {}


def test_ephemeral_run_submits_the_declared_bucket_grants(monkeypatch) -> None:
    """`rebase run` of a function with buckets= must send the same attachment
    list a deploy records, or the server refuses the run its own bucket."""
    bodies: list[dict[str, Any]] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        bodies.append(kwargs.get("json") or {})
        return FakeResponse({"id": "run-1", "status": "succeeded", "result": {}})

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    client.run_ephemeral(
        target_type="function",
        project="demo",
        name="fn",
        source_code="def fn():\n    return 7\n",
        entrypoint="fn",
        run_type="quick_shared",
        buckets=["grid-archive", {"bucket": "scratch"}],
    )

    assert bodies[0]["buckets"] == [{"bucket": "grid-archive"}, {"bucket": "scratch"}]


def test_ephemeral_run_omits_buckets_when_none_are_declared(monkeypatch) -> None:
    bodies: list[dict[str, Any]] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        bodies.append(kwargs.get("json") or {})
        return FakeResponse({"id": "run-1", "status": "succeeded", "result": {}})

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    client.run_ephemeral(
        target_type="function",
        project="demo",
        name="fn",
        source_code="def fn():\n    return 7\n",
        entrypoint="fn",
        run_type="quick_shared",
    )

    assert "buckets" not in bodies[0]


def test_ephemeral_run_non_terminal_response_still_polls(monkeypatch) -> None:
    """Old-server contract: a `submitted` body falls back to the poll loop."""
    calls: list[str] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        calls.append(method)
        if method == "POST":
            return FakeResponse({"id": "run-1", "status": "submitted"})
        return FakeResponse({"id": "run-1", "status": "succeeded", "result": {"value": 1}})

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    run = client.run_ephemeral(
        target_type="function",
        project="demo",
        name="fn",
        source_code="def fn():\n    return 1\n",
        entrypoint="fn",
        run_type="quick_shared",
    )

    assert run.result(poll_interval=0) == {"value": 1}
    assert calls == ["POST", "GET"]


def test_ephemeral_run_uses_long_read_timeout(monkeypatch) -> None:
    """Quick runs execute inside the request; the old 30 s default cut them off."""
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["timeout"] = kwargs["timeout"]
        return FakeResponse({"id": "run-1", "status": "succeeded", "result": None})

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    client.run_ephemeral(
        target_type="function",
        project="demo",
        name="fn",
        source_code="def fn():\n    return None\n",
        entrypoint="fn",
        run_type="quick_shared",
    )

    from rebase.client import EPHEMERAL_RUN_REQUEST_TIMEOUT_SECONDS

    assert observed["timeout"] == EPHEMERAL_RUN_REQUEST_TIMEOUT_SECONDS


def test_client_reuses_one_session_across_requests(monkeypatch) -> None:
    """Keep-alive: every request must go through the same Session object."""
    sessions: list[Any] = []

    class FakeSession:
        def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
            sessions.append(self)
            return FakeResponse([])

    fake_session = FakeSession()
    monkeypatch.setattr("requests.Session", lambda: fake_session)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    client.list_workflows()
    client.list_workflows()

    assert sessions == [fake_session, fake_session]


def test_client_rebuilds_session_after_fork(monkeypatch) -> None:
    """A forked child must not reuse the parent's socket."""
    created: list[Any] = []

    class FakeSession:
        def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
            return FakeResponse([])

    def make_session() -> FakeSession:
        session = FakeSession()
        created.append(session)
        return session

    monkeypatch.setattr("requests.Session", make_session)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    client.list_workflows()
    monkeypatch.setattr("rebase.client.os.getpid", lambda: -1)  # simulate fork
    client.list_workflows()

    assert len(created) == 2


def test_disk_token_cached_and_invalidated_on_401(monkeypatch) -> None:
    """load_access_token() is disk IO: read once, drop the cache on a 401 and retry."""
    token_reads: list[int] = []
    responses: list[FakeResponse] = []

    class FakeUnauthorized(FakeResponse):
        status_code = 401
        text = '{"detail":"expired"}'

        def raise_for_status(self) -> None:
            raise requests.HTTPError("401")

    def fake_load_access_token() -> str:
        token_reads.append(1)
        return f"token-{len(token_reads)}"

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        response = responses.pop(0)
        response.auth_header = kwargs["headers"].get("Authorization")  # type: ignore[attr-defined]
        return response

    monkeypatch.setattr("rebase.client.load_access_token", fake_load_access_token)
    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key=None, api_url="https://workflows.example.com")
    client.access_token = None  # force the disk-token path even if env vars leak in

    ok_one, ok_two = FakeResponse([]), FakeResponse([])
    responses = [ok_one, ok_two]
    client.list_workflows()
    client.list_workflows()
    assert token_reads == [1]  # cached across requests
    assert ok_two.auth_header == "Bearer token-1"  # type: ignore[attr-defined]

    unauthorized, recovered = FakeUnauthorized([]), FakeResponse([])
    responses = [unauthorized, recovered]
    client.list_workflows()
    assert token_reads == [1, 1]  # 401 dropped the cache and re-read
    assert recovered.auth_header == "Bearer token-2"  # type: ignore[attr-defined]


def test_env_workspace_outranks_marker_and_profile(tmp_path, monkeypatch) -> None:
    """REBASE_WORKSPACE is a deliberate act; the directory marker is ambient."""
    config = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config))
    write_profile(api_key="rbw_key", workspace={"id": "agent-work"}, path=config)
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    monkeypatch.chdir(repo)
    _mark(repo, "rebase-grid")
    monkeypatch.setenv("REBASE_WORKSPACE", "other-workspace")

    assert rb.Client().workspace_id == "other-workspace"


def test_deliberate_workspace_override_drops_the_profile_api_key(tmp_path, monkeypatch) -> None:
    """An rbw_ key is minted for one workspace, and the server reads the workspace
    off the key row rather than the header -- so carrying it to another workspace
    would quietly act on the wrong one. Fall through to the signed-in session.
    """
    config = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config))
    write_profile(api_key="rbw_key", workspace={"id": "agent-work"}, path=config)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REBASE_WORKSPACE", "other-workspace")

    client = rb.Client()

    assert client.workspace_id == "other-workspace"
    assert client.api_key is None


def test_override_matching_the_profile_workspace_keeps_the_key(tmp_path, monkeypatch) -> None:
    config = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config))
    write_profile(api_key="rbw_key", workspace={"id": "agent-work"}, path=config)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REBASE_WORKSPACE", "agent-work")

    assert rb.Client().api_key == "rbw_key"


def test_with_workspace_is_process_local_and_drops_the_profile_key(tmp_path, monkeypatch) -> None:
    """The clone is how the TUI switches workspace: nothing reaches the profile,
    and an rbw_ key stays behind for the same reason a `--workspace` override
    leaves it -- it names one workspace, and the server believes the key.
    """
    config = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config))
    write_profile(api_key="rbw_key", workspace={"id": "agent-work"}, path=config)
    set_active_environment("rebase-grid", "prod", path=config)
    monkeypatch.chdir(tmp_path)
    before = config.read_text(encoding="utf-8")

    client = rb.Client()
    moved = client.with_workspace("rebase-grid")

    assert moved.workspace_id == "rebase-grid"
    assert moved.api_key is None
    assert moved.api_url == client.api_url
    # The new workspace's own configured default, not the old client's environment.
    assert moved.environment_name == "prod"
    assert client.with_workspace("rebase-grid", environment_name="staging").environment_name == "staging"
    # Same workspace: the key is still the right credential.
    assert client.with_workspace("agent-work").api_key == "rbw_key"
    assert client.workspace_id == "agent-work"
    assert config.read_text(encoding="utf-8") == before


def test_as_session_steps_off_the_api_key_onto_the_signed_in_session(tmp_path, monkeypatch) -> None:
    config = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config))
    write_profile(api_key="rbw_key", workspace={"id": "agent-work"}, path=config)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("rebase.client.load_access_token", lambda: "session-token")

    client = rb.Client()
    person = client.as_session()

    assert person is not None and person is not client
    assert person.api_key is None
    assert person.access_token == "session-token"
    assert person.workspace_id == "agent-work"
    assert person.api_url == client.api_url
    # Already a person: nothing to step off.
    assert person.as_session() is person

    monkeypatch.setattr("rebase.client.load_access_token", lambda: None)
    assert client.as_session() is None


def test_explicit_api_key_survives_a_workspace_override(tmp_path, monkeypatch) -> None:
    config = tmp_path / "config.json"
    monkeypatch.setenv("REBASE_CONFIG_PATH", str(config))
    write_profile(api_key="rbw_key", workspace={"id": "agent-work"}, path=config)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REBASE_WORKSPACE", "other-workspace")

    assert rb.Client(api_key="rb_explicit").api_key == "rb_explicit"


def test_register_workflow_sends_cpu_and_memory(monkeypatch) -> None:
    """The workflow's own container size has to reach the wire.

    It previously could not: `resources=` was handed to steps only, and
    register/update_workflow had no cpu/memory parameter at all, so a stepless
    job workflow had no way to ask for more than the backend default.
    """
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed.update(kwargs["json"])
        return FakeResponse({"id": "workflow-id", "name": "sync"})

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    client.register_workflow(name="sync", source_code="def sync(): pass", cloud_run_memory="2Gi", cloud_run_cpu="1")

    assert observed["cloud_run_cpu"] == "1"
    assert observed["cloud_run_memory"] == "2Gi"


def test_update_workflow_can_clear_cpu_and_memory(monkeypatch) -> None:
    """None must survive the drop-None filter, or removing `memory=` is a no-op."""
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed.clear()
        observed.update(kwargs["json"])
        return FakeResponse({"id": "workflow-id", "name": "sync"})

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    client.update_workflow("workflow-id", cloud_run_cpu=None, cloud_run_memory=None)
    assert observed["cloud_run_cpu"] is None
    assert observed["cloud_run_memory"] is None

    # Not passing them at all leaves the stored values alone.
    client.update_workflow("workflow-id", source_code="def sync(): pass")
    assert "cloud_run_cpu" not in observed
    assert "cloud_run_memory" not in observed


def test_admin_client_methods_address_the_workspace_in_the_path(monkeypatch) -> None:
    """Admin routes are profile-gated and cross-tenant, so the target rides in the
    path and X-Rebase-Workspace is irrelevant -- whatever this client is configured for."""
    calls: list[tuple[str, str, Any]] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        calls.append((method, url, kwargs.get("json")))
        return FakeResponse([] if url.endswith("/admin/workspaces") else {"workspace_id": "acme"})

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com", workspace_id="somewhere-else")

    client.list_admin_workspaces(limit=25)
    client.get_admin_workspace_usage("acme")
    client.update_admin_compute_policy("acme", max_cloud_run_memory_mib=4096)
    client.update_admin_credit_grant("acme", monthly_credit_cents=500)

    assert [(m, u.removeprefix("https://workflows.example.com"), j) for m, u, j in calls] == [
        ("GET", "/admin/workspaces", None),
        ("GET", "/admin/workspaces/acme/usage", None),
        ("PATCH", "/admin/workspaces/acme/compute-policy", {"max_cloud_run_memory_mib": 4096}),
        ("PATCH", "/admin/workspaces/acme/credit-grant", {"monthly_credit_cents": 500}),
    ]


# --- run lists: summaries by default, bodies on request ----------------------


def test_client_list_runs_include_asks_for_the_bodies(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed["params"] = kwargs["params"]
        return FakeResponse([{"id": "run-id", "status": "succeeded", "result": {"ok": True}}])

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    client.list_runs(include=["result", "parameters"])
    assert observed["params"] == {"limit": 100, "include": "result,parameters"}

    # A typo must not quietly come back as summaries.
    with pytest.raises(rb.RebaseWorkflowError, match="unknown include"):
        client.list_runs(include=["results"])


# --- composite reads: ETags and 304 ------------------------------------------


class FakeTaggedResponse(FakeResponse):
    """A 200 that carries an ETag, the way the overview routes answer."""

    status_code = 200

    def __init__(self, payload: dict[str, Any], etag: str) -> None:
        super().__init__(payload)
        self.headers = {"ETag": etag}


class FakeNotModifiedResponse:
    status_code = 304
    headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        raise ValueError("a 304 has no body")


def test_read_overview_stores_the_etag_and_revalidates_with_it(monkeypatch) -> None:
    """The TUI re-reads its view every few seconds; most of those reads change nothing."""
    from rebase.client import OverviewRead

    sent: list[str | None] = []
    answers: list[Any] = [
        FakeTaggedResponse({"environment": "dev", "projects": []}, 'W/"v1"'),
        FakeNotModifiedResponse(),
        FakeTaggedResponse({"environment": "dev", "projects": [{"id": "p1"}]}, 'W/"v2"'),
        FakeNotModifiedResponse(),
    ]

    def fake_request(method: str, url: str, **kwargs: Any) -> Any:
        sent.append(kwargs["headers"].get("If-None-Match"))
        return answers.pop(0)

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    first = client.read_workspace_overview()
    assert first == OverviewRead({"environment": "dev", "projects": []})
    assert not first.unchanged and not first.absent

    second = client.read_workspace_overview()
    assert second.unchanged and second.payload is None and not second.absent
    # A 304 is not a missing route: the next read still asks.
    assert client._absent_routes == set()

    third = client.read_workspace_overview()
    assert third.payload == {"environment": "dev", "projects": [{"id": "p1"}]}
    fourth = client.read_workspace_overview()
    assert fourth.unchanged

    assert sent == [None, 'W/"v1"', 'W/"v1"', 'W/"v2"']


def test_get_workspace_overview_is_never_conditional(monkeypatch) -> None:
    """None from `get_*` keeps meaning "absent"; only `read_*` can say "unchanged"."""
    sent: list[str | None] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> Any:
        sent.append(kwargs["headers"].get("If-None-Match"))
        return FakeTaggedResponse({"environment": "dev"}, 'W/"v1"')

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    assert client.read_workspace_overview() is not None
    assert client.get_workspace_overview() == {"environment": "dev"}
    assert client.read_workspace_overview(conditional=False).payload == {"environment": "dev"}

    assert sent == [None, None, None]


def test_the_etag_cache_does_not_travel_with_a_clone(monkeypatch) -> None:
    """A clone is what a workspace or environment switch produces: its screen is empty."""
    sent: list[tuple[str | None, str | None]] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> Any:
        sent.append((kwargs["headers"].get("If-None-Match"), kwargs["headers"].get("X-Rebase-Environment")))
        return FakeTaggedResponse({"environment": "dev"}, 'W/"v1"')

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    client.read_workspace_overview()
    client.with_environment("prod").read_workspace_overview()
    client.read_workspace_overview()

    assert sent == [(None, "dev"), (None, "prod"), ('W/"v1"', "dev")]


def test_project_overview_etags_are_per_project(monkeypatch) -> None:
    sent: list[tuple[str, str | None]] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> Any:
        path = url.split("workflows.example.com")[-1]
        sent.append((path, kwargs["headers"].get("If-None-Match")))
        return FakeTaggedResponse({"project": {"id": path.split("/")[2]}}, f'W/"{path}"')

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    client.read_project_overview("p1")
    client.read_project_overview("p2")
    client.read_project_overview("p1")

    assert sent == [
        ("/projects/p1/overview", None),
        ("/projects/p2/overview", None),
        ("/projects/p1/overview", 'W/"/projects/p1/overview"'),
    ]


def test_register_and_update_workflow_send_timeout_seconds(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        observed.clear()
        observed.update(kwargs["json"])
        return FakeResponse({"id": "workflow-id", "name": "sync"})

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")

    client.register_workflow(name="sync", source_code="def sync(): pass", timeout_seconds=900)
    assert observed["timeout_seconds"] == 900

    # Omitted on register still sends the key (None = derive from the steps).
    client.register_workflow(name="sync", source_code="def sync(): pass")
    assert observed["timeout_seconds"] is None

    # On update, None must survive the drop-None filter so removing
    # `timeout_seconds=` really hands the bound back to the derivation.
    client.update_workflow("workflow-id", timeout_seconds=None)
    assert observed["timeout_seconds"] is None
    client.update_workflow("workflow-id", timeout_seconds=1200)
    assert observed["timeout_seconds"] == 1200
    client.update_workflow("workflow-id", source_code="def sync(): pass")
    assert "timeout_seconds" not in observed


def test_workflow_deploy_refuses_a_step_longer_than_the_workflow_before_any_request(monkeypatch) -> None:
    """The server checks the same thing; catching it here names both numbers in the user's file."""
    project = rb.project("timeouts")

    @project.step(timeout_seconds=2100)
    def fetch() -> dict:
        return {}

    @project.workflow(timeout_seconds=900)
    def collect() -> dict:
        return fetch()

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        raise AssertionError("no request should be made")

    patch_client_http(monkeypatch, fake_request)
    collect.client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    with pytest.raises(rb.RebaseWorkflowError, match=r"'fetch' declares timeout_seconds=2100.*timeout_seconds=900"):
        collect.deploy()


def test_workflow_and_step_reject_non_positive_timeouts() -> None:
    with pytest.raises(ValueError, match="timeout_seconds must be greater than or equal to 1"):
        rb.workflow(lambda: None, name="w", timeout_seconds=0)
    with pytest.raises(ValueError, match="timeout_seconds must be greater than 0"):
        rb.step(lambda: None, name="s", timeout_seconds=0)


def test_workflow_reads_back_the_effective_timeout_after_deploy(monkeypatch) -> None:
    project = rb.project("timeouts")

    @project.step(timeout_seconds=120)
    def fetch() -> dict:
        return {}

    @project.workflow()
    def collect() -> dict:
        return fetch()

    responses = {
        "POST /functions": {"id": "fn-id", "name": "fetch", "current_version_id": "fv-id"},
        "POST /workflows": {
            "id": "workflow-id",
            "name": "collect",
            "timeout_seconds": None,
            "effective_timeout_seconds": 180,
            "timeout_source": "derived",
            "timeout_note": None,
        },
    }

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeResponse:
        path = url.split("workflows.example.com", 1)[-1].split("?", 1)[0]
        if method == "GET":
            return FakeResponse([])
        for key, body in responses.items():
            if path.endswith(key.split(" ", 1)[1]) and method == key.split(" ", 1)[0]:
                return FakeResponse(body)
        return FakeResponse({"id": "any-id", "name": "any", "current_version_id": "v-id"})

    patch_client_http(monkeypatch, fake_request)
    collect.client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    collect.deploy()
    assert collect.effective_timeout_seconds == 180
    assert collect.timeout_source == "derived"
