from __future__ import annotations

from typing import Any

import pytest

import rebase as rb
from rebase.runtime import _activate_run_context


class RecordingClient:
    instances: list[RecordingClient] = []
    fail = False

    def __init__(self, *, api_key: str, api_url: str) -> None:
        self.api_key = api_key
        self.api_url = api_url
        self.created: list[tuple[str, dict[str, Any]]] = []
        type(self).instances.append(self)

    def create_run_artifact(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("registration unavailable")
        self.created.append((run_id, payload))
        return {"id": "artifact-1"}


@pytest.fixture(autouse=True)
def _recording_client(monkeypatch):
    RecordingClient.instances = []
    RecordingClient.fail = False
    monkeypatch.setattr("rebase.artifacts.Client", RecordingClient)


def test_artifact_is_a_validated_local_noop() -> None:
    artifact = rb.artifact(
        "curve",
        uri="gs://bucket/curve.json",
        key="curve",
        media_type="application/json",
        metadata={"points": 96},
    )

    assert artifact.id is None
    assert artifact.uri == "gs://bucket/curve.json"
    assert RecordingClient.instances == []

    with pytest.raises(ValueError, match="absolute URI"):
        rb.artifact("curve", uri="curve.json")
    with pytest.raises(TypeError, match="JSON-serializable"):
        rb.artifact("curve", uri="gs://bucket/curve.json", metadata={"bad": object()})


def test_hosted_artifact_reports_run_step_and_inline_task() -> None:
    from rebase.tasks import _active_task_id

    token = _active_task_id.set("task-1")
    try:
        with _activate_run_context(
            "run-1",
            "step-1",
            api_url="https://api.example",
            api_key="rb_run",
        ):
            artifact = rb.artifact(
                "curve",
                uri="gs://bucket/curve.json",
                key="curve",
                disposition="reused",
                size_bytes=42,
            )
    finally:
        _active_task_id.reset(token)

    assert artifact.id == "artifact-1"
    run_id, payload = RecordingClient.instances[0].created[0]
    assert run_id == "run-1"
    assert payload == {
        "name": "curve",
        "uri": "gs://bucket/curve.json",
        "key": "curve",
        "disposition": "reused",
        "media_type": None,
        "size_bytes": 42,
        "version": None,
        "digest": None,
        "metadata": {},
        "client_token": artifact.client_token,
        "step_run_id": "step-1",
        "task_id": "task-1",
    }


def test_mapped_artifact_uses_the_runtime_task_identity() -> None:
    with _activate_run_context(
        "child-run",
        task_id="map-task",
        api_url="https://api.example",
        api_key="rb_run",
    ):
        rb.artifact("curve", uri="gs://bucket/curve.json")

    assert RecordingClient.instances[0].created[0][1]["task_id"] == "map-task"


def test_hosted_artifact_reporting_is_strict() -> None:
    RecordingClient.fail = True
    with (
        _activate_run_context("run-1", api_url="https://api.example", api_key="rb_run"),
        pytest.raises(rb.ArtifactReportingError, match="could not register artifact"),
    ):
        rb.artifact("curve", uri="gs://bucket/curve.json")


def test_hosted_artifact_requires_a_reporting_credential() -> None:
    with (
        _activate_run_context("run-1"),
        pytest.raises(rb.ArtifactReportingError, match="no artifact-reporting credential"),
    ):
        rb.artifact("curve", uri="gs://bucket/curve.json")
