from __future__ import annotations

from collections.abc import Callable
from typing import Any, overload

from rebase.client import (
    DEFAULT_FUNCTION_BACKEND,
    DEFAULT_WORKFLOW_BACKEND,
    Agent,
    AgentHandle,
    ASGIApp,
    Client,
    EndpointConfig,
    Function,
    FunctionBackend,
    Image,
    Model,
    ModelHandle,
    Optimizer,
    OptimizerHandle,
    Predictor,
    PredictorHandle,
    Project,
    RebaseWorkflowError,
    Schedule,
    Secret,
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
    deploy_source: str | None = None,
) -> Project:
    return Project(
        name,
        description=description,
        source_mode=source_mode,
        repo_owner=repo_owner,
        repo_name=repo_name,
        repo_path=repo_path,
        deploy_source=deploy_source,
    )


def endpoint(
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
) -> EndpointConfig:
    return EndpointConfig(
        name=name,
        method=method,
        path=path,
        auth=auth,
        mode=mode,
        timeout=timeout,
        timeout_seconds=timeout_seconds,
        docs=docs,
        enabled=enabled,
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
    env: dict[str, str] | None = None,
    secrets: dict[str, str] | list[Secret | str] | None = None,
    min_instances: int | None = None,
    concurrency: int | None = None,
    enabled: bool = True,
    endpoint: EndpointConfig | dict[str, Any] | None = None,
    deploy_source: str | None = None,
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
    env: dict[str, str] | None = None,
    secrets: dict[str, str] | list[Secret | str] | None = None,
    min_instances: int | None = None,
    concurrency: int | None = None,
    enabled: bool = True,
    endpoint: EndpointConfig | dict[str, Any] | None = None,
    deploy_source: str | None = None,
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
    env: dict[str, str] | None = None,
    secrets: dict[str, str] | list[Secret | str] | None = None,
    min_instances: int | None = None,
    concurrency: int | None = None,
    enabled: bool = True,
    endpoint: EndpointConfig | dict[str, Any] | None = None,
    deploy_source: str | None = None,
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
            env=env,
            secrets=secrets,
            min_instances=min_instances,
            concurrency=concurrency,
            enabled=enabled,
            endpoint=endpoint,
            deploy_source=deploy_source,
        )

    if fn is not None:
        return decorator(fn)
    return decorator


@overload
def asgi_app(
    fn: None = None,
    *,
    project: str | None = None,
    name: str | None = None,
    description: str | None = None,
    base_path: str = "/",
    auth: str = "api_key",
    dependencies: list[str] | tuple[str, ...] | None = None,
    image: Image | dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    secrets: dict[str, str] | list[Secret | str] | None = None,
    min_instances: int | None = None,
    max_instances: int | None = None,
    concurrency: int | None = None,
    timeout_seconds: int | None = None,
    cpu: str | None = None,
    memory: str | None = None,
    enabled: bool = True,
    deploy_source: str | None = None,
) -> Callable[[Callable[..., Any]], ASGIApp]: ...


@overload
def asgi_app(
    fn: Callable[..., Any],
    *,
    project: str | None = None,
    name: str | None = None,
    description: str | None = None,
    base_path: str = "/",
    auth: str = "api_key",
    dependencies: list[str] | tuple[str, ...] | None = None,
    image: Image | dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    secrets: dict[str, str] | list[Secret | str] | None = None,
    min_instances: int | None = None,
    max_instances: int | None = None,
    concurrency: int | None = None,
    timeout_seconds: int | None = None,
    cpu: str | None = None,
    memory: str | None = None,
    enabled: bool = True,
    deploy_source: str | None = None,
) -> ASGIApp: ...


def asgi_app(
    fn: Callable[..., Any] | None = None,
    *,
    project: str | None = None,
    name: str | None = None,
    description: str | None = None,
    base_path: str = "/",
    auth: str = "api_key",
    dependencies: list[str] | tuple[str, ...] | None = None,
    image: Image | dict[str, Any] | None = None,
    env: dict[str, str] | None = None,
    secrets: dict[str, str] | list[Secret | str] | None = None,
    min_instances: int | None = None,
    max_instances: int | None = None,
    concurrency: int | None = None,
    timeout_seconds: int | None = None,
    cpu: str | None = None,
    memory: str | None = None,
    enabled: bool = True,
    deploy_source: str | None = None,
) -> Callable[[Callable[..., Any]], ASGIApp] | ASGIApp:
    def decorator(callable_: Callable[..., Any]) -> ASGIApp:
        return ASGIApp(
            callable_,
            project=project or DEFAULT_PROJECT_NAME,
            name=name,
            description=description,
            base_path=base_path,
            auth=auth,
            dependencies=dependencies,
            image=image,
            env=env,
            secrets=secrets,
            min_instances=min_instances,
            max_instances=max_instances,
            concurrency=concurrency,
            timeout_seconds=timeout_seconds,
            cpu=cpu,
            memory=memory,
            enabled=enabled,
            deploy_source=deploy_source,
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
    enabled: bool = True,
    retries: int = 0,
    timeout_seconds: int | float | None = None,
    cache: bool = False,
    deploy_source: str | None = None,
) -> Callable[[Callable[..., Any]], Step]: ...


