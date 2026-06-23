from __future__ import annotations

import ast
import inspect
import json
import os
import re
import subprocess
import tempfile
import textwrap
import time
import warnings
from collections.abc import Callable
from pathlib import Path
from types import FunctionType
from typing import Any, Self

import requests

from rebase.auth import AuthError, load_access_token
from rebase.config import DEFAULT_SERVER_URL, load_profile

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
_trace_stack: list[_WorkflowTrace] = []
_UNSET = object()
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


class RebaseWorkflowError(RuntimeError):
    pass


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


def _response_error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str):
            return detail
        if detail is not None:
            return json.dumps(detail)
    return response.text


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
DeploySource = str
DEFAULT_DEPLOY_SOURCE: DeploySource = "rebase"
DEFAULT_PYTHON_VERSION = "3.13"
DEFAULT_MODEL_DEPENDENCY = (
    "emflow @ git+https://github.com/rebase-energy/emflow.git@2d0205e1b479d439df72e50c6865735d0b26de8d"
)


def _validate_function_backend(backend: FunctionBackend) -> FunctionBackend:
    if backend not in {"modal", "prefect", "prefect_cloud", "cloud_run", "cloud_run_shared"}:
        raise ValueError(
            "function backend must be 'modal', 'prefect', 'prefect_cloud', 'cloud_run', or 'cloud_run_shared'"
        )
    return backend


def _validate_workflow_backend(backend: WorkflowBackend) -> WorkflowBackend:
    if backend not in {"prefect", "prefect_cloud_run_jobs", "prefect_cloud_run_service"}:
        raise ValueError("workflow backend must be 'prefect', 'prefect_cloud_run_jobs', or 'prefect_cloud_run_service'")
    return backend


def _validate_deploy_source(source: str | None) -> DeploySource | None:
    if source is None:
        return None
    if source not in {"rebase", "github"}:
        raise ValueError("deploy_source must be 'rebase' or 'github'")
    return source


def _is_pinned_dependency(package: str) -> bool:
    return "==" in package or re.search(r"git\+.*\.git@[a-f0-9]{7,40}(?:$|#)", package.strip().lower()) is not None


def _warn_unpinned_dependencies(packages: list[str]) -> None:
    unpinned = [package for package in packages if not _is_pinned_dependency(package)]
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
        return image.to_dict()
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
        packages = _model_dependencies(image.uv_pip_packages)
        return Image(
            kind=image.kind,
            python_version=image.python_version,
            uv_pip_packages=packages,
            uv_version=image.uv_version,
        ).to_dict()
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
) -> None:
    global _default_client
    _default_client = Client(api_key=api_key, api_url=api_url, profile=profile, access_token=access_token)


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


