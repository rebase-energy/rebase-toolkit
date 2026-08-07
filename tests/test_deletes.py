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

import rebase as rb
from rebase.client import RebaseWorkflowError


class FakeNoContentResponse:
    """A 204: raise_for_status passes, and .json() would blow up."""

    text = ""

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        raise ValueError("204 responses have no body")


class FakeConflictResponse:
    text = "conflict"

    def __init__(self, detail: Any) -> None:
        self._detail = detail

    def raise_for_status(self) -> None:
        raise requests.HTTPError("409")

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

    monkeypatch.setattr("requests.request", fake_request)

    assert getattr(_client(), method_name)("p1") is None
    assert observed["method"] == "DELETE"
    assert observed["url"] == f"https://toolkit.example.com{path}"
    assert observed["params"] == {"force": "false"}


def test_force_is_passed_through(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_request(method: str, url: str, **kwargs: Any) -> FakeNoContentResponse:
        observed["params"] = kwargs.get("params")
        return FakeNoContentResponse()

    monkeypatch.setattr("requests.request", fake_request)
    _client().delete_project("p1", force=True)
    assert observed["params"] == {"force": "true"}


def test_conflict_reports_what_the_project_still_holds(monkeypatch) -> None:
    detail = {
        "message": "project is not empty; retry with force=true to delete it and its contents",
        "contents": {"functions": 2, "runs": 17},
    }
    monkeypatch.setattr("requests.request", lambda *a, **k: FakeConflictResponse(detail))

    with pytest.raises(RebaseWorkflowError) as excinfo:
        _client().delete_project("p1")

    message = str(excinfo.value)
    assert "force=true" in message
    assert "contains 2 functions, 17 runs" in message


def test_plain_string_detail_still_surfaces(monkeypatch) -> None:
    monkeypatch.setattr("requests.request", lambda *a, **k: FakeConflictResponse("workflow not found"))

    with pytest.raises(RebaseWorkflowError, match="workflow not found"):
        _client().delete_workflow("w1")
