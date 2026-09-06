from __future__ import annotations

import ast
import contextvars
import dis
import hashlib
import inspect
import json
import logging
import os
import re
import subprocess
import tempfile
import textwrap
import time
import warnings
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from types import FunctionType
from typing import Any, Self

import requests

from rebase.auth import AuthError, load_access_token
from rebase.config import DEFAULT_SERVER_URL, active_environment, load_profile, local_workspace_id
from rebase.image import DEFAULT_PYTHON_VERSION, Image
from rebase.runtime import current_run
from rebase.source_bundle import SourceBundle, build_source_bundle

try:
    from emflow.models import Agent as _ImportedEmflowAgent
    from emflow.models import Model as _ImportedEmflowModel
    from emflow.models import Optimizer as _ImportedEmflowOptimizer
    from emflow.models import Predictor as _ImportedEmflowPredictor
    from emflow.models import Simulator as _ImportedEmflowSimulator
except ImportError:

    class _ImportedEmflowModel:
        def __init__(self, name: str | None = None) -> None:
            self.name = name

    class _ImportedEmflowPredictor(_ImportedEmflowModel):
        pass

    class _ImportedEmflowOptimizer(_ImportedEmflowModel):
        pass

    class _ImportedEmflowAgent(_ImportedEmflowModel):
        pass

    class _ImportedEmflowSimulator(_ImportedEmflowModel):
        pass


_EmflowModel: Any = _ImportedEmflowModel
_EmflowPredictor: Any = _ImportedEmflowPredictor
_EmflowOptimizer: Any = _ImportedEmflowOptimizer
_EmflowAgent: Any = _ImportedEmflowAgent
_EmflowSimulator: Any = _ImportedEmflowSimulator


_default_client: Client | None = None
_environment_context: contextvars.ContextVar[str | None] = contextvars.ContextVar("rebase_environment", default=None)


def _resolve_environment(client: Client, environment: str | None) -> str:
    """Resolve an explicit override, then ambient context, then client default."""
    return environment or _environment_context.get() or getattr(client, "environment_name", "dev")


def _environment_scoped_deploy(method: Callable[..., Any]) -> Callable[..., Any]:
    """Keep every lookup and write in a deploy under one environment."""

    @wraps(method)
    def scoped(self: Any, *args: Any, **kwargs: Any) -> Any:
        environment = _resolve_environment(self._client, kwargs.get("environment"))
        kwargs["environment"] = environment
        token = _environment_context.set(environment)
        try:
            return method(self, *args, **kwargs)
        finally:
            _environment_context.reset(token)

    return scoped


_trace_stack: list[_WorkflowTrace] = []
_UNSET = object()
DEFAULT_PROJECT_NAME = "default"
DEFAULT_API_KEY_PERMISSIONS = [
    "workspace:read",
    "projects:read",
    "endpoints:read",
    "endpoints:execute",
    "functions:read",
    "workflows:read",
    "models:read",
    "runs:read",
]
DEPLOY_REQUEST_TIMEOUT_SECONDS = 300
# (connect, read) for POST /runs/ephemeral: the server executes quick runs inside
# the request and enforces a 300 s run cap, so the read timeout is that plus
# headroom. The old default of 30 s failed any run longer than half a minute.
EPHEMERAL_RUN_REQUEST_TIMEOUT_SECONDS = (10, 330)
IMAGE_BUILD_POLL_SECONDS = 1.0
IMAGE_BUILD_TIMEOUT_SECONDS = 30 * 60


class RebaseWorkflowError(RuntimeError):
    #: HTTP status behind the failure, where there was one. Lets a caller tell a
    #: route this API version simply does not have (404) from a real error.
    status_code: int | None = None


RebaseError = RebaseWorkflowError


class EndpointConfig:
    def __init__(
        self,
        *,
        name: str | None = None,
        method: str = "POST",
        path: str | None = None,
        auth: str = "api_key",
        mode: str | None = None,
        timeout: int | None = None,
        timeout_seconds: int | None = None,
        docs: bool = False,
        enabled: bool = True,
    ) -> None:
        method = method.upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError("endpoint method must be one of: GET, POST, PUT, PATCH, DELETE")
        if auth not in {"api_key", "workspace", "public"}:
            raise ValueError("endpoint auth must be one of: api_key, workspace, public")
        if mode is not None and mode not in {"sync", "async"}:
            raise ValueError("endpoint mode must be one of: sync, async")
        resolved_timeout = timeout_seconds if timeout_seconds is not None else timeout
        if resolved_timeout is not None and resolved_timeout < 1:
            raise ValueError("endpoint timeout must be greater than or equal to 1")
        if path is not None:
            path = path.strip()
            if not path:
                raise ValueError("endpoint path cannot be empty")
            if not path.startswith("/"):
                path = f"/{path}"
            if "?" in path or "#" in path:
                raise ValueError("endpoint path cannot include query strings or fragments")
            path = path.rstrip("/") or "/"
        self.name = name
        self.method = method
        self.path = path
        self.auth = auth
        self.mode = mode
        self.timeout_seconds = resolved_timeout
        self.docs = docs
        self.enabled = enabled

    def __call__(self, target: Any) -> Any:
        target._rebase_endpoint = self
        if isinstance(target, (Function, Workflow, Model)):
            target.endpoint = self
        return target

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "method": self.method,
            "path": self.path,
            "auth": self.auth,
            "mode": self.mode,
            "timeout_seconds": self.timeout_seconds,
            "docs": self.docs,
            "enabled": self.enabled,
        }


def _coerce_endpoint(endpoint: EndpointConfig | dict[str, Any] | None) -> EndpointConfig | None:
    if endpoint is None:
        return None
    if isinstance(endpoint, EndpointConfig):
        return endpoint
    if isinstance(endpoint, dict):
        return EndpointConfig(
            name=endpoint.get("name"),
            method=endpoint.get("method", "POST"),
            path=endpoint.get("path"),
            auth=endpoint.get("auth", "api_key"),
            mode=endpoint.get("mode"),
            timeout_seconds=endpoint.get("timeout_seconds"),
            docs=bool(endpoint.get("docs", False)),
            enabled=bool(endpoint.get("enabled", True)),
        )
    raise TypeError("endpoint must be an EndpointConfig")


def _endpoint_for_callable(fn: Callable[..., Any] | None) -> EndpointConfig | None:
    return _coerce_endpoint(getattr(fn, "_rebase_endpoint", None)) if fn is not None else None


#: Projects per batch-delete request. Must not exceed the API's own ceiling,
#: which rejects a longer list rather than truncating it.
PROJECT_BATCH_DELETE_LIMIT = 100
#: What an API that does not have a route answers, and 404 is not the whole of it.
#: `/projects/batch-delete` is matched by `/projects/{project_id}` on a deployment
#: without the batch route, so the path resolves and only the method is refused:
#: `POST` comes back 405 with `Allow: GET`, never 404. Treating that as a genuine
#: error is what surfaced "Delete failed -- Method Not Allowed" instead of falling
#: back to one request per project.
ROUTE_ABSENT_STATUSES = frozenset({404, 405})
#: What a run list can be asked to carry beyond its summary rows. The platform answers
#: with the whole record when either is named: `parameters` is required on it and a
#: null `result` already means "none yet", so a half-filled record would be
#: indistinguishable from a real one.
RUN_INCLUDES = frozenset({"result", "parameters"})


@dataclass(frozen=True)
class OverviewRead:
    """One read of a composite view.

    `payload` is the view, or None. `unchanged` says which None it is: the platform
    answered 304 to the ETag we sent, so what the caller already holds is still the
    truth. Without it, None means the platform has no such route (`absent`).
    """

    payload: dict[str, Any] | None
    unchanged: bool = False

    @property
    def absent(self) -> bool:
        return self.payload is None and not self.unchanged


def _resolve_run_target(
    *,
    target_id: str | None,
    target_type: str | None,
    workflow_id: str | None,
    function_id: str | None,
    model_id: str | None,
) -> tuple[str | None, str | None]:
    """Collapse the per-kind run filters onto the `(target_id, target_type)` the API has.

    `/runs` has never had a `workflow_id` parameter. Sending one looked like a filter
    and was not: unknown query parameters are dropped without complaint, so the call
    came back with every run in the workspace and one project's runs showed up under
    another project's workflow.
    """
    named = [
        (identifier, kind)
        for identifier, kind in ((workflow_id, "workflow"), (function_id, "function"), (model_id, "model"))
        if identifier is not None
    ]
    if len(named) > 1:
        kinds = ", ".join(kind for _, kind in named)
        raise RebaseWorkflowError(f"list_runs takes one target at a time, got {kinds}")
    if not named:
        return target_id, target_type
    identifier, kind = named[0]
    if target_id is not None and target_id != identifier:
        raise RebaseWorkflowError(f"list_runs got target_id={target_id} and {kind}_id={identifier}")
    if target_type is not None and target_type != kind:
        raise RebaseWorkflowError(f"list_runs got target_type={target_type!r} and {kind}_id={identifier}")
    return identifier, kind


def _batch_failure_message(failure: dict[str, Any]) -> str:
    """Flatten one batch-delete failure into a line worth showing a user."""
    detail = failure.get("detail")
    if isinstance(detail, dict):
        message = str(detail.get("message", detail))
        contents = detail.get("contents")
        return f"{message} ({contents})" if contents else message
    return str(detail if detail is not None else failure.get("status", "failed"))


def _find_named(items: Iterable[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((item for item in items if item["name"] == name), None)


# Receiver for raw image-build log lines while Client.build_image polls.
# Module-level on purpose, mirroring how Modal gates output: deploy targets
# construct their own Client instances internally, so a per-instance attribute
# set by the CLI would never reach the client that actually builds.
_build_log_consumer: Callable[[str], None] | None = None


def set_build_log_consumer(consumer: Callable[[str], None] | None) -> None:
    """Install (or with None remove) the receiver for raw build-log lines."""
    global _build_log_consumer
    _build_log_consumer = consumer


def run_timing_summary(run: dict[str, Any]) -> str | None:
    """One phrase decomposing where a run's time went: "startup 4.1s · execution 0.6s".

    Startup (provisioning + cold start) is the number that tells a user their
    import block — not their code — is what's slow. Dispatch stands in for it
    on the shared fast path, where there is no provisioning. Only phases that
    actually registered are mentioned.
    """
    timings = run.get("timings") or {}
    if not isinstance(timings, dict):
        return None

    def seconds(key: str) -> float | None:
        value = timings.get(key)
        return float(value) if isinstance(value, int | float) else None

    parts: list[str] = []
    startup = seconds("infrastructure_provision_seconds")
    if startup is not None and startup >= 0.05:
        parts.append(f"startup {startup:.1f}s")
    execution = seconds("backend_execution_seconds")
    if execution is not None:
        parts.append(f"execution {execution:.2f}s")
    dispatch = seconds("api_dispatch_seconds")
    if startup is None and dispatch is not None and dispatch >= 0.05:
        parts.append(f"dispatch {dispatch:.2f}s")
    attempts = timings.get("backend_attempts")
    if isinstance(attempts, int) and attempts > 1:
        parts.append(f"{attempts} attempts")
    return " · ".join(parts) or None


def run_failure_summary(run: dict[str, Any]) -> str | None:
    """One human line for a failed run, preferring the structured diagnosis.

    Servers that diagnose failures attach ``failure_reason``
    ({code, message, hint, observed}) alongside the free-text ``error`` — the
    diagnosis knows *why* the container died and which knob to turn (the hint),
    so it wins. Older servers simply have no such key and fall back to
    ``error``; returns None when the run carries neither.
    """
    reason = run.get("failure_reason")
    if isinstance(reason, dict):
        message = reason.get("message")
        if isinstance(message, str) and message:
            summary = message[0].upper() + message[1:]
            hint = reason.get("hint")
            if isinstance(hint, str) and hint:
                return f"{summary}. {hint}"
            return summary
    error = run.get("error")
    return str(error) if error else None


def _response_error_message(response: requests.Response) -> str:
    # The final fallbacks guard against empty bodies (a bare 500, a proxy 502):
    # an empty message here surfaced to users as literally "Error: ".
    try:
        payload = response.json()
    except ValueError:
        return response.text or f"HTTP {response.status_code} with an empty response body"
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str):
            return detail
        if isinstance(detail, dict) and detail.get("code") == "workspace_credits_exhausted":
            remaining = detail.get("remaining_cents")
            required = detail.get("required_reservation_cents")
            message = detail.get("message") or "workspace monthly compute credits are exhausted"
            if isinstance(remaining, int) and isinstance(required, int):
                return (
                    f"{message}. Remaining: {remaining / 100:.2f} EUR; required reservation: {required / 100:.2f} EUR."
                )
            return str(message)
        if isinstance(detail, dict) and isinstance(detail.get("contents"), dict):
            contents = ", ".join(f"{count} {label}" for label, count in detail["contents"].items())
            message = detail.get("message") or "target is not empty"
            return f"{message} (contains {contents})"
        if detail is not None:
            return json.dumps(detail)
    return response.text or f"HTTP {response.status_code} with an empty response body"


class Cron:
    def __init__(
        self,
        cron: str,
        *,
        timezone: str | None = None,
        day_or: bool = True,
        active: bool = True,
    ) -> None:
        if not cron or len(cron.split()) != 5:
            raise ValueError("cron must be a five-field cron expression")
        self.cron = cron
        self.timezone = timezone
        self.day_or = day_or
        self.active = active

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "type": "cron",
                "cron": self.cron,
                "timezone": self.timezone,
                "day_or": self.day_or,
                "active": self.active,
            }.items()
            if value is not None
        }


def _duration_payload(value: str | int | float | timedelta | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, timedelta):
        return f"{int(value.total_seconds())}s"
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be a duration string, seconds, or timedelta")
    if isinstance(value, (int, float)):
        return f"{int(value)}s"
    if isinstance(value, str):
        if not value.strip():
            raise ValueError(f"{field_name} must be a duration string like '15m'")
        return value.strip()
    raise TypeError(f"{field_name} must be a duration string, seconds, or timedelta")


class OnWorkflow:
    def __init__(
        self,
        source: str,
        *,
        on: str = "success",
        active: bool = True,
    ) -> None:
        if not isinstance(source, str) or not source.strip():
            raise ValueError("OnWorkflow requires a non-empty 'project/workflow' source")
        if on not in {"success", "failure", "completion"}:
            raise ValueError("on must be 'success', 'failure', or 'completion'")
        self.source = source.strip()
        self.on = on
        self.active = active

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "type": "on_workflow",
                "source": self.source,
                "on": self.on,
                "active": self.active,
            }.items()
            if value is not None
        }


class OnUpdate:
    def __init__(
        self,
        datasets: list[Any],
        *,
        require: str = "all",
        at_most_every: str | int | float | timedelta | None = None,
        deadline: Cron | dict[str, Any] | None = None,
        only_valid: bool = False,
        active: bool = True,
    ) -> None:
        names: list[str] = []
        for item in datasets or []:
            name = item.name if isinstance(item, Dataset) else item
            if not isinstance(name, str) or not name.strip():
                raise TypeError("OnUpdate datasets must be dataset names or rebase.Dataset instances")
            names.append(name.strip())
        if not names:
            raise ValueError("OnUpdate requires at least one dataset")
        if len(set(names)) != len(names):
            raise ValueError("OnUpdate datasets must be unique")
        if require not in {"all", "any"}:
            raise ValueError("require must be 'all' or 'any'")
        if isinstance(deadline, Cron):
            deadline = deadline.to_dict()
        elif deadline is not None and not isinstance(deadline, dict):
            raise TypeError("deadline must be rb.Cron(...) or a schedule dictionary")
        if not isinstance(only_valid, bool):
            raise TypeError("only_valid must be a boolean")
        self.datasets = names
        self.require = require
        self.at_most_every = _duration_payload(at_most_every, field_name="at_most_every")
        self.deadline = deadline
        self.only_valid = only_valid
        self.active = active

    def to_dict(self) -> dict[str, Any]:
        payload = {
            key: value
            for key, value in {
                "type": "on_update",
                "datasets": list(self.datasets),
                "require": self.require,
                "at_most_every": self.at_most_every,
                "deadline": self.deadline,
                "active": self.active,
            }.items()
            if value is not None
        }
        if self.only_valid:
            # Updates whose validation failed (or was skipped) don't satisfy
            # the trigger; the next clean signal does.
            payload["only_valid"] = True
        return payload


Schedule = Cron | dict[str, Any]
Trigger = OnWorkflow | OnUpdate | dict[str, Any]
_RESERVED_WORKFLOW_PARAMETERS = frozenset({"ctx"})
ExecutionMode = str
Isolation = str
EXECUTION_MODES = ("interactive", "job")
ISOLATIONS = ("shared", "dedicated")
DEFAULT_MODE: ExecutionMode = "interactive"
DEFAULT_ISOLATION: Isolation = "shared"

# Deprecated compatibility names. New code should use mode/isolation.
RunType = str
RUN_TYPES = ("quick", "quick_shared", "long")
DEFAULT_RUN_TYPE: RunType = "quick_shared"
WORKFLOW_RUN_TYPES = ("quick", "long")
DeploySource = str
DEFAULT_DEPLOY_SOURCE: DeploySource = "rebase"
DEFAULT_MODEL_DEPENDENCY = (
    "emflow @ git+https://github.com/rebase-energy/emflow.git@2d0205e1b479d439df72e50c6865735d0b26de8d"
)
LEGACY_BACKEND_REMOVED_ERROR = (
    "the 'backend' parameter was removed; use mode='interactive' | 'job' and isolation='shared' | 'dedicated'"
)


def _validate_execution(
    mode: ExecutionMode | None = None,
    isolation: Isolation | None = None,
    *,
    target_type: str = "function",
    run_type: RunType | None = None,
) -> tuple[ExecutionMode, Isolation]:
    """Validate execution settings and translate deprecated run_type values."""
    if run_type is not None:
        _validate_run_type(run_type, target_type=target_type)
        if mode is not None or isolation is not None:
            raise ValueError("use either mode/isolation or the deprecated run_type, not both")
        if run_type == "long":
            return "job", DEFAULT_ISOLATION
        if run_type == "quick_shared":
            return "interactive", "shared"
        return ("interactive", "shared") if target_type == "workflow" else ("interactive", "dedicated")

    resolved_mode = mode or DEFAULT_MODE
    resolved_isolation = isolation or DEFAULT_ISOLATION
    if resolved_mode not in EXECUTION_MODES:
        raise ValueError("mode must be 'interactive' or 'job'")
    if resolved_isolation not in ISOLATIONS:
        raise ValueError("isolation must be 'shared' or 'dedicated'")
    if target_type == "workflow" and resolved_mode == "interactive" and resolved_isolation == "dedicated":
        raise ValueError("workflows support mode='interactive' only with isolation='shared'")
    return resolved_mode, resolved_isolation


def _legacy_run_type(mode: ExecutionMode, isolation: Isolation, *, target_type: str = "function") -> RunType:
    if mode == "job":
        return "long"
    if target_type == "workflow":
        return "quick"
    return "quick_shared" if isolation == "shared" else "quick"


def _validate_run_type(run_type: RunType, target_type: str = "function") -> RunType:
    if run_type not in RUN_TYPES or (target_type == "workflow" and run_type not in WORKFLOW_RUN_TYPES):
        raise ValueError(
            "run_type must be 'quick', 'quick_shared', or 'long' (workflows: 'quick' or 'long'). "
            "run_type is deprecated; use mode='interactive' | 'job' and isolation='shared' | 'dedicated'."
        )
    return run_type


def _reject_legacy_backend(backend: Any) -> None:
    if backend is not None:
        raise ValueError(LEGACY_BACKEND_REMOVED_ERROR)


def _cloud_run_cpu_value(value: float | int | str | None) -> str | None:
    """Normalize Modal-style cpu (cores as a number) or a Cloud Run string ("2000m")."""
    if value is None:
        return None
    if isinstance(value, str):
        if not value.strip():
            raise ValueError("cpu must be a positive number of cores or a Cloud Run cpu string")
        return value.strip()
    cores = float(value)
    if cores <= 0:
        raise ValueError("cpu must be greater than 0")
    return f"{int(round(cores * 1000))}m"


def _cloud_run_memory_value(value: int | float | str | None) -> str | None:
    """Normalize Modal-style memory (MiB as a number) or a Cloud Run string ("1Gi")."""
    if value is None:
        return None
    if isinstance(value, str):
        if not value.strip():
            raise ValueError("memory must be MiB as a number or a Cloud Run memory string")
        return value.strip()
    mib = int(value)
    if mib <= 0:
        raise ValueError("memory must be greater than 0")
    return f"{mib}Mi"


def _validate_deploy_source(source: str | None) -> DeploySource | None:
    if source is None:
        return None
    if source not in {"rebase", "github"}:
        raise ValueError("deploy_source must be 'rebase' or 'github'")
    return source


def _normalize_path(path: str | None, *, field_name: str = "path") -> str:
    value = (path or "/").strip()
    if not value:
        value = "/"
    if "?" in value or "#" in value:
        raise ValueError(f"{field_name} must not include query strings or fragments")
    if not value.startswith("/"):
        value = f"/{value}"
    while "//" in value:
        value = value.replace("//", "/")
    if len(value) > 1:
        value = value.rstrip("/")
    return value


class _EnvironmentObjects:
    def create(
        self,
        name: str,
        *,
        deploy_mode: str = "direct",
        protected: bool = False,
        require_pr: bool = False,
        allowed_branches: list[str] | None = None,
        client: Client | None = None,
    ) -> Environment:
        resolved = client or default_client()
        resolved.create_environment(
            name,
            deploy_mode=deploy_mode,
            protected=protected,
            require_pr=require_pr,
            allowed_branches=allowed_branches,
        )
        return Environment(name, client=resolved)


class _EnvironmentProjects:
    def __init__(self, environment: Environment) -> None:
        self.environment = environment

    def track(
        self,
        name: str,
        *,
        github_connection_id: str,
        ref: str,
        entrypoint: str,
        repo_path: str | None = None,
    ) -> dict[str, Any]:
        client = self.environment._resolved_client()
        project = client.ensure_project(name, environment_name=self.environment.name)
        return client.track_project(
            str(project["id"]),
            github_connection_id=github_connection_id,
            tracked_ref=ref,
            entrypoint=entrypoint,
            repo_path=repo_path,
        )


