"""Deploy-time graph tracing must never execute a step-free workflow body.

Tracing works by calling the body and intercepting its step calls. A body with
no step references has nothing to intercept, so before the guard in
`_build_step_graph` it simply ran — and for a built job-mode workflow that
meant `rebase deploy` executed the user's real pipeline on their laptop.
Deploying the accounting sync once ran a live financial sync locally, complete
with bank API calls and spreadsheet writes.
"""

from __future__ import annotations

import pytest

import rebase as rb
from rebase.client import RebaseWorkflowError


def test_step_free_body_is_not_executed_at_deploy_time() -> None:
    calls: list[str] = []

    @rb.workflow(name="job-sync")
    def sync() -> dict:
        calls.append("ran")
        return {"ok": True}

    assert sync._build_step_graph() is None
    assert calls == [], "deploy-time tracing executed a step-free workflow body"

    # Calling it directly still runs it: only the deploy-time trace is guarded.
    assert sync() == {"ok": True}
    assert calls == ["ran"]


def test_step_free_async_body_is_not_executed_either() -> None:
    calls: list[str] = []

    @rb.workflow(name="job-async")
    async def sync() -> dict:
        calls.append("ran")
        return {"ok": True}

    assert sync._build_step_graph() is None
    assert calls == []


def test_step_referencing_workflow_still_traces() -> None:
    project = rb.Project("tracing-check")

    @project.step(name="load")
    def load(site_id: str) -> dict:
        raise AssertionError("a traced step call must not run the step body")

    @project.workflow(name="forecast")
    def forecast(site_id: str) -> dict:
        return {"weather": load(site_id)}

    # ephemeral=True: same guarded tracing path, without requiring the steps to
    # be deployed first (non-ephemeral tracing insists on registered step IDs).
    graph = forecast._build_step_graph(ephemeral=True)
    assert graph is not None
    assert [node["node_key"] for node in graph["nodes"]] == ["load"]


def test_step_referencing_workflow_still_rejects_runtime_branching() -> None:
    project = rb.Project("tracing-check")

    @project.step(name="load")
    def load(site_id: str) -> dict:
        raise AssertionError("a traced step call must not run the step body")

    @project.workflow(name="forecast")
    def forecast(site_id: str) -> dict:
        result = load(site_id)
        if result["ready"]:  # subscripting a promise raises at trace time
            return result
        return {}

    with pytest.raises(RebaseWorkflowError, match="static step graph"):
        forecast._build_step_graph(ephemeral=True)