@overload
def step(
    fn: Callable[..., Any],
    *,
    project: str | None = None,
    name: str | None = None,
    description: str | None = None,
    default_parameters: dict[str, Any] | None = None,
    enabled: bool = True,
    retries: int = 0,
    timeout_seconds: int | float | None = None,
    cache: bool = False,
    deploy_source: str | None = None,
) -> Step: ...


def step(
    fn: Callable[..., Any] | None = None,
    *,
    project: str | None = None,
    name: str | None = None,
    description: str | None = None,
    default_parameters: dict[str, Any] | None = None,
    enabled: bool = True,
    retries: int = 0,
    timeout_seconds: int | float | None = None,
    cache: bool = False,
    deploy_source: str | None = None,
) -> Callable[[Callable[..., Any]], Step] | Step:
    def decorator(callable_: Callable[..., Any]) -> Step:
        return Step(
            callable_,
            name=name,
            project=project or DEFAULT_PROJECT_NAME,
            description=description,
            default_parameters=default_parameters,
            enabled=enabled,
            retries=retries,
            timeout_seconds=timeout_seconds,
            cache=cache,
            deploy_source=deploy_source,
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
    endpoint: EndpointConfig | dict[str, Any] | None = None,
    deploy_source: str | None = None,
    dependencies: list[str] | tuple[str, ...] | None = None,
    image: Image | dict[str, Any] | None = None,
    min_instances: int | None = None,
    concurrency: int | None = None,
    resources: dict[str, Any] | None = None,
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
    endpoint: EndpointConfig | dict[str, Any] | None = None,
    deploy_source: str | None = None,
    dependencies: list[str] | tuple[str, ...] | None = None,
    image: Image | dict[str, Any] | None = None,
    min_instances: int | None = None,
    concurrency: int | None = None,
    resources: dict[str, Any] | None = None,
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
    endpoint: EndpointConfig | dict[str, Any] | None = None,
    deploy_source: str | None = None,
    dependencies: list[str] | tuple[str, ...] | None = None,
    image: Image | dict[str, Any] | None = None,
    min_instances: int | None = None,
    concurrency: int | None = None,
    resources: dict[str, Any] | None = None,
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
            endpoint=endpoint,
            deploy_source=deploy_source,
            dependencies=dependencies,
            image=image,
            min_instances=min_instances,
            concurrency=concurrency,
            resources=resources,
        )

    if fn is not None:
        return decorator(fn)
    return decorator


DeployTarget = Project | Function | Workflow | Model | ASGIApp


def deploy(
    *targets: DeployTarget,
    replace: bool = False,
    deploy_source: str | None = None,
    environment: str = "dev",
) -> DeployTarget | list[Any]:
    if not targets:
        raise RebaseWorkflowError("deploy requires at least one Rebase project, function, workflow, ASGI app, or model")
    for target in targets:
        if isinstance(target, Step):
            raise RebaseWorkflowError(
                f"Step {getattr(target, 'name', repr(target))!r} cannot be deployed standalone. "
                "Steps are deployed automatically when their workflow is deployed. "
                "Use rb.deploy(workflow) instead."
            )
    deploy_kwargs: dict[str, Any] = {"replace": replace}
    if deploy_source is not None:
        deploy_kwargs["deploy_source"] = deploy_source
    if environment != "dev":
        deploy_kwargs["environment"] = environment
    deployed = [target.deploy(**deploy_kwargs) for target in targets]
    return deployed[0] if len(deployed) == 1 else deployed


def get_function(project: str, name: str | None = None) -> Function:
    project_name, function_name = _split_ref(project, name=name, target="function")
    return Function.from_name(project_name, function_name)


def get_workflow(project: str, name: str | None = None) -> Workflow:
    project_name, workflow_name = _split_ref(project, name=name, target="workflow")
    return Workflow.from_name(project_name, workflow_name)


def get_asgi_app(project: str, name: str | None = None) -> ASGIApp:
    project_name, asgi_app_name = _split_ref(project, name=name, target="ASGI app")
    return ASGIApp.from_name(project_name, asgi_app_name)


def get_model(project: str, name: str | None = None) -> ModelHandle:
    project_name, model_name = _split_ref(project, name=name, target="model")
    return Model.from_name(project_name, model_name)


def get_predictor(project: str, name: str | None = None) -> PredictorHandle:
    project_name, predictor_name = _split_ref(project, name=name, target="predictor")
    return Predictor.from_name(project_name, predictor_name)


def get_optimizer(project: str, name: str | None = None) -> OptimizerHandle:
    project_name, optimizer_name = _split_ref(project, name=name, target="optimizer")
    return Optimizer.from_name(project_name, optimizer_name)


def get_agent(project: str, name: str | None = None) -> AgentHandle:
    project_name, agent_name = _split_ref(project, name=name, target="agent")
    return Agent.from_name(project_name, agent_name)


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
