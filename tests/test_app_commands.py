"""`rebase app` and the client calls behind it."""

from __future__ import annotations

from typing import Any

import pytest

import rebase as rb
from rebase.cli import main
from rebase.client import Client
from tests.http_stub import patch_client_http

SERVICE = "rebase-as-b0c55942d6fa493a98e15c09"
APP = {
    "id": "app-id",
    "workspace_id": "default",
    "project_id": "project-id",
    "name": "weather-api",
    "enabled": True,
    "url_path": "/e/ws/dev/weather",
    "cloud_run_service_name": SERVICE,
}
PROJECT = {"id": "project-id", "workspace_id": "default", "name": "weather"}
INSTANCES = {
    "asgi_app_id": "app-id",
    "service_name": SERVICE,
    "source": "cloud_run",
    "window_seconds": 120,
    "counts": {"source": "cloud_monitoring", "active": 1, "idle": 0, "as_of": "2026-10-23T16:47:00Z"},
    "instances": [
        {
            "instance_id": "0010dd86075d99695f9c1f96",
            "revision_name": f"{SERVICE}-00002-dtl",
            "status": "serving",
            "first_seen": "2026-10-23T15:50:20Z",
            "last_seen": "2026-10-23T16:16:51Z",
            "started_at": "2026-10-23T15:50:20Z",
            "ready_at": "2026-10-23T15:50:39Z",
            "stopped_at": None,
            "requests": 25,
        },
        {
            "instance_id": "0010dd8607b8aaaa",
            "revision_name": f"{SERVICE}-00002-dtl",
            "status": "starting",
            "first_seen": "2026-10-23T16:16:31Z",
            "last_seen": "2026-10-23T16:16:31Z",
            "started_at": "2026-10-23T16:16:31Z",
            "ready_at": None,
            "stopped_at": None,
            "requests": 0,
        },
    ],
    "events": [
        {
            "timestamp": "2026-10-23T16:16:31Z",
            "kind": "system",
            "severity": "INFO",
            "message": "Starting new instance. Reason: AUTOSCALING",
            "instance_id": "0010dd8607b8aaaa",
            "revision_name": f"{SERVICE}-00002-dtl",
        },
        {
            "timestamp": "2026-10-23T16:16:51Z",
            "kind": "request",
            "severity": "INFO",
            "message": "GET /v1/health -> 200 (0.004s)",
            "instance_id": "0010dd86075d99695f9c1f96",
            "revision_name": f"{SERVICE}-00002-dtl",
        },
    ],
    "next_since": "2026-10-23T16:16:51Z",
    "message": None,
}


class _Response:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = ""
        self.headers: dict[str, str] = {}

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        return None