class Client:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_url: str | None = None,
        profile: str | None = None,
        access_token: str | None = None,
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
        if self.api_key is None and self.access_token is None and isinstance(configured_api_key, str):
            self.api_key = configured_api_key
        self.workspace_id = configured_workspace_id if isinstance(configured_workspace_id, str) else None
        profile_api_url = configured_api_url if isinstance(configured_api_url, str) else None
        selected_api_url = api_url or os.getenv("REBASE_WORKFLOWS_API_URL") or profile_api_url or DEFAULT_SERVER_URL
        self.api_url = selected_api_url.rstrip("/")

    def request(
        self, method: str, path: str, *, auth: bool = True, **kwargs: Any
    ) -> dict[str, Any] | list[dict[str, Any]]:
        headers = dict(kwargs.pop("headers", {}))
        if auth:
            bearer_token = self.api_key or self.access_token
            if bearer_token is None:
                try:
                    bearer_token = load_access_token()
                except AuthError as exc:
                    raise RebaseWorkflowError(str(exc)) from exc
            if bearer_token:
                headers["Authorization"] = f"Bearer {bearer_token}"
        if self.workspace_id and "X-Rebase-Workspace" not in headers:
            headers["X-Rebase-Workspace"] = self.workspace_id
        response = requests.request(method, f"{self.api_url}{path}", headers=headers, timeout=30, **kwargs)
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise RebaseWorkflowError(_response_error_message(response)) from exc
        return response.json()

    def setup_config(self) -> dict[str, Any]:
        response = self.request("GET", "/setup/config", auth=False)
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected setup config response")
        return response

    def list_my_workspaces(self) -> list[dict[str, Any]]:
        response = self.request("GET", "/me/workspaces")
        if not isinstance(response, list):
            raise RebaseWorkflowError("expected workspace list response")
        return response

    def create_workspace(self, workspace_id: str, *, name: str | None = None) -> dict[str, Any]:
        response = self.request("POST", "/workspaces", json={"id": workspace_id, "name": name})
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected workspace response")
        return response

    def list_platform_invites(self) -> list[dict[str, Any]]:
        response = self.request("GET", "/platform/invites")
        if not isinstance(response, list):
            raise RebaseWorkflowError("expected platform invite list response")
        return response

    def create_platform_invite(
        self,
        email: str,
        *,
        expires_at: str | None = None,
        workspace_creation_limit: int = 1,
    ) -> dict[str, Any]:
        response = self.request(
            "POST",
            "/platform/invites",
            json={
                "email": email,
                "expires_at": expires_at,
                "workspace_creation_limit": workspace_creation_limit,
            },
        )
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected platform invite response")
        return response

    def revoke_platform_invite(self, invite_id: str) -> dict[str, Any]:
        response = self.request("DELETE", f"/platform/invites/{invite_id}")
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected platform invite response")
        return response

    def list_workspace_invites(self) -> list[dict[str, Any]]:
        response = self.request("GET", "/workspace/invites")
        if not isinstance(response, list):
            raise RebaseWorkflowError("expected workspace invite list response")
        return response

    def list_workspace_members(self) -> list[dict[str, Any]]:
        response = self.request("GET", "/workspace/members")
        if not isinstance(response, list):
            raise RebaseWorkflowError("expected workspace member list response")
        return response

    def create_workspace_invite(
        self,
        *,
        email: str | None = None,
        github_username: str | None = None,
        role: str = "Viewer",
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        response = self.request(
            "POST",
            "/workspace/invites",
            json={
                "email": email,
                "github_username": github_username,
                "role": role,
                "expires_at": expires_at,
            },
        )
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected workspace invite response")
        return response

    def revoke_workspace_invite(self, invite_id: str) -> dict[str, Any]:
        response = self.request("DELETE", f"/workspace/invites/{invite_id}")
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected workspace invite response")
        return response

    def list_api_keys(self) -> list[dict[str, Any]]:
        response = self.request("GET", "/workspace/api-keys")
        if not isinstance(response, list):
            raise RebaseWorkflowError("expected API key list response")
        return response

    def create_api_key(
        self,
        name: str,
        *,
        project_id: str | None = None,
        permissions: list[str] | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        response = self.request(
            "POST",
            "/workspace/api-keys",
            json={
                "name": name,
                "project_id": project_id,
                "permissions": permissions if permissions is not None else list(DEFAULT_API_KEY_PERMISSIONS),
                "expires_at": expires_at,
            },
        )
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected API key response")
        return response

    def revoke_api_key(self, api_key_id: str) -> dict[str, Any]:
        response = self.request("DELETE", f"/workspace/api-keys/{api_key_id}")
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected API key response")
        return response

    def _with_endpoint_url(self, endpoint: dict[str, Any]) -> dict[str, Any]:
        url_path = endpoint.get("url_path")
        if isinstance(url_path, str):
            return {**endpoint, "url": f"{self.api_url}{url_path}"}
        return endpoint

    def list_endpoints(self, *, project_id: str | None = None) -> list[dict[str, Any]]:
        params = {"project_id": project_id} if project_id is not None else None
        response = self.request("GET", "/endpoints", params=params)
        if not isinstance(response, list):
            raise RebaseWorkflowError("expected endpoint list response")
        return [self._with_endpoint_url(endpoint) for endpoint in response]

    def list_project_endpoints(self, project_id: str) -> list[dict[str, Any]]:
        response = self.request("GET", f"/projects/{project_id}/endpoints")
        if not isinstance(response, list):
            raise RebaseWorkflowError("expected endpoint list response")
        return [self._with_endpoint_url(endpoint) for endpoint in response]

    def get_endpoint(self, endpoint_id: str) -> dict[str, Any]:
        response = self.request("GET", f"/endpoints/{endpoint_id}")
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected endpoint response")
        return self._with_endpoint_url(response)

    def list_endpoint_versions(self, endpoint_id: str) -> list[dict[str, Any]]:
        response = self.request("GET", f"/endpoints/{endpoint_id}/versions")
        if not isinstance(response, list):
            raise RebaseWorkflowError("expected endpoint version list response")
        return response

    def disable_endpoint(self, endpoint_id: str) -> dict[str, Any]:
        response = self.request("PATCH", f"/endpoints/{endpoint_id}", json={"enabled": False})
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected endpoint response")
        return self._with_endpoint_url(response)

    def invoke_endpoint(self, endpoint: dict[str, Any], parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        method = str(endpoint.get("method") or "POST").upper()
        url_path = endpoint.get("url_path")
        if not isinstance(url_path, str):
            raise RebaseWorkflowError("endpoint response is missing url_path")
        kwargs: dict[str, Any] = {"params": parameters or {}} if method == "GET" else {"json": parameters or {}}
        response = self.request(method, url_path, **kwargs)
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected endpoint invoke response")
        return response

    def create_github_setup_session(self, *, workspace_id: str | None = None) -> dict[str, Any]:
        response = self.request("POST", "/integrations/github/setup-sessions", json={"workspace_id": workspace_id})
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected GitHub setup response")
        return response

    def get_github_setup_session(self, setup_session_id: str) -> dict[str, Any]:
        response = self.request("GET", f"/integrations/github/setup-sessions/{setup_session_id}")
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected GitHub setup status response")
        return response

    def list_github_repositories(self, installation_id: int) -> list[dict[str, Any]]:
        response = self.request("GET", "/integrations/github/repositories", params={"installation_id": installation_id})
        if not isinstance(response, list):
            raise RebaseWorkflowError("expected GitHub repository list response")
        return response

    def find_github_repository_installation(self, repo_full_name: str) -> dict[str, Any]:
        response = self.request(
            "GET",
            "/integrations/github/repository-installation",
            params={"repo_full_name": repo_full_name},
        )
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected GitHub repository installation response")
        return response

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
        response = self.request(
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
        )
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected GitHub repo connection response")
        return response

    def create_github_starter_workflow(
        self,
        connection_id: str,
        *,
        path: str = ".rebase/starter_workflow.py",
    ) -> dict[str, Any]:
        response = self.request(
            "POST",
            f"/integrations/github/repo-connections/{connection_id}/starter-workflow",
            json={"path": path},
        )
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected GitHub starter workflow response")
        return response

    def list_projects(self) -> list[dict[str, Any]]:
        response = self.request("GET", "/projects")
        if not isinstance(response, list):
            raise RebaseWorkflowError("expected project list response")
        return response

    def get_project(self, project_id: str) -> dict[str, Any]:
        response = self.request("GET", f"/projects/{project_id}")
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected project response")
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
            resolved_project = self.find_project(project)
            if resolved_project is None:
                return []
            resolved_project_id = resolved_project["id"]
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
        endpoint: EndpointConfig | dict[str, Any] | None = None,
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
                "endpoint": _coerce_endpoint(endpoint).to_payload() if endpoint is not None else None,
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
        endpoint: EndpointConfig | dict[str, Any] | None = None,
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
                "endpoint": _coerce_endpoint(endpoint).to_payload() if endpoint is not None else None,
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

    def list_models(self, *, project: str | None = None, project_id: str | None = None) -> list[dict[str, Any]]:
        resolved_project_id = project_id
        if resolved_project_id is None and project is not None:
            resolved_project = self.find_project(project)
            if resolved_project is None:
                return []
            resolved_project_id = resolved_project["id"]
        path = f"/projects/{resolved_project_id}/models" if resolved_project_id is not None else "/models"
        response = self.request("GET", path)
        if not isinstance(response, list):
            raise RebaseWorkflowError("expected model list response")
        return response

    def get_model(self, model_id: str) -> dict[str, Any]:
        response = self.request("GET", f"/models/{model_id}")
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected model response")
        return response

    def find_model(self, name: str, *, project: str) -> dict[str, Any] | None:
        for model in self.list_models(project=project):
            if model["name"] == name:
                return model
        return None

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
        execution_backend: FunctionBackend = DEFAULT_FUNCTION_BACKEND,
        image_spec: dict[str, Any] | None = None,
        cloud_run_min_instances: int | None = None,
        cloud_run_concurrency: int | None = None,
        enabled: bool = True,
        endpoint: EndpointConfig | dict[str, Any] | None = None,
        environment: str | None = "dev",
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
            f"/projects/{project_id}/models",
            json={
                "name": name,
                "kind": kind,
                "operation_name": operation_name,
                "description": description,
                "source_code": source_code,
                "default_parameters": default_parameters or {},
                "execution_backend": execution_backend,
                "image_spec": image_spec,
                "cloud_run_min_instances": cloud_run_min_instances,
                "cloud_run_concurrency": cloud_run_concurrency,
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
        )
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected model response")
        return response

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
        execution_backend: FunctionBackend | None = None,
        image_spec: dict[str, Any] | None = None,
        cloud_run_min_instances: int | None = None,
        cloud_run_concurrency: int | None = None,
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
        payload = {
            key: value
            for key, value in {
                "name": name,
                "kind": kind,
                "operation_name": operation_name,
                "description": description,
                "source_code": source_code,
                "default_parameters": default_parameters,
                "execution_backend": execution_backend,
                "image_spec": image_spec,
                "cloud_run_min_instances": cloud_run_min_instances,
                "cloud_run_concurrency": cloud_run_concurrency,
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
        response = self.request("PATCH", f"/models/{model_id}", json=payload)
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected model response")
        return response

    def run_model(
        self,
        model_id: str,
        parameters: dict[str, Any] | None = None,
        *,
        environment: str = "dev",
    ) -> Run:
        response = self.request(
            "POST",
            f"/models/{model_id}/runs",
            json={"parameters": parameters or {}, "environment": environment},
        )
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected run response")
        return Run(response["id"], client=self, data=response)

    def list_model_versions(self, model_id: str) -> list[dict[str, Any]]:
        response = self.request("GET", f"/models/{model_id}/versions")
        if not isinstance(response, list):
            raise RebaseWorkflowError("expected model version list response")
        return response

    def get_model_version(self, model_id: str, version_id: str) -> dict[str, Any]:
        response = self.request("GET", f"/models/{model_id}/versions/{version_id}")
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected model version response")
        return response

    def list_model_publications(self, model_id: str) -> list[dict[str, Any]]:
        response = self.request("GET", f"/models/{model_id}/publications")
        if not isinstance(response, list):
            raise RebaseWorkflowError("expected model publication list response")
        return response

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
        response = self.request(
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
        )
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected model publication response")
        return response

    def list_model_deployments(self, model_id: str) -> list[dict[str, Any]]:
        response = self.request("GET", f"/models/{model_id}/deployments")
        if not isinstance(response, list):
            raise RebaseWorkflowError("expected model deployment list response")
        return response

    def deploy_model_version(
        self,
        model_id: str,
        *,
        environment: str,
        model_version_id: str,
        promotion_request_id: str | None = None,
    ) -> dict[str, Any]:
        response = self.request(
            "POST",
            f"/models/{model_id}/deployments/{environment}",
            json={"model_version_id": model_version_id, "promotion_request_id": promotion_request_id},
        )
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected model deployment response")
        return response

    def create_model_promotion_request(
        self,
        model_id: str,
        *,
        model_version_id: str,
        from_environment: str = "staging",
        to_environment: str = "prod",
        reason: str | None = None,
    ) -> dict[str, Any]:
        response = self.request(
            "POST",
            f"/models/{model_id}/promotion-requests",
            json={
                "model_version_id": model_version_id,
                "from_environment": from_environment,
                "to_environment": to_environment,
                "reason": reason,
            },
        )
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected model promotion request response")
        return response

    def approve_model_promotion_request(self, request_id: str, *, reason: str | None = None) -> dict[str, Any]:
        response = self.request("POST", f"/model-promotion-requests/{request_id}/approve", json={"reason": reason})
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected model promotion request response")
        return response

    def reject_model_promotion_request(self, request_id: str, *, reason: str | None = None) -> dict[str, Any]:
        response = self.request("POST", f"/model-promotion-requests/{request_id}/reject", json={"reason": reason})
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected model promotion request response")
        return response

    def promote_model(
        self,
        model_id: str,
        *,
        from_environment: str = "dev",
        to_environment: str,
        model_version_id: str | None = None,
        promotion_request_id: str | None = None,
    ) -> dict[str, Any]:
        response = self.request(
            "POST",
            f"/models/{model_id}/promote",
            json={
                "from_environment": from_environment,
                "to_environment": to_environment,
                "model_version_id": model_version_id,
                "promotion_request_id": promotion_request_id,
            },
        )
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected model deployment response")
        return response

    def rollback_model(
        self,
        model_id: str,
        *,
        environment: str = "prod",
        model_version_id: str | None = None,
    ) -> dict[str, Any]:
        response = self.request(
            "POST",
            f"/models/{model_id}/rollback",
            json={"environment": environment, "model_version_id": model_version_id},
        )
        if not isinstance(response, dict):
            raise RebaseWorkflowError("expected model deployment response")
        return response

    def list_model_events(self, model_id: str) -> list[dict[str, Any]]:
        response = self.request("GET", f"/models/{model_id}/events")
        if not isinstance(response, list):
            raise RebaseWorkflowError("expected model event list response")
        return response

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
            resolved_project = self.find_project(project)
            if resolved_project is None:
                return []
            resolved_project_id = resolved_project["id"]
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
        endpoint: EndpointConfig | dict[str, Any] | None = None,
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
                "endpoint": _coerce_endpoint(endpoint).to_payload() if endpoint is not None else None,
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
        endpoint: EndpointConfig | dict[str, Any] | None = None,
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
                "endpoint": _coerce_endpoint(endpoint).to_payload() if endpoint is not None else None,
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

    def list_runs(
        self,
        *,
        project_id: str | None = None,
        workflow_id: str | None = None,
        function_id: str | None = None,
        model_id: str | None = None,
        target_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params = {
            key: value
            for key, value in {
                "project_id": project_id,
                "workflow_id": workflow_id,
                "function_id": function_id,
                "model_id": model_id,
                "target_type": target_type,
                "limit": limit,
            }.items()
            if value is not None
        }
        response = self.request("GET", "/runs", params=params)
        if not isinstance(response, list):
            raise RebaseWorkflowError("expected run list response")
        return response

    def list_run_steps(self, run_id: str) -> list[dict[str, Any]]:
        response = self.request("GET", f"/runs/{run_id}/steps")
        if not isinstance(response, list):
            raise RebaseWorkflowError("expected step run list response")
        return response

    def list_run_events(self, run_id: str) -> list[dict[str, Any]]:
        response = self.request("GET", f"/runs/{run_id}/events")
        if not isinstance(response, list):
            raise RebaseWorkflowError("expected run event list response")
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

    @property
    def _client(self) -> Client:
        return self.client or default_client()

    def deploy(self, *, replace: bool = False, deploy_source: str | None = None) -> Self:
        for workflow in self._workflows:
            workflow._validate_schedule_defaults()
        resolved_deploy_source = _validate_deploy_source(deploy_source)
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
            function.deploy(replace=replace, deploy_source=resolved_deploy_source)
        for workflow in self._workflows:
            workflow.deploy(replace=replace, deploy_source=resolved_deploy_source)
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
        endpoint: EndpointConfig | dict[str, Any] | None = None,
        deploy_source: str | None = None,
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
                endpoint=endpoint,
                deploy_source=deploy_source if deploy_source is not None else self.deploy_source,
                project_source_mode=self.source_mode,
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
        deploy_source: str | None = None,
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
                deploy_source=deploy_source if deploy_source is not None else self.deploy_source,
                project_source_mode=self.source_mode,
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
        endpoint: EndpointConfig | dict[str, Any] | None = None,
        deploy_source: str | None = None,
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
                endpoint=endpoint,
                deploy_source=deploy_source if deploy_source is not None else self.deploy_source,
                project_source_mode=self.source_mode,
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
        endpoint: EndpointConfig | dict[str, Any] | None = None,
        deploy_source: str | None = None,
        project_source_mode: str | None = None,
        client: Client | None = None,
        function_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.fn = fn
        self.project = project
        self.description = description
        self.enabled = enabled
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

    def _source_metadata_for_deploy(self, deploy_source: str | None = None) -> dict[str, Any]:
        resolved_deploy_source = _validate_deploy_source(deploy_source) or self.deploy_source
        project_source_mode = (
            _connected_source_mode(
                self._client,
                project=self.project,
                project_source_mode=self.project_source_mode,
            )
            if resolved_deploy_source == "github"
            else self.project_source_mode
        )
        return _source_metadata_for_deploy(
            self.source_metadata,
            deploy_source=resolved_deploy_source,
            project_source_mode=project_source_mode,
        )

    def deploy(self, *, replace: bool = False, deploy_source: str | None = None) -> Function:
        if self.source_code is None or self.entrypoint is None:
            raise RebaseWorkflowError("cannot deploy a function handle without source_code and entrypoint")
        if self.name is None:
            raise RebaseWorkflowError("function name is required")
        name = self.name
        source_metadata = self._source_metadata_for_deploy(deploy_source)
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
                endpoint=self.endpoint,
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
            execution_backend=self.execution_backend,
            image_spec=self.image_spec,
            cloud_run_min_instances=self.cloud_run_min_instances,
            cloud_run_concurrency=self.cloud_run_concurrency,
            enabled=self.enabled,
            endpoint=self.endpoint,
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

    def spawn(self, *, environment: str = "dev", **parameters: Any) -> Run:
        return self._handle.spawn(environment=environment, **parameters)

    def remote(self, *, environment: str = "dev", **parameters: Any) -> dict[str, Any]:
        return self._handle.remote(environment=environment, **parameters)

    def run(self, *, environment: str = "dev", **parameters: Any) -> Run:
        return self.spawn(environment=environment, **parameters)

    def __call__(self, *, environment: str = "dev", **parameters: Any) -> dict[str, Any]:
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

    def _operation(self) -> _RemoteModelOperation:
        if self.operation_name is None:
            raise RebaseWorkflowError("model handle does not expose a known remote operation")
        return getattr(self, self.operation_name)

    def spawn(self, *, environment: str = "dev", **parameters: Any) -> Run:
        if self.id is None:
            raise RebaseWorkflowError("model handle has no ID")
        return self._client.run_model(self.id, parameters, environment=environment)

    def remote(self, *, environment: str = "dev", **parameters: Any) -> dict[str, Any]:
        return self.spawn(environment=environment, **parameters).result()

    def run(self, *, environment: str = "dev", **parameters: Any) -> Run:
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
    backend: FunctionBackend = DEFAULT_FUNCTION_BACKEND
    dependencies: list[str] | tuple[str, ...] | None = None
    image: Image | dict[str, Any] | None = None
    min_instances: int | None = None
    concurrency: int | None = None
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
        backend: FunctionBackend | None = None,
        dependencies: list[str] | tuple[str, ...] | None = None,
        image: Image | dict[str, Any] | None = None,
        min_instances: int | None = None,
        concurrency: int | None = None,
        enabled: bool | None = None,
        endpoint: EndpointConfig | dict[str, Any] | None = None,
        deploy_source: str | None = None,
        client: Client | None = None,
    ) -> None:
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
        self.execution_backend = _validate_function_backend(
            backend or getattr(cls, "backend", DEFAULT_FUNCTION_BACKEND)
        )
        self.dependencies = dependencies if dependencies is not None else getattr(cls, "dependencies", None)
        self.image = image if image is not None else getattr(cls, "image", None)
        self.cloud_run_min_instances = (
            min_instances if min_instances is not None else getattr(cls, "min_instances", None)
        )
        self.cloud_run_concurrency = concurrency if concurrency is not None else getattr(cls, "concurrency", None)
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
                enabled=self.enabled,
                deploy_source=self.deploy_source,
                client=self._client,
            )
            function.source_code = _source_for_model(self, operation_name=operation_name)
            function.entrypoint = operation_name
            function.execution_backend = self.execution_backend
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

    def deploy(
        self,
        *,
        replace: bool = False,
        environment: str = "dev",
        huggingface: HuggingFacePublishConfig | None = None,
        deploy_source: str | None = None,
    ) -> Model:
        function = self.as_function()
        if function.source_code is None or function.entrypoint is None:
            raise RebaseWorkflowError("cannot deploy a model without source_code and operation entrypoint")
        source_metadata = function._source_metadata_for_deploy(deploy_source)
        model = self._client.find_model(str(self.name), project=str(self.project or "default"))
        if model is not None:
            model_data = self._client.update_model(
                model["id"],
                kind=_model_kind_for(self),
                operation_name=function.entrypoint,
                description=self.description,
                source_code=function.source_code,
                default_parameters=function.default_parameters,
                execution_backend=function.execution_backend,
                image_spec=function.image_spec,
                cloud_run_min_instances=function.cloud_run_min_instances,
                cloud_run_concurrency=function.cloud_run_concurrency,
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
                execution_backend=function.execution_backend,
                image_spec=function.image_spec,
                cloud_run_min_instances=function.cloud_run_min_instances,
                cloud_run_concurrency=function.cloud_run_concurrency,
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

    def spawn(self, *, environment: str = "dev", **parameters: Any) -> Run:
        if self.id is None:
            self.deploy(environment=environment)
        if self.id is None:
            raise RebaseWorkflowError("model has no ID after deployment")
        return self._client.run_model(self.id, parameters, environment=environment)

    def remote(self, *, environment: str = "dev", **parameters: Any) -> dict[str, Any]:
        return self.spawn(environment=environment, **parameters).result()

    def run(self, *, environment: str = "dev", **parameters: Any) -> Run:
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
            execution_backend=function.execution_backend,
            image_spec=function.image_spec,
            cloud_run_min_instances=function.cloud_run_min_instances,
            cloud_run_concurrency=function.cloud_run_concurrency,
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
        backend: FunctionBackend = "prefect",
        dependencies: list[str] | tuple[str, ...] | None = None,
        image: Image | dict[str, Any] | None = None,
        min_instances: int | None = None,
        concurrency: int | None = None,
        enabled: bool = True,
        deploy_source: str | None = None,
        project_source_mode: str | None = None,
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
        endpoint: EndpointConfig | dict[str, Any] | None = None,
        deploy_source: str | None = None,
        project_source_mode: str | None = None,
        client: Client | None = None,
        workflow_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
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
        self.name = name or (data["name"] if data else None)
        self.flow_ref: str | None = None
        self.source_code: str | None = None
        self.entrypoint: str | None = None
        self.step_graph: dict[str, Any] | None = data.get("step_graph") if data else None
        self.schedule = (
            _schedule_payload(schedule) if schedule is not None else (data.get("schedule") if data else None)
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

    def _source_metadata_for_deploy(self, deploy_source: str | None = None) -> dict[str, Any]:
        resolved_deploy_source = _validate_deploy_source(deploy_source) or self.deploy_source
        project_source_mode = (
            _connected_source_mode(
                self._client,
                project=self.project,
                project_source_mode=self.project_source_mode,
            )
            if resolved_deploy_source == "github"
            else self.project_source_mode
        )
        return _source_metadata_for_deploy(
            self.source_metadata,
            deploy_source=resolved_deploy_source,
            project_source_mode=project_source_mode,
        )

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
                f"Scheduled workflows require defaults for every workflow parameter. Missing defaults: {missing}"
            )

    def deploy(self, *, replace: bool = False, deploy_source: str | None = None) -> Workflow:
        if self.source_code is None or self.entrypoint is None:
            raise RebaseWorkflowError("cannot deploy a workflow handle without source_code and entrypoint")
        if self.name is None:
            raise RebaseWorkflowError("workflow name is required")
        self._validate_schedule_defaults()
        name = self.name
        step_graph = self._build_step_graph()
        source_metadata = self._source_metadata_for_deploy(deploy_source)
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
                endpoint=self.endpoint,
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
            default_parameters=self.default_parameters,
            required_parameters=self.required_parameters,
            execution_backend=self.execution_backend,
            enabled=self.enabled,
            endpoint=self.endpoint,
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

    def events(self) -> list[dict[str, Any]]:
        return self.client.list_run_events(self.id)

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
