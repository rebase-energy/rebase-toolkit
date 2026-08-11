from __future__ import annotations

from typing import Any

import pytest

import rebase as rb
from rebase.client import Bucket, Client, RebaseWorkflowError, _resolve_buckets_payload


class FakeResponse:
    def __init__(self, payload: Any = None, *, status_code: int = 200, content: bytes = b"") -> None:
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.text = ""
        self.content = content

    def json(self) -> Any:
        return self._payload


def test_bucket_is_provider_neutral_and_exported() -> None:
    bucket = rb.Bucket("power-system-data")

    assert isinstance(bucket, Bucket)
    assert repr(bucket) == "Bucket('power-system-data')"
    assert not hasattr(bucket, "uri")
    assert "Bucket" in rb.__all__


def test_bucket_object_io_uses_rebase_capabilities(monkeypatch) -> None:
    client = Client(api_key="rb_run", api_url="https://api.example")
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(method: str, path: str, **kwargs: Any) -> Any:
        calls.append((method, path, kwargs))
        if path.endswith("/signed-urls"):
            key = kwargs["json"]["paths"][0]
            return {"urls": [{"path": key, "url": f"https://capability/{key}"}]}
        if path.endswith("/objects/stat"):
            return {"path": "curve.json", "size": 7, "version": "42"}
        raise AssertionError(path)

    monkeypatch.setattr(client, "request", request)
    monkeypatch.setattr("requests.put", lambda *args, **kwargs: FakeResponse())
    monkeypatch.setattr("requests.get", lambda *args, **kwargs: FakeResponse(content=b"payload"))
    bucket = Bucket("power-system-data", client=client)

    assert bucket.put("curve.json", "payload", content_type="application/json") == "curve.json"
    assert bucket.get("curve.json") == b"payload"
    assert bucket.stat("curve.json")["version"] == "42"
    assert all("power-system-data" in path for _, path, _ in calls)


def test_bucket_attachment_payload_is_logical_and_unique() -> None:
    assert _resolve_buckets_payload([Bucket("power-system-data")]) == [{"bucket": "power-system-data"}]
    with pytest.raises(RebaseWorkflowError, match="unique"):
        _resolve_buckets_payload(["raw", "raw"])


def test_create_if_missing_uses_the_logical_create_route(monkeypatch) -> None:
    client = Client(api_key="rb_test", api_url="https://api.example")
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(method: str, path: str, **kwargs: Any) -> dict[str, str]:
        calls.append((method, path, kwargs))
        return {"name": "new-store"}

    monkeypatch.setattr(client, "request", request)
    value = Bucket("new-store", create_if_missing=True, client=client).ensure()

    assert value == {"name": "new-store"}
    assert calls == [("POST", "/buckets", {"json": {"name": "new-store"}})]


def test_workflow_keeps_bucket_attachment_until_deploy() -> None:
    @rb.workflow(buckets=[rb.Bucket("power-system-data")])
    def collector() -> dict[str, bool]:
        return {"ok": True}

    assert collector.buckets[0].name == "power-system-data"


def test_register_workflow_sends_logical_bucket_attachment(monkeypatch) -> None:
    client = Client(api_key="rb_test", api_url="https://api.example")
    observed: dict[str, Any] = {}
    monkeypatch.setattr(client, "ensure_project", lambda name: {"id": "project-id"})

    def request(method: str, path: str, **kwargs: Any) -> dict[str, str]:
        observed.update(kwargs["json"])
        return {"id": "workflow-id"}

    monkeypatch.setattr(client, "request", request)
    client.register_workflow(
        project="nordpool",
        name="collector",
        source_code="def collector(): pass",
        entrypoint="collector",
        buckets=[{"bucket": "power-system-data"}],
    )

    assert observed["buckets"] == [{"bucket": "power-system-data"}]
    assert "gs://" not in str(observed)
