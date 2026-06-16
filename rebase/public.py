from __future__ import annotations

from collections.abc import Callable
from typing import Any, overload

from rebase.client import (
    DEFAULT_FUNCTION_BACKEND,
    DEFAULT_WORKFLOW_BACKEND,
    Client,
    Function,
    FunctionBackend,
    Image,
    Project,
    RebaseWorkflowError,
    Schedule,
    Step,
    Workflow,
    WorkflowBackend,
)

DEFAULT_PROJECT_NAME = "default"


def project(
    name: str,
    *,
    description: str | None = None,
    source_mode: str | None = None,
    repo_owner: str | None = None,
    repo_name: str | None = None,
    repo_path: str | None = None,
) -> Project:
    return Project(
        name,
        description=description,
        source_mode=source_mode,
        repo_owner=repo_owner,
        repo_name=repo_name,
        repo_path=repo_path,
    )


@overload
def function(
    fn: None = None,
    *,
    project: str | None = None,
    name: str | None = None,
    description: str | None = None,
    default_parameters: dict[str, Any] | None = None,
    backend: FunctionBackend = DEFAULT_FUNCTION_BACKEND,
    dependencies: list[str] | tuple[str, ...] | None = None,
    image: Image | dict[str, Any] | None = None,
    min_instances: int | None = None,
    concurrency: int | None = None,
    enabled: bool = True,
) -> Callable[[Callable[..., Any]], Function]: ...


@overload
def function(
    fn: Callable[..., Any],
    *,
    project: str | None = None,
    name: str | None = None,
    description: str | None = None,
    default_parameters: dict[str, Any] | None = None,
    backend: FunctionBackend = DEFAULT_FUNCTION_BACKEND,
    dependencies: list[str] | tuple[str, ...] | None = None,
    image: Image | dict[str, Any] | None = None,
    min_instances: int | None = None,
    concurrency: int | None = None,
    enabled: bool = True,
) -> Function: ...


def function(
    fn: Callable[..., Any] | None = None,
    *,
    project: str | None = None,
    name: str | None = None,
    description: str | None = None,
    default_parameters: dict[str, Any] | None = None,
    backend: FunctionBackend = DEFAULT_FUNCTION_BACKEND,
    dependencies: list[str] | tuple[str, ...] | None = None,
    image: Image | dict[str, Any] | None = None,
    min_instances: int | None = None,
    concurrency: int | None = None,
    enabled: bool = True,
) -> Callable[[Callable[..., Any]], Function] | Function:
    def decorator(callable_: Callable[..., Any]) -> Function:
        return Function(
            callable_,
            name=name,
            project=project or DEFAULT_PROJECT_NAME,
            description=description,
            default_parameters=default_parameters,
            backend=backend,
            dependencies=dependencies,
            image=image,
            min_instances=min_instances,
            concurrency=concurrency,
            enabled=enabled,
        )

    if fn is not None:
        return decorator(fn)
    return decorator


@overload
def step(
    fn: None = None,
    *,
    project: str | None = None,
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
) -> Callable[[Callable[..., Any]], Step]: ...


@overload
def step(
    fn: Callable[..., Any],
    *,
    project: str | None = None,
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
) -> Step: ...


def step(
    fn: Callable[..., Any] | None = None,
    *,
    project: str | None = None,
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
) -> Callable[[Callable[..., Any]], Step] | Step:
    def decorator(callable_: Callable[..., Any]) -> Step:
        return Step(
            callable_,
            name=name,
            project=project or DEFAULT_PROJECT_NAME,
            description=description,
            default_parameters=default_parameters,
            backend=backend,
            dependencies=dependencies,
            image=image,
            min_instances=min_instances,
            concurrency=concurrency,
            enabled=enabled,
            retries=retries,
            timeout_seconds=timeout_seconds,
            cache=cache,
            resources=resources,
        )

    if fn is not None:
        return decorator(fn)
    return decorator


@overload
def workflow(
    fn: None = None,
    *,
    project: str | None = None,
    name: str | None = None,
    description: str | None = None,
    schedule: Schedule | None = None,
    default_parameters: dict[str, Any] | None = None,
    backend: WorkflowBackend = DEFAULT_WORKFLOW_BACKEND,
    enabled: bool = True,
) -> Callable[[Callable[..., Any]], Workflow]: ...


@overload
def workflow(
    fn: Callable[..., Any],
    *,
    project: str | None = None,
    name: str | None = None,
    description: str | None = None,
    schedule: Schedule | None = None,
    default_parameters: dict[str, Any] | None = None,
    backend: WorkflowBackend = DEFAULT_WORKFLOW_BACKEND,
    enabled: bool = True,
) -> Workflow: ...


def workflow(
    fn: Callable[..., Any] | None = None,
    *,
    project: str | None = None,
    name: str | None = None,
    description: str | None = None,
    schedule: Schedule | None = None,
    default_parameters: dict[str, Any] | None = None,
    backend: WorkflowBackend = DEFAULT_WORKFLOW_BACKEND,
    enabled: bool = True,
) -> Callable[[Callable[..., Any]], Workflow] | Workflow:
    def decorator(callable_: Callable[..., Any]) -> Workflow:
        return Workflow(
            callable_,
            name=name,
            project=project or DEFAULT_PROJECT_NAME,
            description=description,
            schedule=schedule,
            default_parameters=default_parameters,
            backend=backend,
            enabled=enabled,
        )

    if fn is not None:
        return decorator(fn)
    return decorator


def deploy(*targets: Project | Function | Workflow, replace: bool = False) -> Project | Function | Workflow | list[Any]:
    if not targets:
        raise RebaseWorkflowError("deploy requires at least one Rebase project, function, or workflow")
    deployed = [target.deploy(replace=replace) for target in targets]
    return deployed[0] if len(deployed) == 1 else deployed


def get_function(project: str, name: str | None = None) -> Function:
    project_name, function_name = _split_ref(project, name=name, target="function")
    return Function.from_name(project_name, function_name)


def get_workflow(project: str, name: str | None = None) -> Workflow:
    project_name, workflow_name = _split_ref(project, name=name, target="workflow")
    return Workflow.from_name(project_name, workflow_name)


def workspace() -> dict[str, Any]:
    return Client().get_workspace()


def update_workspace(
    *,
    name: str | None = None,
    source_mode: str | None = None,
    repo_owner: str | None = None,
    repo_name: str | None = None,
    repo_path: str | None = None,
) -> dict[str, Any]:
    return Client().update_workspace(
        name=name,
        source_mode=source_mode,
        repo_owner=repo_owner,
        repo_name=repo_name,
        repo_path=repo_path,
    )


def projects() -> list[dict[str, Any]]:
    return Client().list_projects()


def _split_ref(project: str, *, name: str | None, target: str) -> tuple[str, str]:
    if name is not None:
        return project, name
    if "/" not in project:
        raise ValueError(f"{target} reference must be 'project/name' or pass name=...")
    project_name, target_name = project.split("/", 1)
    if not project_name or not target_name:
        raise ValueError(f"{target} reference must be 'project/name'")
    return project_name, target_name