def test_client_get_asgi_app_instances_sends_query_params(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[dict[str, Any]] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> _Response:
        observed.append(
            {"method": method, "url": url, "params": kwargs.get("params"), "headers": kwargs.get("headers")}
        )
        return _Response(INSTANCES)

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com", environment_name="dev")

    data = client.get_asgi_app_instances(
        "app-id", since="2026-10-23T16:00:00Z", window_seconds=60, limit=50, include_requests=False
    )

    assert data["service_name"] == SERVICE
    [call] = observed
    assert call["method"] == "GET"
    assert call["url"] == "https://workflows.example.com/asgi-apps/app-id/instances"
    assert call["params"] == {
        "since": "2026-10-23T16:00:00Z",
        "window_seconds": 60,
        "limit": 50,
        "include_requests": "false",
    }
    assert call["headers"]["X-Rebase-Environment"] == "dev"


def test_client_get_asgi_app_instances_defaults_send_no_params(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[Any] = []

    def fake_request(method: str, url: str, **kwargs: Any) -> _Response:
        observed.append(kwargs.get("params"))
        return _Response(INSTANCES)

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    client.get_asgi_app_instances("app-id")
    assert observed == [None]


def test_client_get_asgi_app(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(method: str, url: str, **kwargs: Any) -> _Response:
        assert (method, url) == ("GET", "https://workflows.example.com/asgi-apps/app-id")
        return _Response(APP)

    patch_client_http(monkeypatch, fake_request)
    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    assert client.get_asgi_app("app-id")["name"] == "weather-api"


@pytest.fixture
def stubbed_client(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    # Wide tables do not fit an 80-column capture; rich reads COLUMNS on every render.
    monkeypatch.setenv("COLUMNS", "200")
    calls: dict[str, Any] = {}
    monkeypatch.setattr(Client, "list_projects", lambda self, *a, **k: [PROJECT])

    def list_asgi_apps(self: Any, *, project: str | None = None, project_id: str | None = None) -> list[dict[str, Any]]:
        calls["list_project_id"] = project_id
        return [APP]

    def get_asgi_app_instances(self: Any, asgi_app_id: str, **kwargs: Any) -> dict[str, Any]:
        calls["instances"] = {"asgi_app_id": asgi_app_id, **kwargs}
        return INSTANCES

    monkeypatch.setattr(Client, "list_asgi_apps", list_asgi_apps)
    monkeypatch.setattr(Client, "get_asgi_app", lambda self, asgi_app_id: {**APP, "id": asgi_app_id})
    monkeypatch.setattr(Client, "get_asgi_app_instances", get_asgi_app_instances)
    return calls


def test_app_list_resolves_the_project_without_creating_it(stubbed_client: dict[str, Any], capsys: Any) -> None:
    assert main(["app", "list", "--project", "weather"]) == 0
    out = capsys.readouterr().out
    assert "weather-api" in out and SERVICE in out
    assert stubbed_client["list_project_id"] == "project-id"


def test_app_list_json(stubbed_client: dict[str, Any], capsys: Any) -> None:
    assert main(["app", "list", "-p", "weather", "--json"]) == 0
    assert '"weather-api"' in capsys.readouterr().out


def test_app_get_by_name_and_by_id(stubbed_client: dict[str, Any], capsys: Any) -> None:
    assert main(["app", "get", "weather-api", "--project", "weather"]) == 0
    assert SERVICE in capsys.readouterr().out
    assert main(["app", "get", "--id", "other-id", "--json"]) == 0
    assert '"other-id"' in capsys.readouterr().out


def test_app_get_by_name_requires_project(stubbed_client: dict[str, Any], capsys: Any) -> None:
    assert main(["app", "get", "weather-api"]) == 1
    assert "--project is required" in capsys.readouterr().err


def test_app_instances_renders_counts_instances_and_events(stubbed_client: dict[str, Any], capsys: Any) -> None:
    assert main(["app", "instances", "weather-api", "-p", "weather", "--window", "60", "--no-requests"]) == 0
    out = capsys.readouterr().out
    assert "1 active" in out and "0 idle" in out
    assert "0010dd86075d" in out and "00002-dtl" in out and "serving" in out and "19s" in out and "25" in out
    assert "0010dd8607b8" in out and "starting" in out
    assert "Starting new instance" in out and "GET /v1/health -> 200" in out
    assert stubbed_client["instances"] == {
        "asgi_app_id": "app-id",
        "since": None,
        "window_seconds": 60,
        "include_requests": False,
    }


def test_app_instances_json(stubbed_client: dict[str, Any], capsys: Any) -> None:
    assert main(["app", "instances", "--id", "app-id", "--json"]) == 0
    out = capsys.readouterr().out
    assert '"service_name"' in out and SERVICE in out


def test_app_instances_explains_unknown_counts(
    stubbed_client: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    degraded = {
        **INSTANCES,
        "counts": {"source": "none", "message": "Instance counts are unavailable: 403"},
        "instances": [],
        "events": [],
        "message": "Container events are unavailable: 429",
    }
    monkeypatch.setattr(Client, "get_asgi_app_instances", lambda self, asgi_app_id, **kw: degraded)
    assert main(["app", "instances", "--id", "app-id"]) == 0
    out = capsys.readouterr().out
    assert "unknown" in out and "403" in out and "429" in out and "scaled to zero" in out


def test_app_instances_watch_polls_with_the_cursor(
    stubbed_client: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    seen_since: list[Any] = []
    polls = {"n": 0}

    def get_asgi_app_instances(self: Any, asgi_app_id: str, **kwargs: Any) -> dict[str, Any]:
        seen_since.append(kwargs.get("since"))
        polls["n"] += 1
        if polls["n"] == 3:
            raise KeyboardInterrupt
        return INSTANCES

    monkeypatch.setattr(Client, "get_asgi_app_instances", get_asgi_app_instances)
    monkeypatch.setattr("rebase.cli.time.sleep", lambda seconds: None)
    assert main(["app", "instances", "--id", "app-id", "--watch", "--poll-interval", "1"]) == 130
    assert seen_since == [None, "2026-10-23T16:16:51Z", "2026-10-23T16:16:51Z"]
