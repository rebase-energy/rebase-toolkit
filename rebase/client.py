from __future__ import annotations

import ast
import inspect
import json
import re
import subprocess
import textwrap
import time
import warnings
from collections.abc import Callable
from pathlib import Path
from types import FunctionType
from typing import Any, Self

import requests

from rebase.config import DEFAULT_SERVER_URL, load_profile

_default_client: Client | None = None
_trace_stack: list[_WorkflowTrace] = []
_UNSET = object()


class RebaseWorkflowError(RuntimeError):
    pass


RebaseError = RebaseWorkflowError


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


Schedule = Cron | dict[str, Any]
FunctionBackend = str
DEFAULT_FUNCTION_BACKEND: FunctionBackend = "cloud_run"
WorkflowBackend = str
DEFAULT_WORKFLOW_BACKEND: WorkflowBackend = "prefect_cloud_run_service"
DEFAULT_PYTHON_VERSION = "3.13"


def _validate_function_backend(backend: FunctionBackend) -> FunctionBackend:
    if backend not in {"modal", "prefect", "prefect_cloud", "cloud_run", "cloud_run_shared"}:
        raise ValueError(
            "function backend must be 'modal', 'prefect', 'prefect_cloud', 'cloud_run', or 'cloud_run_shared'"
        )
    return backend


def _validate_workflow_backend(backend: WorkflowBackend) -> WorkflowBackend:
    if backend not in {"prefect", "prefect_cloud_run_jobs", "prefect_cloud_run_service"}:
        raise ValueError(
            "workflow backend must be 'prefect', 'prefect_cloud_run_jobs', or 'prefect_cloud_run_service'"
        )
    return backend


def _warn_unpinned_dependencies(packages: list[str]) -> None:
    unpinned = [package for package in packages if "==" not in package]
    if unpinned:
        warnings.warn(
            "Unpinned Rebase function dependencies are allowed but make function versions less reproducible. "
            f"Prefer exact pins for: {', '.join(unpinned)}",
            stacklevel=3,
        )


class Image:
    def __init__(
        self,
        *,
        kind: str = "python",
        python_version: str = DEFAULT_PYTHON_VERSION,
        uv_pip_packages: list[str] | None = None,
        uv_version: str | None = None,
    ) -> None:
        if kind != "python":
            raise ValueError("only python images are supported")
        self.kind = kind
        self.python_version = python_version
        self.uv_pip_packages = list(uv_pip_packages or [])
        self.uv_version = uv_version

    @classmethod
    def python(cls, version: str = DEFAULT_PYTHON_VERSION) -> Image:
        return cls(python_version=version)

    def uv_pip_install(self, *packages: str, uv_version: str | None = None) -> Image:
        self.uv_pip_packages.extend(packages)
        if uv_version is not None:
            self.uv_version = uv_version
        return self

    def to_dict(self) -> dict[str, Any]:
        packages = [package.strip() for package in self.uv_pip_packages if package.strip()]
        _warn_unpinned_dependencies(packages)
        return {
            "kind": self.kind,
            "python_version": self.python_version,
            "uv_pip_packages": packages,
            "uv_version": self.uv_version,
        }


def _image_spec_for(
    *,
    image: Image | dict[str, Any] | None = None,
    dependencies: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any] | None:
    if image is not None and dependencies:
        raise ValueError("provide either image or dependencies, not both")
    if isinstance(image, Image):
        return image.to_dict()
    if isinstance(image, dict):
        return image
    if dependencies is not None:
        return Image.python().uv_pip_install(*dependencies).to_dict()
    return Image.python().to_dict()


def configure(*, api_key: str | None = None, api_url: str | None = None, profile: str | None = None) -> None:
    global _default_client
    _default_client = Client(api_key=api_key, api_url=api_url, profile=profile)


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


def _defaults_for(fn: Callable[..., Any], *, target: str) -> dict[str, Any]:
    signature = inspect.signature(fn)
    defaults: dict[str, Any] = {}
    for name, parameter in signature.parameters.items():
        if parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
            inspect.Parameter.POSITIONAL_ONLY,
        }:
            raise TypeError(f"{target} can only use positional-or-keyword and keyword-only parameters")
        if parameter.default is inspect.Parameter.empty:
            continue
        defaults[name] = parameter.default
    return defaults


