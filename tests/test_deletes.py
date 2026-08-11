"""Client-side deletes for projects, functions and workflows.

These endpoints answer 204 with an empty body, so they go through
``request_no_content`` rather than ``request`` (which always parses JSON).
The other thing worth pinning down is that the API's 409 refusal is rendered
as something a human can act on rather than a raw JSON blob.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests
from http_stub import patch_client_http

import rebase as rb
from rebase.client import RebaseWorkflowError


class FakeNoContentResponse:
    """A 204: raise_for_status passes, and .json() would blow up."""

    text = ""

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        raise ValueError("204 responses have no body")


class FakeJsonResponse:
    """A 200 with a body, which the batch delete answers with."""

    text = ""
    status_code = 200

    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


class FakeConflictResponse:
    text = "conflict"

    def __init__(self, detail: Any, *, status_code: int = 409) -> None:
        self._detail = detail
        self.status_code = status_code

    def raise_for_status(self) -> None:
        raise requests.HTTPError(str(self.status_code))

    def json(self) -> Any:
        return {"detail": self._detail}


def _client() -> rb.Client:
    return rb.Client(api_key="rbw_test", api_url="https://toolkit.example.com")


@pytest.mark.parametrize(
    ("method_name", "path"),
    [
        ("delete_project", "/projects/p1"),
        ("delete_function", "/functions/p1"),
        ("delete_workflow", "/workflows/p1"),
    ],
)
def test_delete_issues_delete_and_tolerates_empty_body(monkeypatch, method_name: str, path: str) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeNoContentResponse:
        observed["method"] = method
        observed["url"] = url
        observed["params"] = kwargs.get("params")
        return FakeNoContentResponse()

    patch_client_http(monkeypatch, fake_request)

    assert getattr(_client(), method_name)("p1") is None
    assert observed["method"] == "DELETE"
    assert observed["url"] == f"https://toolkit.example.com{path}"
    assert observed["params"] == {"force": "false"}


def test_force_is_passed_through(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeNoContentResponse:
        observed["params"] = kwargs.get("params")
        return FakeNoContentResponse()

    patch_client_http(monkeypatch, fake_request)
    _client().delete_project("p1", force=True)
    assert observed["params"] == {"force": "true"}


def test_conflict_reports_what_the_project_still_holds(monkeypatch) -> None:
    detail = {
        "message": "project is not empty; retry with force=true to delete it and its contents",
        "contents": {"functions": 2, "runs": 17},
    }
    patch_client_http(monkeypatch, lambda *a, **k: FakeConflictResponse(detail))

    with pytest.raises(RebaseWorkflowError) as excinfo:
        _client().delete_project("p1")

    message = str(excinfo.value)
    assert "force=true" in message
    assert "contains 2 functions, 17 runs" in message


class TestBatchDeleteProjects:
    """`delete_projects` is one request that reports per project rather than raising."""

    def _fake_batch(self, monkeypatch, payload: Any, observed: dict[str, Any]):
        def fake_request(method: str, url: str, **kwargs: Any) -> FakeJsonResponse:
            observed["method"] = method
            observed["url"] = url
            observed["json"] = kwargs.get("json")
            return FakeJsonResponse(payload)

        patch_client_http(monkeypatch, fake_request)

    def test_one_request_carries_every_id(self, monkeypatch) -> None:
        observed: dict[str, Any] = {}
        self._fake_batch(monkeypatch, {"deleted": ["p1", "p2"], "failed": []}, observed)

        assert _client().delete_projects(["p1", "p2"], force=True) == []
        assert observed["method"] == "POST"
        assert observed["url"] == "https://toolkit.example.com/projects/batch-delete"
        assert observed["json"] == {"project_ids": ["p1", "p2"], "force": True}

    def test_failures_come_back_readable_instead_of_raising(self, monkeypatch) -> None:
        payload = {
            "deleted": ["p1"],
            "failed": [
                {
                    "project_id": "p2",
                    "status": 409,
                    "detail": {"message": "project is not empty", "contents": {"runs": 3}},
                }
            ],
        }
        self._fake_batch(monkeypatch, payload, {})

        failures = _client().delete_projects(["p1", "p2"])

        assert [project_id for project_id, _ in failures] == ["p2"]
        assert "project is not empty" in failures[0][1]
        assert "runs" in failures[0][1]

    def test_an_empty_list_makes_no_request(self, monkeypatch) -> None:
        def explode(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("no request should be made")

        patch_client_http(monkeypatch, explode)
        assert _client().delete_projects([]) == []

    # 404 is the obvious way for an API to say "no such route". 405 is what it actually
    # says: without the batch route, `/projects/batch-delete` is matched by
    # `/projects/{project_id}`, so the path resolves and only the method is refused.
    @pytest.mark.parametrize(("status", "detail"), [(404, "Not Found"), (405, "Method Not Allowed")])
    def test_an_api_without_the_route_falls_back_to_one_by_one(self, monkeypatch, status, detail) -> None:
        """A toolkit ahead of its platform must still delete."""
        calls: list[tuple[str, str]] = []

        def fake_request(method: str, url: str, **_kwargs: Any) -> Any:
            calls.append((method, url))
            if method == "POST":
                return FakeConflictResponse(detail, status_code=status)
            return FakeNoContentResponse()

        patch_client_http(monkeypatch, fake_request)

        assert _client().delete_projects(["p1", "p2"], force=True) == []
        assert calls == [
            ("POST", "https://toolkit.example.com/projects/batch-delete"),
            ("DELETE", "https://toolkit.example.com/projects/p1"),
            ("DELETE", "https://toolkit.example.com/projects/p2"),
        ]

    def test_more_than_the_api_ceiling_is_sent_in_chunks(self, monkeypatch) -> None:
        """The API rejects an oversized list, so a long selection must not become one 422."""
        sent: list[list[str]] = []

        def fake_request(method: str, url: str, **kwargs: Any) -> FakeJsonResponse:
            sent.append(kwargs["json"]["project_ids"])
            return FakeJsonResponse({"deleted": kwargs["json"]["project_ids"], "failed": []})

        patch_client_http(monkeypatch, fake_request)
        ids = [f"p{index}" for index in range(250)]

        assert _client().delete_projects(ids) == []
        assert [len(chunk) for chunk in sent] == [100, 100, 50]
        assert [project_id for chunk in sent for project_id in chunk] == ids

    def test_other_errors_are_not_swallowed_by_the_fallback(self, monkeypatch) -> None:
        patch_client_http(monkeypatch, lambda *a, **k: FakeConflictResponse("nope", status_code=403))

        with pytest.raises(RebaseWorkflowError, match="nope"):
            _client().delete_projects(["p1"])


def test_plain_string_detail_still_surfaces(monkeypatch) -> None:
    patch_client_http(monkeypatch, lambda *a, **k: FakeConflictResponse("workflow not found"))

    with pytest.raises(RebaseWorkflowError, match="workflow not found"):
        _client().delete_workflow("w1")
