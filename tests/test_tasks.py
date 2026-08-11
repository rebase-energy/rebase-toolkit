from __future__ import annotations

import asyncio
from typing import Any

import pytest

import rebase as rb
from rebase.runtime import _activate_run_context
from rebase.tasks import _current_task_id


class RecordingClient:
    instances: list[RecordingClient] = []
    fail_start = False
    fail_finish = False

    def __init__(self, *, api_key: str, api_url: str) -> None:
        self.api_key = api_key
        self.api_url = api_url
        self.started: list[tuple[str, dict[str, Any]]] = []
        self.finished: list[tuple[str, str, dict[str, Any]]] = []
        type(self).instances.append(self)

    def create_run_task(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.fail_start:
            raise RuntimeError("start unavailable")
        self.started.append((run_id, payload))
        return {"id": "task-1", "status": "running"}

    def complete_run_task(self, run_id: str, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.fail_finish:
            raise RuntimeError("finish unavailable")
        self.finished.append((run_id, task_id, payload))
        return {"id": task_id, "status": payload["status"]}


@pytest.fixture(autouse=True)
def _recording_client(monkeypatch):
    RecordingClient.instances = []
    RecordingClient.fail_start = False
    RecordingClient.fail_finish = False
    monkeypatch.setattr("rebase.tasks.Client", RecordingClient)


def test_task_is_a_local_noop_with_normal_python_failure_semantics() -> None:
    with rb.task("local", parameters={"unit": 1}) as task:
        task.set_result({"outcome": "ok"})
    assert task.status == "succeeded"
    assert task.result == {"outcome": "ok"}
    assert RecordingClient.instances == []

    with pytest.raises(ValueError, match="boom"), rb.task("local-failure") as failed:
        raise ValueError("boom")
    assert failed.status == "failed"
    assert failed.error_type == "ValueError"
    assert failed.error == "boom"


def test_hosted_task_reports_success_with_run_and_step_identity() -> None:
    with (
        _activate_run_context("run-1", "step-1", api_url="https://api.example", api_key="rb_run"),
        rb.task("capture", key="2026-08-11/SE", parameters={"cluster": "SE"}) as task,
    ):
        task.set_result({"outcome": "captured"})

    client = RecordingClient.instances[0]
    assert client.started[0][0] == "run-1"
    assert client.started[0][1] == {
        "name": "capture",
        "key": "2026-08-11/SE",
        "parameters": {"cluster": "SE"},
        "client_token": task.client_token,
        "step_run_id": "step-1",
    }
    assert client.finished == [
        ("run-1", "task-1", {"status": "succeeded", "result": {"outcome": "captured"}})
    ]


def test_hosted_task_reports_failure_and_reraises() -> None:
    with (
        _activate_run_context("run-1", api_url="https://api.example", api_key="rb_run"),
        pytest.raises(LookupError, match="missing"),
        rb.task("capture"),
    ):
        raise LookupError("missing")

    assert RecordingClient.instances[0].finished == [
        (
            "run-1",
            "task-1",
            {"status": "failed", "error_type": "LookupError", "error": "missing"},
        )
    ]


def test_hosted_reporting_is_strict() -> None:
    RecordingClient.fail_start = True
    with (
        _activate_run_context("run-1", api_url="https://api.example", api_key="rb_run"),
        pytest.raises(rb.TaskReportingError, match="could not start"),
        rb.task("capture"),
    ):
        raise AssertionError("body must not execute")

    RecordingClient.fail_start = False
    RecordingClient.fail_finish = True
    with (
        _activate_run_context("run-1", api_url="https://api.example", api_key="rb_run"),
        pytest.raises(rb.TaskReportingError, match="workload raised ValueError: original") as excinfo,
        rb.task("capture"),
    ):
        raise ValueError("original")
    assert isinstance(excinfo.value.__cause__, ValueError)


def test_task_supports_async_context_and_contexts_do_not_leak() -> None:
    async def one(run_id: str) -> str:
        with _activate_run_context(run_id, api_url="https://api.example", api_key=f"key-{run_id}"):
            async with rb.task("async") as task:
                await asyncio.sleep(0)
                assert rb.current_run() == rb.RunContext(run_id=run_id)
                assert _current_task_id() == task.id
                task.set_result({"run": run_id})
            return task.status

    async def scenario() -> list[str]:
        return list(await asyncio.gather(one("run-a"), one("run-b")))

    assert asyncio.run(scenario()) == ["succeeded", "succeeded"]
    assert rb.current_run() is None
    assert _current_task_id() is None