def _required_parameters_for(fn: Callable[..., Any], *, target: str) -> list[str]:
    signature = inspect.signature(fn)
    required: list[str] = []
    for name, parameter in signature.parameters.items():
        if parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
            inspect.Parameter.POSITIONAL_ONLY,
        }:
            raise TypeError(f"{target} can only use positional-or-keyword and keyword-only parameters")
        if parameter.default is inspect.Parameter.empty:
            required.append(name)
    return required


def _target_name(fn: FunctionType, name: str | None) -> str:
    return name or fn.__name__.replace("_", "-")


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
    def __init__(self, *, ephemeral: bool = False) -> None:
        self.ephemeral = ephemeral
        self.nodes: list[dict[str, Any]] = []
        self._node_keys: set[str] = set()

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
                    "execution_backend": step.execution_backend,
                    "image_spec": step.image_spec,
                }
            )
        self.nodes.append(node)
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


def _git_metadata_for(fn: Callable[..., Any]) -> dict[str, Any]:
    source_file = inspect.getsourcefile(fn)
    if source_file is None:
        return {}

    source_path = Path(source_file).resolve()
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
    dirty = _git(["status", "--porcelain"], cwd=root_path) is not None

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


class Client:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_url: str | None = None,
        profile: str | None = None,
    ) -> None:
        profile_data = load_profile(profile)
        configured_api_key = profile_data.get("api_key")
        configured_api_url = profile_data.get("api_url")
        self.api_key = api_key or (configured_api_key if isinstance(configured_api_key, str) else None)
        profile_api_url = configured_api_url if isinstance(configured_api_url, str) else None
        self.api_url = (api_url or profile_api_url or DEFAULT_SERVER_URL).rstrip("/")

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any] | list[dict[str, Any]]:
        headers = dict(kwargs.pop("headers", {}))
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        response = requests.request(method, f"{self.api_url}{path}", headers=headers, timeout=30, **kwargs)
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise RebaseWorkflowError(response.text) from exc
        return response.json()

    def list_projects(self) -> list[dict[str, Any]]:
        response = self.request("GET", "/projects")
        if not isinstance(response, list):
            raise RebaseWorkflowError("expected project list response")
        return response

    def get_workspace(self) -> dict[str, Any]:
        response = self.request("GET", "/workspace")
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected workspace response")
        return response

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
        response = self.request("PATCH", "/workspace", json=payload)
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected workspace response")
        return response

    def create_project(
        self,
        *,
        name: str,
        description: str | None = None,
        source_mode: str | None = None,
        repo_owner: str | None = None,
        repo_name: str | None = None,
        repo_path: str | None = None,
    ) -> dict[str, Any]:
        response = self.request(
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
        )
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected project response")
        return response

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
        response = self.request("PATCH", f"/projects/{project_id}", json=payload)
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected project response")
        return response

    def find_project(self, name: str) -> dict[str, Any] | None:
        for project in self.list_projects():
            if project["name"] == name:
                return project
        return None

    def ensure_project(
        self,
        name: str,
        *,
        description: str | None = None,
        source_mode: str | None = None,
        repo_owner: str | None = None,
        repo_name: str | None = None,
        repo_path: str | None = None,
    ) -> dict[str, Any]:
        existing = self.find_project(name)
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
        )

    def list_functions(self, *, project: str | None = None, project_id: str | None = None) -> list[dict[str, Any]]:
        resolved_project_id = project_id
        if resolved_project_id is None and project is not None:
            resolved_project_id = self.ensure_project(project)["id"]
        if resolved_project_id is None:
            projects = self.list_projects()
            functions: list[dict[str, Any]] = []
            for item in projects:
                functions.extend(self.list_functions(project_id=item["id"]))
            return functions
        response = self.request("GET", f"/projects/{resolved_project_id}/functions")
        if not isinstance(response, list):
            raise RebaseWorkflowError("expected function list response")
        return response

    def get_function(self, function_id: str) -> dict[str, Any]:
        response = self.request("GET", f"/functions/{function_id}")
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected function response")
        return response

    def find_function(self, name: str, *, project: str) -> dict[str, Any] | None:
        for function in self.list_functions(project=project):
            if function["name"] == name:
                return function
        return None

    def register_function(
        self,
        *,
        project: str,
        name: str,
        source_code: str,
        entrypoint: str,
        description: str | None = None,
        default_parameters: dict[str, Any] | None = None,
        execution_backend: FunctionBackend = DEFAULT_FUNCTION_BACKEND,
        image_spec: dict[str, Any] | None = None,
        cloud_run_min_instances: int | None = None,
        cloud_run_concurrency: int | None = None,
        enabled: bool = True,
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
        project_id = self.ensure_project(project)["id"]
        response = self.request(
            "POST",
            f"/projects/{project_id}/functions",
            json={
                "name": name,
                "description": description,
                "source_code": source_code,
                "entrypoint": entrypoint,
                "default_parameters": default_parameters or {},
                "execution_backend": execution_backend,
                "image_spec": image_spec,
                "cloud_run_min_instances": cloud_run_min_instances,
                "cloud_run_concurrency": cloud_run_concurrency,
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
            },
        )
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected function response")
        return response

    def update_function(
        self,
        function_id: str,
        *,
        name: str | None = None,
        source_code: str | None = None,
        entrypoint: str | None = None,
        description: str | None = None,
        default_parameters: dict[str, Any] | None = None,
        execution_backend: FunctionBackend | None = None,
        image_spec: dict[str, Any] | None = None,
        cloud_run_min_instances: int | None = None,
        cloud_run_concurrency: int | None = None,
        enabled: bool | None = None,
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
        payload = {
            key: value
            for key, value in {
                "name": name,
                "description": description,
                "source_code": source_code,
                "entrypoint": entrypoint,
                "default_parameters": default_parameters,
                "execution_backend": execution_backend,
                "image_spec": image_spec,
                "cloud_run_min_instances": cloud_run_min_instances,
                "cloud_run_concurrency": cloud_run_concurrency,
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
        response = self.request("PATCH", f"/functions/{function_id}", json=payload)
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected function response")
        return response

    def run_function(self, function_id: str, parameters: dict[str, Any] | None = None) -> Run:
        response = self.request("POST", f"/functions/{function_id}/runs", json={"parameters": parameters or {}})
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected run response")
        return Run(response["id"], client=self, data=response)

    def list_function_versions(self, function_id: str) -> list[dict[str, Any]]:
        response = self.request("GET", f"/functions/{function_id}/versions")
        if not isinstance(response, list):
            raise RebaseWorkflowError("expected function version list response")
        return response

    def get_function_version(self, function_id: str, version_id: str) -> dict[str, Any]:
        response = self.request("GET", f"/functions/{function_id}/versions/{version_id}")
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected function version response")
        return response

    def list_workflows(self, *, project: str | None = None, project_id: str | None = None) -> list[dict[str, Any]]:
        resolved_project_id = project_id
        if resolved_project_id is None and project is not None:
            resolved_project_id = self.ensure_project(project)["id"]
        path = f"/projects/{resolved_project_id}/workflows" if resolved_project_id is not None else "/workflows"
        response = self.request("GET", path)
        if not isinstance(response, list):
            raise RebaseWorkflowError("expected workflow list response")
        return response

    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        response = self.request("GET", f"/workflows/{workflow_id}")
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected workflow response")
        return response

    def find_workflow(self, name: str, *, project: str | None = None) -> dict[str, Any] | None:
        for workflow in self.list_workflows(project=project) if project is not None else self.list_workflows():
            if workflow["name"] == name:
                return workflow
        return None

    def register_workflow(
        self,
        *,
        name: str,
        flow_ref: str | None = None,
        source_code: str | None = None,
        entrypoint: str | None = None,
        step_graph: dict[str, Any] | None = None,
        schedule: dict[str, Any] | None = None,
        description: str | None = None,
        default_parameters: dict[str, Any] | None = None,
        required_parameters: list[str] | None = None,
        execution_backend: WorkflowBackend = DEFAULT_WORKFLOW_BACKEND,
        enabled: bool = True,
        project: str | None = None,
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
        path = "/workflows"
        if project is not None:
            project_id = self.ensure_project(project)["id"]
            path = f"/projects/{project_id}/workflows"
        response = self.request(
            "POST",
            path,
            json={
                "name": name,
                "description": description,
                "flow_ref": flow_ref,
                "source_code": source_code,
                "entrypoint": entrypoint,
                "step_graph": step_graph,
                "schedule": schedule,
                "default_parameters": default_parameters or {},
                "required_parameters": required_parameters or [],
                "execution_backend": execution_backend,
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
            },
        )
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected workflow response")
        return response

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
        description: str | None = None,
        default_parameters: dict[str, Any] | None = None,
        required_parameters: list[str] | None = None,
        execution_backend: WorkflowBackend | None = None,
        enabled: bool | None = None,
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
                "execution_backend": execution_backend,
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
        if step_graph is not _UNSET:
            payload["step_graph"] = step_graph
        if schedule is not _UNSET:
            payload["schedule"] = schedule
        response = self.request("PATCH", f"/workflows/{workflow_id}", json=payload)
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected workflow response")
        return response

    def run_workflow(self, workflow_id: str, parameters: dict[str, Any] | None = None) -> Run:
        response = self.request("POST", f"/workflows/{workflow_id}/runs", json={"parameters": parameters or {}})
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected run response")
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
        execution_backend: str,
        image_spec: dict[str, Any] | None = None,
        step_graph: dict[str, Any] | None = None,
        required_parameters: list[str] | None = None,
        cloud_run_min_instances: int | None = None,
        cloud_run_concurrency: int | None = None,
    ) -> Run:
        response = self.request(
            "POST",
            "/runs/ephemeral",
            json={
                "target_type": target_type,
                "project": project,
                "name": name,
                "source_code": source_code,
                "entrypoint": entrypoint,
                "default_parameters": default_parameters or {},
                "parameters": parameters or {},
                "execution_backend": execution_backend,
                "image_spec": image_spec,
                "step_graph": step_graph,
                "required_parameters": required_parameters or [],
                "cloud_run_min_instances": cloud_run_min_instances,
                "cloud_run_concurrency": cloud_run_concurrency,
            },
        )
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected run response")
        return Run(response["id"], client=self, data=response)

    def list_workflow_versions(self, workflow_id: str) -> list[dict[str, Any]]:
        response = self.request("GET", f"/workflows/{workflow_id}/versions")
        if not isinstance(response, list):
            raise RebaseWorkflowError("expected workflow version list response")
        return response

    def get_workflow_version(self, workflow_id: str, version_id: str) -> dict[str, Any]:
        response = self.request("GET", f"/workflows/{workflow_id}/versions/{version_id}")
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected workflow version response")
        return response

    def get_run(self, run_id: str) -> dict[str, Any]:
        response = self.request("GET", f"/runs/{run_id}")
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected run response")
        return response

    def list_run_steps(self, run_id: str) -> list[dict[str, Any]]:
        response = self.request("GET", f"/runs/{run_id}/steps")
        if not isinstance(response, list):
            raise RebaseWorkflowError("expected step run list response")
        return response


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
        client: Client | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.source_mode = source_mode
        self.repo_owner = repo_owner
        self.repo_name = repo_name
        self.repo_path = repo_path
        self.client = client
        self.id: str | None = None
        self._functions: list[Function] = []
        self._steps: list[Step] = []
        self._workflows: list[Workflow] = []

    @property
    def _client(self) -> Client:
        return self.client or default_client()

    def deploy(self, *, replace: bool = False) -> Self:
        for workflow in self._workflows:
            workflow._validate_schedule_defaults()
        project = self._client.ensure_project(
            self.name,
            description=self.description,
            source_mode=self.source_mode,
            repo_owner=self.repo_owner,
            repo_name=self.repo_name,
            repo_path=self.repo_path,
        )
        self.id = project["id"]
        for function in self._functions:
            function.deploy(replace=replace)
        for workflow in self._workflows:
            workflow.deploy(replace=replace)
        return self

    def function(
        self,
        *,
        name: str | None = None,
        description: str | None = None,
        default_parameters: dict[str, Any] | None = None,
        backend: FunctionBackend = DEFAULT_FUNCTION_BACKEND,
        dependencies: list[str] | tuple[str, ...] | None = None,
        image: Image | dict[str, Any] | None = None,
        min_instances: int | None = None,
        concurrency: int | None = None,
        enabled: bool = True,
    ) -> Callable[[Callable[..., Any]], Function]:
        def decorator(fn: Callable[..., Any]) -> Function:
            function = Function(
                fn,
                name=name,
                project=self.name,
                description=description,
                default_parameters=default_parameters,
                backend=backend,
                dependencies=dependencies,
                image=image,
                min_instances=min_instances,
                concurrency=concurrency,
                enabled=enabled,
                client=self._client,
            )
            self._functions.append(function)
            return function

        return decorator

    def step(
        self,
        *,
        name: str | None = None,
        description: str | None = None,
        default_parameters: dict[str, Any] | None = None,
        backend: FunctionBackend = "prefect",
        dependencies: list[str] | tuple[str, ...] | None = None,
        image: Image | dict[str, Any] | None = None,
        min_instances: int | None = None,
        concurrency: int | None = None,
        enabled: bool = True,
        retries: int = 0,
        timeout_seconds: int | float | None = None,
        cache: bool = False,
        resources: dict[str, Any] | None = None,
    ) -> Callable[[Callable[..., Any]], Step]:
        def decorator(fn: Callable[..., Any]) -> Step:
            step = Step(
                fn,
                name=name,
                project=self.name,
                description=description,
                default_parameters=default_parameters,
                backend=backend,
                dependencies=dependencies,
                image=image,
                min_instances=min_instances,
                concurrency=concurrency,
                enabled=enabled,
                client=self._client,
                retries=retries,
                timeout_seconds=timeout_seconds,
                cache=cache,
                resources=resources,
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
        default_parameters: dict[str, Any] | None = None,
        backend: WorkflowBackend = DEFAULT_WORKFLOW_BACKEND,
        enabled: bool = True,
    ) -> Callable[[Callable[..., Any]], Workflow]:
        def decorator(fn: Callable[..., Any]) -> Workflow:
            workflow = Workflow(
                fn,
                name=name,
                project=self.name,
                description=description,
                schedule=schedule,
                default_parameters=default_parameters,
                backend=backend,
                enabled=enabled,
                client=self._client,
            )
            self._workflows.append(workflow)
            return workflow

        return decorator


class Function:
    def __init__(
        self,
        fn: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        project: str,
        description: str | None = None,
        default_parameters: dict[str, Any] | None = None,
        backend: FunctionBackend = DEFAULT_FUNCTION_BACKEND,
        dependencies: list[str] | tuple[str, ...] | None = None,
        image: Image | dict[str, Any] | None = None,
        min_instances: int | None = None,
        concurrency: int | None = None,
        enabled: bool = True,
        client: Client | None = None,
        function_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.fn = fn
        self.project = project
        self.description = description
        self.enabled = enabled
        self.client = client
        self.id: str | None = function_id
        self.data = data or {}
        self.name = name or (data["name"] if data else None)
        self.source_code: str | None = None
        self.entrypoint: str | None = None
        self.default_parameters = default_parameters or {}
        self.execution_backend: FunctionBackend = (
            data.get("execution_backend", DEFAULT_FUNCTION_BACKEND) if data else DEFAULT_FUNCTION_BACKEND
        )
        self.image_spec: dict[str, Any] | None = data.get("image_spec") if data else None
        self.image_fingerprint: str | None = data.get("image_fingerprint") if data else None
        self.cloud_run_min_instances: int | None = data.get("cloud_run_min_instances") if data else None
        self.cloud_run_concurrency: int | None = data.get("cloud_run_concurrency") if data else None
        self.source_metadata: dict[str, Any] = {}

        if fn is not None:
            if not isinstance(fn, FunctionType):
                raise TypeError("Function requires a plain Python function")
            self.name = _target_name(fn, name)
            inferred_defaults = _defaults_for(fn, target="Function")
            self.default_parameters = {**inferred_defaults, **(default_parameters or {})}
            self.execution_backend = _validate_function_backend(backend)
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

    def deploy(self, *, replace: bool = False) -> Function:
        if self.source_code is None or self.entrypoint is None:
            raise RebaseWorkflowError("cannot deploy a function handle without source_code and entrypoint")
        if self.name is None:
            raise RebaseWorkflowError("function name is required")
        name = self.name
        existing = self._client.find_function(name, project=self.project)
        if existing is not None:
            function = self._client.update_function(
                existing["id"],
                description=self.description,
                source_code=self.source_code,
                entrypoint=self.entrypoint,
                default_parameters=self.default_parameters,
                execution_backend=self.execution_backend,
                image_spec=self.image_spec,
                cloud_run_min_instances=self.cloud_run_min_instances,
                cloud_run_concurrency=self.cloud_run_concurrency,
                enabled=self.enabled,
                **self.source_metadata,
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
            execution_backend=self.execution_backend,
            image_spec=self.image_spec,
            cloud_run_min_instances=self.cloud_run_min_instances,
            cloud_run_concurrency=self.cloud_run_concurrency,
            enabled=self.enabled,
            **self.source_metadata,
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
            execution_backend=self.execution_backend,
            image_spec=self.image_spec,
            cloud_run_min_instances=self.cloud_run_min_instances,
            cloud_run_concurrency=self.cloud_run_concurrency,
        )

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self.fn is None:
            raise RebaseWorkflowError("remote function handles cannot be called locally; use .remote(...)")
        return self.fn(*args, **kwargs)


class Step(Function):
    def __init__(
        self,
        fn: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        project: str,
        description: str | None = None,
        default_parameters: dict[str, Any] | None = None,
        backend: FunctionBackend = "prefect",
        dependencies: list[str] | tuple[str, ...] | None = None,
        image: Image | dict[str, Any] | None = None,
        min_instances: int | None = None,
        concurrency: int | None = None,
        enabled: bool = True,
        client: Client | None = None,
        function_id: str | None = None,
        data: dict[str, Any] | None = None,
        retries: int = 0,
        timeout_seconds: int | float | None = None,
        cache: bool = False,
        resources: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            fn,
            name=name,
            project=project,
            description=description,
            default_parameters=default_parameters,
            backend=backend,
            dependencies=dependencies,
            image=image,
            min_instances=min_instances,
            concurrency=concurrency,
            enabled=enabled,
            client=client,
            function_id=function_id,
            data=data,
        )
        if retries < 0:
            raise ValueError("retries must be greater than or equal to 0")
        self.retries = retries
        self.timeout_seconds = timeout_seconds
        self.cache = cache
        self.resources = resources or {}

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
        default_parameters: dict[str, Any] | None = None,
        backend: WorkflowBackend = DEFAULT_WORKFLOW_BACKEND,
        enabled: bool = True,
        client: Client | None = None,
        workflow_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.fn = fn
        self.project = project
        self.description = description
        self.enabled = enabled
        self.client = client
        self.id: str | None = workflow_id
        self.data = data or {}
        self.name = name or (data["name"] if data else None)
        self.flow_ref: str | None = None
        self.source_code: str | None = None
        self.entrypoint: str | None = None
        self.step_graph: dict[str, Any] | None = data.get("step_graph") if data else None
        self.schedule = (
            _schedule_payload(schedule)
            if schedule is not None
            else (data.get("schedule") if data else None)
        )
        self.default_parameters = default_parameters or {}
        self.execution_backend: WorkflowBackend = (
            data.get("execution_backend", DEFAULT_WORKFLOW_BACKEND) if data else DEFAULT_WORKFLOW_BACKEND
        )
        self.required_parameters: list[str] = list(data.get("required_parameters", [])) if data else []
        self.source_metadata: dict[str, Any] = {}

        if fn is not None:
            if not isinstance(fn, FunctionType):
                raise TypeError("Workflow requires a plain Python function")
            self.name = _target_name(fn, name)
            inferred_defaults = _defaults_for(fn, target="Workflow")
            self.required_parameters = _required_parameters_for(fn, target="Workflow")
            self.default_parameters = {**inferred_defaults, **(default_parameters or {})}
            self.execution_backend = _validate_workflow_backend(backend)
            self.source_code = _source_for(fn, target="workflow")
            self.entrypoint = fn.__name__
            self.source_metadata = _git_metadata_for(fn)
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

    def _references_step(self) -> bool:
        if self.fn is None:
            return False
        try:
            closure = inspect.getclosurevars(self.fn)
        except TypeError:
            return False
        values = [*closure.nonlocals.values(), *closure.globals.values()]
        return any(isinstance(value, Step) for value in values)

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
                "Scheduled workflows require defaults for every workflow parameter. "
                f"Missing defaults: {missing}"
            )

    def deploy(self, *, replace: bool = False) -> Workflow:
        if self.source_code is None or self.entrypoint is None:
            raise RebaseWorkflowError("cannot deploy a workflow handle without source_code and entrypoint")
        if self.name is None:
            raise RebaseWorkflowError("workflow name is required")
        self._validate_schedule_defaults()
        name = self.name
        step_graph = self._build_step_graph()
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
                default_parameters=self.default_parameters,
                required_parameters=self.required_parameters,
                execution_backend=self.execution_backend,
                enabled=self.enabled,
                **self.source_metadata,
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
            default_parameters=self.default_parameters,
            required_parameters=self.required_parameters,
            execution_backend=self.execution_backend,
            enabled=self.enabled,
            **self.source_metadata,
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
            execution_backend=self.execution_backend,
            step_graph=self._build_step_graph(ephemeral=True),
            required_parameters=self.required_parameters,
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

    @property
    def status(self) -> str:
        if not self.data:
            self.refresh()
        return str(self.data["status"])

    def result(self, *, timeout: int = 600, poll_interval: float = 5.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        terminal_statuses = {"succeeded", "failed", "cancelled"}

        while True:
            data = self.refresh()
            status = data["status"]
            if status in terminal_statuses:
                if status == "succeeded":
                    return data["result"]
                raise RebaseWorkflowError(data.get("error") or f"run ended with status {status}")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"run {self.id} did not finish within {timeout} seconds")
            time.sleep(poll_interval)