class Environment:
    """A workspace namespace for projects, storage, secrets, runs and policies."""

    objects = _EnvironmentObjects()

    def __init__(self, name: str, *, client: Client | None = None) -> None:
        if not name or not name.strip():
            raise ValueError("Environment requires a non-empty name")
        self.name = name.strip().lower()
        self._client = client
        self._context_tokens: list[contextvars.Token[str | None]] = []
        self.projects = _EnvironmentProjects(self)

    @classmethod
    def from_name(
        cls,
        name: str,
        *,
        create_if_missing: bool = False,
        client: Client | None = None,
    ) -> Environment:
        environment = cls(name, client=client)
        if create_if_missing:
            try:
                environment._resolved_client().get_environment(environment.name)
            except RebaseWorkflowError as exc:
                if exc.status_code != 404:
                    raise
                environment._resolved_client().create_environment(environment.name)
        return environment

    @classmethod
    def from_context(cls, *, client: Client | None = None) -> Environment:
        resolved = client or default_client()
        return cls(_environment_context.get() or resolved.environment_name, client=resolved)

    def _resolved_client(self) -> Client:
        if self._client is None:
            self._client = default_client()
        return self._client

    def hydrate(self) -> dict[str, Any]:
        return self._resolved_client().get_environment(self.name)

    def configure(
        self,
        *,
        deploy_mode: str | None = None,
        protected: bool | None = None,
        require_pr: bool | None = None,
        allowed_branches: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._resolved_client().update_environment_policy(
            self.name,
            deploy_mode=deploy_mode,
            protected=protected,
            require_pr=require_pr,
            allowed_branches=allowed_branches,
        )

    def grants(self) -> list[dict[str, Any]]:
        """List explicit profile/API-key access grants for this environment."""
        return self._resolved_client().list_environment_grants(self.name)

    def grant(
        self,
        *,
        profile_id: str | None = None,
        api_key_id: str | None = None,
        access: str = "read",
    ) -> dict[str, Any]:
        """Grant read, write, or admin access to exactly one principal."""
        return self._resolved_client().grant_environment_access(
            self.name,
            profile_id=profile_id,
            api_key_id=api_key_id,
            access=access,
        )

    def revoke(self, grant_id: str) -> None:
        """Remove one explicit access grant."""
        self._resolved_client().revoke_environment_access(self.name, grant_id)

    def __enter__(self) -> Environment:
        self._context_tokens.append(_environment_context.set(self.name))
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._context_tokens:
            _environment_context.reset(self._context_tokens.pop())


class Secret:
    """A named bundle of environment variables, injected into deployed code.

    Mirrors Modal's secrets: create a bundle once (``rebase secret create acme-snowflake
    SNOWFLAKE_ACCOUNT=... SNOWFLAKE_PASSWORD=...``), then attach it by name::

        @rb.function(secrets=[rb.Secret.from_name("acme-snowflake")])
        def load(): ...

    All keys in the bundle become environment variables at run time. Values live in
    Secret Manager and are injected by Cloud Run; they never pass through the deploy
    payload or source snapshot.
    """

    def __init__(
        self,
        *,
        name: str | None = None,
        env_dict: dict[str, str] | None = None,
        environment_name: str | None = None,
    ) -> None:
        if name is None and env_dict is None:
            raise ValueError("Secret requires a name (from_name) or values (from_dict)")
        self.name = name
        self.env_dict = dict(env_dict) if env_dict is not None else None
        self.environment_name = environment_name

    @classmethod
    def from_name(cls, name: str, *, environment_name: str | None = None) -> Secret:
        """Reference an existing workspace secret bundle by name."""
        if not name or not name.strip():
            raise ValueError("Secret.from_name requires a non-empty name")
        return cls(name=name.strip(), environment_name=environment_name)

    @classmethod
    def from_dict(
        cls,
        env_dict: dict[str, str],
        *,
        name: str | None = None,
        environment_name: str | None = None,
    ) -> Secret:
        """Create (or update) a bundle from a dict at deploy time, then attach it.

        Without ``name``, a stable content-derived name (``inline-<hash>``) is used.
        Prefer ``from_name`` with ``rebase secret create`` for shared credentials.
        """
        if not env_dict:
            raise ValueError("Secret.from_dict requires at least one KEY: value entry")
        return cls(name=name, env_dict=env_dict, environment_name=environment_name)

    @classmethod
    def from_dotenv(cls, path: str | Path = ".env", *, name: str | None = None) -> Secret:
        """Create a bundle from a local dotenv file at deploy time, then attach it."""
        values: dict[str, str] = {}
        for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("'\"")
        if not values:
            raise ValueError(f"no KEY=value entries found in {path}")
        return cls(name=name, env_dict=values)

    def _resolved_name(self) -> str:
        if self.name is not None:
            return self.name
        canonical = json.dumps(self.env_dict, sort_keys=True, separators=(",", ":"))
        return f"inline-{hashlib.sha256(canonical.encode()).hexdigest()[:10]}"

    def resolve(self, client: Client) -> dict[str, str]:
        """Materialize this secret into the env-name -> secret-ref map deploys send."""
        if self.environment_name is not None:
            client = client.with_environment(self.environment_name)
        if self.env_dict is not None:
            created = client.set_secret(self._resolved_name(), self.env_dict)
            refs = created.get("secret_refs")
            if not isinstance(refs, dict):
                raise RebaseWorkflowError("secret create did not return secret_refs")
            return refs
        data = client.get_secret(self._resolved_name())
        refs = data.get("secret_refs")
        if not isinstance(refs, dict):
            raise RebaseWorkflowError(f"secret {self.name!r} did not resolve to secret_refs")
        return refs


def _resolve_secrets_payload(
    secrets: dict[str, str] | list[Any] | tuple[Any, ...] | None,
    client: Client | None,
) -> dict[str, str]:
    """Normalize ``secrets=`` into the env-name -> secret-ref map the API stores.

    Accepts the Modal-style list form (``[Secret.from_name("x"), "y"]`` — strings are
    treated as bundle names) or the raw ``{ENV: secret_ref}`` map.
    """
    if secrets is None:
        return {}
    if isinstance(secrets, dict):
        return dict(secrets)
    resolved: dict[str, str] = {}
    for item in secrets:
        secret = Secret.from_name(item) if isinstance(item, str) else item
        if not isinstance(secret, Secret):
            raise RebaseWorkflowError(f"secrets entries must be rebase.Secret or bundle names, got {type(item)!r}")
        if client is None:
            raise RebaseWorkflowError("resolving secrets requires an authenticated client")
        resolved.update(secret.resolve(client))
    return resolved


class Volume:
    """A named, persistent file store shared between deployed code and your machine.

    Mirrors Modal's volumes: create one lazily and mount it into functions or apps::

        vol = rb.Volume.from_name("model-cache", create_if_missing=True)

        @rb.function(volumes={"/models": vol})
        def train():
            open("/models/model.pkl", "wb").write(...)

    Inside the container the volume is a directory (GCS-backed via gcsfuse in v1).
    From your machine, use :meth:`put_file`, :meth:`read_file`, :meth:`listdir`, and
    the ``rebase volume`` CLI; bytes move over presigned URLs, never through the API.
    """

    def __init__(
        self,
        name: str,
        *,
        create_if_missing: bool = False,
        read_only: bool = False,
        client: Client | None = None,
        environment_name: str | None = None,
    ) -> None:
        if not name or not name.strip():
            raise ValueError("Volume requires a non-empty name")
        self.name = name.strip()
        self.create_if_missing = create_if_missing
        self.read_only = read_only
        self._client = client
        self.environment_name = environment_name
        self._ensured = False

    @classmethod
    def from_name(
        cls,
        name: str,
        *,
        create_if_missing: bool = False,
        read_only: bool = False,
        environment_name: str | None = None,
    ) -> Volume:
        """Reference a workspace volume by name, optionally creating it lazily."""
        return cls(
            name,
            create_if_missing=create_if_missing,
            read_only=read_only,
            environment_name=environment_name,
        )

    def _resolved_client(self) -> Client:
        if self._client is None:
            self._client = default_client()
        if self.environment_name is not None and self._client.environment_name != self.environment_name:
            self._client = self._client.with_environment(self.environment_name)
        return self._client

    def ensure(self, client: Client | None = None) -> dict[str, Any]:
        """Make sure the volume exists (creates it when ``create_if_missing``)."""
        resolved = client or self._resolved_client()
        data = resolved.create_volume(self.name) if self.create_if_missing else resolved.get_volume(self.name)
        self._ensured = True
        return data

    def listdir(self, path: str = "") -> list[dict[str, Any]]:
        """List objects in the volume, optionally under a path prefix."""
        return self._resolved_client().list_volume_objects(self.name, prefix=path)

    def put_file(self, local_path: str | Path, remote_path: str | None = None) -> str:
        """Upload one local file into the volume. Returns the remote path."""
        local = Path(local_path)
        remote = (remote_path or local.name).lstrip("/")
        client = self._resolved_client()
        if self.create_if_missing and not self._ensured:
            self.ensure(client)
        signed = client.create_volume_upload_url(self.name, remote)
        with local.open("rb") as handle:
            response = requests.put(signed["url"], data=handle, timeout=600)
        if response.status_code >= 400:
            raise RebaseWorkflowError(f"volume upload failed: {response.status_code} {response.text[:200]}")
        return remote

    def put_directory(self, local_dir: str | Path, remote_prefix: str = "") -> list[str]:
        """Recursively upload a local directory. Returns the remote paths written."""
        base = Path(local_dir)
        if not base.is_dir():
            raise RebaseWorkflowError(f"not a directory: {local_dir}")
        written: list[str] = []
        prefix = remote_prefix.strip("/")
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(base).as_posix()
            remote = f"{prefix}/{relative}" if prefix else relative
            written.append(self.put_file(path, remote))
        return written

    def read_file(self, remote_path: str) -> bytes:
        """Download one file from the volume into memory."""
        client = self._resolved_client()
        signed = client.create_volume_download_url(self.name, remote_path.lstrip("/"))
        response = requests.get(signed["url"], timeout=600)
        if response.status_code >= 400:
            raise RebaseWorkflowError(f"volume download failed: {response.status_code} {response.text[:200]}")
        return response.content

    def get_file(self, remote_path: str, local_path: str | Path) -> Path:
        """Download one file from the volume to disk."""
        local = Path(local_path)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(self.read_file(remote_path))
        return local

    def remove_file(self, remote_path: str) -> None:
        """Delete one object from the volume."""
        self._resolved_client().delete_volume_object(self.name, remote_path.lstrip("/"))

    def commit(self) -> None:
        """No-op for Modal compatibility: GCS-backed mounts persist writes directly."""

    def reload(self) -> None:
        """No-op for Modal compatibility: GCS-backed mounts read the live bucket state."""


def _resolve_volumes_payload(
    volumes: dict[str, Any] | list[Any] | None,
    client: Client | None,
) -> list[dict[str, Any]]:
    """Normalize ``volumes=`` into the attachment list the API stores.

    Accepts the Modal-style ``{"/mount/path": Volume}`` map (strings are treated as
    volume names) or a pre-built attachment list.
    """
    if volumes is None:
        return []
    if isinstance(volumes, list):
        return list(volumes)
    attachments: list[dict[str, Any]] = []
    for mount_path, item in sorted(volumes.items()):
        volume = Volume.from_name(item) if isinstance(item, str) else item
        if not isinstance(volume, Volume):
            raise RebaseWorkflowError(f"volumes values must be rebase.Volume or volume names, got {type(item)!r}")
        if volume.create_if_missing:
            if client is None:
                raise RebaseWorkflowError("resolving volumes requires an authenticated client")
            volume.ensure(client)
        attachments.append({"volume": volume.name, "mount_path": mount_path, "read_only": volume.read_only})
    return attachments


def bucket_env_var(name: str) -> str:
    """The env var an attached bucket is injected as, matching the API's mapping."""
    return "REBASE_BUCKET_" + re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")


@dataclass(frozen=True)
class BucketObject:
    """One object in a bucket."""

    key: str
    size: int
    updated: str | None = None
    content_type: str | None = None
    etag: str | None = None
    version: str | None = None
    digest: str | None = None


class Bucket:
    """A named object store, backed one-to-one by a real cloud bucket.

    Keys and objects, not files and directories::

        b = rb.Bucket.from_name("forecasts", create_if_missing=True)
        b.put("2026/08/10.parquet", data)
        b.get("2026/08/10.parquet")
        for obj in b.iter_all(prefix="2026/"):
            ...

    Attach one to deployed code to read it at full speed, without signed URLs::

        @rb.function(buckets=["forecasts"])
        def train():
            pd.read_parquet(rb.Bucket.from_name("forecasts").uri + "/2026/08/10.parquet")

    Unlike :class:`Volume` this is never mounted, so nothing here pretends an
    object store is a filesystem: there is no ``commit``/``reload`` pair, and a
    write costs one request rather than a silent read-modify-write of the whole
    object.

    The backing provider is deliberately not part of this API: reads and writes
    use short-lived capability URLs issued by Rebase, so hosted code never
    receives cloud credentials and callers need no provider SDKs.
    """

    def __init__(
        self,
        name: str,
        *,
        create_if_missing: bool = False,
        client: Client | None = None,
        environment_name: str | None = None,
    ) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Bucket requires a non-empty name")
        self.name = name.strip()
        self.create_if_missing = create_if_missing
        self._client = client
        self.environment_name = environment_name
        self._ensured = False
        self._uri: str | None = None

    @classmethod
    def from_name(
        cls,
        name: str,
        *,
        create_if_missing: bool = False,
        environment_name: str | None = None,
    ) -> Bucket:
        """Reference a workspace bucket by name, optionally creating it lazily."""
        return cls(
            name,
            create_if_missing=create_if_missing,
            environment_name=environment_name,
        )

    def __repr__(self) -> str:
        return f"Bucket({self.name!r})"

    def _resolved_client(self) -> Client:
        if self._client is None:
            self._client = default_client()
        if self.environment_name is not None and self._client.environment_name != self.environment_name:
            self._client = self._client.with_environment(self.environment_name)
        return self._client

    def ensure(self, client: Client | None = None) -> dict[str, Any]:
        """Make sure the bucket exists (creates it when ``create_if_missing``)."""
        resolved = client or self._resolved_client()
        data = resolved.create_bucket(self.name) if self.create_if_missing else resolved.get_bucket(self.name)
        self._ensured = True
        self._uri = str(data.get("uri") or "") or None
        return data

    def _ensure_once(self) -> None:
        if self.create_if_missing and not self._ensured:
            self.ensure()

    @property
    def uri(self) -> str:
        """The ``gs://`` URI, for handing to pandas, polars, duckdb or fsspec.

        Inside a deployed run this comes from the injected environment, so it
        costs nothing; elsewhere it is fetched once and cached.
        """
        if self._uri is None:
            injected = os.environ.get(bucket_env_var(self.name))
            self._uri = injected if injected else str(self.ensure().get("uri") or "")
        return self._uri

    def put(self, key: str, data: bytes | str | Path, *, content_type: str | None = None) -> str:
        """Write one object. Accepts bytes, text, or a path to upload."""
        self._ensure_once()
        if isinstance(data, Path):
            return self.put_file(data, key, content_type=content_type)
        payload = data.encode("utf-8") if isinstance(data, str) else data
        signed = self._signed_url(key, method="PUT", content_type=content_type)
        headers = {"Content-Type": content_type} if content_type else None
        response = requests.put(signed, data=payload, headers=headers, timeout=600)
        if response.status_code >= 400:
            raise RebaseWorkflowError(f"bucket upload failed: {response.status_code} {response.text[:200]}")
        return key.lstrip("/")

    def put_file(
        self,
        local_path: str | Path,
        key: str | None = None,
        *,
        content_type: str | None = None,
    ) -> str:
        """Upload one local file. Streams, so object size is not bounded by memory."""
        local = Path(local_path)
        target = (key or local.name).lstrip("/")
        self._ensure_once()
        signed = self._signed_url(target, method="PUT", content_type=content_type)
        headers = {"Content-Type": content_type} if content_type else None
        with local.open("rb") as handle:
            response = requests.put(signed, data=handle, headers=headers, timeout=600)
        if response.status_code >= 400:
            raise RebaseWorkflowError(f"bucket upload failed: {response.status_code} {response.text[:200]}")
        return target

    def put_directory(self, local_dir: str | Path, prefix: str = "") -> Sequence[str]:
        """Recursively upload a local directory. Returns the keys written."""
        base = Path(local_dir)
        if not base.is_dir():
            raise RebaseWorkflowError(f"not a directory: {local_dir}")
        files = [path for path in sorted(base.rglob("*")) if path.is_file()]
        if not files:
            return []
        self._ensure_once()
        clean = prefix.strip("/")
        relatives = [path.relative_to(base).as_posix() for path in files]
        keys = [f"{clean}/{relative}" if clean else relative for relative in relatives]
        # One signing round trip for the whole tree rather than one per file.
        signed = self._signed_urls(keys, method="PUT")
        for path, key in zip(files, keys, strict=True):
            with path.open("rb") as handle:
                response = requests.put(signed[key], data=handle, timeout=600)
            if response.status_code >= 400:
                raise RebaseWorkflowError(f"bucket upload failed for {key}: {response.status_code}")
        return keys

    def get(self, key: str) -> bytes:
        """Read one object into memory."""
        signed = self._signed_url(key, method="GET")
        response = requests.get(signed, timeout=600)
        if response.status_code >= 400:
            raise RebaseWorkflowError(f"bucket download failed: {response.status_code} {response.text[:200]}")
        return response.content

    def download(self, key: str, local_path: str | Path) -> Path:
        """Download one object to disk, streaming it rather than buffering."""
        local = Path(local_path)
        local.parent.mkdir(parents=True, exist_ok=True)
        signed = self._signed_url(key, method="GET")
        with requests.get(signed, timeout=600, stream=True) as response:
            if response.status_code >= 400:
                raise RebaseWorkflowError(f"bucket download failed: {response.status_code} {response.text[:200]}")
            with local.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    handle.write(chunk)
        return local

    def list(
        self,
        prefix: str = "",
        *,
        delimiter: str | None = None,
        limit: int = 1000,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        """One page of objects. Pass ``delimiter="/"`` to browse folder-style.

        Returns ``{"objects": [...], "prefixes": [...], "next_page_token": ...}``.
        Use :meth:`iter_all` when you want every object rather than one page.

        Note this name shadows the ``list`` builtin inside the class body, so
        annotations below here use ``Sequence`` rather than ``list[...]``.
        """
        data = self._resolved_client().list_bucket_objects(
            self.name, prefix=prefix, delimiter=delimiter, limit=limit, page_token=page_token
        )
        return {
            "objects": [_bucket_object(item) for item in data.get("objects", [])],
            "prefixes": list(data.get("prefixes", [])),
            "next_page_token": data.get("next_page_token"),
        }

    def iter_all(self, prefix: str = "") -> Iterator[BucketObject]:
        """Every object under a prefix, following pagination transparently."""
        page_token: str | None = None
        while True:
            page = self.list(prefix, page_token=page_token)
            yield from page["objects"]
            page_token = page["next_page_token"]
            if not page_token:
                return

    def stat(self, key: str) -> dict[str, Any]:
        """Metadata for one object, including checksums and generation."""
        return self._resolved_client().stat_bucket_object(self.name, key)

    def exists(self, key: str) -> bool:
        try:
            self.stat(key)
        except RebaseWorkflowError as exc:
            if exc.status_code == 404:
                return False
            raise
        return True

    def delete(self, key: str) -> None:
        """Delete one object."""
        self._resolved_client().delete_bucket_object(self.name, key.lstrip("/"))

    def delete_prefix(self, prefix: str = "") -> int:
        """Delete every object under a prefix. Returns the number deleted.

        Paged from here rather than server-side: emptying a large bucket inside
        one API request would time out long before it finished, and the retry
        would restart against a half-emptied bucket.
        """
        deleted = 0
        while True:
            page = self.list(prefix, limit=1000)
            if not page["objects"]:
                return deleted
            for obj in page["objects"]:
                self.delete(obj.key)
                deleted += 1

    def signed_url(self, key: str, *, method: str = "GET", content_type: str | None = None) -> str:
        """A short-lived capability URL for one object."""
        return self._signed_url(key, method=method, content_type=content_type)

    def signed_urls(self, keys: Sequence[str], *, method: str = "GET") -> dict[str, str]:
        """Presigned URLs for many objects in one round trip."""
        return self._signed_urls(list(keys), method=method)

    def _signed_url(self, key: str, *, method: str, content_type: str | None = None) -> str:
        cleaned = key.lstrip("/")
        return self._signed_urls([cleaned], method=method, content_type=content_type)[cleaned]

    def _signed_urls(
        self,
        keys: Sequence[str],
        *,
        method: str,
        content_type: str | None = None,
    ) -> dict[str, str]:
        cleaned = [key.lstrip("/") for key in keys]
        urls: dict[str, str] = {}
        client = self._resolved_client()
        # The API caps a batch; chunk so a big directory upload still works.
        for start in range(0, len(cleaned), _SIGNED_URL_BATCH):
            chunk = cleaned[start : start + _SIGNED_URL_BATCH]
            data = client.create_bucket_signed_urls(self.name, chunk, method=method, content_type=content_type)
            urls.update({item["path"]: item["url"] for item in data.get("urls", [])})
        missing = [key for key in cleaned if key not in urls]
        if missing:
            raise RebaseWorkflowError(f"no signed URL returned for: {missing[:5]}")
        return urls


_SIGNED_URL_BATCH = 100


def _bucket_object(item: dict[str, Any]) -> BucketObject:
    return BucketObject(
        key=str(item.get("path", "")),
        size=int(item.get("size", 0)),
        updated=item.get("updated"),
        content_type=item.get("content_type"),
        etag=item.get("etag"),
        version=item.get("version"),
        digest=item.get("digest"),
    )


def _resolve_buckets_payload(
    buckets: list[Any] | None,
    client: Client | None,
) -> list[dict[str, Any]]:
    """Normalize ``buckets=`` into the attachment list the API stores.

    Accepts bare names or :class:`Bucket` handles. Sorted by name so the same
    set in a different order does not churn the deployed version.
    """
    if buckets is None:
        return []
    attachments: list[dict[str, Any]] = []
    for item in buckets:
        # A pre-built attachment list round-trips unchanged.
        if isinstance(item, dict):
            attachments.append(item)
            continue
        bucket = Bucket.from_name(item) if isinstance(item, str) else item
        if not isinstance(bucket, Bucket):
            raise RebaseWorkflowError(f"buckets entries must be rebase.Bucket or bucket names, got {type(item)!r}")
        if bucket.create_if_missing:
            if client is None:
                raise RebaseWorkflowError("resolving buckets requires an authenticated client")
            bucket.ensure(client)
        attachments.append({"bucket": bucket.name})
    names = [str(entry.get("bucket", "")) for entry in attachments]
    if len(names) != len(set(names)):
        raise RebaseWorkflowError("bucket attachments must be unique")
    return sorted(attachments, key=lambda entry: str(entry.get("bucket", "")))


def _coerce_config_dict(value: Any, *, field_name: str) -> dict[str, Any] | None:
    """Normalize a Contract/Freshness instance (anything with ``to_dict``) or dict to a dict."""
    if value is None:
        return None
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    if not isinstance(value, dict):
        raise TypeError(f"{field_name} must be a dict or expose to_dict() (e.g. rb.Contract / rb.Freshness)")
    return value


# Process-level registry of datasets declared with a contract/freshness config.
# Deploy preflight and `rebase dataset check` reconcile these against the
# platform before any code runs. Keyed by name, last-wins: notebook re-runs
# legitimately redefine a dataset, so re-registration warns on a changed
# config instead of raising.
_dataset_registry: dict[str, Dataset] = {}


def registered_datasets() -> list[Dataset]:
    """Datasets declared with a contract or freshness config in this process."""
    return list(_dataset_registry.values())


def _register_dataset(dataset: Dataset) -> None:
    previous = _dataset_registry.get(dataset.name)
    if previous is not None and previous is not dataset:
        changed = [
            key
            for key in ("contract", "freshness")
            if json.dumps(getattr(previous, key), sort_keys=True) != json.dumps(getattr(dataset, key), sort_keys=True)
        ]
        if changed:
            warnings.warn(
                f"dataset {dataset.name}: redefined with a different {' and '.join(changed)}; "
                "the newest definition wins",
                stacklevel=3,
            )
    _dataset_registry[dataset.name] = dataset


class Dataset:
    """A named signal channel that fans dataset updates into on-update workflow triggers.

    Reference one lazily and watch it from a workflow, then signal it whenever fresh
    data lands::

        prices = rb.Dataset.from_name("nordpool/prices", create_if_missing=True)

        @project.workflow(trigger=rb.OnUpdate([prices]))
        def forecast(ctx=None): ...

        prices.mark_updated(watermark="2026-07-11T09:00:00Z")

    Inside deployed code, ``mark_updated`` authenticates via the platform-injected
    ``REBASE_API_KEY``/``REBASE_WORKFLOWS_API_URL`` environment variables.
    """

    def __init__(
        self,
        name: str,
        *,
        create_if_missing: bool = False,
        client: Client | None = None,
        contract: Any = None,
        freshness: Any = None,
    ) -> None:
        if not name or not name.strip():
            raise ValueError("Dataset requires a non-empty name")
        self.name = name.strip()
        self.create_if_missing = create_if_missing
        self.contract = _coerce_config_dict(contract, field_name="contract")
        self.freshness = _coerce_config_dict(freshness, field_name="freshness")
        self._client = client
        self._ensured = False
        self._config_synced = False
        self._fetched_contract: Any = _UNSET  # _UNSET = never fetched; None = fetched but absent
        if self.contract is not None or self.freshness is not None:
            _register_dataset(self)

    @classmethod
    def from_name(
        cls,
        name: str,
        *,
        create_if_missing: bool = False,
        contract: Any = None,
        freshness: Any = None,
    ) -> Dataset:
        """Reference a workspace dataset by name, optionally creating it lazily."""
        return cls(name, create_if_missing=create_if_missing, contract=contract, freshness=freshness)

    def _resolved_client(self) -> Client:
        if self._client is None:
            self._client = default_client()
        return self._client

    def ensure(self, client: Client | None = None) -> dict[str, Any]:
        """Make sure the dataset exists (creates it when ``create_if_missing``)."""
        resolved = client or self._resolved_client()
        data = resolved.create_dataset(self.name) if self.create_if_missing else resolved.get_dataset(self.name)
        self._ensured = True
        return data

    def get(self) -> dict[str, Any]:
        """Fetch the dataset's metadata, including its current watermark."""
        return self._resolved_client().get_dataset(self.name)

    def _stored_contract(self, *, swallow_errors: bool = True) -> dict[str, Any] | None:
        """Fetch (once per instance) and cache the contract stored on the platform."""
        if self._fetched_contract is _UNSET:
            try:
                self._fetched_contract = self._resolved_client().get_dataset(self.name).get("contract")
            except Exception:
                if not swallow_errors:
                    self._fetched_contract = _UNSET
                    raise
                self._fetched_contract = None
        return self._fetched_contract

    def config_diff(self, client: Client | None = None) -> dict[str, tuple[Any, Any]]:
        """Compare the in-code contract/freshness against the stored config.

        Returns ``{key: (stored, local)}`` for each differing key. Raises on
        fetch failure — callers decide policy. An unknown dataset counts as
        ``stored = {}`` when ``create_if_missing``, else the 404 propagates.
        """
        resolved = client or self._resolved_client()
        try:
            stored = resolved.get_dataset(self.name)
        except RebaseWorkflowError:
            if not self.create_if_missing:
                raise
            stored = {}
        if self._fetched_contract is _UNSET:
            self._fetched_contract = stored.get("contract")
        diff: dict[str, tuple[Any, Any]] = {}
        for key, local in (("contract", self.contract), ("freshness", self.freshness)):
            remote = stored.get(key)
            if local is None or json.dumps(local, sort_keys=True) == json.dumps(remote, sort_keys=True):
                continue
            diff[key] = (remote, local)
        return diff

    def push_config(self, client: Client | None = None, *, diff: dict[str, tuple[Any, Any]] | None = None) -> None:
        """Publish the in-code contract/freshness to the platform."""
        resolved = client or self._resolved_client()
        if diff is None:
            diff = self.config_diff(resolved)
        if not diff:
            return
        if self.create_if_missing and not self._ensured:
            self.ensure(resolved)
        updates = {key: local for key, (_, local) in diff.items()}
        resolved.update_dataset(self.name, **updates)
        if "contract" in updates:
            self._fetched_contract = updates["contract"]
        self._config_synced = True

    def _sync_config(self, client: Client | None = None) -> None:
        """First-use config publication (once per instance).

        Publishes the in-code contract/freshness only when the platform has
        none stored yet. Drift against an existing stored config is warned
        about but never overwritten at runtime — changing a published config
        is deliberate: ``rebase dataset sync`` or a deploy after syncing.
        """
        if self._config_synced or (self.contract is None and self.freshness is None):
            return
        self._config_synced = True
        resolved = client or self._resolved_client()
        try:
            diff = self.config_diff(resolved)
        except Exception as exc:  # noqa: BLE001 - runtime config sync is best-effort
            warnings.warn(
                f"dataset {self.name}: could not fetch stored config, skipping sync: {exc}",
                stacklevel=2,
            )
            return
        publishable = {key: pair for key, pair in diff.items() if pair[0] is None}
        drifted = [key for key, pair in diff.items() if pair[0] is not None]
        if drifted:
            warnings.warn(
                f"dataset {self.name}: in-code {' and '.join(drifted)} differs from the stored config; "
                "run `rebase dataset sync` to update it (the in-code version is still used locally)",
                stacklevel=2,
            )
        if publishable:
            self.push_config(resolved, diff=publishable)

    def validate(self, df: Any, *, raise_on_failure: bool = False) -> Any:
        """Validate a DataFrame against this dataset's contract (in-code, else stored).

        Returns a :class:`rebase.ValidationReport`; with ``raise_on_failure=True`` a failed
        report raises :class:`rebase.ContractViolation` instead.
        """
        from rebase.contract import ContractViolation, validate_frame, violation_message

        contract = self.contract or self._stored_contract(swallow_errors=False)
        if not contract:
            raise ValueError(f"dataset {self.name!r} has no contract")
        report = validate_frame(df, contract, dataset_name=self.name)
        if raise_on_failure and not report.passed:
            raise ContractViolation(violation_message(self.name, report), report=report)
        return report

    def mark_updated(
        self,
        watermark: Any = None,
        *,
        validation: dict[str, Any] | None = None,
        source: str = "sdk",
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Signal that fresh data landed, firing any listening on-update triggers.

        Suppressed during replay runs: replays are shadow executions and never fire
        downstream triggers or move dataset watermarks.
        """
        from rebase.sources.base import _replay_knowledge_time

        if _replay_knowledge_time() is not None:
            logging.getLogger("rebase.client").warning(
                "replay run: suppressing dataset signal for %r — replays never fire downstream triggers",
                self.name,
            )
            return {"suppressed": "replay", "dataset": self.name}
        client = self._resolved_client()
        if self.create_if_missing and not self._ensured:
            self.ensure(client)
        self._sync_config(client)
        return client.signal_dataset(
            self.name, watermark=watermark, validation=validation, source=source, run_id=run_id
        )

    def listeners(self) -> list[str]:
        """List the workflows whose triggers watch this dataset."""
        return self._resolved_client().list_dataset_listeners(self.name)


def _flatten_config(value: Any, prefix: str = "") -> dict[str, Any]:
    if not isinstance(value, dict):
        return {prefix or "<value>": value}
    flat: dict[str, Any] = {}
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, dict):
            flat.update(_flatten_config(item, path))
        else:
            flat[path] = item
    return flat


def _config_diff_lines(key: str, stored: Any, local: Any) -> list[str]:
    """Compact key-level description of a config change, e.g.
    ``contract: properties.price_eur_mwh.maximum: 4000 -> 5000``."""
    if stored is None:
        return [f"{key}: not stored -> declared in code"]
    stored_flat = _flatten_config(stored)
    local_flat = _flatten_config(local)
    lines = []
    for path in sorted(set(stored_flat) | set(local_flat)):
        before = stored_flat.get(path, "<absent>")
        after = local_flat.get(path, "<absent>")
        if before != after:
            lines.append(f"{key}: {path}: {before!r} -> {after!r}")
    return lines or [f"{key}: changed"]


def preflight_datasets(client: Client | None = None, *, datasets: list[Dataset] | None = None) -> None:
    """Reconcile declared dataset configs with the platform before a deploy.

    First publications (nothing stored yet) are pushed; drift against an
    existing stored config fails the deploy — contract evolution is deliberate
    (``rebase dataset sync``). Fetch failures also fail: a deploy must not
    proceed on unknown dataset state.
    """
    resolved = client or default_client()
    drift_reports: list[str] = []
    for dataset in datasets if datasets is not None else registered_datasets():
        try:
            diff = dataset.config_diff(resolved)
        except Exception as exc:
            raise RebaseWorkflowError(
                f"dataset {dataset.name}: could not verify stored config before deploy: {exc}"
            ) from exc
        publishable = {key: pair for key, pair in diff.items() if pair[0] is None}
        drifted = {key: pair for key, pair in diff.items() if pair[0] is not None}
        if drifted:
            lines = [
                line
                for key, (stored, local) in sorted(drifted.items())
                for line in _config_diff_lines(key, stored, local)
            ]
            drift_reports.append(f"  {dataset.name}:\n    " + "\n    ".join(lines))
            continue
        if publishable:
            dataset.push_config(resolved, diff=publishable)
    if drift_reports:
        raise RebaseWorkflowError(
            "dataset config drift — the in-code definition differs from the stored one:\n\n"
            + "\n".join(drift_reports)
            + "\n\nRun `rebase dataset check <file>` to review and `rebase dataset sync <file>` to apply, "
            "then deploy again."
        )


@dataclass
class TriggerContext:
    """Why a triggered workflow run fired, injected into the reserved ``ctx`` parameter.

    Declare ``ctx=None`` on a workflow entrypoint (``def forecast(ctx=None): ...``) and the
    platform passes a payload describing the firing; rebuild it with :meth:`from_payload`.
    """

    reason: str = "api"
    fired_at: str | None = None
    source_run_id: str | None = None
    source_workflow: str | None = None
    since: dict[str, Any] = field(default_factory=dict)
    latest: dict[str, Any] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    deadline: str | None = None
    is_replay: bool = False
    replay: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> TriggerContext:
        data = payload if isinstance(payload, dict) else {}
        return cls(
            reason=str(data.get("reason", "api")),
            fired_at=data.get("fired_at"),
            source_run_id=data.get("source_run_id"),
            source_workflow=data.get("source_workflow"),
            since=dict(data.get("since") or {}),
            latest=dict(data.get("latest") or {}),
            missing=list(data.get("missing") or []),
            deadline=data.get("deadline"),
            is_replay=bool(data.get("is_replay", False)),
            replay=dict(data.get("replay") or {}),
            raw=data,
        )


class HuggingFacePublishConfig:
    def __init__(
        self,
        repo_id: str,
        *,
        private: bool = True,
        repo_type: str = "model",
        artifact_path: str | Path | None = None,
        path_in_repo: str | None = None,
        revision: str = "main",
        token: str | None = None,
        create_repo: bool = True,
        include_source: bool = True,
        sync_source_git: bool = True,
        allow_patterns: str | list[str] | None = None,
        ignore_patterns: str | list[str] | None = None,
        delete_patterns: str | list[str] | None = None,
        commit_message: str | None = None,
    ) -> None:
        cleaned_repo_id = repo_id.strip()
        if not cleaned_repo_id or "/" not in cleaned_repo_id or cleaned_repo_id.startswith("/"):
            raise ValueError("repo_id must use the Hugging Face namespace/name format")
        if cleaned_repo_id.endswith("/") or "//" in cleaned_repo_id:
            raise ValueError("repo_id must use the Hugging Face namespace/name format")
        if repo_type not in {"model", "dataset"}:
            raise ValueError("repo_type must be 'model' or 'dataset'")
        cleaned_revision = revision.strip()
        if not cleaned_revision:
            raise ValueError("revision cannot be empty")
        self.repo_id = cleaned_repo_id
        self.private = private
        self.repo_type = repo_type
        self.artifact_path = Path(artifact_path).expanduser() if artifact_path is not None else None
        self.path_in_repo = path_in_repo.strip("/") if path_in_repo else None
        self.revision = cleaned_revision
        self.token = token
        self.create_repo = create_repo
        self.include_source = include_source
        self.sync_source_git = sync_source_git
        self.allow_patterns = allow_patterns
        self.ignore_patterns = ignore_patterns
        self.delete_patterns = delete_patterns
        self.commit_message = commit_message


def _image_spec_for(
    *,
    image: Image | dict[str, Any] | None = None,
    dependencies: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any] | None:
    if image is not None and dependencies:
        raise ValueError("provide either image or dependencies, not both")
    if isinstance(image, Image):
        return image.legacy_spec()
    if isinstance(image, dict):
        return image
    if dependencies is not None:
        return Image.python().uv_pip_install(*dependencies).to_dict()
    return Image.python().to_dict()


def _installs_emflow(package: str) -> bool:
    normalized = package.strip().lower()
    return (
        normalized == "emflow"
        or normalized.startswith("emflow ")
        or normalized.startswith("emflow=")
        or normalized.startswith("emflow>")
        or normalized.startswith("emflow<")
        or "github.com/rebase-energy/emflow" in normalized
    )


def _model_dependencies(dependencies: list[str] | tuple[str, ...] | None = None) -> list[str]:
    packages = [package for package in dependencies or [] if package.strip()]
    if not any(_installs_emflow(package) for package in packages):
        packages.append(DEFAULT_MODEL_DEPENDENCY)
    return packages


def _model_image_spec_for(
    *,
    image: Image | dict[str, Any] | None = None,
    dependencies: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any] | None:
    if image is not None and dependencies:
        raise ValueError("provide either image or dependencies, not both")
    if image is None:
        return _image_spec_for(dependencies=_model_dependencies(dependencies))
    if isinstance(image, Image):
        if image.build_enabled:
            raise ValueError("models do not support uv_sync or add_local_* image operations yet")
        packages = _model_dependencies(image.uv_pip_packages)
        built = Image.python(image.python_version).uv_pip_install(*packages, uv_version=image.uv_version)
        return built.legacy_spec()
    if isinstance(image, dict):
        if image.get("kind", "python") != "python":
            raise ValueError("only python images are supported")
        copied = dict(image)
        copied["uv_pip_packages"] = _model_dependencies(
            [str(package) for package in copied.get("uv_pip_packages") or []]
        )
        copied.setdefault("python_version", DEFAULT_PYTHON_VERSION)
        copied.setdefault("uv_version", None)
        return copied
    return _image_spec_for(image=image)


def configure(
    *,
    api_key: str | None = None,
    api_url: str | None = None,
    profile: str | None = None,
    access_token: str | None = None,
    environment_name: str | None = None,
) -> None:
    global _default_client
    _default_client = Client(
        api_key=api_key,
        api_url=api_url,
        profile=profile,
        access_token=access_token,
        environment_name=environment_name,
    )


def default_client() -> Client:
    global _default_client
    if _default_client is None:
        _default_client = Client()
    return _default_client


def _source_for(fn: Callable[..., Any], *, target: str) -> str:
    if not isinstance(fn, FunctionType):
        raise TypeError(f"{target} requires a plain Python function")
    try:
        source = textwrap.dedent(inspect.getsource(fn))
    except OSError as exc:
        raise RebaseWorkflowError(
            f"Could not read source for {target}. Define it in a .py file or a notebook cell "
            "where Python can inspect the function source."
        ) from exc
    try:
        module = ast.parse(source)
    except SyntaxError:
        return source

    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == fn.__name__:
            if node.end_lineno is None:
                return source
            lines = source.splitlines()
            return "\n".join(lines[node.lineno - 1 : node.end_lineno]) + "\n"
    return source


def _source_slice(source_lines: list[str], node: ast.AST) -> str:
    lineno = getattr(node, "lineno", None)
    end_lineno = getattr(node, "end_lineno", None)
    if not isinstance(lineno, int) or not isinstance(end_lineno, int):
        return ""
    return "\n".join(source_lines[lineno - 1 : end_lineno])


def _format_import_alias(alias: ast.alias) -> str:
    return f"{alias.name} as {alias.asname}" if alias.asname else alias.name


def _module_import_source_for(obj: Any) -> tuple[list[str], list[str]]:
    module = inspect.getmodule(obj)
    if module is None:
        return [], []
    try:
        source = textwrap.dedent(inspect.getsource(module))
    except (OSError, TypeError):
        return [], []
    try:
        parsed = ast.parse(source)
    except SyntaxError:
        return [], []

    source_lines = source.splitlines()
    future_imports: list[str] = []
    imports: list[str] = []
    for node in parsed.body:
        if isinstance(node, ast.Import):
            aliases = [alias for alias in node.names if alias.name != "rebase"]
            if aliases:
                imports.append("import " + ", ".join(_format_import_alias(alias) for alias in aliases))
        elif isinstance(node, ast.ImportFrom):
            module_name = node.module or ""
            if module_name == "__future__":
                future_imports.append(_source_slice(source_lines, node))
            elif module_name == "rebase" or module_name.startswith("rebase."):
                continue
            else:
                imports.append(_source_slice(source_lines, node))
    return _dedupe_lines(future_imports), _dedupe_lines(imports)


def _dedupe_lines(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for line in lines:
        if not line or line in seen:
            continue
        seen.add(line)
        deduped.append(line)
    return deduped


def _validate_model_constructor(model: Model) -> None:
    signature = inspect.signature(model.__class__)
    required = [
        name
        for name, parameter in signature.parameters.items()
        if parameter.default is inspect.Parameter.empty
        and parameter.kind
        in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }
    ]
    if required:
        raise RebaseWorkflowError(
            "Deployable models must be constructible without required __init__ arguments. "
            f"Missing defaults for: {', '.join(required)}"
        )


def _source_for_model(model: Model, *, operation_name: str) -> str:
    _validate_model_constructor(model)
    try:
        class_source = textwrap.dedent(inspect.getsource(model.__class__))
    except OSError as exc:
        raise RebaseWorkflowError(
            "Could not read source for model. Define it in a .py file or a notebook cell "
            "where Python can inspect the class source."
        ) from exc

    operation = getattr(model, operation_name)
    signature = inspect.signature(operation)
    parameter_names: list[str] = []
    for name, parameter in signature.parameters.items():
        if parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
            inspect.Parameter.POSITIONAL_ONLY,
        }:
            raise TypeError(
                f"{model.__class__.__name__}.{operation_name} can only use positional-or-keyword "
                "and keyword-only parameters"
            )
        parameter_names.append(name)

    call_arguments = ", ".join(f"{name}={name}" for name in parameter_names)
    call_expression = f"model.{operation_name}({call_arguments})" if call_arguments else f"model.{operation_name}()"
    future_imports, imports = _module_import_source_for(model.__class__)
    runtime_prelude = textwrap.dedent(
        """
        class _RebaseModel:
            def __init__(self, name=None):
                cls = type(self)
                self.name = name if name is not None else getattr(cls, "name", None)

        class _RebasePredictor(_RebaseModel):
            pass

        class _RebaseOptimizer(_RebaseModel):
            pass

        class _RebaseAgent(_RebaseModel):
            pass

        class _RebaseSimulator(_RebaseModel):
            pass

        class _RebaseNamespace:
            Model = _RebaseModel
            Predictor = _RebasePredictor
            Optimizer = _RebaseOptimizer
            Agent = _RebaseAgent
            Simulator = _RebaseSimulator

        rb = _RebaseNamespace()
        rebase = rb
        Model = _RebaseModel
        Predictor = _RebasePredictor
        Optimizer = _RebaseOptimizer
        Agent = _RebaseAgent
        Simulator = _RebaseSimulator
        """
    ).strip()
    wrapper = textwrap.dedent(
        f"""
        def {operation_name}{signature}:
            model = {model.__class__.__name__}()
            return {call_expression}
        """
    ).strip()
    sections = [*future_imports, *imports, runtime_prelude, class_source.strip(), wrapper]
    return "\n\n".join(section for section in sections if section) + "\n"


def _defaults_for(fn: Callable[..., Any], *, target: str, reserved: frozenset[str] = frozenset()) -> dict[str, Any]:
    signature = inspect.signature(fn)
    defaults: dict[str, Any] = {}
    for name, parameter in signature.parameters.items():
        if parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
            inspect.Parameter.POSITIONAL_ONLY,
        }:
            raise TypeError(f"{target} can only use positional-or-keyword and keyword-only parameters")
        if name in reserved:
            continue
        if parameter.default is inspect.Parameter.empty:
            continue
        default = parameter.default
        # ForecastWindow defaults are stored in their JSON form so remote runs can
        # round-trip them with ForecastWindow.coerce().
        from rebase.timing import ForecastWindow

        if isinstance(default, ForecastWindow):
            default = default.to_dict()
        defaults[name] = default
    return defaults


def _required_parameters_for(
    fn: Callable[..., Any], *, target: str, reserved: frozenset[str] = frozenset()
) -> list[str]:
    signature = inspect.signature(fn)
    required: list[str] = []
    for name, parameter in signature.parameters.items():
        if parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
            inspect.Parameter.POSITIONAL_ONLY,
        }:
            raise TypeError(f"{target} can only use positional-or-keyword and keyword-only parameters")
        if name in reserved:
            continue
        if parameter.default is inspect.Parameter.empty:
            required.append(name)
    return required


def _target_name(fn: FunctionType, name: str | None) -> str:
    return name or fn.__name__.replace("_", "-")


def _model_target_name(cls: type[Any]) -> str:
    name = re.sub(r"(?<!^)(?=[A-Z])", "-", cls.__name__).replace("_", "-").lower()
    return name or "model"


def _node_key_for(name: str, existing: set[str]) -> str:
    base = re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_").lower() or "step"
    candidate = base
    suffix = 2
    while candidate in existing:
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def _assert_json_literal(value: Any) -> None:
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise RebaseWorkflowError(
            f"Step workflows can only capture JSON-serializable literal values; got {type(value).__name__}."
        ) from exc


class _TraceValue:
    def _unsupported(self, operation: str) -> RebaseWorkflowError:
        return RebaseWorkflowError(
            f"Step workflows must be static. Runtime values cannot be used with {operation} during deployment."
        )

    def __bool__(self) -> bool:
        raise self._unsupported("boolean branching")

    def __iter__(self):
        raise self._unsupported("iteration")

    def __len__(self) -> int:
        raise self._unsupported("len()")

    def __getitem__(self, key: Any) -> Any:
        raise self._unsupported(f"index access {key!r}")

    def __getattr__(self, name: str) -> Any:
        raise self._unsupported(f"attribute access {name!r}")


class _WorkflowParameter(_TraceValue):
    def __init__(self, name: str) -> None:
        self.name = name


class StepPromise(_TraceValue):
    def __init__(self, node_key: str) -> None:
        self.node_key = node_key


def _binding_for(value: Any) -> dict[str, Any]:
    if isinstance(value, StepPromise):
        return {"type": "node_output", "node_key": value.node_key}
    if isinstance(value, _WorkflowParameter):
        return {"type": "parameter", "name": value.name}
    if isinstance(value, dict):
        items: dict[str, dict[str, Any]] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise RebaseWorkflowError("Step workflow dict bindings must use string keys.")
            items[key] = _binding_for(item)
        return {"type": "dict", "items": items}
    if isinstance(value, list):
        return {"type": "list", "items": [_binding_for(item) for item in value]}
    if isinstance(value, tuple):
        return {"type": "tuple", "items": [_binding_for(item) for item in value]}
    _assert_json_literal(value)
    return {"type": "literal", "value": value}


def _binding_node_keys(binding: dict[str, Any]) -> set[str]:
    binding_type = binding.get("type")
    if binding_type == "node_output":
        return {str(binding["node_key"])}
    if binding_type in {"list", "tuple"}:
        node_keys: set[str] = set()
        for item in binding.get("items", []):
            node_keys.update(_binding_node_keys(item))
        return node_keys
    if binding_type == "dict":
        node_keys = set()
        for item in binding.get("items", {}).values():
            node_keys.update(_binding_node_keys(item))
        return node_keys
    return set()


class _WorkflowTrace:
    """Compiles a workflow body into a chain of step nodes.

    Steps are sequential by contract: each one runs after the one before it, whether or
    not it consumes its output. The graph could express parallel roots — dependencies are
    derived from data bindings, so two steps that ignore each other would have none — but
    a chain is what makes a workflow legible: a straight line to draw, and one unambiguous
    answer to "which step failed, and what never ran because of it". Independent work that
    wants to run side by side belongs to tasks inside a step, not to steps.
    """

    def __init__(self, *, ephemeral: bool = False) -> None:
        self.ephemeral = ephemeral
        self.nodes: list[dict[str, Any]] = []
        self._node_keys: set[str] = set()
        self._previous_node_key: str | None = None

    def record_step(self, step: Step, args: tuple[Any, ...], kwargs: dict[str, Any]) -> StepPromise:
        step_name = str(step.name)
        if step.fn is None:
            raise RebaseWorkflowError(f"Step {step_name!r} cannot be used in a workflow without local source.")
        if not self.ephemeral and step.id is None:
            raise RebaseWorkflowError(
                f"Step {step_name!r} has not been deployed yet. Deploy the Project so steps are registered "
                "before workflows are compiled."
            )
        function_version_id = step.data.get("current_version_id")
        if not self.ephemeral and function_version_id is None:
            raise RebaseWorkflowError(f"Step {step_name!r} has no current function version after deployment.")

        signature = inspect.signature(step.fn)
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        input_bindings = {name: _binding_for(value) for name, value in bound.arguments.items()}
        upstream_node_keys: set[str] = set()
        for binding in input_bindings.values():
            upstream_node_keys.update(_binding_node_keys(binding))
        # The ordering edge, on top of whatever the data bindings imply. Kept separate from
        # `input_bindings`, so the graph still shows which dependency carries a value and
        # which is only sequence.
        if self._previous_node_key is not None:
            upstream_node_keys.add(self._previous_node_key)

        node_key = _node_key_for(step_name, self._node_keys)
        self._node_keys.add(node_key)
        node = {
            "node_key": node_key,
            "name": step_name,
            "function_id": step.id,
            "function_version_id": function_version_id,
            "entrypoint": step.entrypoint,
            "input_bindings": input_bindings,
            "upstream_node_keys": sorted(upstream_node_keys),
            "retry_policy": {"retries": step.retries},
            "timeout_seconds": step.timeout_seconds,
            "cache_policy": {"enabled": step.cache},
            "resource_policy": step.resources,
        }
        if self.ephemeral:
            node.update(
                {
                    "source_code": step.source_code,
                    "default_parameters": step.default_parameters,
                    "mode": step.mode,
                    "isolation": step.isolation,
                    "image_spec": step.image_spec,
                }
            )
        self.nodes.append(node)
        self._previous_node_key = node_key
        return StepPromise(node_key)


def _current_trace() -> _WorkflowTrace | None:
    return _trace_stack[-1] if _trace_stack else None


def _schedule_payload(schedule: Schedule | None) -> dict[str, Any] | None:
    if schedule is None:
        return None
    if isinstance(schedule, Cron):
        return schedule.to_dict()
    if isinstance(schedule, dict):
        if "parameters" in schedule:
            raise TypeError("Cron schedules do not accept parameters; define defaults on the workflow function")
        return schedule
    raise TypeError("schedule must be rb.Cron(...) or a schedule dictionary")


def _trigger_payload(trigger: Trigger | None) -> dict[str, Any] | None:
    if trigger is None:
        return None
    if isinstance(trigger, (OnWorkflow, OnUpdate)):
        return trigger.to_dict()
    if isinstance(trigger, dict):
        if "parameters" in trigger:
            raise TypeError("Triggers do not accept parameters; define defaults on the workflow function")
        return trigger
    raise TypeError("trigger must be rb.OnWorkflow(...), rb.OnUpdate(...), or a trigger dictionary")


def _git(args: list[str], *, cwd: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    value = completed.stdout.strip()
    return value or None


def _parse_github_remote(remote: str | None) -> tuple[str | None, str | None]:
    if remote is None:
        return None, None
    patterns = [
        r"^git@github\.com:(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$",
        r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$",
    ]
    for pattern in patterns:
        match = re.match(pattern, remote)
        if match:
            return match.group("owner"), match.group("repo")
    return None, None


def _parse_gitlab_remote(remote: str | None) -> tuple[str | None, str | None]:
    """Parse a GitLab remote (gitlab.com or self-managed) into (namespace, project).

    GitLab namespaces can nest (group/subgroup/project) — owner is the full namespace path.
    """
    if remote is None:
        return None, None
    patterns = [
        r"^git@(?P<host>[^:]*gitlab[^:]*):(?P<path>.+?)(?:\.git)?$",
        r"^https://(?P<host>[^/]*gitlab[^/]*)/(?P<path>.+?)(?:\.git)?$",
    ]
    for pattern in patterns:
        match = re.match(pattern, remote)
        if match:
            path = match.group("path").strip("/")
            if "/" in path:
                owner, _, name = path.rpartition("/")
                return owner, name
    return None, None


def _git_metadata_for(fn: Callable[..., Any]) -> dict[str, Any]:
    source_file = inspect.getsourcefile(fn)
    if source_file is None:
        return {}

    source_path = Path(source_file).resolve()
    reconciler_root = os.getenv("REBASE_GITOPS_REPO_ROOT")
    reconciler_sha = os.getenv("REBASE_GITOPS_COMMIT_SHA")
    if reconciler_root and reconciler_sha:
        try:
            relative_source_path = source_path.relative_to(Path(reconciler_root).resolve())
        except ValueError:
            pass
        else:
            return {
                "repo_owner": os.getenv("REBASE_GITOPS_REPO_OWNER"),
                "repo_name": os.getenv("REBASE_GITOPS_REPO_NAME"),
                "source_path": str(relative_source_path),
                "git_commit_sha": reconciler_sha,
                "git_branch": os.getenv("REBASE_GITOPS_BRANCH"),
                "git_tag": os.getenv("REBASE_GITOPS_TAG"),
                "git_dirty": False,
            }
    root = _git(["rev-parse", "--show-toplevel"], cwd=source_path.parent)
    if root is None:
        return {}

    root_path = Path(root).resolve()
    try:
        relative_source_path = source_path.relative_to(root_path)
    except ValueError:
        return {}

    remote = _git(["config", "--get", "remote.origin.url"], cwd=root_path)
    repo_owner, repo_name = _parse_github_remote(remote)
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root_path)
    if branch == "HEAD":
        branch = None
    tag = _git(["describe", "--tags", "--exact-match", "HEAD"], cwd=root_path)
    dirty = _git(["status", "--porcelain", "--", str(relative_source_path)], cwd=root_path) is not None

    return {
        key: value
        for key, value in {
            "repo_owner": repo_owner,
            "repo_name": repo_name,
            "source_path": str(relative_source_path),
            "git_commit_sha": _git(["rev-parse", "HEAD"], cwd=root_path),
            "git_branch": branch,
            "git_tag": tag,
            "git_dirty": dirty,
        }.items()
        if value is not None
    }


def _source_metadata_for_deploy(
    metadata: dict[str, Any],
    *,
    deploy_source: str | None,
    project_source_mode: str | None = None,
) -> dict[str, Any]:
    resolved_source = _validate_deploy_source(deploy_source) or DEFAULT_DEPLOY_SOURCE
    resolved = dict(metadata)
    if resolved_source == "rebase":
        resolved["source_mode"] = "rebase_hosted"
        resolved["git_dirty"] = bool(resolved.get("git_dirty", False))
        return resolved

    missing = [
        field for field in ("repo_owner", "repo_name", "source_path", "git_commit_sha") if not resolved.get(field)
    ]
    if missing:
        raise RebaseWorkflowError(
            "GitHub deploy requires the source file to be in a GitHub-backed git repository. "
            f"Missing metadata: {', '.join(missing)}."
        )
    if resolved.get("git_dirty"):
        raise RebaseWorkflowError(
            "GitHub deploy requires the source file to be committed. "
            "Commit or discard changes to this file before deploying."
        )
    resolved["source_mode"] = (
        project_source_mode if project_source_mode in {"workspace_repo", "project_repo"} else "workspace_repo"
    )
    resolved["git_dirty"] = False
    return resolved


def _connected_source_mode(
    client: Any,
    *,
    project: str | None,
    project_source_mode: str | None,
) -> str | None:
    if project_source_mode in {"workspace_repo", "project_repo"}:
        return project_source_mode
    if project:
        project_data = client.find_project(project)
        project_mode = project_data.get("source_mode") if isinstance(project_data, dict) else None
        if project_mode in {"workspace_repo", "project_repo"}:
            return project_mode
    workspace = client.get_workspace()
    workspace_mode = workspace.get("source_mode") if isinstance(workspace, dict) else None
    return workspace_mode if workspace_mode in {"workspace_repo", "project_repo"} else None


def _target_source_metadata_for_deploy(target: Any, deploy_source: str | None) -> dict[str, Any]:
    """Resolve the source metadata shared by functions, workflows, and ASGI apps."""
    resolved_deploy_source = _validate_deploy_source(deploy_source) or target.deploy_source
    project_source_mode = target.project_source_mode
    if resolved_deploy_source == "github":
        project_source_mode = _connected_source_mode(
            target._client,
            project=target.project,
            project_source_mode=project_source_mode,
        )
    return _source_metadata_for_deploy(
        target.source_metadata,
        deploy_source=resolved_deploy_source,
        project_source_mode=project_source_mode,
    )


class Client:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_url: str | None = None,
        profile: str | None = None,
        access_token: str | None = None,
        workspace_id: str | None = None,
        environment_name: str | None = None,
    ) -> None:
        env_api_key = os.getenv("REBASE_API_KEY") or os.getenv("REBASE_WORKFLOWS_API_KEY")
        env_access_token = os.getenv("REBASE_ACCESS_TOKEN") or os.getenv("REBASE_WORKFLOWS_ACCESS_TOKEN")
        explicit_credentials = any(
            credential is not None for credential in (api_key, access_token, env_api_key, env_access_token)
        )
        profile_data = {} if explicit_credentials and profile is None else load_profile(profile)
        configured_api_key = profile_data.get("api_key")
        configured_api_url = profile_data.get("api_url")
        configured_workspace_id = profile_data.get("workspace_id")
        self.access_token = access_token or env_access_token
        self.api_key = api_key or env_api_key
        # A `.rebase/config.json` marker pins the workspace, not the credentials: the
        # workspace travels as one header and an API key reaches every workspace you
        # belong to, so a repo can override the globally active workspace without a
        # second profile or another sign-in. Precedence: argument, REBASE_WORKSPACE,
        # marker, profile -- the env var outranks the marker because setting it is a
        # deliberate act where the marker is ambient.
        pinned_workspace_id = local_workspace_id()
        env_workspace_id = os.getenv("REBASE_WORKSPACE")
        resolved_workspace_id = (
            workspace_id
            or env_workspace_id
            or pinned_workspace_id
            or (configured_workspace_id if isinstance(configured_workspace_id, str) else None)
        )
        self.workspace_id = resolved_workspace_id or None
        if self.api_key is None and self.access_token is None and isinstance(configured_api_key, str):
            # A deliberate override -- an explicit argument or REBASE_WORKSPACE, as set
            # by `--workspace` -- must not silently reuse the profile's stored API key.
            # That key is minted for one workspace (api_keys.workspace_id is NOT NULL)
            # and the server reads the workspace off the key row, ignoring the header,
            # so carrying it would quietly act on the wrong workspace. Falling through
            # to the signed-in session is also the only path on which superadmin works.
            #
            # The directory marker is deliberately exempt: it is ambient context, and
            # keeping the key on that path is long-standing documented behaviour.
            deliberate_override = workspace_id or env_workspace_id
            if not deliberate_override or resolved_workspace_id == configured_workspace_id:
                self.api_key = configured_api_key
        configured_environment = active_environment(self.workspace_id) if self.workspace_id else None
        self.environment_name = environment_name or os.getenv("REBASE_ENVIRONMENT") or configured_environment or "dev"
        profile_api_url = configured_api_url if isinstance(configured_api_url, str) else None
        selected_api_url = api_url or os.getenv("REBASE_WORKFLOWS_API_URL") or profile_api_url or DEFAULT_SERVER_URL
        self.api_url = selected_api_url.rstrip("/")
        # Lazily-created keep-alive session plus the pid that owns it: reusing one
        # connection avoids a fresh TCP+TLS handshake per request (the bulk of
        # per-call latency), and the pid check rebuilds it after a fork, where an
        # inherited socket would be shared with the parent.
        self._session: requests.Session | None = None
        self._session_pid: int | None = None
        # Cache for tokens read from disk by load_access_token(); explicit
        # credentials (api_key/access_token) never go through this.
        self._cached_disk_token: str | None = None
        # Routes this platform answered 404/405 for. Only the composite reads use it,
        # and only to stop re-asking: the TUI re-reads its view every few seconds, so
        # against a platform behind this package the wasted probe would be paid on every
        # refresh rather than once. A platform upgraded mid-session is picked up on the
        # next run, which is soon enough for a route whose absence only costs speed.
        self._absent_routes: set[str] = set()
        # The ETag of the last composite view read, per path, per workspace and
        # environment it was read in. A conditional re-read sends it back and gets a 304
        # when nothing changed, which is most of the TUI's ticks. Deliberately not copied
        # by `_clone`/`as_session`: a clone is what a workspace or environment switch
        # produces, and its first read must be answered in full.
        self._etags: dict[tuple[str, str | None, str], str] = {}

    def _http_session(self) -> requests.Session:
        pid = os.getpid()
        if self._session is None or self._session_pid != pid:
            # No retry adapter on purpose: a retried POST /runs/ephemeral would
            # execute the run twice.
            self._session = requests.Session()
            self._session_pid = pid
        return self._session

    def with_environment(self, environment_name: str) -> Client:
        return self._clone(workspace_id=self.workspace_id, environment_name=environment_name, api_key=self.api_key)

    def with_workspace(self, workspace_id: str, *, environment_name: str | None = None) -> Client:
        """The same credentials pointed at another workspace, for this process only.

        Nothing is written to the profile: two terminals can each hold a different
        workspace against one signed-in profile, and closing the process forgets the
        choice. An API key does not travel across: it is minted for one workspace and
        the server reads the workspace off the key row rather than the header, so the
        clone falls through to the signed-in session the way `--workspace` does.
        Without an environment the new workspace's configured default applies.
        """
        api_key = self.api_key if workspace_id == self.workspace_id else None
        resolved_environment = environment_name or active_environment(workspace_id) or "dev"
        return self._clone(workspace_id=workspace_id, environment_name=resolved_environment, api_key=api_key)

    def as_session(self) -> Client | None:
        """This client on the signed-in session instead of its API key; None without one.

        `/me/workspaces` and the other personal routes answer to a person. An API key
        belongs to one workspace and has no memberships to list, so a key-backed
        profile has to step over to the session `rebase setup` signed in with.
        """
        if not self.api_key:
            return self
        try:
            token = load_access_token()
        except AuthError:
            return None
        if token is None:
            return None
        clone = Client(
            access_token=token,
            api_url=self.api_url,
            workspace_id=self.workspace_id,
            environment_name=self.environment_name,
        )
        clone._absent_routes = self._absent_routes
        return clone

    def _clone(self, *, workspace_id: str | None, environment_name: str, api_key: str | None) -> Client:
        clone = Client(
            api_key=api_key,
            api_url=self.api_url,
            access_token=self.access_token,
            workspace_id=workspace_id,
            environment_name=environment_name,
        )
        clone._cached_disk_token = self._cached_disk_token
        # Same platform, so the same routes are missing from it.
        clone._absent_routes = self._absent_routes
        return clone

    def _http_request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """Single seam for all HTTP traffic; tests patch this instead of `requests`."""
        return self._http_session().request(method, f"{self.api_url}{path}", **kwargs)

    def _bearer_token(self) -> str | None:
        token = self.api_key or self.access_token
        if token is not None:
            return token
        if self._cached_disk_token is None:
            try:
                self._cached_disk_token = load_access_token()
            except AuthError as exc:
                raise RebaseWorkflowError(str(exc)) from exc
        return self._cached_disk_token

    def _invalidate_cached_token(self) -> bool:
        """Drop the disk-token cache after a 401. Returns whether a retry makes sense:
        only when the rejected token actually came from the cache (not an explicit
        api_key/access_token, which a retry would resend unchanged)."""
        if self.api_key or self.access_token or self._cached_disk_token is None:
            return False
        self._cached_disk_token = None
        return True

    def _request_headers(self, *, auth: bool, headers: dict[str, str] | None = None) -> dict[str, str]:
        resolved_headers = dict(headers or {})
        if auth:
            bearer_token = self._bearer_token()
            if bearer_token:
                resolved_headers["Authorization"] = f"Bearer {bearer_token}"
        if self.workspace_id and "X-Rebase-Workspace" not in resolved_headers:
            resolved_headers["X-Rebase-Workspace"] = self.workspace_id
        if "X-Rebase-Environment" not in resolved_headers:
            resolved_headers["X-Rebase-Environment"] = _environment_context.get() or self.environment_name
        release_id = os.getenv("REBASE_GITOPS_RELEASE_ID")
        if release_id and "X-Rebase-GitOps-Release" not in resolved_headers:
            resolved_headers["X-Rebase-GitOps-Release"] = release_id
        return resolved_headers

    def request(
        self, method: str, path: str, *, auth: bool = True, **kwargs: Any
    ) -> dict[str, Any] | list[dict[str, Any]]:
        return self._request_response(method, path, auth=auth, **kwargs).json()

    def _request_response(self, method: str, path: str, *, auth: bool = True, **kwargs: Any) -> requests.Response:
        """Send one authenticated request, including the disk-token retry and error mapping."""
        caller_headers = kwargs.pop("headers", {})
        headers = self._request_headers(auth=auth, headers=caller_headers)
        timeout = kwargs.pop("timeout", 30)
        response = self._http_request(method, path, headers=headers, timeout=timeout, **kwargs)
        if getattr(response, "status_code", None) == 401 and auth and self._invalidate_cached_token():
            headers = self._request_headers(auth=auth, headers=caller_headers)
            response = self._http_request(method, path, headers=headers, timeout=timeout, **kwargs)
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            error = RebaseWorkflowError(_response_error_message(response))
            error.status_code = response.status_code
            raise error from exc
        return response

    def _request_dict(self, method: str, path: str, *, expected: str, **kwargs: Any) -> dict[str, Any]:
        response = self.request(method, path, **kwargs)
        if not isinstance(response, dict):
            raise RebaseWorkflowError(f"expected {expected}")
        return response

    def _request_list(self, method: str, path: str, *, expected: str, **kwargs: Any) -> list[Any]:
        response = self.request(method, path, **kwargs)
        if not isinstance(response, list):
            raise RebaseWorkflowError(f"expected {expected}")
        return response

    def request_no_content(self, method: str, path: str, *, auth: bool = True, **kwargs: Any) -> None:
        """Like :meth:`request`, for endpoints that answer 204 with an empty body.

        ``request`` always parses the response as JSON, which a 204 has none of.
        """
        self._request_response(method, path, auth=auth, **kwargs)

    def stream_request(self, method: str, path: str, *, auth: bool = True, **kwargs: Any) -> Iterator[dict[str, Any]]:
        headers = self._request_headers(auth=auth, headers=kwargs.pop("headers", {}))
        timeout = kwargs.pop("timeout", None)
        response = self._http_request(
            method,
            path,
            headers=headers,
            timeout=timeout,
            stream=True,
            **kwargs,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise RebaseWorkflowError(_response_error_message(response)) from exc

        try:
            for line in response.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError as exc:
                    raise RebaseWorkflowError("expected NDJSON stream response") from exc
                if not isinstance(event, dict):
                    raise RebaseWorkflowError("expected NDJSON stream item to be an object")
                yield event
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

    def setup_config(self) -> dict[str, Any]:
        return self._request_dict("GET", "/setup/config", auth=False, expected="setup config response")

    def list_my_workspaces(self) -> list[dict[str, Any]]:
        return self._request_list("GET", "/me/workspaces", expected="workspace list response")

    def get_my_profile(self) -> dict[str, Any]:
        return self._request_dict("GET", "/me/profile", expected="profile response")

    def create_workspace(self, workspace_id: str, *, name: str | None = None) -> dict[str, Any]:
        return self._request_dict(
            "POST", "/workspaces", json={"id": workspace_id, "name": name}, expected="workspace response"
        )

    def get_workspace_usage(self) -> dict[str, Any]:
        return self._request_dict("GET", "/workspace/usage", expected="workspace usage response")

    def get_workspace_usage_breakdown(self, *, limit: int = 10) -> dict[str, Any]:
        return self._request_dict(
            "GET",
            "/workspace/usage/breakdown",
            params={"limit": limit},
            expected="workspace usage breakdown response",
        )

    def get_run_cost(self, run_id: str) -> dict[str, Any]:
        return self._request_dict("GET", f"/runs/{run_id}/cost", expected="run cost response")

    def list_platform_invites(self) -> list[dict[str, Any]]:
        return self._request_list("GET", "/platform/invites", expected="platform invite list response")

    def create_platform_invite(
        self,
        email: str,
        *,
        expires_at: str | None = None,
        workspace_creation_limit: int | None = 1,
    ) -> dict[str, Any]:
        return self._request_dict(
            "POST",
            "/platform/invites",
            json={"email": email, "expires_at": expires_at, "workspace_creation_limit": workspace_creation_limit},
            expected="platform invite response",
        )

    def revoke_platform_invite(self, invite_id: str) -> dict[str, Any]:
        return self._request_dict("DELETE", f"/platform/invites/{invite_id}", expected="platform invite response")

    def list_workspace_invites(self) -> list[dict[str, Any]]:
        return self._request_list("GET", "/workspace/invites", expected="workspace invite list response")

    def list_workspace_members(self) -> list[dict[str, Any]]:
        return self._request_list("GET", "/workspace/members", expected="workspace member list response")

    def update_workspace_member(
        self,
        profile_id: str,
        *,
        role: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if role is not None:
            payload["role"] = role
        if enabled is not None:
            payload["enabled"] = enabled
        return self._request_dict(
            "PATCH", f"/workspace/members/{profile_id}", json=payload, expected="workspace member response"
        )

    def create_workspace_invite(
        self,
        *,
        email: str | None = None,
        github_username: str | None = None,
        role: str = "Viewer",
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        return self._request_dict(
            "POST",
            "/workspace/invites",
            json={"email": email, "github_username": github_username, "role": role, "expires_at": expires_at},
            expected="workspace invite response",
        )

    def revoke_workspace_invite(self, invite_id: str) -> dict[str, Any]:
        return self._request_dict("DELETE", f"/workspace/invites/{invite_id}", expected="workspace invite response")

    def list_api_keys(self) -> list[dict[str, Any]]:
        return self._request_list("GET", "/workspace/api-keys", expected="API key list response")

    def create_api_key(
        self,
        name: str,
        *,
        project_id: str | None = None,
        permissions: list[str] | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        return self._request_dict(
            "POST",
            "/workspace/api-keys",
            json={
                "name": name,
                "project_id": project_id,
                "permissions": permissions if permissions is not None else list(DEFAULT_API_KEY_PERMISSIONS),
                "expires_at": expires_at,
            },
            expected="API key response",
        )

    def revoke_api_key(self, api_key_id: str) -> dict[str, Any]:
        return self._request_dict("DELETE", f"/workspace/api-keys/{api_key_id}", expected="API key response")

    def set_secret(self, name: str, values: dict[str, str]) -> dict[str, Any]:
        return self._request_dict("PUT", "/secrets", json={"name": name, "values": values}, expected="secret response")

    def get_secret(self, name: str) -> dict[str, Any]:
        return self._request_dict("GET", f"/secrets/{name}", expected="secret response")

    def delete_secret(self, name: str) -> dict[str, Any]:
        return self._request_dict("DELETE", f"/secrets/{name}", expected="secret response")

    def list_secrets(self) -> list[dict[str, Any]]:
        return self._request_list("GET", "/secrets", expected="secret list response")

    def create_volume(self, name: str) -> dict[str, Any]:
        return self._request_dict("POST", "/volumes", json={"name": name}, expected="volume response")

    def list_volumes(self) -> list[dict[str, Any]]:
        return self._request_list("GET", "/volumes", expected="volume list response")

    def get_volume(self, name: str) -> dict[str, Any]:
        return self._request_dict("GET", f"/volumes/{name}", expected="volume response")

    def delete_volume(self, name: str) -> None:
        self.request("DELETE", f"/volumes/{name}")

    def list_volume_objects(self, name: str, *, prefix: str = "", limit: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if prefix:
            params["prefix"] = prefix
        if limit is not None:
            params["limit"] = limit
        return self._request_list(
            "GET", f"/volumes/{name}/objects", params=params or None, expected="volume object list response"
        )

    def create_volume_upload_url(self, name: str, path: str) -> dict[str, Any]:
        return self._request_dict(
            "POST", f"/volumes/{name}/upload-url", json={"path": path}, expected="signed URL response"
        )

    def create_volume_download_url(self, name: str, path: str) -> dict[str, Any]:
        return self._request_dict(
            "POST", f"/volumes/{name}/download-url", json={"path": path}, expected="signed URL response"
        )

    def delete_volume_object(self, name: str, path: str) -> None:
        self.request("DELETE", f"/volumes/{name}/objects", params={"path": path})

    def create_bucket(self, name: str) -> dict[str, Any]:
        return self._request_dict("POST", "/buckets", json={"name": name}, expected="bucket response")

    def list_buckets(self) -> list[dict[str, Any]]:
        return self._request_list("GET", "/buckets", expected="bucket list response")

    def get_bucket(self, name: str) -> dict[str, Any]:
        return self._request_dict("GET", f"/buckets/{name}", expected="bucket response")

    def delete_bucket(self, name: str) -> None:
        self.request("DELETE", f"/buckets/{name}")

    def list_bucket_objects(
        self,
        name: str,
        *,
        prefix: str = "",
        delimiter: str | None = None,
        limit: int | None = None,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if prefix:
            params["prefix"] = prefix
        if delimiter:
            params["delimiter"] = delimiter
        if limit is not None:
            params["limit"] = limit
        if page_token:
            params["page_token"] = page_token
        return self._request_dict(
            "GET", f"/buckets/{name}/objects", params=params or None, expected="bucket object list response"
        )

    def stat_bucket_object(self, name: str, path: str) -> dict[str, Any]:
        return self._request_dict(
            "GET", f"/buckets/{name}/objects/stat", params={"path": path}, expected="bucket object stat response"
        )

    def delete_bucket_object(self, name: str, path: str) -> None:
        self.request("DELETE", f"/buckets/{name}/objects", params={"path": path})

    def create_bucket_signed_urls(
        self,
        name: str,
        paths: list[str],
        *,
        method: str = "GET",
        content_type: str | None = None,
        expires_seconds: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"paths": paths, "method": method}
        if content_type is not None:
            payload["content_type"] = content_type
        if expires_seconds is not None:
            payload["expires_seconds"] = expires_seconds
        return self._request_dict(
            "POST", f"/buckets/{name}/signed-urls", json=payload, expected="bucket signed URL response"
        )

    def create_dataset(self, name: str, description: str | None = None) -> dict[str, Any]:
        return self._request_dict(
            "POST", "/datasets", json={"name": name, "description": description}, expected="dataset response"
        )

    def list_datasets(self) -> list[dict[str, Any]]:
        return self._request_list("GET", "/datasets", expected="dataset list response")

    def get_dataset(self, name: str) -> dict[str, Any]:
        return self._request_dict("GET", f"/datasets/{name}", expected="dataset response")

    def update_dataset(
        self,
        name: str,
        *,
        contract: dict[str, Any] | None | object = _UNSET,
        freshness: dict[str, Any] | None | object = _UNSET,
        description: str | None | object = _UNSET,
    ) -> dict[str, Any]:
        """Update a dataset's contract/freshness/description; omitted keys stay unchanged."""
        payload: dict[str, Any] = {}
        if contract is not _UNSET:
            payload["contract"] = contract
        if freshness is not _UNSET:
            payload["freshness"] = freshness
        if description is not _UNSET:
            payload["description"] = description
        return self._request_dict("PATCH", f"/datasets/{name}", json=payload, expected="dataset response")

    def delete_dataset(self, name: str) -> dict[str, Any]:
        return self._request_dict("DELETE", f"/datasets/{name}", expected="dataset response")

    def signal_dataset(
        self,
        name: str,
        *,
        watermark: Any = None,
        source: str = "sdk",
        run_id: str | None = None,
        validation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"watermark": watermark, "source": source, "run_id": run_id}
        if validation is not None:
            body["validation"] = validation
        return self._request_dict("POST", f"/datasets/{name}/signal", json=body, expected="dataset signal response")

    def list_dataset_listeners(self, name: str) -> list[str]:
        response = self._request_list("GET", f"/datasets/{name}/listeners", expected="dataset listener list response")
        return [str(listener) for listener in response]

    def _with_endpoint_url(self, endpoint: dict[str, Any]) -> dict[str, Any]:
        url_path = endpoint.get("url_path")
        if isinstance(url_path, str):
            return {**endpoint, "url": f"{self.api_url}{url_path}"}
        return endpoint

    def list_endpoints(self, *, project_id: str | None = None) -> list[dict[str, Any]]:
        params = {"project_id": project_id} if project_id is not None else None
        response = self._request_list("GET", "/endpoints", params=params, expected="endpoint list response")
        return [self._with_endpoint_url(endpoint) for endpoint in response]

    def list_project_endpoints(self, project_id: str) -> list[dict[str, Any]]:
        response = self._request_list("GET", f"/projects/{project_id}/endpoints", expected="endpoint list response")
        return [self._with_endpoint_url(endpoint) for endpoint in response]

    def get_endpoint(self, endpoint_id: str) -> dict[str, Any]:
        response = self._request_dict("GET", f"/endpoints/{endpoint_id}", expected="endpoint response")
        return self._with_endpoint_url(response)

    def list_endpoint_versions(self, endpoint_id: str) -> list[dict[str, Any]]:
        return self._request_list(
            "GET", f"/endpoints/{endpoint_id}/versions", expected="endpoint version list response"
        )

    def disable_endpoint(self, endpoint_id: str) -> dict[str, Any]:
        response = self._request_dict(
            "PATCH", f"/endpoints/{endpoint_id}", json={"enabled": False}, expected="endpoint response"
        )
        return self._with_endpoint_url(response)

    def invoke_endpoint(self, endpoint: dict[str, Any], parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        method = str(endpoint.get("method") or "POST").upper()
        url_path = endpoint.get("url_path")
        if not isinstance(url_path, str):
            raise RebaseWorkflowError("endpoint response is missing url_path")
        kwargs: dict[str, Any] = {"params": parameters or {}} if method == "GET" else {"json": parameters or {}}
        return self._request_dict(method, url_path, **kwargs, expected="endpoint invoke response")

    def create_github_setup_session(self, *, workspace_id: str | None = None) -> dict[str, Any]:
        return self._request_dict(
            "POST",
            "/integrations/github/setup-sessions",
            json={"workspace_id": workspace_id},
            expected="GitHub setup response",
        )

    def list_environment_policies(self) -> list[dict[str, Any]]:
        return self._request_list(
            "GET", "/workspace/environments", expected="workspace environment policy list response"
        )

    def list_environments(self) -> list[dict[str, Any]]:
        return self.list_environment_policies()

    def get_environment(self, name: str) -> dict[str, Any]:
        return self._request_dict("GET", f"/environments/{name}", expected="environment response")

    def list_environment_grants(self, name: str) -> list[dict[str, Any]]:
        return self._request_list("GET", f"/environments/{name}/grants", expected="environment grant list response")

    def grant_environment_access(
        self,
        name: str,
        *,
        profile_id: str | None = None,
        api_key_id: str | None = None,
        access: str = "read",
    ) -> dict[str, Any]:
        if (profile_id is None) == (api_key_id is None):
            raise ValueError("provide exactly one of profile_id or api_key_id")
        if access not in {"read", "write", "admin"}:
            raise ValueError("access must be read, write, or admin")
        return self._request_dict(
            "PUT",
            f"/environments/{name}/grants",
            json={"profile_id": profile_id, "api_key_id": api_key_id, "access": access},
            expected="environment grant response",
        )

    def revoke_environment_access(self, name: str, grant_id: str) -> None:
        self.request_no_content("DELETE", f"/environments/{name}/grants/{grant_id}")

    def create_environment(
        self,
        name: str,
        *,
        deploy_mode: str = "direct",
        protected: bool = False,
        require_pr: bool = False,
        allowed_branches: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._request_dict(
            "POST",
            "/environments",
            json={
                "name": name,
                "deploy_mode": deploy_mode,
                "protected": protected,
                "require_pr": require_pr,
                "allowed_branches": allowed_branches or [],
            },
            expected="environment response",
        )

    def delete_environment(self, name: str) -> None:
        self.request_no_content("DELETE", f"/environments/{name}")

    def update_environment_policy(
        self,
        environment: str,
        *,
        deploy_mode: str | None = None,
        protected: bool | None = None,
        require_pr: bool | None = None,
        allowed_branches: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._request_dict(
            "PATCH",
            f"/workspace/environments/{environment}",
            json={
                key: value
                for key, value in {
                    "deploy_mode": deploy_mode,
                    "protected": protected,
                    "require_pr": require_pr,
                    "allowed_branches": allowed_branches,
                }.items()
                if value is not None
            },
            expected="workspace environment policy response",
        )

    def create_gitops_deployment_intent(
        self,
        *,
        environment: str,
        source_repo: str,
        repo_owner: str,
        repo_name: str,
        source_path: str,
        git_commit_sha: str,
        repo_path: str | None = None,
        git_branch: str | None = None,
        project_id: str | None = None,
        plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request_dict(
            "POST",
            "/gitops/deployment-intents",
            json={
                "environment": environment,
                "source_repo": source_repo,
                "repo_owner": repo_owner,
                "repo_name": repo_name,
                "repo_path": repo_path,
                "source_path": source_path,
                "git_commit_sha": git_commit_sha,
                "git_branch": git_branch,
                "project_id": project_id,
                "plan": plan or {},
            },
            expected="GitOps deployment intent response",
        )

    def get_github_setup_session(self, setup_session_id: str) -> dict[str, Any]:
        return self._request_dict(
            "GET", f"/integrations/github/setup-sessions/{setup_session_id}", expected="GitHub setup status response"
        )

    def list_github_repositories(self, installation_id: int) -> list[dict[str, Any]]:
        return self._request_list(
            "GET",
            "/integrations/github/repositories",
            params={"installation_id": installation_id},
            expected="GitHub repository list response",
        )

    def find_github_repository_installation(self, repo_full_name: str) -> dict[str, Any]:
        return self._request_dict(
            "GET",
            "/integrations/github/repository-installation",
            params={"repo_full_name": repo_full_name},
            expected="GitHub repository installation response",
        )

    def connect_gitlab_repo(
        self,
        *,
        scope: str,
        repo: str,
        token: str,
        host: str = "gitlab.com",
        repo_path: str | None = None,
        default_branch: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request_dict(
            "POST",
            "/integrations/gitlab/repo-connections",
            json={
                "scope": scope,
                "repo": repo,
                "token": token,
                "host": host,
                "repo_path": repo_path,
                "default_branch": default_branch,
                "project_id": project_id,
            },
            expected="GitLab repo connection response",
        )

    def list_gitlab_repo_connections(self, *, project_id: str | None = None) -> list[dict[str, Any]]:
        params = {"project_id": project_id} if project_id else {}
        return self._request_list(
            "GET",
            "/integrations/gitlab/repo-connections",
            params=params,
            expected="GitLab repo connection list response",
        )

    def get_gitlab_repo_file(self, connection_id: str, *, path: str) -> dict[str, Any]:
        return self._request_dict(
            "GET",
            f"/integrations/gitlab/repo-connections/{connection_id}/file",
            params={"path": path},
            expected="GitLab repo file response",
        )

    def create_gitlab_starter_workflow(self, connection_id: str, *, path: str | None = None) -> dict[str, Any]:
        payload = {"path": path} if path else {}
        return self._request_dict(
            "POST",
            f"/integrations/gitlab/repo-connections/{connection_id}/starter-workflow",
            json=payload,
            expected="GitLab starter workflow response",
        )

    def create_gitlab_promotion_mr(
        self,
        connection_id: str,
        *,
        path: str,
        content: str,
        title: str,
        body: str = "",
        branch: str | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        return self._request_dict(
            "POST",
            f"/integrations/gitlab/repo-connections/{connection_id}/promotion-mr",
            json={"path": path, "content": content, "title": title, "body": body, "branch": branch, "message": message},
            expected="GitLab promotion MR response",
        )

    def list_github_repo_connections(self, *, project_id: str | None = None) -> list[dict[str, Any]]:
        params = {"project_id": project_id} if project_id else {}
        return self._request_list(
            "GET",
            "/integrations/github/repo-connections",
            params=params,
            expected="GitHub repo connection list response",
        )

    def connect_github_repo(
        self,
        *,
        scope: str,
        installation_id: int,
        repo_id: int,
        repo_owner: str,
        repo_name: str,
        repo_path: str | None = None,
        default_branch: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request_dict(
            "POST",
            "/integrations/github/repo-connections",
            json={
                "scope": scope,
                "installation_id": installation_id,
                "repo_id": repo_id,
                "repo_owner": repo_owner,
                "repo_name": repo_name,
                "repo_path": repo_path,
                "default_branch": default_branch,
                "project_id": project_id,
            },
            expected="GitHub repo connection response",
        )

    def create_github_starter_workflow(
        self,
        connection_id: str,
        *,
        path: str = ".rebase/starter_workflow.py",
    ) -> dict[str, Any]:
        return self._request_dict(
            "POST",
            f"/integrations/github/repo-connections/{connection_id}/starter-workflow",
            json={"path": path},
            expected="GitHub starter workflow response",
        )

    def get_github_repo_file(self, connection_id: str, *, path: str) -> dict[str, Any]:
        """Read one file from the connected repo: {path, exists, content}."""
        return self._request_dict(
            "GET",
            f"/integrations/github/repo-connections/{connection_id}/file",
            params={"path": path},
            expected="GitHub repo file response",
        )

    def create_github_promotion_pr(
        self,
        connection_id: str,
        *,
        path: str,
        content: str,
        title: str,
        body: str = "",
        branch: str | None = None,
    ) -> dict[str, Any]:
        """Open a PR on the connected repo adding/updating one file — the
        promotion path for hillclimb-search winners."""
        payload: dict[str, Any] = {
            "path": path,
            "content": content,
            "title": title,
            "body": body,
        }
        if branch:
            payload["branch"] = branch
        return self._request_dict(
            "POST",
            f"/integrations/github/repo-connections/{connection_id}/promotion-pr",
            json=payload,
            expected="GitHub promotion PR response",
        )

    def list_projects(self, *, environment_name: str | None = None) -> list[dict[str, Any]]:
        headers = {"X-Rebase-Environment": environment_name} if environment_name else None
        return self._request_list("GET", "/projects", headers=headers, expected="project list response")

    def get_project(self, project_id: str) -> dict[str, Any]:
        return self._request_dict("GET", f"/projects/{project_id}", expected="project response")

    def get_workspace(self) -> dict[str, Any]:
        return self._request_dict("GET", "/workspace", expected="workspace response")

    def _composite_read(
        self, path: str, *, expected: str, route: str | None = None, conditional: bool = False
    ) -> OverviewRead:
        """A whole-view read; absent where the platform has no such route.

        With `conditional`, the ETag of the last answer for this path is sent back as
        `If-None-Match`, and a 304 comes back as `unchanged` rather than as a payload.
        This goes through `_request_response` rather than `request()` because a 304 has
        no body to parse. A platform without ETags never answers 304, so against it a
        conditional read is an ordinary one.
        """
        key = route or path
        if key in self._absent_routes:
            return OverviewRead(None)
        etag_key = (path, self.workspace_id, _environment_context.get() or self.environment_name)
        headers: dict[str, str] = {}
        if conditional and (etag := self._etags.get(etag_key)):
            headers["If-None-Match"] = etag
        try:
            response = self._request_response("GET", path, headers=headers)
        except RebaseWorkflowError as exc:
            if exc.status_code in ROUTE_ABSENT_STATUSES:
                self._absent_routes.add(key)
                return OverviewRead(None)
            raise
        if getattr(response, "status_code", None) == 304:
            return OverviewRead(None, unchanged=True)
        payload = response.json()
        if not isinstance(payload, dict):
            raise RebaseWorkflowError(f"expected {expected}")
        etag = (getattr(response, "headers", None) or {}).get("ETag")
        if isinstance(etag, str) and etag:
            self._etags[etag_key] = etag
        return OverviewRead(payload)

    def read_workspace_overview(self, *, conditional: bool = True) -> OverviewRead:
        """`get_workspace_overview`, with the answer's provenance.

        `unchanged` when the platform confirmed the last answer still stands, which is
        the one a periodic re-read wants to hear: nothing to parse, nothing to repaint.
        """
        return self._composite_read(
            "/workspace/overview", expected="workspace overview response", conditional=conditional
        )

    def read_project_overview(self, project_id: str, *, conditional: bool = True) -> OverviewRead:
        """`get_project_overview`, with the answer's provenance; see `read_workspace_overview`."""
        return self._composite_read(
            f"/projects/{project_id}/overview",
            expected="project overview response",
            route="/projects/*/overview",
            conditional=conditional,
        )

    def get_workspace_overview(self) -> dict[str, Any] | None:
        """Everything the workspace view draws, in one request, where the API offers it.

        `None` on an API old enough not to have the route, so the caller can fall back to
        assembling the same answer from the individual list calls. That fallback is not
        theoretical: a toolkit is routinely ahead of the platform it is pointed at.

        Never conditional, so None keeps meaning "absent"; `read_workspace_overview` is
        the one that can answer "unchanged".
        """
        return self.read_workspace_overview(conditional=False).payload

    def get_project_overview(self, project_id: str) -> dict[str, Any] | None:
        """Everything the project view draws, in one request, where the API offers it.

        `None` when the route is absent, on the same terms as `get_workspace_overview`.
        """
        return self.read_project_overview(project_id, conditional=False).payload

    def update_workspace(
        self,
        *,
        name: str | None = None,
        source_mode: str | None = None,
        repo_owner: str | None = None,
        repo_name: str | None = None,
        repo_path: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            key: value
            for key, value in {
                "name": name,
                "source_mode": source_mode,
                "repo_owner": repo_owner,
                "repo_name": repo_name,
                "repo_path": repo_path,
            }.items()
            if value is not None
        }
        return self._request_dict("PATCH", "/workspace", json=payload, expected="workspace response")

    def create_project(
        self,
        *,
        name: str,
        description: str | None = None,
        source_mode: str | None = None,
        repo_owner: str | None = None,
        repo_name: str | None = None,
        repo_path: str | None = None,
        environment_name: str | None = None,
    ) -> dict[str, Any]:
        return self._request_dict(
            "POST",
            "/projects",
            json={
                "name": name,
                "description": description,
                "source_mode": source_mode,
                "repo_owner": repo_owner,
                "repo_name": repo_name,
                "repo_path": repo_path,
            },
            headers={"X-Rebase-Environment": environment_name} if environment_name else None,
            expected="project response",
        )

    def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        source_mode: str | None = None,
        repo_owner: str | None = None,
        repo_name: str | None = None,
        repo_path: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            key: value
            for key, value in {
                "name": name,
                "description": description,
                "source_mode": source_mode,
                "repo_owner": repo_owner,
                "repo_name": repo_name,
                "repo_path": repo_path,
            }.items()
            if value is not None
        }
        return self._request_dict("PATCH", f"/projects/{project_id}", json=payload, expected="project response")

    def track_project(
        self,
        project_id: str,
        *,
        github_connection_id: str,
        tracked_ref: str,
        entrypoint: str,
        repo_path: str | None = None,
    ) -> dict[str, Any]:
        return self._request_dict(
            "PUT",
            f"/projects/{project_id}/git-track",
            json={
                "github_connection_id": github_connection_id,
                "tracked_ref": tracked_ref,
                "entrypoint": entrypoint,
                "repo_path": repo_path,
            },
            expected="project Git track response",
        )

    def get_project_git_track(self, project_id: str) -> dict[str, Any]:
        return self._request_dict("GET", f"/projects/{project_id}/git-track", expected="project Git track response")

    def list_project_releases(self, project_id: str) -> list[dict[str, Any]]:
        return self._request_list("GET", f"/projects/{project_id}/releases", expected="project release list response")

    def delete_project(self, project_id: str, *, force: bool = False) -> None:
        """Delete a project. Without *force* the API refuses a non-empty one."""
        self.request_no_content("DELETE", f"/projects/{project_id}", params={"force": str(force).lower()})

    def delete_projects(self, project_ids: Sequence[str], *, force: bool = False) -> list[tuple[str, str]]:
        """Delete several projects in one request; returns ``(project_id, error)`` per failure.

        Deliberately not all-or-nothing -- a project delete tears down Cloud Run
        services and Prefect deployments, which cannot be rolled back -- so the
        API answers 200 with a per-project verdict and this returns the failures
        rather than raising on them.

        Falls back to one request per project against an API too old to have the
        batch route -- see `ROUTE_ABSENT_STATUSES` for how such an API says so --
        so a toolkit ahead of its platform still deletes.
        """
        if not project_ids:
            return []
        failures: list[tuple[str, str]] = []
        # The API refuses an oversized batch outright, so send it in whole chunks
        # rather than turning a long selection into one 422.
        for start in range(0, len(project_ids), PROJECT_BATCH_DELETE_LIMIT):
            chunk = list(project_ids[start : start + PROJECT_BATCH_DELETE_LIMIT])
            try:
                response = self._request_dict(
                    "POST",
                    "/projects/batch-delete",
                    json={"project_ids": chunk, "force": force},
                    expected="batch delete response",
                )
            except RebaseWorkflowError as exc:
                if exc.status_code not in ROUTE_ABSENT_STATUSES:
                    raise
                failures.extend(self._delete_projects_one_by_one(chunk, force=force))
                continue
            failures.extend(
                (str(failure.get("project_id", "-")), _batch_failure_message(failure))
                for failure in response.get("failed", [])
            )
        return failures

    def _delete_projects_one_by_one(self, project_ids: Sequence[str], *, force: bool) -> list[tuple[str, str]]:
        failures: list[tuple[str, str]] = []
        for project_id in project_ids:
            try:
                self.delete_project(project_id, force=force)
            except RebaseWorkflowError as exc:
                failures.append((project_id, str(exc)))
        return failures

    def delete_function(self, function_id: str, *, force: bool = False) -> None:
        self.request_no_content("DELETE", f"/functions/{function_id}", params={"force": str(force).lower()})

    def delete_workflow(self, workflow_id: str, *, force: bool = False) -> None:
        self.request_no_content("DELETE", f"/workflows/{workflow_id}", params={"force": str(force).lower()})

    def find_project(self, name: str, *, environment_name: str | None = None) -> dict[str, Any] | None:
        projects = self.list_projects(environment_name=environment_name) if environment_name else self.list_projects()
        return _find_named(projects, name)

    def _resolve_project_id(self, project: str | None, project_id: str | None) -> str | None:
        if project_id is not None or project is None:
            return project_id
        resolved = self.find_project(project)
        return str(resolved["id"]) if resolved is not None else None

    def ensure_project(
        self,
        name: str,
        *,
        description: str | None = None,
        source_mode: str | None = None,
        repo_owner: str | None = None,
        repo_name: str | None = None,
        repo_path: str | None = None,
        environment_name: str | None = None,
    ) -> dict[str, Any]:
        existing = self.find_project(name, environment_name=environment_name)
        desired = {
            key: value
            for key, value in {
                "description": description,
                "source_mode": source_mode,
                "repo_owner": repo_owner,
                "repo_name": repo_name,
                "repo_path": repo_path,
            }.items()
            if value is not None
        }
        if existing is not None and not any(existing.get(key) != value for key, value in desired.items()):
            return existing
        if existing is not None:
            return self.update_project(existing["id"], **desired)
        return self.create_project(
            name=name,
            description=description,
            source_mode=source_mode,
            repo_owner=repo_owner,
            repo_name=repo_name,
            repo_path=repo_path,
            environment_name=environment_name,
        )

    def _ensure_project_in_environment(self, name: str, environment: str) -> dict[str, Any]:
        token = _environment_context.set(environment)
        try:
            return self.ensure_project(name)
        finally:
            _environment_context.reset(token)

    def list_functions(self, *, project: str | None = None, project_id: str | None = None) -> list[dict[str, Any]]:
        resolved_project_id = self._resolve_project_id(project, project_id)
        if project is not None and resolved_project_id is None:
            return []
        if resolved_project_id is None:
            return self._list_workspace_functions()
        return self._request_list(
            "GET", f"/projects/{resolved_project_id}/functions", expected="function list response"
        )

    def _list_workspace_functions(self) -> list[dict[str, Any]]:
        """Every function in the workspace, in one request where the API allows it.

        This used to walk the projects and ask for each one's functions in turn, so an
        unfiltered `list_functions()` cost a request per project — serially, which is what
        made the TUI's workspace view slow in proportion to the size of the workspace. The
        fallback keeps that behaviour for a platform without `GET /functions`, so a toolkit
        ahead of its API loses the speed rather than the answer.
        """
        try:
            response = self._request_list("GET", "/functions", expected="function list response")
        except RebaseWorkflowError as exc:
            if exc.status_code != 404:
                raise
            functions: list[dict[str, Any]] = []
            for item in self.list_projects():
                functions.extend(self.list_functions(project_id=item["id"]))
            return functions
        return response

    def get_function(self, function_id: str) -> dict[str, Any]:
        return self._request_dict("GET", f"/functions/{function_id}", expected="function response")

    def find_function(self, name: str, *, project: str) -> dict[str, Any] | None:
        return _find_named(self.list_functions(project=project), name)

    def list_asgi_apps(self, *, project: str | None = None, project_id: str | None = None) -> list[dict[str, Any]]:
        resolved_project_id = project_id
        if resolved_project_id is None:
            resolved_project_id = self.ensure_project(project or DEFAULT_PROJECT_NAME)["id"]
        return self._request_list(
            "GET", f"/projects/{resolved_project_id}/asgi-apps", expected="ASGI app list response"
        )

    def find_asgi_app(self, name: str, *, project: str) -> dict[str, Any] | None:
        return _find_named(self.list_asgi_apps(project=project), name)

    def register_asgi_app(
        self,
        *,
        project: str,
        name: str,
        source_code: str,
        entrypoint: str,
        description: str | None = None,
        base_path: str = "/",
        auth: str = "api_key",
        image_spec: dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
        secrets: dict[str, str] | None = None,
        volumes: list[dict[str, Any]] | None = None,
        buckets: list[dict[str, Any]] | None = None,
        cloud_run_min_instances: int | None = None,
        cloud_run_max_instances: int | None = None,
        cloud_run_concurrency: int | None = None,
        cloud_run_timeout_seconds: int | None = None,
        cloud_run_cpu: str | None = None,
        cloud_run_memory: str | None = None,
        enabled: bool = True,
        environment: str | None = None,
        source_mode: str | None = None,
        repo_owner: str | None = None,
        repo_name: str | None = None,
        repo_path: str | None = None,
        source_path: str | None = None,
        git_commit_sha: str | None = None,
        git_branch: str | None = None,
        git_tag: str | None = None,
        git_dirty: bool | None = None,
        build: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        environment = _resolve_environment(self, environment)
        project_id = self._ensure_project_in_environment(project, environment)["id"]
        payload: dict[str, Any] = {
            "name": name,
            "description": description,
            "source_code": source_code,
            "entrypoint": entrypoint,
            "base_path": base_path,
            "auth": auth,
            "image_spec": image_spec,
            "env": env or {},
            "secrets": secrets or {},
            "volumes": volumes or [],
            "cloud_run_min_instances": cloud_run_min_instances,
            "cloud_run_max_instances": cloud_run_max_instances,
            "cloud_run_concurrency": cloud_run_concurrency,
            "cloud_run_timeout_seconds": cloud_run_timeout_seconds,
            "cloud_run_cpu": cloud_run_cpu,
            "cloud_run_memory": cloud_run_memory,
            "enabled": enabled,
            "source_mode": source_mode,
            "repo_owner": repo_owner,
            "repo_name": repo_name,
            "repo_path": repo_path,
            "source_path": source_path,
            "git_commit_sha": git_commit_sha,
            "git_branch": git_branch,
            "git_tag": git_tag,
            "git_dirty": git_dirty or False,
        }
        # Sent only when non-empty: an API predating this field forbids extras,
        # so an unconditional "buckets" would 422 every deploy from this client.
        if buckets:
            payload["buckets"] = buckets
        if build:
            payload["build"] = build
        return self._request_dict(
            "POST",
            f"/projects/{project_id}/asgi-apps",
            timeout=DEPLOY_REQUEST_TIMEOUT_SECONDS,
            json=payload,
            expected="ASGI app response",
        )

    def update_asgi_app(
        self,
        asgi_app_id: str,
        *,
        name: str | None = None,
        source_code: str | None = None,
        entrypoint: str | None = None,
        description: str | None = None,
        base_path: str | None = None,
        auth: str | None = None,
        image_spec: dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
        secrets: dict[str, str] | None = None,
        volumes: list[dict[str, Any]] | None = None,
        buckets: list[dict[str, Any]] | None = None,
        cloud_run_min_instances: int | None = None,
        cloud_run_max_instances: int | None = None,
        cloud_run_concurrency: int | None = None,
        cloud_run_timeout_seconds: int | None = None,
        cloud_run_cpu: str | None = None,
        cloud_run_memory: str | None = None,
        enabled: bool | None = None,
        environment: str | None = None,
        source_mode: str | None = None,
        repo_owner: str | None = None,
        repo_name: str | None = None,
        repo_path: str | None = None,
        source_path: str | None = None,
        git_commit_sha: str | None = None,
        git_branch: str | None = None,
        git_tag: str | None = None,
        git_dirty: bool | None = None,
        build: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            key: value
            for key, value in {
                "name": name,
                "description": description,
                "source_code": source_code,
                "entrypoint": entrypoint,
                "base_path": base_path,
                "auth": auth,
                "image_spec": image_spec,
                "env": env,
                "secrets": secrets,
                "volumes": volumes,
                # `or None` so an empty list is dropped by the filter below, the
                # same rule the create path applies: an API predating this field
                # forbids extras, and an unconditional "buckets" 422s every
                # update-deploy from this client.
                "buckets": buckets or None,
                "cloud_run_min_instances": cloud_run_min_instances,
                "cloud_run_max_instances": cloud_run_max_instances,
                "cloud_run_concurrency": cloud_run_concurrency,
                "cloud_run_timeout_seconds": cloud_run_timeout_seconds,
                "cloud_run_cpu": cloud_run_cpu,
                "cloud_run_memory": cloud_run_memory,
                "enabled": enabled,
                "source_mode": source_mode,
                "repo_owner": repo_owner,
                "repo_name": repo_name,
                "repo_path": repo_path,
                "source_path": source_path,
                "git_commit_sha": git_commit_sha,
                "git_branch": git_branch,
                "git_tag": git_tag,
                "git_dirty": git_dirty,
            }.items()
            if value is not None
        }
        if build:
            payload["build"] = build
        return self._request_dict(
            "PATCH",
            f"/asgi-apps/{asgi_app_id}",
            timeout=DEPLOY_REQUEST_TIMEOUT_SECONDS,
            json=payload,
            expected="ASGI app response",
        )

    def register_function(
        self,
        *,
        project: str,
        name: str,
        source_code: str,
        entrypoint: str,
        description: str | None = None,
        default_parameters: dict[str, Any] | None = None,
        mode: ExecutionMode | None = None,
        isolation: Isolation | None = None,
        run_type: RunType | None = None,
        image_spec: dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
        secrets: dict[str, str] | None = None,
        volumes: list[dict[str, Any]] | None = None,
        buckets: list[dict[str, Any]] | None = None,
        cloud_run_min_instances: int | None = None,
        cloud_run_concurrency: int | None = None,
        cloud_run_cpu: str | None = None,
        cloud_run_memory: str | None = None,
        enabled: bool = True,
        endpoint: EndpointConfig | dict[str, Any] | None = None,
        environment: str | None = None,
        source_mode: str | None = None,
        repo_owner: str | None = None,
        repo_name: str | None = None,
        repo_path: str | None = None,
        source_path: str | None = None,
        git_commit_sha: str | None = None,
        git_branch: str | None = None,
        git_tag: str | None = None,
        git_dirty: bool | None = None,
        build: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        environment = _resolve_environment(self, environment)
        project_id = self._ensure_project_in_environment(project, environment)["id"]
        resolved_mode, resolved_isolation = _validate_execution(mode, isolation, run_type=run_type)
        payload: dict[str, Any] = {
            "name": name,
            "description": description,
            "source_code": source_code,
            "entrypoint": entrypoint,
            "default_parameters": default_parameters or {},
            "mode": resolved_mode,
            "isolation": resolved_isolation,
            "image_spec": image_spec,
            "env": env or {},
            "secrets": secrets or {},
            "volumes": volumes or [],
            "cloud_run_min_instances": cloud_run_min_instances,
            "cloud_run_concurrency": cloud_run_concurrency,
            "cloud_run_cpu": cloud_run_cpu,
            "cloud_run_memory": cloud_run_memory,
            "enabled": enabled,
            "endpoint": _coerce_endpoint(endpoint).to_payload() if endpoint is not None else None,
            "environment": environment,
            "source_mode": source_mode,
            "repo_owner": repo_owner,
            "repo_name": repo_name,
            "repo_path": repo_path,
            "source_path": source_path,
            "git_commit_sha": git_commit_sha,
            "git_branch": git_branch,
            "git_tag": git_tag,
            "git_dirty": git_dirty or False,
        }
        # Sent only when non-empty: an API predating this field forbids extras,
        # so an unconditional "buckets" would 422 every deploy from this client.
        if buckets:
            payload["buckets"] = buckets
        if build:
            payload["build"] = build
        return self._request_dict(
            "POST", f"/projects/{project_id}/functions", json=payload, expected="function response"
        )

    def update_function(
        self,
        function_id: str,
        *,
        name: str | None = None,
        source_code: str | None = None,
        entrypoint: str | None = None,
        description: str | None = None,
        default_parameters: dict[str, Any] | None = None,
        mode: ExecutionMode | None = None,
        isolation: Isolation | None = None,
        run_type: RunType | None = None,
        image_spec: dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
        secrets: dict[str, str] | None = None,
        volumes: list[dict[str, Any]] | None = None,
        buckets: list[dict[str, Any]] | None = None,
        cloud_run_min_instances: int | None = None,
        cloud_run_concurrency: int | None = None,
        cloud_run_cpu: str | None = None,
        cloud_run_memory: str | None = None,
        enabled: bool | None = None,
        endpoint: EndpointConfig | dict[str, Any] | None = None,
        environment: str | None = None,
        source_mode: str | None = None,
        repo_owner: str | None = None,
        repo_name: str | None = None,
        repo_path: str | None = None,
        source_path: str | None = None,
        git_commit_sha: str | None = None,
        git_branch: str | None = None,
        git_tag: str | None = None,
        git_dirty: bool | None = None,
        build: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        execution_payload: dict[str, Any] = {}
        if mode is not None or isolation is not None or run_type is not None:
            resolved_mode, resolved_isolation = _validate_execution(mode, isolation, run_type=run_type)
            execution_payload = {"mode": resolved_mode, "isolation": resolved_isolation}
        payload = {
            key: value
            for key, value in {
                "name": name,
                "description": description,
                "source_code": source_code,
                "entrypoint": entrypoint,
                "default_parameters": default_parameters,
                "image_spec": image_spec,
                "env": env,
                "secrets": secrets,
                "volumes": volumes,
                # See the note on update_asgi_app: empty must be omitted, not sent.
                "buckets": buckets or None,
                "cloud_run_min_instances": cloud_run_min_instances,
                "cloud_run_concurrency": cloud_run_concurrency,
                "cloud_run_cpu": cloud_run_cpu,
                "cloud_run_memory": cloud_run_memory,
                "enabled": enabled,
                "endpoint": _coerce_endpoint(endpoint).to_payload() if endpoint is not None else None,
                "environment": environment,
                "source_mode": source_mode,
                "repo_owner": repo_owner,
                "repo_name": repo_name,
                "repo_path": repo_path,
                "source_path": source_path,
                "git_commit_sha": git_commit_sha,
                "git_branch": git_branch,
                "git_tag": git_tag,
                "git_dirty": git_dirty,
            }.items()
            if value is not None
        }
        payload.update(execution_payload)
        if build:
            payload["build"] = build
        return self._request_dict("PATCH", f"/functions/{function_id}", json=payload, expected="function response")

    def run_function(self, function_id: str, parameters: dict[str, Any] | None = None) -> Run:
        response = self._request_dict(
            "POST", f"/functions/{function_id}/runs", json={"parameters": parameters or {}}, expected="run response"
        )
        return Run(response["id"], client=self, data=response)

    def run_function_map(
        self,
        function_id: str,
        *,
        items: list[Any],
        parameter: str | None = None,
        kwargs: dict[str, Any] | None = None,
        max_concurrency: int | None = None,
        ordered: bool = True,
        return_exceptions: bool = False,
        timeout: float | None = None,
    ) -> Iterator[dict[str, Any]]:
        payload: dict[str, Any] = {
            "items": items,
            "parameter": parameter,
            "kwargs": kwargs or {},
            "ordered": ordered,
            "return_exceptions": return_exceptions,
        }
        if max_concurrency is not None:
            payload["max_concurrency"] = max_concurrency
        if timeout is not None:
            payload["timeout_seconds"] = timeout
        # A map issued from inside a run is that run's tasks, and the platform can only
        # know it from here — the batch is created by this request. Absent on a laptop,
        # where the batch legitimately belongs to no run.
        context = current_run()
        if context is not None:
            payload["workflow_run_id"] = context.run_id
            if context.step_run_id is not None:
                payload["step_run_id"] = context.step_run_id
        yield from self.stream_request("POST", f"/functions/{function_id}/map", json=payload, timeout=None)

    def list_models(self, *, project: str | None = None, project_id: str | None = None) -> list[dict[str, Any]]:
        resolved_project_id = self._resolve_project_id(project, project_id)
        if project is not None and resolved_project_id is None:
            return []
        path = f"/projects/{resolved_project_id}/models" if resolved_project_id is not None else "/models"
        return self._request_list("GET", path, expected="model list response")

    def get_model(self, model_id: str) -> dict[str, Any]:
        return self._request_dict("GET", f"/models/{model_id}", expected="model response")

    def find_model(self, name: str, *, project: str) -> dict[str, Any] | None:
        return _find_named(self.list_models(project=project), name)

    def register_model(
        self,
        *,
        project: str,
        name: str,
        kind: str,
        operation_name: str,
        source_code: str,
        description: str | None = None,
        default_parameters: dict[str, Any] | None = None,
        mode: ExecutionMode | None = None,
        isolation: Isolation | None = None,
        run_type: RunType | None = None,
        image_spec: dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
        secrets: dict[str, str] | None = None,
        cloud_run_min_instances: int | None = None,
        cloud_run_concurrency: int | None = None,
        cloud_run_cpu: str | None = None,
        cloud_run_memory: str | None = None,
        enabled: bool = True,
        endpoint: EndpointConfig | dict[str, Any] | None = None,
        environment: str | None = None,
        source_mode: str | None = None,
        repo_owner: str | None = None,
        repo_name: str | None = None,
        repo_path: str | None = None,
        source_path: str | None = None,
        git_commit_sha: str | None = None,
        git_branch: str | None = None,
        git_tag: str | None = None,
        git_dirty: bool | None = None,
    ) -> dict[str, Any]:
        environment = _resolve_environment(self, environment)
        project_id = self._ensure_project_in_environment(project, environment)["id"]
        resolved_mode, resolved_isolation = _validate_execution(mode, isolation, run_type=run_type)
        return self._request_dict(
            "POST",
            f"/projects/{project_id}/models",
            json={
                "name": name,
                "kind": kind,
                "operation_name": operation_name,
                "description": description,
                "source_code": source_code,
                "default_parameters": default_parameters or {},
                "mode": resolved_mode,
                "isolation": resolved_isolation,
                "image_spec": image_spec,
                "env": env or {},
                "secrets": secrets or {},
                "cloud_run_min_instances": cloud_run_min_instances,
                "cloud_run_concurrency": cloud_run_concurrency,
                "cloud_run_cpu": cloud_run_cpu,
                "cloud_run_memory": cloud_run_memory,
                "enabled": enabled,
                "endpoint": _coerce_endpoint(endpoint).to_payload() if endpoint is not None else None,
                "environment": environment,
                "source_mode": source_mode,
                "repo_owner": repo_owner,
                "repo_name": repo_name,
                "repo_path": repo_path,
                "source_path": source_path,
                "git_commit_sha": git_commit_sha,
                "git_branch": git_branch,
                "git_tag": git_tag,
                "git_dirty": git_dirty or False,
            },
            expected="model response",
        )

    def update_model(
        self,
        model_id: str,
        *,
        name: str | None = None,
        kind: str | None = None,
        operation_name: str | None = None,
        source_code: str | None = None,
        description: str | None = None,
        default_parameters: dict[str, Any] | None = None,
        mode: ExecutionMode | None = None,
        isolation: Isolation | None = None,
        run_type: RunType | None = None,
        image_spec: dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
        secrets: dict[str, str] | None = None,
        cloud_run_min_instances: int | None = None,
        cloud_run_concurrency: int | None = None,
        cloud_run_cpu: str | None = None,
        cloud_run_memory: str | None = None,
        enabled: bool | None = None,
        endpoint: EndpointConfig | dict[str, Any] | None = None,
        environment: str | None = None,
        source_mode: str | None = None,
        repo_owner: str | None = None,
        repo_name: str | None = None,
        repo_path: str | None = None,
        source_path: str | None = None,
        git_commit_sha: str | None = None,
        git_branch: str | None = None,
        git_tag: str | None = None,
        git_dirty: bool | None = None,
    ) -> dict[str, Any]:
        execution_payload: dict[str, Any] = {}
        if mode is not None or isolation is not None or run_type is not None:
            resolved_mode, resolved_isolation = _validate_execution(mode, isolation, run_type=run_type)
            execution_payload = {"mode": resolved_mode, "isolation": resolved_isolation}
        payload = {
            key: value
            for key, value in {
                "name": name,
                "kind": kind,
                "operation_name": operation_name,
                "description": description,
                "source_code": source_code,
                "default_parameters": default_parameters,
                "image_spec": image_spec,
                "env": env,
                "secrets": secrets,
                "cloud_run_min_instances": cloud_run_min_instances,
                "cloud_run_concurrency": cloud_run_concurrency,
                "cloud_run_cpu": cloud_run_cpu,
                "cloud_run_memory": cloud_run_memory,
                "enabled": enabled,
                "endpoint": _coerce_endpoint(endpoint).to_payload() if endpoint is not None else None,
                "environment": environment,
                "source_mode": source_mode,
                "repo_owner": repo_owner,
                "repo_name": repo_name,
                "repo_path": repo_path,
                "source_path": source_path,
                "git_commit_sha": git_commit_sha,
                "git_branch": git_branch,
                "git_tag": git_tag,
                "git_dirty": git_dirty,
            }.items()
            if value is not None
        }
        payload.update(execution_payload)
        return self._request_dict("PATCH", f"/models/{model_id}", json=payload, expected="model response")

    def run_model(
        self,
        model_id: str,
        parameters: dict[str, Any] | None = None,
        *,
        environment: str | None = None,
    ) -> Run:
        environment = _resolve_environment(self, environment)
        response = self._request_dict(
            "POST",
            f"/models/{model_id}/runs",
            json={"parameters": parameters or {}, "environment": environment},
            expected="run response",
        )
        return Run(response["id"], client=self, data=response)

    def list_model_versions(self, model_id: str) -> list[dict[str, Any]]:
        return self._request_list("GET", f"/models/{model_id}/versions", expected="model version list response")

    def get_model_version(self, model_id: str, version_id: str) -> dict[str, Any]:
        return self._request_dict("GET", f"/models/{model_id}/versions/{version_id}", expected="model version response")

    def list_model_publications(self, model_id: str) -> list[dict[str, Any]]:
        return self._request_list("GET", f"/models/{model_id}/publications", expected="model publication list response")

    def record_model_publication(
        self,
        model_id: str,
        *,
        model_version_id: str,
        repo_id: str,
        provider_commit_sha: str,
        provider: str = "huggingface",
        repo_type: str = "model",
        visibility: str = "private",
        path_in_repo: str | None = None,
        revision: str = "main",
        source_git_commit_sha: str | None = None,
        publication_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request_dict(
            "POST",
            f"/models/{model_id}/publications",
            json={
                "model_version_id": model_version_id,
                "provider": provider,
                "repo_type": repo_type,
                "repo_id": repo_id,
                "visibility": visibility,
                "path_in_repo": path_in_repo,
                "revision": revision,
                "provider_commit_sha": provider_commit_sha,
                "source_git_commit_sha": source_git_commit_sha,
                "publication_metadata": publication_metadata or {},
            },
            expected="model publication response",
        )

    def list_model_deployments(self, model_id: str) -> list[dict[str, Any]]:
        return self._request_list("GET", f"/models/{model_id}/deployments", expected="model deployment list response")

    def deploy_model_version(
        self,
        model_id: str,
        *,
        environment: str,
        model_version_id: str,
        promotion_request_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request_dict(
            "POST",
            f"/models/{model_id}/deployments/{environment}",
            json={"model_version_id": model_version_id, "promotion_request_id": promotion_request_id},
            expected="model deployment response",
        )

    def create_model_promotion_request(
        self,
        model_id: str,
        *,
        model_version_id: str,
        from_environment: str = "staging",
        to_environment: str = "prod",
        reason: str | None = None,
    ) -> dict[str, Any]:
        return self._request_dict(
            "POST",
            f"/models/{model_id}/promotion-requests",
            json={
                "model_version_id": model_version_id,
                "from_environment": from_environment,
                "to_environment": to_environment,
                "reason": reason,
            },
            expected="model promotion request response",
        )

    def approve_model_promotion_request(self, request_id: str, *, reason: str | None = None) -> dict[str, Any]:
        return self._request_dict(
            "POST",
            f"/model-promotion-requests/{request_id}/approve",
            json={"reason": reason},
            expected="model promotion request response",
        )

    def reject_model_promotion_request(self, request_id: str, *, reason: str | None = None) -> dict[str, Any]:
        return self._request_dict(
            "POST",
            f"/model-promotion-requests/{request_id}/reject",
            json={"reason": reason},
            expected="model promotion request response",
        )

    def promote_model(
        self,
        model_id: str,
        *,
        from_environment: str = "dev",
        to_environment: str,
        model_version_id: str | None = None,
        promotion_request_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request_dict(
            "POST",
            f"/models/{model_id}/promote",
            json={
                "from_environment": from_environment,
                "to_environment": to_environment,
                "model_version_id": model_version_id,
                "promotion_request_id": promotion_request_id,
            },
            expected="model deployment response",
        )

    def rollback_model(
        self,
        model_id: str,
        *,
        environment: str = "prod",
        model_version_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request_dict(
            "POST",
            f"/models/{model_id}/rollback",
            json={"environment": environment, "model_version_id": model_version_id},
            expected="model deployment response",
        )

    def list_model_events(self, model_id: str) -> list[dict[str, Any]]:
        return self._request_list("GET", f"/models/{model_id}/events", expected="model event list response")

    def list_function_versions(self, function_id: str) -> list[dict[str, Any]]:
        return self._request_list(
            "GET", f"/functions/{function_id}/versions", expected="function version list response"
        )

    def get_function_version(self, function_id: str, version_id: str) -> dict[str, Any]:
        return self._request_dict(
            "GET", f"/functions/{function_id}/versions/{version_id}", expected="function version response"
        )

    def list_workflows(self, *, project: str | None = None, project_id: str | None = None) -> list[dict[str, Any]]:
        resolved_project_id = self._resolve_project_id(project, project_id)
        if project is not None and resolved_project_id is None:
            return []
        path = f"/projects/{resolved_project_id}/workflows" if resolved_project_id is not None else "/workflows"
        return self._request_list("GET", path, expected="workflow list response")

    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        return self._request_dict("GET", f"/workflows/{workflow_id}", expected="workflow response")

    def find_workflow(self, name: str, *, project: str | None = None) -> dict[str, Any] | None:
        workflows = self.list_workflows(project=project) if project is not None else self.list_workflows()
        return _find_named(workflows, name)

    def register_workflow(
        self,
        *,
        name: str,
        flow_ref: str | None = None,
        source_code: str | None = None,
        entrypoint: str | None = None,
        step_graph: dict[str, Any] | None = None,
        schedule: dict[str, Any] | None = None,
        trigger: dict[str, Any] | None = None,
        description: str | None = None,
        default_parameters: dict[str, Any] | None = None,
        required_parameters: list[str] | None = None,
        mode: ExecutionMode | None = None,
        isolation: Isolation | None = None,
        run_type: RunType | None = None,
        env: dict[str, str] | None = None,
        secrets: dict[str, str] | None = None,
        buckets: list[dict[str, str]] | None = None,
        enabled: bool = True,
        endpoint: EndpointConfig | dict[str, Any] | None = None,
        project: str | None = None,
        environment: str | None = None,
        source_mode: str | None = None,
        repo_owner: str | None = None,
        repo_name: str | None = None,
        repo_path: str | None = None,
        source_path: str | None = None,
        git_commit_sha: str | None = None,
        git_branch: str | None = None,
        git_tag: str | None = None,
        git_dirty: bool | None = None,
        build: dict[str, Any] | None = None,
        cloud_run_cpu: str | None = None,
        cloud_run_memory: str | None = None,
    ) -> dict[str, Any]:
        environment = _resolve_environment(self, environment)
        path = "/workflows"
        if project is not None:
            project_id = self._ensure_project_in_environment(project, environment)["id"]
            path = f"/projects/{project_id}/workflows"
        resolved_mode, resolved_isolation = _validate_execution(
            mode,
            isolation,
            target_type="workflow",
            run_type=run_type,
        )
        payload = {
            "name": name,
            "description": description,
            "flow_ref": flow_ref,
            "source_code": source_code,
            "entrypoint": entrypoint,
            "step_graph": step_graph,
            "schedule": schedule,
            "trigger": trigger,
            "default_parameters": default_parameters or {},
            "required_parameters": required_parameters or [],
            "mode": resolved_mode,
            "isolation": resolved_isolation,
            "env": env or {},
            "secrets": secrets or {},
            "buckets": buckets or [],
            "cloud_run_cpu": cloud_run_cpu,
            "cloud_run_memory": cloud_run_memory,
            "enabled": enabled,
            "endpoint": _coerce_endpoint(endpoint).to_payload() if endpoint is not None else None,
            "environment": environment,
            "source_mode": source_mode,
            "repo_owner": repo_owner,
            "repo_name": repo_name,
            "repo_path": repo_path,
            "source_path": source_path,
            "git_commit_sha": git_commit_sha,
            "git_branch": git_branch,
            "git_tag": git_tag,
            "git_dirty": git_dirty or False,
        }
        if build:
            payload["build"] = build
        return self._request_dict("POST", path, json=payload, expected="workflow response")

    def update_workflow(
        self,
        workflow_id: str,
        *,
        name: str | None = None,
        flow_ref: str | None = None,
        source_code: str | None = None,
        entrypoint: str | None = None,
        step_graph: dict[str, Any] | None | object = _UNSET,
        schedule: dict[str, Any] | None | object = _UNSET,
        trigger: dict[str, Any] | None | object = _UNSET,
        description: str | None = None,
        default_parameters: dict[str, Any] | None = None,
        required_parameters: list[str] | None = None,
        mode: ExecutionMode | None = None,
        isolation: Isolation | None = None,
        run_type: RunType | None = None,
        env: dict[str, str] | None = None,
        secrets: dict[str, str] | None = None,
        buckets: list[dict[str, str]] | object = _UNSET,
        enabled: bool | None = None,
        endpoint: EndpointConfig | dict[str, Any] | None = None,
        environment: str | None = None,
        source_mode: str | None = None,
        repo_owner: str | None = None,
        repo_name: str | None = None,
        repo_path: str | None = None,
        source_path: str | None = None,
        git_commit_sha: str | None = None,
        git_branch: str | None = None,
        git_tag: str | None = None,
        git_dirty: bool | None = None,
        build: dict[str, Any] | None = None,
        # _UNSET, not None, for the same reason as buckets/schedule above: None
        # is a meaningful value here ("back to the backend default"), so it has
        # to survive the drop-None filter below. Deleting `memory=` from a
        # decorator must actually clear the limit, not silently keep the old one.
        cloud_run_cpu: str | None | object = _UNSET,
        cloud_run_memory: str | None | object = _UNSET,
    ) -> dict[str, Any]:
        execution_payload: dict[str, Any] = {}
        if mode is not None or isolation is not None or run_type is not None:
            resolved_mode, resolved_isolation = _validate_execution(
                mode,
                isolation,
                target_type="workflow",
                run_type=run_type,
            )
            execution_payload = {"mode": resolved_mode, "isolation": resolved_isolation}
        payload: dict[str, Any] = {
            key: value
            for key, value in {
                "name": name,
                "description": description,
                "flow_ref": flow_ref,
                "source_code": source_code,
                "entrypoint": entrypoint,
                "default_parameters": default_parameters,
                "required_parameters": required_parameters,
                "env": env,
                "secrets": secrets,
                "enabled": enabled,
                "endpoint": _coerce_endpoint(endpoint).to_payload() if endpoint is not None else None,
                "environment": environment,
                "source_mode": source_mode,
                "repo_owner": repo_owner,
                "repo_name": repo_name,
                "repo_path": repo_path,
                "source_path": source_path,
                "git_commit_sha": git_commit_sha,
                "git_branch": git_branch,
                "git_tag": git_tag,
                "git_dirty": git_dirty,
            }.items()
            if value is not None
        }
        payload.update(execution_payload)
        if step_graph is not _UNSET:
            payload["step_graph"] = step_graph
        if schedule is not _UNSET:
            payload["schedule"] = schedule
        if trigger is not _UNSET:
            payload["trigger"] = trigger
        if buckets is not _UNSET:
            payload["buckets"] = buckets
        if cloud_run_cpu is not _UNSET:
            payload["cloud_run_cpu"] = cloud_run_cpu
        if cloud_run_memory is not _UNSET:
            payload["cloud_run_memory"] = cloud_run_memory
        if build:
            payload["build"] = build
        return self._request_dict("PATCH", f"/workflows/{workflow_id}", json=payload, expected="workflow response")

    def run_workflow(self, workflow_id: str, parameters: dict[str, Any] | None = None) -> Run:
        response = self._request_dict(
            "POST", f"/workflows/{workflow_id}/runs", json={"parameters": parameters or {}}, expected="run response"
        )
        return Run(response["id"], client=self, data=response)

    def replay_run(
        self,
        run_id: str,
        *,
        version: str | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> Run:
        """Replay a workflow run: re-execute it bounded to what was knowable at the time.

        ``version`` selects the code: ``None`` replays the original pinned version,
        ``"latest"`` (or ``"current"``) uses the workflow's current version, and any other
        string is a specific version id. ``parameters`` are merged over the original run's
        parameters. Returns the new run, which carries ``replay_of`` and
        ``trigger_source: "replay"``.
        """
        payload: dict[str, Any] = {"parameters": parameters or {}}
        if version in {"latest", "current"}:
            payload["use_current_version"] = True
        elif version is not None:
            payload["target_version_id"] = version
        response = self._request_dict("POST", f"/runs/{run_id}/replay", json=payload, expected="run response")
        return Run(response["id"], client=self, data=response)

    def run_ephemeral(
        self,
        *,
        target_type: str,
        project: str,
        name: str,
        source_code: str,
        entrypoint: str,
        default_parameters: dict[str, Any] | None = None,
        parameters: dict[str, Any] | None = None,
        mode: ExecutionMode | None = None,
        isolation: Isolation | None = None,
        run_type: RunType | None = None,
        image_spec: dict[str, Any] | None = None,
        step_graph: dict[str, Any] | None = None,
        required_parameters: list[str] | None = None,
        cloud_run_min_instances: int | None = None,
        cloud_run_concurrency: int | None = None,
        env: dict[str, str] | None = None,
        secrets: dict[str, str] | None = None,
    ) -> Run:
        resolved_mode, resolved_isolation = _validate_execution(
            mode,
            isolation,
            target_type=target_type,
            run_type=run_type,
        )
        payload: dict[str, Any] = {
            "target_type": target_type,
            "project": project,
            "name": name,
            "source_code": source_code,
            "entrypoint": entrypoint,
            "default_parameters": default_parameters or {},
            "parameters": parameters or {},
            "mode": resolved_mode,
            "isolation": resolved_isolation,
            "image_spec": image_spec,
            "step_graph": step_graph,
            "required_parameters": required_parameters or [],
            "cloud_run_min_instances": cloud_run_min_instances,
            "cloud_run_concurrency": cloud_run_concurrency,
        }
        # Ephemeral runs carry env and secret references just like deployed ones.
        # Without them the run starts with an empty environment, so a step reading
        # os.environ["..."] raises KeyError even though the identical code works
        # once deployed.
        #
        # Sent only when non-empty: the API rejects unknown fields, so an older
        # deployment would 422 on every ephemeral run. Omitting the empty case
        # keeps this client working against both, and callers that actually use
        # secrets get a loud error instead of a container with no environment.
        if env:
            payload["env"] = dict(env)
        if secrets:
            payload["secrets"] = dict(secrets)

        response = self._request_dict(
            "POST",
            "/runs/ephemeral",
            json=payload,
            timeout=EPHEMERAL_RUN_REQUEST_TIMEOUT_SECONDS,
            expected="run response",
        )
        return Run(response["id"], client=self, data=response)

    def list_workflow_versions(self, workflow_id: str) -> list[dict[str, Any]]:
        return self._request_list(
            "GET", f"/workflows/{workflow_id}/versions", expected="workflow version list response"
        )

    def get_workflow_version(self, workflow_id: str, version_id: str) -> dict[str, Any]:
        return self._request_dict(
            "GET", f"/workflows/{workflow_id}/versions/{version_id}", expected="workflow version response"
        )

    def get_workflow_schedule(self, workflow_id: str) -> dict[str, Any]:
        return self._request_dict("GET", f"/workflows/{workflow_id}/schedule", expected="workflow schedule response")

    def pause_workflow(self, workflow_id: str, *, until: str | None = None) -> dict[str, Any]:
        """Pause a workflow's scheduled runs; ``until`` (ISO 8601) lifts the pause on its own."""
        return self._request_dict(
            "POST", f"/workflows/{workflow_id}/pause", json={"until": until}, expected="workflow response"
        )

    def resume_workflow(self, workflow_id: str) -> dict[str, Any]:
        """Lift a workflow's pause so scheduled runs fire again."""
        return self._request_dict("POST", f"/workflows/{workflow_id}/resume", expected="workflow response")

    def get_workflow_trigger(self, workflow_id: str) -> dict[str, Any]:
        return self._request_dict("GET", f"/workflows/{workflow_id}/trigger", expected="workflow trigger response")

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._request_dict("GET", f"/runs/{run_id}", expected="run response")

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        return self._request_dict("POST", f"/runs/{run_id}/cancel", expected="run response")

    def get_run_logs(
        self,
        run_id: str,
        *,
        since: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        params = {key: value for key, value in {"since": since, "limit": limit}.items() if value is not None}
        return self._request_dict("GET", f"/runs/{run_id}/logs", params=params or None, expected="run logs response")

    def get_workspace_notifications(self) -> dict[str, Any]:
        return self._request_dict("GET", "/workspace/notifications", expected="workspace notification response")

    def update_workspace_notifications(
        self,
        *,
        notify_on_failure: bool | None = None,
        notify_on_stale: bool | None = None,
        notify_owner_email: bool | None = None,
        webhook_url: str | None | object = _UNSET,
        webhook_secret: str | None | object = _UNSET,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if notify_on_failure is not None:
            payload["notify_on_failure"] = notify_on_failure
        if notify_on_stale is not None:
            payload["notify_on_stale"] = notify_on_stale
        if notify_owner_email is not None:
            payload["notify_owner_email"] = notify_owner_email
        if webhook_url is not _UNSET:
            payload["webhook_url"] = "" if webhook_url is None else webhook_url
        if webhook_secret is not _UNSET:
            payload["webhook_secret"] = "" if webhook_secret is None else webhook_secret
        return self._request_dict(
            "PATCH", "/workspace/notifications", json=payload, expected="workspace notification response"
        )

    def get_workspace_compute_policy(self) -> dict[str, Any]:
        return self._request_dict("GET", "/workspace/compute-policy", expected="workspace compute policy response")

    def update_workspace_compute_policy(
        self,
        *,
        max_run_timeout_seconds: int | None = None,
        max_concurrent_cloud_run_runs: int | None = None,
        max_cloud_run_instances: int | None = None,
        max_cloud_run_concurrency: int | None = None,
        max_cloud_run_cpu_milli: int | None = None,
        max_cloud_run_memory_mib: int | None = None,
    ) -> dict[str, Any]:
        # No _UNSET sentinel here, unlike update_workspace_notifications: these are
        # NOT NULL integers with no clear-to-null semantics, so plain "None means not
        # set" is the correct encoding rather than an omission the server must undo.
        payload: dict[str, Any] = {}
        if max_run_timeout_seconds is not None:
            payload["max_run_timeout_seconds"] = max_run_timeout_seconds
        if max_concurrent_cloud_run_runs is not None:
            payload["max_concurrent_cloud_run_runs"] = max_concurrent_cloud_run_runs
        if max_cloud_run_instances is not None:
            payload["max_cloud_run_instances"] = max_cloud_run_instances
        if max_cloud_run_concurrency is not None:
            payload["max_cloud_run_concurrency"] = max_cloud_run_concurrency
        if max_cloud_run_cpu_milli is not None:
            payload["max_cloud_run_cpu_milli"] = max_cloud_run_cpu_milli
        if max_cloud_run_memory_mib is not None:
            payload["max_cloud_run_memory_mib"] = max_cloud_run_memory_mib
        return self._request_dict(
            "PATCH", "/workspace/compute-policy", json=payload, expected="workspace compute policy response"
        )

    # Vendor-side administration across every workspace. These routes are gated by
    # the profile-level superadmin check, so they need a session credential -- an
    # API key is refused -- and they name the target workspace in the path rather
    # than through X-Rebase-Workspace, which is why none of them take a header
    # override or care what workspace this client is configured for.

    def list_admin_workspaces(self, *, limit: int = 200) -> list[dict[str, Any]]:
        return self._request_list(
            "GET", "/admin/workspaces", params={"limit": limit}, expected="admin workspace list response"
        )

    def get_admin_workspace_usage(self, workspace_id: str) -> dict[str, Any]:
        return self._request_dict("GET", f"/admin/workspaces/{workspace_id}/usage", expected="workspace usage response")

    def update_admin_compute_policy(
        self,
        workspace_id: str,
        *,
        max_run_timeout_seconds: int | None = None,
        max_concurrent_cloud_run_runs: int | None = None,
        max_cloud_run_instances: int | None = None,
        max_cloud_run_concurrency: int | None = None,
        max_cloud_run_cpu_milli: int | None = None,
        max_cloud_run_memory_mib: int | None = None,
    ) -> dict[str, Any]:
        # Same encoding as update_workspace_compute_policy above: NOT NULL integers,
        # so None means "not set", never "clear".
        payload: dict[str, Any] = {}
        for key, value in (
            ("max_run_timeout_seconds", max_run_timeout_seconds),
            ("max_concurrent_cloud_run_runs", max_concurrent_cloud_run_runs),
            ("max_cloud_run_instances", max_cloud_run_instances),
            ("max_cloud_run_concurrency", max_cloud_run_concurrency),
            ("max_cloud_run_cpu_milli", max_cloud_run_cpu_milli),
            ("max_cloud_run_memory_mib", max_cloud_run_memory_mib),
        ):
            if value is not None:
                payload[key] = value
        return self._request_dict(
            "PATCH",
            f"/admin/workspaces/{workspace_id}/compute-policy",
            json=payload,
            expected="workspace compute policy response",
        )

    def update_admin_credit_grant(self, workspace_id: str, *, monthly_credit_cents: int) -> dict[str, Any]:
        """Set the monthly grant for this month and the ones after; returns the resulting usage."""
        return self._request_dict(
            "PATCH",
            f"/admin/workspaces/{workspace_id}/credit-grant",
            json={"monthly_credit_cents": monthly_credit_cents},
            expected="workspace usage response",
        )

    def list_runs(
        self,
        *,
        project_id: str | None = None,
        workflow_id: str | None = None,
        function_id: str | None = None,
        model_id: str | None = None,
        target_id: str | None = None,
        target_type: str | None = None,
        since: datetime | str | None = None,
        until: datetime | str | None = None,
        status: str | None = None,
        trigger_source: str | None = None,
        limit: int = 100,
        include: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Runs, newest first. Name what ran with `target_id` + `target_type`.

        The rows are summaries: status, target and timestamps, plus `result_bytes` and
        `parameters_bytes` saying how big the bodies are, but not the bodies themselves —
        a list is read to see what ran, and one run's result can be a megabyte. Pass
        `include=["result", "parameters"]` for the whole record per row, or read one run
        with `get_run`. A platform older than this parameter ignores it and sends the
        whole record either way, so treat `result` and `parameters` as optional keys.

        `workflow_id` / `function_id` / `model_id` are kept as spellings of the same
        filter, because a run's own `workflow_id` column is null for an ordinary
        registered run -- what it ran is `target_type` + `target_id`, and that is what
        `/runs` filters on.
        """
        unknown = sorted(set(include or ()) - RUN_INCLUDES)
        if unknown:
            raise RebaseWorkflowError(
                f"unknown include value(s): {', '.join(unknown)}; expected {', '.join(sorted(RUN_INCLUDES))}"
            )
        resolved_id, resolved_type = _resolve_run_target(
            target_id=target_id,
            target_type=target_type,
            workflow_id=workflow_id,
            function_id=function_id,
            model_id=model_id,
        )
        params = {
            key: value
            for key, value in {
                "project_id": project_id,
                "target_id": resolved_id,
                "target_type": resolved_type,
                "since": since.isoformat() if isinstance(since, datetime) else since,
                "until": until.isoformat() if isinstance(until, datetime) else until,
                "status": status,
                "trigger_source": trigger_source,
                "limit": limit,
                "include": ",".join(include) if include else None,
            }.items()
            if value is not None
        }
        return self._request_list("GET", "/runs", params=params, expected="run list response")

    def list_latest_runs_by_project(self) -> list[dict[str, Any]]:
        """The newest run of each project, one row per project.

        `list_runs` cannot answer this: it returns runs newest-first across the
        whole workspace, so one project running every fifteen minutes fills any
        page size and the quiet projects fall off the end — and those are exactly
        the ones where "when did this last run" is worth asking.

        Empty against an API without the route, so a toolkit ahead of its
        platform loses the column rather than the view.

        Such an API does not answer 404. `/runs/{run_id}` is declared there and
        matches first, so FastAPI takes "latest-by-project" for a run id and
        rejects it as an invalid UUID — a 422. Only that shape is treated as an
        absent route: a real call to this path carries no run id, so a
        `uuid_parsing` complaint about one cannot be a genuine error, and 422
        stays meaningful everywhere else.
        """
        try:
            response = self._request_list("GET", "/runs/latest-by-project", expected="run list response")
        except RebaseWorkflowError as exc:
            if exc.status_code in ROUTE_ABSENT_STATUSES:
                return []
            if exc.status_code == 422 and "uuid_parsing" in str(exc) and "run_id" in str(exc):
                return []
            raise
        return response

    def list_run_steps(self, run_id: str) -> list[dict[str, Any]]:
        return self._request_list("GET", f"/runs/{run_id}/steps", expected="step run list response")

    def list_run_tasks(self, run_id: str, *, step_run_id: str | None = None) -> list[dict[str, Any]]:
        """The named inline and Function.map tasks reported for a run.

        Empty when no tasks were reported, and against an API without the route —
        the caller gets a run with no tasks rather than a broken run view.
        """
        params = {"step_run_id": step_run_id} if step_run_id is not None else None
        try:
            response = self._request_list(
                "GET", f"/runs/{run_id}/tasks", params=params, expected="run task list response"
            )
        except RebaseWorkflowError as exc:
            if exc.status_code in ROUTE_ABSENT_STATUSES:
                return []
            raise
        return response

    def create_run_task(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request_dict("POST", f"/runs/{run_id}/tasks", json=payload, expected="run task response")

    def complete_run_task(self, run_id: str, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request_dict(
            "PATCH", f"/runs/{run_id}/tasks/{task_id}", json=payload, expected="run task response"
        )

    def list_run_artifacts(
        self,
        run_id: str,
        *,
        step_run_id: str | None = None,
        task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """The durable output pointers registered for a run."""
        params = {
            key: value for key, value in {"step_run_id": step_run_id, "task_id": task_id}.items() if value is not None
        }
        try:
            response = self._request_list(
                "GET",
                f"/runs/{run_id}/artifacts",
                params=params or None,
                expected="run artifact list response",
            )
        except RebaseWorkflowError as exc:
            if exc.status_code in ROUTE_ABSENT_STATUSES:
                return []
            raise
        return response

    def create_run_artifact(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request_dict("POST", f"/runs/{run_id}/artifacts", json=payload, expected="run artifact response")

    def open_run_artifact(self, run_id: str, artifact_id: str) -> str:
        response = self._request_dict(
            "POST", f"/runs/{run_id}/artifacts/{artifact_id}/open", expected="artifact open URL response"
        )
        if not isinstance(response.get("url"), str):
            raise RebaseWorkflowError("expected artifact open URL response")
        return response["url"]

    def list_run_events(self, run_id: str) -> list[dict[str, Any]]:
        return self._request_list("GET", f"/runs/{run_id}/events", expected="run event list response")

    def create_shell_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Creating a session deploys and starts the shell container's Cloud Run
        # job synchronously, which can take a couple of minutes on first use.
        return self._request_dict(
            "POST", "/shell-sessions", json=payload, timeout=300, expected="shell session response"
        )

    def prepare_source_bundle(self, bundle: SourceBundle) -> dict[str, Any]:
        response = self._request_dict(
            "POST",
            "/source-bundles/prepare",
            json={
                "digest": bundle.digest,
                "archive_size": len(bundle.archive),
                "expanded_size": bundle.expanded_size,
                "file_count": bundle.file_count,
                "manifest": bundle.manifest,
            },
            expected="source bundle preparation response",
        )
        upload_url = response.get("upload_url")
        if isinstance(upload_url, str):
            upload = requests.put(
                upload_url,
                data=bundle.archive,
                headers={"Content-Type": "application/gzip"},
                timeout=DEPLOY_REQUEST_TIMEOUT_SECONDS,
            )
            if upload.status_code >= 400:
                raise RebaseWorkflowError(f"source bundle upload failed: {upload.status_code} {upload.text[:200]}")
            bundle_id = response.get("id")
            response = self._request_dict(
                "POST",
                f"/source-bundles/{bundle_id}/finalize",
                timeout=DEPLOY_REQUEST_TIMEOUT_SECONDS,
                expected="finalized source bundle response",
            )
        if response.get("status") != "ready":
            raise RebaseWorkflowError(str(response.get("error") or "source bundle did not become ready"))
        return response

    def get_image_build_logs(
        self, build_id: str, *, cursor: str | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        if limit:
            params["limit"] = limit
        return self._request_dict(
            "GET", f"/image-builds/{build_id}/logs", params=params, expected="image build logs response"
        )

    def _stream_build_logs(self, build_id: str, cursor: str | None) -> tuple[str | None, bool]:
        """Feed one increment of build output to the installed consumer.

        Returns (cursor, still_streaming). Any failure disables streaming
        rather than the build: older servers answer this route with a redirect
        to a console URL, and a log hiccup must not kill a deploy.
        """
        consumer = _build_log_consumer
        if consumer is None:
            return cursor, False
        try:
            payload = self.get_image_build_logs(build_id, cursor=cursor)
        except Exception:
            return cursor, False
        for line in payload.get("lines") or []:
            consumer(str(line))
        next_cursor = payload.get("cursor")
        return (next_cursor if isinstance(next_cursor, str) else cursor), True

    def build_image(self, *, source_bundle_id: str, recipe: dict[str, Any]) -> dict[str, Any]:
        response = self._request_dict(
            "POST",
            "/image-builds",
            json={"source_bundle_id": source_bundle_id, "recipe": recipe},
            timeout=DEPLOY_REQUEST_TIMEOUT_SECONDS,
            expected="image build response",
        )
        build_id = response.get("id")
        deadline = time.monotonic() + IMAGE_BUILD_TIMEOUT_SECONDS
        log_cursor: str | None = None
        streaming = _build_log_consumer is not None
        while response.get("status") in {"queued", "building"}:
            if not isinstance(build_id, str):
                raise RebaseWorkflowError("image build response is missing an id")
            if time.monotonic() >= deadline:
                logs_url = response.get("logs_url")
                suffix = f" Logs: {logs_url}" if logs_url else ""
                raise RebaseWorkflowError(f"image build did not finish within 30 minutes.{suffix}")
            time.sleep(IMAGE_BUILD_POLL_SECONDS)
            response = self._request_dict("GET", f"/image-builds/{build_id}", expected="image build status response")
            if streaming:
                log_cursor, streaming = self._stream_build_logs(build_id, log_cursor)
        if streaming and isinstance(build_id, str):
            # Final drain: log ingestion lags the terminal status. On failure
            # keep draining briefly — the trailing lines are the ones that
            # explain it, and Cloud Logging can be seconds behind.
            attempts = 1 if response.get("status") == "succeeded" else 4
            for attempt in range(attempts):
                log_cursor, streaming = self._stream_build_logs(build_id, log_cursor)
                if not streaming:
                    break
                if attempt < attempts - 1:
                    time.sleep(1.0)
        if response.get("status") != "succeeded" or not response.get("image_digest"):
            logs_url = response.get("logs_url")
            suffix = f" Logs: {logs_url}" if logs_url else ""
            raise RebaseWorkflowError(str(response.get("error") or "image build failed") + suffix)
        return response

    def prepare_built_image(self, fn: FunctionType, image: Image) -> dict[str, Any]:
        bundle = build_source_bundle(fn, image)
        source = self.prepare_source_bundle(bundle)
        build = self.build_image(
            source_bundle_id=str(source["id"]),
            recipe=bundle.manifest["image_recipe"],
        )
        return {
            "image_build_id": build["id"],
            "image_digest": build["image_digest"],
            "image_recipe": bundle.manifest["image_recipe"],
            "source_bundle_id": source["id"],
            "source_bundle_digest": bundle.digest,
            "entrypoint_module": bundle.entrypoint_module,
            "entrypoint_qualname": bundle.entrypoint_qualname,
        }

    def get_shell_session(self, session_id: str) -> dict[str, Any]:
        return self._request_dict("GET", f"/shell-sessions/{session_id}", expected="shell session response")

    def delete_shell_session(self, session_id: str) -> None:
        self.request_no_content("DELETE", f"/shell-sessions/{session_id}")


class Project:
    def __init__(
        self,
        name: str,
        *,
        description: str | None = None,
        source_mode: str | None = None,
        repo_owner: str | None = None,
        repo_name: str | None = None,
        repo_path: str | None = None,
        deploy_source: str | None = None,
        client: Client | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.source_mode = source_mode
        self.repo_owner = repo_owner
        self.repo_name = repo_name
        self.repo_path = repo_path
        self.deploy_source = _validate_deploy_source(deploy_source)
        self.client = client
        self.id: str | None = None
        self._functions: list[Function] = []
        self._steps: list[Step] = []
        self._workflows: list[Workflow] = []
        self._asgi_apps: list[ASGIApp] = []

    @property
    def _client(self) -> Client:
        return self.client or default_client()

    @_environment_scoped_deploy
    def deploy(
        self, *, replace: bool = False, deploy_source: str | None = None, environment: str | None = None
    ) -> Self:
        environment = _resolve_environment(self._client, environment)
        for workflow in self._workflows:
            workflow._validate_schedule_defaults()
        resolved_deploy_source = _validate_deploy_source(deploy_source)
        preflight_datasets(self._client)
        project = self._client.ensure_project(
            self.name,
            description=self.description,
            source_mode=self.source_mode,
            repo_owner=self.repo_owner,
            repo_name=self.repo_name,
            repo_path=self.repo_path,
            environment_name=environment,
        )
        self.id = project["id"]
        for function in self._functions:
            if isinstance(function, Step):
                continue  # Steps are deployed automatically via their workflow
            function.deploy(replace=replace, deploy_source=resolved_deploy_source, environment=environment)
        for workflow in self._workflows:
            workflow.deploy(
                replace=replace,
                deploy_source=resolved_deploy_source,
                environment=environment,
                _skip_dataset_preflight=True,
            )
        for asgi_app in self._asgi_apps:
            asgi_app.deploy(replace=replace, deploy_source=resolved_deploy_source, environment=environment)
        return self

    def function(
        self,
        *,
        name: str | None = None,
        description: str | None = None,
        default_parameters: dict[str, Any] | None = None,
        mode: ExecutionMode | None = None,
        isolation: Isolation | None = None,
        run_type: RunType | None = None,
        dependencies: list[str] | tuple[str, ...] | None = None,
        image: Image | dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
        secrets: dict[str, str] | list[Secret | str] | None = None,
        volumes: dict[str, Volume | str] | list[dict[str, Any]] | None = None,
        buckets: list[Bucket | str] | list[dict[str, Any]] | None = None,
        min_instances: int | None = None,
        concurrency: int | None = None,
        cpu: float | int | str | None = None,
        memory: int | float | str | None = None,
        enabled: bool = True,
        endpoint: EndpointConfig | dict[str, Any] | None = None,
        deploy_source: str | None = None,
        backend: str | None = None,
    ) -> Callable[[Callable[..., Any]], Function]:
        _reject_legacy_backend(backend)

        def decorator(fn: Callable[..., Any]) -> Function:
            function = Function(
                fn,
                name=name,
                project=self.name,
                description=description,
                default_parameters=default_parameters,
                mode=mode,
                isolation=isolation,
                run_type=run_type,
                dependencies=dependencies,
                image=image,
                env=env,
                secrets=secrets,
                volumes=volumes,
                buckets=buckets,
                min_instances=min_instances,
                concurrency=concurrency,
                cpu=cpu,
                memory=memory,
                enabled=enabled,
                endpoint=endpoint,
                deploy_source=deploy_source if deploy_source is not None else self.deploy_source,
                project_source_mode=self.source_mode,
                client=self._client,
            )
            self._functions.append(function)
            return function

        return decorator

    def asgi_app(
        self,
        *,
        name: str | None = None,
        description: str | None = None,
        base_path: str = "/",
        auth: str = "api_key",
        dependencies: list[str] | tuple[str, ...] | None = None,
        image: Image | dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
        secrets: dict[str, str] | list[Secret | str] | None = None,
        volumes: dict[str, Volume | str] | list[dict[str, Any]] | None = None,
        buckets: list[Bucket | str] | list[dict[str, Any]] | None = None,
        min_instances: int | None = None,
        max_instances: int | None = None,
        concurrency: int | None = None,
        timeout_seconds: int | None = None,
        cpu: str | None = None,
        memory: str | None = None,
        enabled: bool = True,
        deploy_source: str | None = None,
    ) -> Callable[[Callable[..., Any]], ASGIApp]:
        def decorator(fn: Callable[..., Any]) -> ASGIApp:
            asgi_app = ASGIApp(
                fn,
                name=name,
                project=self.name,
                description=description,
                base_path=base_path,
                auth=auth,
                dependencies=dependencies,
                image=image,
                env=env,
                secrets=secrets,
                volumes=volumes,
                buckets=buckets,
                min_instances=min_instances,
                max_instances=max_instances,
                concurrency=concurrency,
                timeout_seconds=timeout_seconds,
                cpu=cpu,
                memory=memory,
                enabled=enabled,
                deploy_source=deploy_source if deploy_source is not None else self.deploy_source,
                project_source_mode=self.source_mode,
                client=self._client,
            )
            self._asgi_apps.append(asgi_app)
            return asgi_app

        return decorator

    def step(
        self,
        *,
        name: str | None = None,
        description: str | None = None,
        default_parameters: dict[str, Any] | None = None,
        enabled: bool = True,
        retries: int = 0,
        timeout_seconds: int | float | None = None,
        cache: bool = False,
        deploy_source: str | None = None,
    ) -> Callable[[Callable[..., Any]], Step]:
        def decorator(fn: Callable[..., Any]) -> Step:
            step = Step(
                fn,
                name=name,
                project=self.name,
                description=description,
                default_parameters=default_parameters,
                enabled=enabled,
                deploy_source=deploy_source if deploy_source is not None else self.deploy_source,
                project_source_mode=self.source_mode,
                client=self._client,
                retries=retries,
                timeout_seconds=timeout_seconds,
                cache=cache,
            )
            self._steps.append(step)
            self._functions.append(step)
            return step

        return decorator

    def workflow(
        self,
        *,
        name: str | None = None,
        description: str | None = None,
        schedule: Schedule | None = None,
        trigger: Trigger | None = None,
        default_parameters: dict[str, Any] | None = None,
        mode: ExecutionMode | None = None,
        isolation: Isolation | None = None,
        run_type: RunType | None = None,
        enabled: bool = True,
        endpoint: EndpointConfig | dict[str, Any] | None = None,
        deploy_source: str | None = None,
        dependencies: list[str] | tuple[str, ...] | None = None,
        image: Image | dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
        secrets: dict[str, str] | list[Secret | str] | None = None,
        buckets: list[Bucket | str] | None = None,
        min_instances: int | None = None,
        concurrency: int | None = None,
        cpu: float | int | str | None = None,
        memory: int | float | str | None = None,
        resources: dict[str, Any] | None = None,
        backend: str | None = None,
    ) -> Callable[[Callable[..., Any]], Workflow]:
        _reject_legacy_backend(backend)

        def decorator(fn: Callable[..., Any]) -> Workflow:
            workflow = Workflow(
                fn,
                name=name,
                project=self.name,
                description=description,
                schedule=schedule,
                trigger=trigger,
                default_parameters=default_parameters,
                mode=mode,
                isolation=isolation,
                run_type=run_type,
                enabled=enabled,
                endpoint=endpoint,
                deploy_source=deploy_source if deploy_source is not None else self.deploy_source,
                project_source_mode=self.source_mode,
                client=self._client,
                dependencies=dependencies,
                image=image,
                env=env,
                secrets=secrets,
                buckets=buckets,
                min_instances=min_instances,
                concurrency=concurrency,
                cpu=cpu,
                memory=memory,
                resources=resources,
            )
            self._workflows.append(workflow)
            return workflow

        return decorator


class ASGIApp:
    def __init__(
        self,
        fn: Callable[..., Any] | None = None,
        *,
        project: str = DEFAULT_PROJECT_NAME,
        name: str | None = None,
        description: str | None = None,
        base_path: str = "/",
        auth: str = "api_key",
        dependencies: list[str] | tuple[str, ...] | None = None,
        image: Image | dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
        secrets: dict[str, str] | list[Secret | str] | None = None,
        volumes: dict[str, Volume | str] | list[dict[str, Any]] | None = None,
        buckets: list[Bucket | str] | list[dict[str, Any]] | None = None,
        min_instances: int | None = None,
        max_instances: int | None = None,
        concurrency: int | None = None,
        timeout_seconds: int | None = None,
        cpu: str | None = None,
        memory: str | None = None,
        enabled: bool = True,
        deploy_source: str | None = None,
        project_source_mode: str | None = None,
        client: Client | None = None,
        asgi_app_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.fn = fn
        self.project = project
        self.description = description
        self.base_path = _normalize_path(base_path, field_name="base_path")
        if auth not in {"api_key", "workspace", "public"}:
            raise ValueError("ASGI app auth must be one of: api_key, workspace, public")
        self.auth = auth
        self.env = dict(env or {})
        # Kept unresolved (Secret handles or an {ENV: ref} map) until deploy needs a client.
        self.secrets = secrets if secrets is not None else {}
        # Kept unresolved (Volume handles or an attachment list) until deploy needs a client.
        self.volumes = volumes if volumes is not None else list((data.get("volumes") if data else None) or [])
        # Kept unresolved (Bucket handles or an attachment list) until deploy needs a client.
        self.buckets = buckets if buckets is not None else list((data.get("buckets") if data else None) or [])
        self.enabled = enabled
        self.deploy_source = _validate_deploy_source(deploy_source)
        self.project_source_mode = project_source_mode
        self.client = client
        self.id: str | None = asgi_app_id
        self.data = data or {}
        self.name = name or (data["name"] if data else None)
        self.source_code: str | None = None
        self.entrypoint: str | None = None
        self.image_spec: dict[str, Any] | None = data.get("image_spec") if data else None
        self.image: Image | None = image if isinstance(image, Image) else None
        self.image_fingerprint: str | None = data.get("image_fingerprint") if data else None
        self.cloud_run_min_instances: int | None = data.get("cloud_run_min_instances") if data else None
        self.cloud_run_max_instances: int | None = data.get("cloud_run_max_instances") if data else None
        self.cloud_run_concurrency: int | None = data.get("cloud_run_concurrency") if data else None
        self.cloud_run_timeout_seconds: int | None = data.get("cloud_run_timeout_seconds") if data else None
        self.cloud_run_cpu: str | None = data.get("cloud_run_cpu") if data else None
        self.cloud_run_memory: str | None = data.get("cloud_run_memory") if data else None
        self.source_metadata: dict[str, Any] = {}

        if fn is not None:
            if not isinstance(fn, FunctionType):
                raise TypeError("ASGIApp requires a plain Python function")
            self.name = _target_name(fn, name)
            self.image_spec = _image_spec_for(image=image, dependencies=dependencies)
            if min_instances is not None and min_instances < 0:
                raise ValueError("min_instances must be greater than or equal to 0")
            if max_instances is not None and max_instances < 0:
                raise ValueError("max_instances must be greater than or equal to 0")
            if concurrency is not None and concurrency < 1:
                raise ValueError("concurrency must be greater than or equal to 1")
            if timeout_seconds is not None and timeout_seconds < 1:
                raise ValueError("timeout_seconds must be greater than or equal to 1")
            self.cloud_run_min_instances = min_instances
            self.cloud_run_max_instances = max_instances
            self.cloud_run_concurrency = concurrency
            self.cloud_run_timeout_seconds = timeout_seconds
            self.cloud_run_cpu = cpu
            self.cloud_run_memory = memory
            self.source_code = _source_for(fn, target="ASGI app")
            self.entrypoint = fn.__name__
            self.source_metadata = _git_metadata_for(fn)
        if self.name is None:
            raise ValueError("ASGI app name is required")

    @classmethod
    def from_name(cls, project: str, name: str, *, client: Client | None = None) -> ASGIApp:
        resolved_client = client or default_client()
        data = resolved_client.find_asgi_app(name, project=project)
        if data is None:
            raise RebaseWorkflowError(f"ASGI app not found: {project}/{name}")
        return cls(project=project, name=name, client=resolved_client, asgi_app_id=data["id"], data=data)

    @property
    def _client(self) -> Client:
        return self.client or default_client()

    def _source_metadata_for_deploy(self, deploy_source: str | None = None) -> dict[str, Any]:
        return _target_source_metadata_for_deploy(self, deploy_source)

    @_environment_scoped_deploy
    def deploy(
        self, *, replace: bool = False, deploy_source: str | None = None, environment: str | None = None
    ) -> ASGIApp:
        environment = _resolve_environment(self._client, environment)
        if self.source_code is None or self.entrypoint is None:
            raise RebaseWorkflowError("cannot deploy an ASGI app handle without source_code and entrypoint")
        if self.name is None:
            raise RebaseWorkflowError("ASGI app name is required")
        source_metadata = self._source_metadata_for_deploy(deploy_source)
        secrets_payload = _resolve_secrets_payload(self.secrets, self._client)
        volumes_payload = _resolve_volumes_payload(self.volumes, self._client)
        buckets_payload = _resolve_buckets_payload(self.buckets, self._client)
        build = (
            self._client.prepare_built_image(self.fn, self.image)
            if self.fn and self.image and self.image.build_enabled
            else None
        )
        existing = self._client.find_asgi_app(self.name, project=self.project)
        if existing is not None:
            asgi_app = self._client.update_asgi_app(
                existing["id"],
                description=self.description,
                source_code=self.source_code,
                entrypoint=self.entrypoint,
                base_path=self.base_path,
                auth=self.auth,
                image_spec=self.image_spec,
                env=self.env,
                secrets=secrets_payload,
                volumes=volumes_payload,
                buckets=buckets_payload,
                cloud_run_min_instances=self.cloud_run_min_instances,
                cloud_run_max_instances=self.cloud_run_max_instances,
                cloud_run_concurrency=self.cloud_run_concurrency,
                cloud_run_timeout_seconds=self.cloud_run_timeout_seconds,
                cloud_run_cpu=self.cloud_run_cpu,
                cloud_run_memory=self.cloud_run_memory,
                enabled=self.enabled,
                build=build,
                environment=environment,
                **source_metadata,
            )
            self.id = asgi_app["id"]
            self.data = asgi_app
            return self

        asgi_app = self._client.register_asgi_app(
            project=self.project,
            name=self.name,
            description=self.description,
            source_code=self.source_code,
            entrypoint=self.entrypoint,
            base_path=self.base_path,
            auth=self.auth,
            image_spec=self.image_spec,
            env=self.env,
            secrets=secrets_payload,
            volumes=volumes_payload,
            buckets=buckets_payload,
            cloud_run_min_instances=self.cloud_run_min_instances,
            cloud_run_max_instances=self.cloud_run_max_instances,
            cloud_run_concurrency=self.cloud_run_concurrency,
            cloud_run_timeout_seconds=self.cloud_run_timeout_seconds,
            cloud_run_cpu=self.cloud_run_cpu,
            cloud_run_memory=self.cloud_run_memory,
            enabled=self.enabled,
            build=build,
            environment=environment,
            **source_metadata,
        )
        self.id = asgi_app["id"]
        self.data = asgi_app
        return self


class Function:
    def __init__(
        self,
        fn: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        project: str,
        description: str | None = None,
        default_parameters: dict[str, Any] | None = None,
        mode: ExecutionMode | None = None,
        isolation: Isolation | None = None,
        run_type: RunType | None = None,
        dependencies: list[str] | tuple[str, ...] | None = None,
        image: Image | dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
        secrets: dict[str, str] | list[Secret | str] | None = None,
        volumes: dict[str, Volume | str] | list[dict[str, Any]] | None = None,
        buckets: list[Bucket | str] | list[dict[str, Any]] | None = None,
        min_instances: int | None = None,
        concurrency: int | None = None,
        cpu: float | int | str | None = None,
        memory: int | float | str | None = None,
        enabled: bool = True,
        endpoint: EndpointConfig | dict[str, Any] | None = None,
        deploy_source: str | None = None,
        project_source_mode: str | None = None,
        client: Client | None = None,
        function_id: str | None = None,
        data: dict[str, Any] | None = None,
        backend: str | None = None,
    ) -> None:
        _reject_legacy_backend(backend)
        self.fn = fn
        self.project = project
        self.description = description
        self.enabled = enabled
        self.env = dict(env or (data.get("env") if data else None) or {})
        # Kept unresolved (Secret handles or an {ENV: ref} map) until deploy needs a client.
        self.secrets = secrets if secrets is not None else dict((data.get("secrets") if data else None) or {})
        # Kept unresolved (Volume handles or an attachment list) until deploy needs a client.
        self.volumes = volumes if volumes is not None else list((data.get("volumes") if data else None) or [])
        # Kept unresolved (Bucket handles or an attachment list) until deploy needs a client.
        self.buckets = buckets if buckets is not None else list((data.get("buckets") if data else None) or [])
        self.endpoint = _coerce_endpoint(endpoint) or _endpoint_for_callable(fn)
        self.client = client
        self.id: str | None = function_id
        self.data = data or {}
        self.deploy_source = _validate_deploy_source(deploy_source)
        self.project_source_mode = project_source_mode
        self.name = name or (data["name"] if data else None)
        self.source_code: str | None = None
        self.entrypoint: str | None = None
        self.default_parameters = default_parameters or {}
        data_mode = data.get("mode") if data else None
        data_isolation = data.get("isolation") if data else None
        data_run_type = data.get("run_type") if data and data_mode is None and data_isolation is None else None
        self.mode, self.isolation = _validate_execution(
            data_mode if data else mode,
            data_isolation if data else isolation,
            run_type=data_run_type if data else run_type,
        )
        self.run_type: RunType = _legacy_run_type(self.mode, self.isolation)
        self.image_spec: dict[str, Any] | None = data.get("image_spec") if data else None
        self.image: Image | None = image if isinstance(image, Image) else None
        self.image_fingerprint: str | None = data.get("image_fingerprint") if data else None
        self.cloud_run_min_instances: int | None = data.get("cloud_run_min_instances") if data else None
        self.cloud_run_concurrency: int | None = data.get("cloud_run_concurrency") if data else None
        self.cloud_run_cpu: str | None = _cloud_run_cpu_value(cpu) or (data.get("cloud_run_cpu") if data else None)
        self.cloud_run_memory: str | None = _cloud_run_memory_value(memory) or (
            data.get("cloud_run_memory") if data else None
        )
        self.source_metadata: dict[str, Any] = {}

        if fn is not None:
            if not isinstance(fn, FunctionType):
                raise TypeError("Function requires a plain Python function")
            self.name = _target_name(fn, name)
            inferred_defaults = _defaults_for(fn, target="Function")
            self.default_parameters = {**inferred_defaults, **(default_parameters or {})}
            self.mode, self.isolation = _validate_execution(mode, isolation, run_type=run_type)
            self.run_type = _legacy_run_type(self.mode, self.isolation)
            self.image_spec = _image_spec_for(image=image, dependencies=dependencies)
            if min_instances is not None and min_instances < 0:
                raise ValueError("min_instances must be greater than or equal to 0")
            if concurrency is not None and concurrency < 1:
                raise ValueError("concurrency must be greater than or equal to 1")
            self.cloud_run_min_instances = min_instances
            self.cloud_run_concurrency = concurrency
            self.source_code = _source_for(fn, target="function")
            self.entrypoint = fn.__name__
            self.source_metadata = _git_metadata_for(fn)
        if self.name is None:
            raise ValueError("function name is required")

    @classmethod
    def from_name(cls, project: str, name: str, *, client: Client | None = None) -> Function:
        resolved_client = client or default_client()
        data = resolved_client.find_function(name, project=project)
        if data is None:
            raise RebaseWorkflowError(f"function not found: {project}/{name}")
        return cls(project=project, name=name, client=resolved_client, function_id=data["id"], data=data)

    @property
    def _client(self) -> Client:
        return self.client or default_client()

    def _source_metadata_for_deploy(self, deploy_source: str | None = None) -> dict[str, Any]:
        return _target_source_metadata_for_deploy(self, deploy_source)

    @_environment_scoped_deploy
    def deploy(
        self, *, replace: bool = False, deploy_source: str | None = None, environment: str | None = None
    ) -> Function:
        environment = _resolve_environment(self._client, environment)
        if self.source_code is None or self.entrypoint is None:
            raise RebaseWorkflowError("cannot deploy a function handle without source_code and entrypoint")
        if self.name is None:
            raise RebaseWorkflowError("function name is required")
        name = self.name
        source_metadata = self._source_metadata_for_deploy(deploy_source)
        secrets_payload = _resolve_secrets_payload(self.secrets, self._client)
        volumes_payload = _resolve_volumes_payload(self.volumes, self._client)
        buckets_payload = _resolve_buckets_payload(self.buckets, self._client)
        build = None
        if self.fn and self.image and self.image.build_enabled:
            if self.mode == "interactive" and self.isolation == "shared":
                raise RebaseWorkflowError(
                    "Images using uv_sync or add_local_* cannot run on the shared runner. "
                    "Use isolation='dedicated' or mode='job'."
                )
            build = self._client.prepare_built_image(self.fn, self.image)
        existing = self._client.find_function(name, project=self.project)
        if existing is not None:
            function = self._client.update_function(
                existing["id"],
                description=self.description,
                source_code=self.source_code,
                entrypoint=self.entrypoint,
                default_parameters=self.default_parameters,
                mode=self.mode,
                isolation=self.isolation,
                image_spec=self.image_spec,
                env=self.env,
                secrets=secrets_payload,
                volumes=volumes_payload,
                buckets=buckets_payload,
                cloud_run_min_instances=self.cloud_run_min_instances,
                cloud_run_concurrency=self.cloud_run_concurrency,
                cloud_run_cpu=self.cloud_run_cpu,
                cloud_run_memory=self.cloud_run_memory,
                enabled=self.enabled,
                endpoint=self.endpoint,
                build=build,
                environment=environment,
                **source_metadata,
            )
            self.id = function["id"]
            self.data = function
            return self

        function = self._client.register_function(
            project=self.project,
            name=name,
            description=self.description,
            source_code=self.source_code,
            entrypoint=self.entrypoint,
            default_parameters=self.default_parameters,
            mode=self.mode,
            isolation=self.isolation,
            image_spec=self.image_spec,
            env=self.env,
            secrets=secrets_payload,
            volumes=volumes_payload,
            buckets=buckets_payload,
            cloud_run_min_instances=self.cloud_run_min_instances,
            cloud_run_concurrency=self.cloud_run_concurrency,
            cloud_run_cpu=self.cloud_run_cpu,
            cloud_run_memory=self.cloud_run_memory,
            enabled=self.enabled,
            endpoint=self.endpoint,
            build=build,
            environment=environment,
            **source_metadata,
        )
        self.id = function["id"]
        self.data = function
        return self

    def spawn(self, **parameters: Any) -> Run:
        if self.id is None:
            self.deploy()
        if self.id is None:
            raise RebaseWorkflowError("function has no ID after deployment")
        return self._client.run_function(self.id, parameters)

    def remote(self, **parameters: Any) -> dict[str, Any]:
        return self.spawn(**parameters).result()

    def _infer_map_parameter(self) -> str:
        required_parameters = _required_parameters_for(self.fn, target="Function") if self.fn is not None else []
        if len(required_parameters) == 1:
            return required_parameters[0]
        raise RebaseWorkflowError("parameter is required when Function.map items are not dictionaries")

    def _map_event_value(
        self,
        event: dict[str, Any],
        *,
        return_exceptions: bool,
    ) -> dict[str, Any] | RebaseWorkflowError:
        if event.get("status") == "succeeded":
            result = event.get("result")
            if isinstance(result, dict):
                return result
            return {"value": result}

        error = RebaseWorkflowError(str(event.get("error") or "Function.map item failed"))
        if return_exceptions:
            return error
        raise error

    def map(
        self,
        items: Iterable[Any],
        *,
        parameter: str | None = None,
        kwargs: dict[str, Any] | None = None,
        max_concurrency: int | None = None,
        ordered: bool = True,
        return_exceptions: bool = False,
        timeout: float | None = None,
    ) -> Iterator[dict[str, Any] | RebaseWorkflowError]:
        item_list = list(items)
        if not item_list:
            raise RebaseWorkflowError("Function.map requires at least one item")
        if parameter is None and any(not isinstance(item, dict) for item in item_list):
            parameter = self._infer_map_parameter()
        if self.id is None:
            self.deploy()
        if self.id is None:
            raise RebaseWorkflowError("function has no ID after deployment")

        events = self._client.run_function_map(
            self.id,
            items=item_list,
            parameter=parameter,
            kwargs=kwargs,
            max_concurrency=max_concurrency,
            ordered=ordered,
            return_exceptions=return_exceptions,
            timeout=timeout,
        )
        if not ordered:
            for event in events:
                if event.get("type") == "item":
                    yield self._map_event_value(event, return_exceptions=return_exceptions)
            return

        expected_index = 0
        buffer: dict[int, dict[str, Any]] = {}
        for event in events:
            if event.get("type") != "item":
                continue
            index = int(event["index"])
            buffer[index] = event
            while expected_index in buffer:
                buffered_event = buffer.pop(expected_index)
                yield self._map_event_value(buffered_event, return_exceptions=return_exceptions)
                expected_index += 1

    def run(self, **parameters: Any) -> Run:
        return self.spawn(**parameters)

    def ephemeral_run(self, **parameters: Any) -> Run:
        if self.source_code is None or self.entrypoint is None:
            raise RebaseWorkflowError("cannot run an ephemeral function without local source")
        return self._client.run_ephemeral(
            target_type="function",
            project=self.project,
            name=str(self.name),
            source_code=self.source_code,
            entrypoint=self.entrypoint,
            default_parameters=self.default_parameters,
            parameters=parameters,
            mode=self.mode,
            isolation=self.isolation,
            image_spec=self.image_spec,
            cloud_run_min_instances=self.cloud_run_min_instances,
            cloud_run_concurrency=self.cloud_run_concurrency,
            env=self.env,
            secrets=_resolve_secrets_payload(self.secrets, self._client),
        )

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self.fn is None:
            raise RebaseWorkflowError("remote function handles cannot be called locally; use .remote(...)")
        return self.fn(*args, **kwargs)


MODEL_NOT_DIRECTLY_DEPLOYABLE_ERROR = (
    "rebase.Model is not directly deployable; inherit from rebase.Predictor, rebase.Optimizer, or rebase.Agent"
)
SIMULATOR_NOT_DEPLOYABLE_ERROR = (
    "rebase.Simulator cloud deployment is not supported yet; simulators are local-only until state/session "
    "semantics are defined"
)


class _RemoteModelOperation:
    def __init__(self, handle: ModelHandle) -> None:
        self._handle = handle

    def spawn(self, *, environment: str | None = None, **parameters: Any) -> Run:
        return self._handle.spawn(environment=environment, **parameters)

    def remote(self, *, environment: str | None = None, **parameters: Any) -> dict[str, Any]:
        return self._handle.remote(environment=environment, **parameters)

    def run(self, *, environment: str | None = None, **parameters: Any) -> Run:
        return self.spawn(environment=environment, **parameters)

    def __call__(self, *, environment: str | None = None, **parameters: Any) -> dict[str, Any]:
        return self.remote(environment=environment, **parameters)


class ModelHandle:
    def __init__(
        self,
        data: dict[str, Any],
        *,
        client: Client | None = None,
        project: str | None = None,
        operation_name: str | None = None,
    ) -> None:
        self.data = data
        self.client = client
        self.project_name = project
        self.operation_name = operation_name or data.get("operation_name")
        if self.operation_name is not None:
            setattr(self, str(self.operation_name), _RemoteModelOperation(self))

    @property
    def id(self) -> str | None:
        model_id = self.data.get("id")
        return str(model_id) if model_id is not None else None

    @property
    def name(self) -> str:
        return str(self.data.get("name") or "")

    @property
    def project(self) -> str:
        return self.project_name or str(self.data.get("project_id") or "")

    @property
    def _client(self) -> Client:
        return self.client or default_client()

    def spawn(self, *, environment: str | None = None, **parameters: Any) -> Run:
        if self.id is None:
            raise RebaseWorkflowError("model handle has no ID")
        return self._client.run_model(self.id, parameters, environment=environment)

    def remote(self, *, environment: str | None = None, **parameters: Any) -> dict[str, Any]:
        return self.spawn(environment=environment, **parameters).result()

    def run(self, *, environment: str | None = None, **parameters: Any) -> Run:
        return self.spawn(environment=environment, **parameters)


class PredictorHandle(ModelHandle):
    predict: _RemoteModelOperation

    def __init__(self, data: dict[str, Any], *, client: Client | None = None, project: str | None = None) -> None:
        super().__init__(data, client=client, project=project, operation_name="predict")


class OptimizerHandle(ModelHandle):
    optimize: _RemoteModelOperation

    def __init__(self, data: dict[str, Any], *, client: Client | None = None, project: str | None = None) -> None:
        super().__init__(data, client=client, project=project, operation_name="optimize")


class AgentHandle(ModelHandle):
    act: _RemoteModelOperation

    def __init__(self, data: dict[str, Any], *, client: Client | None = None, project: str | None = None) -> None:
        super().__init__(data, client=client, project=project, operation_name="act")


def _model_handle_for_data(
    data: dict[str, Any],
    *,
    client: Client | None = None,
    project: str | None = None,
) -> ModelHandle:
    operation_name = data.get("operation_name")
    if operation_name == "predict":
        return PredictorHandle(data, client=client, project=project)
    if operation_name == "optimize":
        return OptimizerHandle(data, client=client, project=project)
    if operation_name == "act":
        return AgentHandle(data, client=client, project=project)
    return ModelHandle(
        data,
        client=client,
        project=project,
        operation_name=str(operation_name) if operation_name else None,
    )


def _model_kind_for(model: Model) -> str:
    if isinstance(model, Predictor):
        return "predictor"
    if isinstance(model, Optimizer):
        return "optimizer"
    if isinstance(model, Agent):
        return "agent"
    return "model"


def _hf_repo_type_arg(repo_type: str) -> str | None:
    return None if repo_type == "model" else repo_type


def _hf_commit_oid(commit_info: Any) -> str | None:
    oid = getattr(commit_info, "oid", None) or getattr(commit_info, "commit_id", None)
    if isinstance(oid, str) and oid:
        return oid
    value = str(commit_info)
    return value if value and value.startswith("http") is False else None


def _hf_commit_url(commit_info: Any) -> str | None:
    commit_url = getattr(commit_info, "commit_url", None)
    return commit_url if isinstance(commit_url, str) and commit_url else None


def _hf_latest_commit(api: Any, *, repo_id: str, repo_type: str, revision: str) -> str | None:
    try:
        commits = api.list_repo_commits(
            repo_id=repo_id,
            repo_type=_hf_repo_type_arg(repo_type),
            revision=revision,
        )
    except Exception:
        return None
    if not commits:
        return None
    commit_id = getattr(commits[0], "commit_id", None)
    return commit_id if isinstance(commit_id, str) and commit_id else None


def _version_source_url(version: dict[str, Any]) -> str | None:
    owner = version.get("repo_owner")
    repo = version.get("repo_name")
    commit = version.get("git_commit_sha")
    source_path = version.get("source_path")
    if not owner or not repo or not commit:
        return None
    base = f"https://github.com/{owner}/{repo}/tree/{commit}"
    return f"{base}/{source_path}" if source_path else base


def _write_huggingface_provenance_files(
    folder: Path,
    *,
    model_name: str,
    model_data: dict[str, Any],
    version: dict[str, Any],
    include_source: bool,
    include_readme: bool,
) -> None:
    provenance = {
        "schema_version": 1,
        "provider": "rebase",
        "target_type": "model",
        "model_id": version.get("model_id") or model_data.get("id"),
        "model_name": model_name,
        "model_kind": version.get("kind") or model_data.get("kind"),
        "model_version_id": version.get("id"),
        "model_version_number": version.get("version_number"),
        "model_fingerprint": version.get("fingerprint"),
        "operation_name": version.get("operation_name"),
        "source_repo": (
            f"{version.get('repo_owner')}/{version.get('repo_name')}"
            if version.get("repo_owner") and version.get("repo_name")
            else None
        ),
        "source_path": version.get("source_path"),
        "source_url": _version_source_url(version),
        "git_commit_sha": version.get("git_commit_sha"),
        "git_branch": version.get("git_branch"),
        "git_tag": version.get("git_tag"),
        "git_dirty": version.get("git_dirty"),
        "source_hash": version.get("source_hash"),
        "image_fingerprint": version.get("image_fingerprint"),
    }
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "rebase_model.json").write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if include_source and version.get("source_code"):
        (folder / "rebase_source.py").write_text(str(version["source_code"]), encoding="utf-8")
    if include_readme:
        source_commit = provenance["git_commit_sha"] or "not recorded"
        source_ref = provenance["source_url"] or provenance["source_hash"] or "not recorded"
        readme = textwrap.dedent(
            f"""
            ---
            tags:
            - rebase
            ---

            # {model_name}

            This repository was published from Rebase model version `{provenance["model_version_id"]}`.

            Source commit: `{source_commit}`

            Source reference: {source_ref}

            Rebase fingerprint: `{provenance["model_fingerprint"]}`
            """
        ).lstrip()
        (folder / "README.md").write_text(readme, encoding="utf-8")


class Model(_EmflowModel):
    _operation_name: str | None = None
    _emflow_init_mode = "name"
    _not_deployable_message = MODEL_NOT_DIRECTLY_DEPLOYABLE_ERROR

    project: str | None = "default"
    description: str | None = None
    default_parameters: dict[str, Any] | None = None
    mode: ExecutionMode = DEFAULT_MODE
    isolation: Isolation = DEFAULT_ISOLATION
    run_type: RunType | None = None
    dependencies: list[str] | tuple[str, ...] | None = None
    image: Image | dict[str, Any] | None = None
    env: dict[str, str] | None = None
    secrets: dict[str, str] | list[Secret | str] | None = None
    min_instances: int | None = None
    concurrency: int | None = None
    cpu: float | int | str | None = None
    memory: int | float | str | None = None
    enabled: bool = True
    endpoint: EndpointConfig | dict[str, Any] | None = None
    deploy_source: str | None = None

    def __init__(
        self,
        name: str | None = None,
        *,
        project: str | None = None,
        description: str | None = None,
        default_parameters: dict[str, Any] | None = None,
        mode: ExecutionMode | None = None,
        isolation: Isolation | None = None,
        run_type: RunType | None = None,
        dependencies: list[str] | tuple[str, ...] | None = None,
        image: Image | dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
        secrets: dict[str, str] | list[Secret | str] | None = None,
        min_instances: int | None = None,
        concurrency: int | None = None,
        cpu: float | int | str | None = None,
        memory: int | float | str | None = None,
        enabled: bool | None = None,
        endpoint: EndpointConfig | dict[str, Any] | None = None,
        deploy_source: str | None = None,
        client: Client | None = None,
        backend: str | None = None,
    ) -> None:
        _reject_legacy_backend(backend)
        cls = type(self)
        resolved_name = name or getattr(cls, "name", None) or _model_target_name(cls)
        self._init_emflow_base(resolved_name)
        self.name = resolved_name
        self.project = project if project is not None else getattr(cls, "project", "default")
        self.description = description if description is not None else getattr(cls, "description", None)
        class_default_parameters = getattr(cls, "default_parameters", None)
        self.default_parameters: dict[str, Any] = (
            dict(default_parameters) if default_parameters is not None else dict(class_default_parameters or {})
        )
        class_run_type = (
            cls.__dict__.get("run_type") if mode is None and isolation is None and run_type is None else None
        )
        resolved_mode = (
            mode if mode is not None else (None if class_run_type is not None else getattr(cls, "mode", DEFAULT_MODE))
        )
        resolved_isolation = (
            isolation
            if isolation is not None
            else (None if class_run_type is not None else getattr(cls, "isolation", DEFAULT_ISOLATION))
        )
        self.mode, self.isolation = _validate_execution(
            resolved_mode,
            resolved_isolation,
            run_type=run_type if run_type is not None else class_run_type,
        )
        self.run_type = _legacy_run_type(self.mode, self.isolation)
        self.dependencies = dependencies if dependencies is not None else getattr(cls, "dependencies", None)
        self.image = image if image is not None else getattr(cls, "image", None)
        self.env = dict(env if env is not None else (getattr(cls, "env", None) or {}))
        # Kept unresolved (Secret handles or an {ENV: ref} map) until deploy needs a client.
        self.secrets = secrets if secrets is not None else getattr(cls, "secrets", None) or {}
        self.cloud_run_min_instances = (
            min_instances if min_instances is not None else getattr(cls, "min_instances", None)
        )
        self.cloud_run_concurrency = concurrency if concurrency is not None else getattr(cls, "concurrency", None)
        self.cloud_run_cpu = _cloud_run_cpu_value(cpu if cpu is not None else getattr(cls, "cpu", None))
        self.cloud_run_memory = _cloud_run_memory_value(memory if memory is not None else getattr(cls, "memory", None))
        self.enabled = enabled if enabled is not None else bool(getattr(cls, "enabled", True))
        self.endpoint = _coerce_endpoint(endpoint if endpoint is not None else getattr(cls, "endpoint", None))
        self.deploy_source = _validate_deploy_source(
            deploy_source if deploy_source is not None else getattr(cls, "deploy_source", None)
        )
        self.client = client
        self.id: str | None = None
        self.data: dict[str, Any] = {}
        self._function: Function | None = None

        if self.project is None:
            self.project = "default"
        if self.cloud_run_min_instances is not None and self.cloud_run_min_instances < 0:
            raise ValueError("min_instances must be greater than or equal to 0")
        if self.cloud_run_concurrency is not None and self.cloud_run_concurrency < 1:
            raise ValueError("concurrency must be greater than or equal to 1")

    @classmethod
    def from_name(cls, project: str, name: str, *, client: Client | None = None) -> ModelHandle:
        resolved_client = client or default_client()
        data = resolved_client.find_model(name, project=project)
        if data is None:
            raise RebaseWorkflowError(f"model not found: {project}/{name}")
        return _model_handle_for_data(data, client=resolved_client, project=project)

    @property
    def _client(self) -> Client:
        return self.client or default_client()

    def _init_emflow_base(self, resolved_name: str) -> None:
        mode = getattr(type(self), "_emflow_init_mode", "name")
        if mode == "skip":
            return
        try:
            if mode == "name":
                super().__init__(resolved_name)
            else:
                super().__init__()
        except TypeError:
            return

    def _deploy_operation_name(self) -> str:
        if self._operation_name is None:
            raise RebaseWorkflowError(self._not_deployable_message)
        return self._operation_name

    def as_function(self) -> Function:
        if self._function is None:
            operation_name = self._deploy_operation_name()
            operation = getattr(self, operation_name)
            inferred_defaults = _defaults_for(operation, target=f"{self.__class__.__name__}.{operation_name}")
            default_parameters = self.default_parameters or {}
            function = Function(
                project=str(self.project or "default"),
                name=str(self.name),
                description=self.description,
                default_parameters={**inferred_defaults, **default_parameters},
                mode=self.mode,
                isolation=self.isolation,
                enabled=self.enabled,
                deploy_source=self.deploy_source,
                client=self._client,
            )
            function.source_code = _source_for_model(self, operation_name=operation_name)
            function.entrypoint = operation_name
            function.image_spec = _model_image_spec_for(image=self.image, dependencies=self.dependencies)
            function.cloud_run_min_instances = self.cloud_run_min_instances
            function.cloud_run_concurrency = self.cloud_run_concurrency
            function.source_metadata = _git_metadata_for(operation)
            self._function = function
        return self._function

    def _publish_to_huggingface(
        self,
        *,
        version: dict[str, Any],
        config: HuggingFacePublishConfig,
    ) -> dict[str, Any]:
        source_git_commit_sha = version.get("git_commit_sha")
        record_source_git_commit_sha = (
            str(source_git_commit_sha)
            if config.sync_source_git and source_git_commit_sha and not version.get("git_dirty")
            else None
        )
        if config.artifact_path is not None and not config.artifact_path.exists():
            raise RebaseWorkflowError(f"Hugging Face artifact path does not exist: {config.artifact_path}")

        try:
            from huggingface_hub import HfApi
        except ImportError as exc:
            raise RebaseWorkflowError(
                "Install the Hugging Face extra before publishing: uv add 'rebase-toolkit[huggingface]' "
                "or pip install 'rebase-toolkit[huggingface]'."
            ) from exc

        api = HfApi(token=config.token)
        repo_type = _hf_repo_type_arg(config.repo_type)
        if config.create_repo:
            api.create_repo(
                repo_id=config.repo_id,
                repo_type=repo_type,
                private=config.private,
                exist_ok=True,
            )

        parent_commit = _hf_latest_commit(
            api,
            repo_id=config.repo_id,
            repo_type=config.repo_type,
            revision=config.revision,
        )
        commit_message = config.commit_message or f"Publish Rebase model {self.name} v{version.get('version_number')}"
        final_commit_info: Any

        if config.artifact_path is not None:
            if config.artifact_path.is_file():
                final_commit_info = api.upload_file(
                    path_or_fileobj=str(config.artifact_path),
                    path_in_repo=(config.path_in_repo or config.artifact_path.name),
                    repo_id=config.repo_id,
                    repo_type=repo_type,
                    revision=config.revision,
                    commit_message=commit_message,
                    parent_commit=parent_commit,
                )
            else:
                final_commit_info = api.upload_folder(
                    folder_path=str(config.artifact_path),
                    path_in_repo=config.path_in_repo,
                    repo_id=config.repo_id,
                    repo_type=repo_type,
                    revision=config.revision,
                    commit_message=commit_message,
                    parent_commit=parent_commit,
                    allow_patterns=config.allow_patterns,
                    ignore_patterns=config.ignore_patterns,
                    delete_patterns=config.delete_patterns,
                )
            parent_commit = _hf_commit_oid(final_commit_info) or parent_commit
            with tempfile.TemporaryDirectory() as temp_dir:
                provenance_dir = Path(temp_dir)
                _write_huggingface_provenance_files(
                    provenance_dir,
                    model_name=str(self.name),
                    model_data=self.data,
                    version=version,
                    include_source=config.include_source,
                    include_readme=False,
                )
                final_commit_info = api.upload_folder(
                    folder_path=str(provenance_dir),
                    repo_id=config.repo_id,
                    repo_type=repo_type,
                    revision=config.revision,
                    commit_message=f"Record Rebase provenance for {self.name} v{version.get('version_number')}",
                    parent_commit=parent_commit,
                )
        else:
            with tempfile.TemporaryDirectory() as temp_dir:
                folder = Path(temp_dir)
                _write_huggingface_provenance_files(
                    folder,
                    model_name=str(self.name),
                    model_data=self.data,
                    version=version,
                    include_source=config.include_source,
                    include_readme=True,
                )
                final_commit_info = api.upload_folder(
                    folder_path=str(folder),
                    path_in_repo=config.path_in_repo,
                    repo_id=config.repo_id,
                    repo_type=repo_type,
                    revision=config.revision,
                    commit_message=commit_message,
                    parent_commit=parent_commit,
                )

        provider_commit_sha = _hf_commit_oid(final_commit_info)
        if provider_commit_sha is None:
            raise RebaseWorkflowError("Hugging Face upload did not return a commit SHA")
        publication_metadata = {
            "hub_commit_url": _hf_commit_url(final_commit_info),
            "artifact_path": str(config.artifact_path) if config.artifact_path is not None else None,
            "source_included": config.include_source,
            "source_git_sync": record_source_git_commit_sha is not None,
            "provenance_file": "rebase_model.json",
        }
        return self._client.record_model_publication(
            str(self.id),
            model_version_id=str(version["id"]),
            repo_id=config.repo_id,
            repo_type=config.repo_type,
            visibility="private" if config.private else "public",
            path_in_repo=config.path_in_repo,
            revision=config.revision,
            provider_commit_sha=provider_commit_sha,
            source_git_commit_sha=record_source_git_commit_sha,
            publication_metadata=publication_metadata,
        )

    @_environment_scoped_deploy
    def deploy(
        self,
        *,
        replace: bool = False,
        environment: str | None = None,
        huggingface: HuggingFacePublishConfig | None = None,
        deploy_source: str | None = None,
    ) -> Model:
        environment = _resolve_environment(self._client, environment)
        function = self.as_function()
        if function.source_code is None or function.entrypoint is None:
            raise RebaseWorkflowError("cannot deploy a model without source_code and operation entrypoint")
        source_metadata = function._source_metadata_for_deploy(deploy_source)
        secrets_payload = _resolve_secrets_payload(self.secrets, self._client)
        model = self._client.find_model(str(self.name), project=str(self.project or "default"))
        if model is not None:
            model_data = self._client.update_model(
                model["id"],
                kind=_model_kind_for(self),
                operation_name=function.entrypoint,
                description=self.description,
                source_code=function.source_code,
                default_parameters=function.default_parameters,
                mode=function.mode,
                isolation=function.isolation,
                image_spec=function.image_spec,
                env=self.env,
                secrets=secrets_payload,
                cloud_run_min_instances=function.cloud_run_min_instances,
                cloud_run_concurrency=function.cloud_run_concurrency,
                cloud_run_cpu=self.cloud_run_cpu,
                cloud_run_memory=self.cloud_run_memory,
                enabled=self.enabled,
                environment=environment,
                endpoint=self.endpoint,
                **source_metadata,
            )
        else:
            model_data = self._client.register_model(
                project=str(self.project or "default"),
                name=str(self.name),
                kind=_model_kind_for(self),
                operation_name=function.entrypoint,
                description=self.description,
                source_code=function.source_code,
                default_parameters=function.default_parameters,
                mode=function.mode,
                isolation=function.isolation,
                image_spec=function.image_spec,
                env=self.env,
                secrets=secrets_payload,
                cloud_run_min_instances=function.cloud_run_min_instances,
                cloud_run_concurrency=function.cloud_run_concurrency,
                cloud_run_cpu=self.cloud_run_cpu,
                cloud_run_memory=self.cloud_run_memory,
                enabled=self.enabled,
                environment=environment,
                endpoint=self.endpoint,
                **source_metadata,
            )
        self.id = model_data["id"]
        self.data = model_data
        if huggingface is not None:
            version_id = model_data.get("current_version_id")
            if not version_id:
                raise RebaseWorkflowError("cannot publish model to Hugging Face without a current Rebase version")
            version = self._client.get_model_version(str(self.id), str(version_id))
            publication = self._publish_to_huggingface(version=version, config=huggingface)
            self.data["huggingface_publication"] = publication
        return self

    def spawn(self, *, environment: str | None = None, **parameters: Any) -> Run:
        environment = _resolve_environment(self._client, environment)
        if self.id is None:
            self.deploy(environment=environment)
        if self.id is None:
            raise RebaseWorkflowError("model has no ID after deployment")
        return self._client.run_model(self.id, parameters, environment=environment)

    def remote(self, *, environment: str | None = None, **parameters: Any) -> dict[str, Any]:
        return self.spawn(environment=environment, **parameters).result()

    def run(self, *, environment: str | None = None, **parameters: Any) -> Run:
        return self.spawn(environment=environment, **parameters)

    def ephemeral_run(self, **parameters: Any) -> Run:
        function = self.as_function()
        if function.source_code is None or function.entrypoint is None:
            raise RebaseWorkflowError("cannot run an ephemeral model without local source")
        return self._client.run_ephemeral(
            target_type="model",
            project=str(self.project or "default"),
            name=str(self.name),
            source_code=function.source_code,
            entrypoint=function.entrypoint,
            default_parameters=function.default_parameters,
            parameters=parameters,
            mode=function.mode,
            isolation=function.isolation,
            image_spec=function.image_spec,
            cloud_run_min_instances=function.cloud_run_min_instances,
            cloud_run_concurrency=function.cloud_run_concurrency,
            env=function.env,
            secrets=_resolve_secrets_payload(function.secrets, self._client),
        )


class Predictor(Model, _EmflowPredictor):
    _operation_name = "predict"
    _emflow_init_mode = "empty"

    @classmethod
    def from_name(cls, project: str, name: str, *, client: Client | None = None) -> PredictorHandle:
        resolved_client = client or default_client()
        data = resolved_client.find_model(name, project=project)
        if data is None:
            raise RebaseWorkflowError(f"model not found: {project}/{name}")
        return PredictorHandle(data, client=resolved_client, project=project)


class Optimizer(Model, _EmflowOptimizer):
    _operation_name = "optimize"
    _emflow_init_mode = "empty"

    @classmethod
    def from_name(cls, project: str, name: str, *, client: Client | None = None) -> OptimizerHandle:
        resolved_client = client or default_client()
        data = resolved_client.find_model(name, project=project)
        if data is None:
            raise RebaseWorkflowError(f"model not found: {project}/{name}")
        return OptimizerHandle(data, client=resolved_client, project=project)


class Agent(Model, _EmflowAgent):
    _operation_name = "act"
    _emflow_init_mode = "empty"

    @classmethod
    def from_name(cls, project: str, name: str, *, client: Client | None = None) -> AgentHandle:
        resolved_client = client or default_client()
        data = resolved_client.find_model(name, project=project)
        if data is None:
            raise RebaseWorkflowError(f"model not found: {project}/{name}")
        return AgentHandle(data, client=resolved_client, project=project)


class Simulator(Model, _EmflowSimulator):
    _emflow_init_mode = "skip"
    _not_deployable_message = SIMULATOR_NOT_DEPLOYABLE_ERROR


class Step(Function):
    def __init__(
        self,
        fn: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        project: str,
        description: str | None = None,
        default_parameters: dict[str, Any] | None = None,
        enabled: bool = True,
        deploy_source: str | None = None,
        project_source_mode: str | None = None,
        client: Client | None = None,
        function_id: str | None = None,
        data: dict[str, Any] | None = None,
        retries: int = 0,
        timeout_seconds: int | float | None = None,
        cache: bool = False,
    ) -> None:
        super().__init__(
            fn,
            name=name,
            project=project,
            description=description,
            default_parameters=default_parameters,
            # Steps execute in-flow inside their workflow's Prefect run; the
            # stored execution settings are never used for dispatch.
            mode=DEFAULT_MODE,
            isolation=DEFAULT_ISOLATION,
            dependencies=None,
            image=None,
            min_instances=None,
            concurrency=None,
            enabled=enabled,
            deploy_source=deploy_source,
            project_source_mode=project_source_mode,
            client=client,
            function_id=function_id,
            data=data,
        )
        if retries < 0:
            raise ValueError("retries must be greater than or equal to 0")
        self.retries = retries
        self.timeout_seconds = timeout_seconds
        self.cache = cache
        self.resources: dict[str, Any] = {}

    def deploy(self, **_kwargs: Any) -> Step:
        raise RebaseWorkflowError(
            "Steps are deployed automatically when their workflow is deployed. "
            "Use rb.deploy(workflow) instead of deploying steps directly."
        )

    def _deploy_for_workflow(
        self,
        *,
        image_spec: dict[str, Any] | None,
        cloud_run_min_instances: int | None,
        cloud_run_concurrency: int | None,
        resource_policy: dict[str, Any],
        replace: bool,
        deploy_source: str | None,
        environment: str,
        client: Client | None = None,
    ) -> None:
        self.image_spec = image_spec
        self.cloud_run_min_instances = cloud_run_min_instances
        self.cloud_run_concurrency = cloud_run_concurrency
        self.resources = resource_policy
        if client is not None:
            self.client = client
        Function.deploy(self, replace=replace, deploy_source=deploy_source, environment=environment)

    def _parameters_from_call(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
        if self.fn is None:
            if args:
                raise TypeError("Remote step handles can only be called with keyword arguments")
            return dict(kwargs)
        signature = inspect.signature(self.fn)
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        return dict(bound.arguments)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        trace = _current_trace()
        if trace is not None:
            return trace.record_step(self, args, kwargs)
        if self.fn is None:
            raise RebaseWorkflowError("remote step handles cannot be called locally; use .remote(...)")
        return self.fn(*args, **kwargs)

    def submit(self, *args: Any, **kwargs: Any) -> Run:
        return self.spawn(**self._parameters_from_call(args, kwargs))


class Workflow:
    def __init__(
        self,
        fn: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        project: str | None = None,
        description: str | None = None,
        schedule: Schedule | None = None,
        trigger: Trigger | None = None,
        default_parameters: dict[str, Any] | None = None,
        mode: ExecutionMode | None = None,
        isolation: Isolation | None = None,
        run_type: RunType | None = None,
        enabled: bool = True,
        endpoint: EndpointConfig | dict[str, Any] | None = None,
        deploy_source: str | None = None,
        project_source_mode: str | None = None,
        client: Client | None = None,
        workflow_id: str | None = None,
        data: dict[str, Any] | None = None,
        dependencies: list[str] | tuple[str, ...] | None = None,
        image: Image | dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
        secrets: dict[str, str] | list[Secret | str] | None = None,
        buckets: list[Bucket | str] | list[dict[str, Any]] | None = None,
        min_instances: int | None = None,
        concurrency: int | None = None,
        cpu: float | int | str | None = None,
        memory: int | float | str | None = None,
        resources: dict[str, Any] | None = None,
        backend: str | None = None,
    ) -> None:
        _reject_legacy_backend(backend)
        self.fn = fn
        self.project = project
        self.description = description
        self.enabled = enabled
        self.endpoint = _coerce_endpoint(endpoint) or _endpoint_for_callable(fn)
        self.client = client
        self.id: str | None = workflow_id
        self.data = data or {}
        self.deploy_source = _validate_deploy_source(deploy_source)
        self.project_source_mode = project_source_mode
        self.env = dict(env or (data.get("env") if data else None) or {})
        self.secrets = secrets if secrets is not None else dict((data.get("secrets") if data else None) or {})
        self.buckets = buckets if buckets is not None else list((data.get("buckets") if data else None) or [])
        self.name = name or (data["name"] if data else None)
        self.flow_ref: str | None = None
        self.source_code: str | None = None
        self.entrypoint: str | None = None
        self.step_graph: dict[str, Any] | None = data.get("step_graph") if data else None
        self.schedule = (
            _schedule_payload(schedule) if schedule is not None else (data.get("schedule") if data else None)
        )
        self.trigger = _trigger_payload(trigger) if trigger is not None else (data.get("trigger") if data else None)
        self.default_parameters = default_parameters or {}
        data_mode = data.get("mode") if data else None
        data_isolation = data.get("isolation") if data else None
        data_run_type = data.get("run_type") if data and data_mode is None and data_isolation is None else None
        self.mode, self.isolation = _validate_execution(
            data_mode if data else mode,
            data_isolation if data else isolation,
            target_type="workflow",
            run_type=data_run_type if data else run_type,
        )
        self.run_type: RunType = _legacy_run_type(self.mode, self.isolation, target_type="workflow")
        self.required_parameters: list[str] = list(data.get("required_parameters", [])) if data else []
        self.source_metadata: dict[str, Any] = {}
        self.image_spec: dict[str, Any] | None = None
        self.image: Image | None = image if isinstance(image, Image) else None
        self.cloud_run_min_instances: int | None = None
        self.cloud_run_concurrency: int | None = None
        # The workflow's OWN container, distinct from `resource_policy` below,
        # which annotates its steps. Normalized eagerly, as Function does, so
        # "2Gi" and a bare MiB int both become the Cloud Run spelling.
        self.cloud_run_cpu: str | None = _cloud_run_cpu_value(cpu) or (data.get("cloud_run_cpu") if data else None)
        self.cloud_run_memory: str | None = _cloud_run_memory_value(memory) or (
            data.get("cloud_run_memory") if data else None
        )
        self.resource_policy: dict[str, Any] = {}

        if fn is not None:
            if not isinstance(fn, FunctionType):
                raise TypeError("Workflow requires a plain Python function")
            self.name = _target_name(fn, name)
            inferred_defaults = _defaults_for(fn, target="Workflow", reserved=_RESERVED_WORKFLOW_PARAMETERS)
            self.required_parameters = _required_parameters_for(
                fn, target="Workflow", reserved=_RESERVED_WORKFLOW_PARAMETERS
            )
            self.default_parameters = {**inferred_defaults, **(default_parameters or {})}
            self.mode, self.isolation = _validate_execution(
                mode,
                isolation,
                target_type="workflow",
                run_type=run_type,
            )
            self.run_type = _legacy_run_type(self.mode, self.isolation, target_type="workflow")
            self.source_code = _source_for(fn, target="workflow")
            self.entrypoint = fn.__name__
            self.source_metadata = _git_metadata_for(fn)
            self.image_spec = _image_spec_for(image=image, dependencies=dependencies)
            if min_instances is not None and min_instances < 0:
                raise ValueError("min_instances must be greater than or equal to 0")
            if concurrency is not None and concurrency < 1:
                raise ValueError("concurrency must be greater than or equal to 1")
            self.cloud_run_min_instances = min_instances
            self.cloud_run_concurrency = concurrency
            self.resource_policy = resources or {}
        if self.name is None:
            raise ValueError("workflow name is required")

    @classmethod
    def from_name(cls, project: str, name: str, *, client: Client | None = None) -> Workflow:
        resolved_client = client or default_client()
        data = resolved_client.find_workflow(name, project=project)
        if data is None:
            raise RebaseWorkflowError(f"workflow not found: {project}/{name}")
        return cls(project=project, name=name, client=resolved_client, workflow_id=data["id"], data=data)

    @property
    def _client(self) -> Client:
        return self.client or default_client()

    def _source_metadata_for_deploy(self, deploy_source: str | None = None) -> dict[str, Any]:
        return _target_source_metadata_for_deploy(self, deploy_source)

    def _collect_steps(self) -> list[Step]:
        if self.fn is None:
            return []
        try:
            closure = inspect.getclosurevars(self.fn)
        except TypeError:
            return []
        referenced = {**closure.globals, **closure.nonlocals}
        ordered: list[Step] = []
        seen: set[int] = set()
        # Closure mappings are not a source-order contract (and Python 3.14
        # changed their observed order). Bytecode preserves first use, which is
        # the deploy order users see in a straight-line workflow declaration.
        for instruction in dis.get_instructions(self.fn):
            value = referenced.get(str(instruction.argval))
            if isinstance(value, Step) and id(value) not in seen:
                seen.add(id(value))
                ordered.append(value)
        for value in referenced.values():
            if isinstance(value, Step) and id(value) not in seen:
                seen.add(id(value))
                ordered.append(value)
        return ordered

    def _references_step(self) -> bool:
        return bool(self._collect_steps())

    def _trace_arguments(self) -> dict[str, _WorkflowParameter]:
        if self.fn is None:
            return {}
        signature = inspect.signature(self.fn)
        parameters: dict[str, _WorkflowParameter] = {}
        for name, parameter in signature.parameters.items():
            if parameter.kind in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            }:
                raise TypeError("Workflow step graphs can only use positional-or-keyword and keyword-only parameters")
            parameters[name] = _WorkflowParameter(name)
        return parameters

    def _build_step_graph(self, *, ephemeral: bool = False) -> dict[str, Any] | None:
        if self.fn is None:
            return None
        if inspect.iscoroutinefunction(self.fn):
            if self._references_step():
                raise RebaseWorkflowError("Step workflows must be synchronous in the SDK graph compiler.")
            return None
        # Tracing works by CALLING the body and intercepting its step calls, so a
        # body that references no steps has nothing to intercept and would simply
        # run — which for a job-mode workflow means executing the user's real
        # pipeline on their laptop at `rebase deploy` time. That is not
        # hypothetical: deploying the accounting sync once ran a live financial
        # sync locally. A step-free body could never produce trace nodes anyway
        # (the no-nodes path below already returns None), so skipping the call is
        # behaviour-preserving minus the side effects.
        if not self._references_step():
            return None
        trace = _WorkflowTrace(ephemeral=ephemeral)
        _trace_stack.append(trace)
        try:
            result = self.fn(**self._trace_arguments())
        except Exception as exc:
            if trace.nodes or self._references_step():
                raise RebaseWorkflowError(
                    "Could not compile a static step graph for this workflow. Rebase step workflows support "
                    "direct step calls and literal containers at deployment time; put runtime branching inside "
                    "a step function."
                ) from exc
            return None
        finally:
            _trace_stack.pop()

        if not trace.nodes:
            return None
        return {
            "schema_version": 1,
            "engine": "prefect",
            "nodes": trace.nodes,
            "return_binding": _binding_for(result),
        }

    def _validate_schedule_defaults(self) -> None:
        if self.schedule is not None and self.required_parameters:
            missing = ", ".join(self.required_parameters)
            raise RebaseWorkflowError(
                f"Scheduled workflows require defaults for every workflow parameter. Missing defaults: {missing}"
            )
        if self.trigger is not None and self.required_parameters:
            missing = ", ".join(self.required_parameters)
            raise RebaseWorkflowError(
                f"Triggered workflows require defaults for every workflow parameter. Missing defaults: {missing}"
            )

    @_environment_scoped_deploy
    def deploy(
        self,
        *,
        replace: bool = False,
        deploy_source: str | None = None,
        environment: str | None = None,
        _skip_dataset_preflight: bool = False,
    ) -> Workflow:
        environment = _resolve_environment(self._client, environment)
        if self.source_code is None or self.entrypoint is None:
            raise RebaseWorkflowError("cannot deploy a workflow handle without source_code and entrypoint")
        if self.name is None:
            raise RebaseWorkflowError("workflow name is required")
        self._validate_schedule_defaults()
        build = None
        if self.fn and self.image and self.image.build_enabled:
            if self.mode != "job":
                raise RebaseWorkflowError(
                    "Workflows using uv_sync or add_local_* require mode='job'; "
                    "interactive custom-image workflows are not supported yet."
                )
            build = self._client.prepare_built_image(self.fn, self.image)
        if not _skip_dataset_preflight:
            # Project.deploy runs the preflight once for all targets.
            preflight_datasets(self.client)
        name = self.name
        for step in self._collect_steps():
            step._deploy_for_workflow(
                image_spec=self.image_spec,
                cloud_run_min_instances=self.cloud_run_min_instances,
                cloud_run_concurrency=self.cloud_run_concurrency,
                resource_policy=self.resource_policy,
                replace=replace,
                deploy_source=deploy_source,
                environment=environment,
                client=self.client,
            )
        step_graph = self._build_step_graph()
        source_metadata = self._source_metadata_for_deploy(deploy_source)
        secrets_payload = _resolve_secrets_payload(self.secrets, self._client)
        buckets_payload = _resolve_buckets_payload(self.buckets, self._client)
        existing = self._client.find_workflow(name, project=self.project)
        if existing is not None:
            workflow = self._client.update_workflow(
                existing["id"],
                description=self.description,
                flow_ref=self.flow_ref,
                source_code=self.source_code,
                entrypoint=self.entrypoint,
                step_graph=step_graph,
                schedule=self.schedule,
                trigger=self.trigger,
                default_parameters=self.default_parameters,
                required_parameters=self.required_parameters,
                mode=self.mode,
                isolation=self.isolation,
                env=self.env,
                secrets=secrets_payload,
                buckets=buckets_payload,
                cloud_run_cpu=self.cloud_run_cpu,
                cloud_run_memory=self.cloud_run_memory,
                enabled=self.enabled,
                endpoint=self.endpoint,
                build=build,
                environment=environment,
                **source_metadata,
            )
            self.id = workflow["id"]
            self.data = workflow
            self.step_graph = step_graph
            return self

        workflow = self._client.register_workflow(
            project=self.project,
            name=name,
            description=self.description,
            flow_ref=self.flow_ref,
            source_code=self.source_code,
            entrypoint=self.entrypoint,
            step_graph=step_graph,
            schedule=self.schedule,
            trigger=self.trigger,
            default_parameters=self.default_parameters,
            required_parameters=self.required_parameters,
            mode=self.mode,
            isolation=self.isolation,
            env=self.env,
            secrets=secrets_payload,
            buckets=buckets_payload,
            cloud_run_cpu=self.cloud_run_cpu,
            cloud_run_memory=self.cloud_run_memory,
            enabled=self.enabled,
            endpoint=self.endpoint,
            build=build,
            environment=environment,
            **source_metadata,
        )
        self.id = workflow["id"]
        self.data = workflow
        self.step_graph = step_graph
        return self

    def spawn(self, **parameters: Any) -> Run:
        if self.id is None:
            self.deploy()
        if self.id is None:
            raise RebaseWorkflowError("workflow has no ID after deployment")
        return self._client.run_workflow(self.id, parameters)

    def remote(self, **parameters: Any) -> dict[str, Any]:
        return self.spawn(**parameters).result()

    def run(self, **parameters: Any) -> Run:
        return self.spawn(**parameters)

    def ephemeral_run(self, **parameters: Any) -> Run:
        if self.source_code is None or self.entrypoint is None:
            raise RebaseWorkflowError("cannot run an ephemeral workflow without local source")
        return self._client.run_ephemeral(
            target_type="workflow",
            project=self.project or "default",
            name=str(self.name),
            source_code=self.source_code,
            entrypoint=self.entrypoint,
            default_parameters=self.default_parameters,
            parameters=parameters,
            mode=self.mode,
            isolation=self.isolation,
            step_graph=self._build_step_graph(ephemeral=True),
            required_parameters=self.required_parameters,
            # image_spec too: without it an ephemeral workflow runs on the default
            # image and any uv_pip_install() the author declared is silently dropped.
            image_spec=self.image_spec,
            env=self.env,
            secrets=_resolve_secrets_payload(self.secrets, self._client),
        )

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self.fn is None:
            raise RebaseWorkflowError("remote workflow handles cannot be called locally; use .remote(...)")
        return self.fn(*args, **kwargs)


class Run:
    def __init__(self, run_id: str, *, client: Client | None = None, data: dict[str, Any] | None = None) -> None:
        self.id = run_id
        self.client = client or default_client()
        self.data = data or {}

    def refresh(self) -> dict[str, Any]:
        self.data = self.client.get_run(self.id)
        return self.data

    def steps(self) -> list[dict[str, Any]]:
        return self.client.list_run_steps(self.id)

    def tasks(self) -> list[dict[str, Any]]:
        return self.client.list_run_tasks(self.id)

    def artifacts(self) -> list[dict[str, Any]]:
        return self.client.list_run_artifacts(self.id)

    def events(self) -> list[dict[str, Any]]:
        return self.client.list_run_events(self.id)

    def logs(self, *, since: str | None = None, limit: int | None = None) -> dict[str, Any]:
        return self.client.get_run_logs(self.id, since=since, limit=limit)

    def cancel(self) -> dict[str, Any]:
        self.data = self.client.cancel_run(self.id)
        return self.data

    def replay(self, *, version: str | None = None, parameters: dict[str, Any] | None = None) -> Run:
        """Replay this run; see :meth:`Client.replay_run` for the ``version`` semantics."""
        return self.client.replay_run(self.id, version=version, parameters=parameters)

    @property
    def status(self) -> str:
        if not self.data:
            self.refresh()
        return str(self.data["status"])

    def result(self, *, timeout: int = 600, poll_interval: float = 5.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        terminal_statuses = {"succeeded", "failed", "cancelled"}
        # The ephemeral submit response is already terminal on current servers, so
        # the data in hand usually answers without a single GET. Non-terminal data
        # (older servers, async run types) falls through to the poll loop.
        first_iteration = True

        while True:
            if first_iteration and self.data and self.data.get("status") in terminal_statuses:
                data = self.data
            else:
                data = self.refresh()
            first_iteration = False
            status = data["status"]
            if status in terminal_statuses:
                if status == "succeeded":
                    return data["result"]
                raise RebaseWorkflowError(run_failure_summary(data) or f"run ended with status {status}")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"run {self.id} did not finish within {timeout} seconds")
            time.sleep(poll_interval)
