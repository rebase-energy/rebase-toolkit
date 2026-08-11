"""Inline task reporting for code executing inside a Rebase run."""

from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar, Token
from types import TracebackType
from typing import Any, Self
from uuid import uuid4

from rebase.client import Client, RebaseWorkflowError
from rebase.runtime import _current_reporting_context, current_run


class TaskReportingError(RebaseWorkflowError):
    """The platform could not create or finalize a requested task report."""


_active_task_id: ContextVar[str | None] = ContextVar("rebase_active_task_id", default=None)


def _current_task_id() -> str | None:
    """The innermost active inline task, when one has a platform identity."""
    return _active_task_id.get()


class Task:
    """A lightweight unit of work recorded on the current run.

    The body executes in the caller's process. This object reports lifecycle only;
    it does not allocate compute, retry work, or suppress workload exceptions.
    """

    def __init__(
        self,
        name: str,
        *,
        key: str | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("task name must be a non-empty string")
        if key is not None and not isinstance(key, str):
            raise TypeError("task key must be a string or None")
        self.name = name.strip()
        self.key = key
        self.parameters = dict(parameters or {})
        self._validate_json_object(self.parameters, field="parameters")
        self.client_token = str(uuid4())
        self.id: str | None = None
        self.status = "queued"
        self.result: dict[str, Any] | None = None
        self.error: str | None = None
        self.error_type: str | None = None
        self._entered = False
        self._finished = False
        self._client: Client | None = None
        self._run_id: str | None = None
        self._task_context_token: Token[str | None] | None = None

    @staticmethod
    def _validate_json_object(value: dict[str, Any], *, field: str) -> None:
        try:
            json.dumps(value)
        except (TypeError, ValueError) as exc:
            raise TypeError(f"task {field} must be JSON-serializable") from exc

    def set_result(self, result: dict[str, Any] | None) -> None:
        if not self._entered or self._finished:
            raise RuntimeError("task result can only be set inside its active context")
        value = dict(result or {})
        self._validate_json_object(value, field="result")
        self.result = value

    def _start(self) -> None:
        if self._entered:
            raise RuntimeError("a Task context cannot be entered more than once")
        self._entered = True
        self.status = "running"
        run = current_run()
        if run is None:
            return
        reporting = _current_reporting_context()
        if reporting is None:
            raise TaskReportingError(f"run {run.run_id} has no task-reporting credential")
        self._run_id = run.run_id
        self._client = Client(api_key=reporting.api_key, api_url=reporting.api_url)
        payload: dict[str, Any] = {
            "name": self.name,
            "key": self.key,
            "parameters": self.parameters,
            "client_token": self.client_token,
        }
        if run.step_run_id is not None:
            payload["step_run_id"] = run.step_run_id
        try:
            created = self._client.create_run_task(run.run_id, payload)
        except Exception as exc:
            raise TaskReportingError(f"could not start task {self.name!r}: {exc}") from exc
        self.id = str(created["id"])
        self.status = str(created.get("status") or "running")

    def _finish(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
    ) -> None:
        if self._finished:
            return
        self._finished = True
        if exc is None:
            self.status = "succeeded"
            payload: dict[str, Any] = {"status": "succeeded", "result": self.result}
        else:
            self.status = "failed"
            self.result = None
            self.error_type = exc_type.__name__ if exc_type is not None else type(exc).__name__
            self.error = str(exc)
            payload = {"status": "failed", "error_type": self.error_type, "error": self.error}
        if self._client is None or self._run_id is None:
            return
        if self.id is None:
            raise TaskReportingError(f"task {self.name!r} has no platform id")
        try:
            completed = self._client.complete_run_task(self._run_id, self.id, payload)
        except Exception as report_exc:
            message = f"could not finalize task {self.name!r}: {report_exc}"
            if exc is not None:
                message += f"; workload raised {type(exc).__name__}: {exc}"
            raise TaskReportingError(message) from exc
        self.status = str(completed.get("status") or self.status)

    def __enter__(self) -> Self:
        self._start()
        self._task_context_token = _active_task_id.set(self.id)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del traceback
        try:
            self._finish(exc_type, exc)
        finally:
            if self._task_context_token is not None:
                _active_task_id.reset(self._task_context_token)
                self._task_context_token = None
        return False

    async def __aenter__(self) -> Self:
        await asyncio.to_thread(self._start)
        self._task_context_token = _active_task_id.set(self.id)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del traceback
        try:
            await asyncio.to_thread(self._finish, exc_type, exc)
        finally:
            if self._task_context_token is not None:
                _active_task_id.reset(self._task_context_token)
                self._task_context_token = None
        return False


def task(
    name: str,
    *,
    key: str | None = None,
    parameters: dict[str, Any] | None = None,
) -> Task:
    """Create an inline task context attached to the current hosted run."""
    return Task(name, key=key, parameters=parameters)
