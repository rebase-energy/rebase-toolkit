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

import importlib
import inspect
import os
import sys
import threading
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

#: Set by the runner around a whole run, and around each step of it.
RUN_ID_ENV = "REBASE_RUN_ID"
STEP_RUN_ID_ENV = "REBASE_STEP_RUN_ID"
TASK_ID_ENV = "REBASE_TASK_ID"
API_URL_ENV = "REBASE_WORKFLOWS_API_URL"
API_KEY_ENV = "REBASE_API_KEY"


@dataclass(frozen=True)
class RunContext:
    """The run, and the step of it, that this code is executing inside."""

    run_id: str
    step_run_id: str | None = None
    task_id: str | None = None


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
    task_id: str | None = None,
    *,
    api_url: str | None = None,
    api_key: str | None = None,
) -> Iterator[None]:
    """Activate request-local run identity for a user-code invocation.

    Cloud Run services can execute concurrent requests in one process, so their
    identity must not be stored in process-global environment variables. Jobs keep
    using the environment fallback because one process belongs to one run there.
    """
    run_token = _run_context.set(
        RunContext(run_id=str(run_id), step_run_id=step_run_id or None, task_id=task_id or None)
    )
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
    task_id = os.environ.get(TASK_ID_ENV)
    return RunContext(run_id=run_id, step_run_id=step_run_id or None, task_id=task_id or None)


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


# --- worker state: what a setup hook returned --------------------------------
#
# On the platform the warm pool runs a function's or step's setup hook once per
# worker process and keeps the value in the backend's `app.pool.handlers`, which
# every pool worker has imported. When the SDK runs the function itself (`rebase
# run --local`, or calling the decorated function), it plays that part: the hook
# runs once per process, on the first call that asks for the state.

_POOL_HANDLERS = "app.pool.handlers"


@dataclass(frozen=True)
class _LocalCall:
    """A function or step the SDK is running in this process, and its setup hook."""

    name: str
    setup: Callable[[], Any] | None


_local_call_context: ContextVar[_LocalCall | None] = ContextVar("rebase_local_call", default=None)
_worker_states: dict[Callable[[], Any], Any] = {}
_worker_states_lock = threading.RLock()
_running_setups: set[Callable[[], Any]] = set()


@contextmanager
def _local_call(name: str, setup: Callable[[], Any] | None) -> Iterator[None]:
    outer = _local_call_context.get()
    if setup is None and outer is not None and outer.setup is not None:
        # A call with no hook of its own, made from one that has a hook, runs in
        # that call's process, as it would in a pool worker: it sees that state.
        yield
        return
    token = _local_call_context.set(_LocalCall(name, setup))
    try:
        yield
    finally:
        _local_call_context.reset(token)


def _call_locally(
    name: str, setup: Callable[[], Any] | None, fn: Callable[..., Any], /, *args: Any, **kwargs: Any
) -> Any:
    """Call `fn` the way a worker would: `worker_state()` inside it answers from `setup`."""
    with _local_call(name, setup):
        result = fn(*args, **kwargs)
    if inspect.iscoroutine(result):
        # The body of an async function runs when awaited, after the scope above.
        return _await_in_local_call(name, setup, result)
    return result


async def _await_in_local_call(name: str, setup: Callable[[], Any] | None, coroutine: Coroutine[Any, Any, Any]) -> Any:
    with _local_call(name, setup):
        return await coroutine


def _pool_handlers() -> Any | None:
    """The backend's pool handler module, when this process can import it."""
    module = sys.modules.get(_POOL_HANDLERS)
    if module is None:
        try:
            module = importlib.import_module(_POOL_HANDLERS)
        except ModuleNotFoundError as exc:
            if exc.name in {"app", "app.pool", _POOL_HANDLERS}:
                return None
            raise
    return module if callable(getattr(module, "worker_state", None)) else None


def _local_worker_state(setup: Callable[[], Any]) -> Any:
    with _worker_states_lock:
        if setup in _worker_states:
            return _worker_states[setup]
        if setup in _running_setups:
            from rebase.client import RebaseWorkflowError

            raise RebaseWorkflowError(
                f"setup hook {getattr(setup, '__name__', setup)!r} called rb.worker_state() "
                "before it returned its state"
            )
        _running_setups.add(setup)
        try:
            # Not cached when it raises: a hook may raise to say "not ready yet".
            value = setup()
        finally:
            _running_setups.discard(setup)
        _worker_states[setup] = value
        return value


def worker_state() -> Any:
    """What the setup hook of the running function or step returned.

    Declare the hook with ``setup=`` on ``@project.function(...)`` or
    ``@project.step(...)``: a module-level function taking no arguments that
    prepares what every call can share (imports, read-only data, a loaded model).
    It runs once per worker process, not once per call, so treat the state as
    read-only and per process. Inside the platform's warm pool the worker
    provides the value; when the SDK runs the function itself (``rebase run
    --local``, or calling the decorated function) the hook runs on the first call
    that asks, once per process. Raises :class:`RebaseWorkflowError` when the
    running function declares no setup hook.
    """
    local = _local_call_context.get()
    if local is not None and local.setup is not None:
        return _local_worker_state(local.setup)
    handlers = _pool_handlers()
    if handlers is not None:
        return handlers.worker_state()
    from rebase.client import RebaseWorkflowError

    if local is not None:
        raise RebaseWorkflowError(
            f"{local.name!r} declares no setup hook, so rb.worker_state() has nothing to return; "
            "declare one with setup=... on its decorator"
        )
    raise RebaseWorkflowError(
        "rb.worker_state() has no setup hook to answer from: call it inside a function or step declared with setup=..."
    )
