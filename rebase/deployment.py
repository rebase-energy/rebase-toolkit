"""Per-invocation deployment results, shared by the SDK and CLI."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal

DeploymentStatus = Literal["succeeded", "failed", "uncertain", "unattempted"]


@dataclass
class DeploymentResult:
    """Last outcome for one target in one deployment environment.

    Success means the control plane acknowledged the definition, not that its
    runtime has finished starting. Steps are reported separately from workflows.
    """

    target_type: str
    name: str
    project: str | None
    environment: str
    status: DeploymentStatus = "unattempted"
    id: str | None = None
    error: str | None = None


@dataclass
class DeploymentReport:
    """Results accumulated before completion, failure, or interruption."""

    results: list[DeploymentResult] = field(default_factory=list)
    error: str | None = None
    _targets: dict[tuple[int, str], DeploymentResult] = field(default_factory=dict, repr=False)

    @property
    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys(("succeeded", "failed", "uncertain", "unattempted"), 0)
        for result in self.results:
            counts[result.status] += 1
        return counts

    def _plan(
        self, target: object, *, target_type: str, name: str, project: str | None, environment: str
    ) -> DeploymentResult:
        key = (id(target), environment)
        if key not in self._targets:
            result = DeploymentResult(target_type, name, project, environment)
            self._targets[key] = result
            self.results.append(result)
        return self._targets[key]


_report_context: ContextVar[DeploymentReport | None] = ContextVar("rebase_deployment_report", default=None)
_target_context: ContextVar[DeploymentResult | None] = ContextVar("rebase_deployment_target", default=None)


@contextmanager
def _deployment_scope() -> Iterator[DeploymentReport]:
    existing = _report_context.get()
    if existing is not None:
        yield existing
        return
    report = DeploymentReport()
    token = _report_context.set(report)
    try:
        yield report
    finally:
        _report_context.reset(token)


def _mark_uncertain(message: str) -> None:
    target = _target_context.get()
    if target is not None:
        target.status = "uncertain"
        target.error = message
