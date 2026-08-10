"""What a running workflow knows about itself.

The runner injects the ids of the run, and of the step inside it, into the
container's environment. That is the only thing connecting a `Function.map`
issued from inside a step back to the step that issued it: without it the
platform records a batch belonging to nothing and the run has no tasks to list.

Reading the environment on each call rather than caching it is deliberate — the
steps of one run share a process, so the step id changes underneath user code
between steps and a value captured at import would name the wrong one.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

#: Set by the runner around a whole run, and around each step of it.
RUN_ID_ENV = "REBASE_RUN_ID"
STEP_RUN_ID_ENV = "REBASE_STEP_RUN_ID"
API_URL_ENV = "REBASE_WORKFLOWS_API_URL"
API_KEY_ENV = "REBASE_API_KEY"


@dataclass(frozen=True)
class RunContext:
    """The run, and the step of it, that this code is executing inside."""

    run_id: str
    step_run_id: str | None = None


@dataclass(frozen=True)
class _ReportingContext:
    api_url: str
    api_key: str


_run_context: ContextVar[RunContext | None] = ContextVar("rebase_run_context", default=None)
_reporting_context: ContextVar[_ReportingContext | None] = ContextVar("rebase_reporting_context", default=None)


@contextmanager
def _activate_run_context(
    run_id: str,
    step_run_id: str | None = None,
    *,
    api_url: str | None = None,
    api_key: str | None = None,
) -> Iterator[None]:
    """Activate request-local run identity for a user-code invocation.

    Cloud Run services can execute concurrent requests in one process, so their
    identity must not be stored in process-global environment variables. Jobs keep
    using the environment fallback because one process belongs to one run there.
    """
    run_token = _run_context.set(RunContext(run_id=str(run_id), step_run_id=step_run_id or None))
    reporting_token = None
    if api_url and api_key:
        reporting_token = _reporting_context.set(_ReportingContext(api_url=api_url, api_key=api_key))
    try:
        yield
    finally:
        if reporting_token is not None:
            _reporting_context.reset(reporting_token)
        _run_context.reset(run_token)


def current_run() -> RunContext | None:
    """The run this code is part of, or None when it is not running on the platform.

    None is the ordinary answer on a laptop: a `Function.map` from a shell belongs
    to no run, and the platform accepts an unattributed batch for exactly that case.
    """
    active = _run_context.get()
    if active is not None:
        return active
    run_id = os.environ.get(RUN_ID_ENV)
    if not run_id:
        return None
    step_run_id = os.environ.get(STEP_RUN_ID_ENV)
    return RunContext(run_id=run_id, step_run_id=step_run_id or None)


def _current_reporting_context() -> _ReportingContext | None:
    """Credentials for reporting against the active run, if the runtime supplied them."""
    active = _reporting_context.get()
    if active is not None:
        return active
    if current_run() is None:
        return None
    api_url = os.environ.get(API_URL_ENV)
    api_key = os.environ.get(API_KEY_ENV)
    if not api_url or not api_key:
        return None
    return _ReportingContext(api_url=api_url, api_key=api_key)
