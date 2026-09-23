"""Exercise real grid-pipeline definitions against an in-memory control plane.

Run with the grid repository's Python environment. No platform or upstream HTTP
is allowed. This verifies client behavior and payloads, not production latency.

Using the grid repository's Python environment, run::

    python /path/to/rebase-toolkit/scripts/verify_grid_deploy.py \
        --grid-repo /path/to/rebase-grid --output grid-results.json

Use --toolkit-repo with --record-only to record results from an older SDK checkout.
"""

from __future__ import annotations

import argparse
import copy
import importlib
import inspect
import json
import os
import runpy
import time
from collections import Counter
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch

RESOURCE_FIELDS = set(
    [
        "name",
        "description",
        "flow_ref",
        "source_code",
        "entrypoint",
        "schedule",
        "trigger",
        "default_parameters",
        "mode",
        "isolation",
        "env",
        "secrets",
        "buckets",
        "cloud_run_cpu",
        "cloud_run_memory",
        "timeout_seconds",
        "enabled",
        "endpoint",
    ]
)
VERSION_FIELDS = set(
    [
        "source_code",
        "entrypoint",
        "flow_ref",
        "step_graph",
        "required_parameters",
        "schedule",
        "trigger",
        "default_parameters",
        "mode",
        "isolation",
        "buckets",
        "cloud_run_cpu",
        "cloud_run_memory",
        "timeout_seconds",
        "enabled",
        "source_mode",
        "repo_owner",
        "repo_name",
        "repo_path",
        "source_path",
        "git_commit_sha",
        "git_branch",
        "git_tag",
        "git_dirty",
    ]
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grid-repo", type=Path, required=True)
    parser.add_argument("--toolkit-repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--dependencies", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--record-only", action="store_true", help="Record a baseline without asserting fixed behavior")
    args = parser.parse_args()
    args.output = args.output.resolve()
    import sys

    grid = args.grid_repo.resolve()
    sys.path[:0] = [str(args.toolkit_repo.resolve()), str(grid / "src")]
    if args.dependencies:
        sys.path.insert(0, str(args.dependencies.resolve()))
    import requests

    import rebase as rb

    sdk = importlib.import_module("rebase.client")
    os.chdir(grid)
    # The module normally fetches origin/main before a LIVE deploy. Here HTTP is
    # blocked, all credentials are fake, and writes only change in-memory dicts.
    os.environ["GRID_PIPELINE_ALLOW_BRANCH_DEPLOY"] = "1"
    index = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
    os.environ.update(
        {
            "GIT_CONFIG_COUNT": str(index + 1),
            f"GIT_CONFIG_KEY_{index}": "safe.directory",
            f"GIT_CONFIG_VALUE_{index}": grid.as_posix(),
        }
    )

    with ExitStack() as patches:

        def block_http(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("Live HTTP is forbidden in grid deployment verification")

        patches.enter_context(patch.object(requests.sessions.Session, "request", block_http))
        rb.configure(api_key="offline-verification", api_url="https://offline.invalid", environment_name="dev")
        # Hundreds of generated functions share one source file. Cache only their
        # identical git metadata during definition loading, outside the measurement.
        original_metadata = sdk._git_metadata_for
        metadata: dict[str | None, dict[str, Any]] = {}

        def metadata_for(fn: Any) -> dict[str, Any]:
            path = inspect.getsourcefile(fn)
            if path not in metadata:
                metadata[path] = original_metadata(fn)
            return dict(metadata[path])

        with patch.object(sdk, "_git_metadata_for", metadata_for):
            module = runpy.run_path(str(grid / "deploy/rebase/grid_pipeline.py"))
        project = module["project"]
        targets = [w.name for w in project._workflows]
        print(
            json.dumps(
                {"loaded_workflows": len(targets), "steps": len(project._steps), "apps": len(project._asgi_apps)}
            ),
            flush=True,
        )
        patches.enter_context(patch.object(sdk.time, "sleep", lambda _seconds: None))

        class ControlPlane:
            def __init__(self, scenario: str) -> None:
                self.scenario = scenario
                self.calls: Counter[str] = Counter()
                self.timeouts: set[Any] = set()
                self.fired = False
                self.versions: dict[str, dict[str, Any]] = {
                    name + "-old": {"id": name + "-old", "workflow_id": name, "source_code": "old source"}
                    for name in targets
                }
                self.workflows = (
                    {
                        name: {
                            "id": name,
                            "name": name,
                            "source_code": "old source",
                            "default_parameters": {"commit_sha": "old"},
                            "current_version_id": name + "-old",
                        }
                        for name in targets
                    }
                    if scenario != "create_lost_response"
                    else {}
                )
                self.functions = {
                    step.name: {"id": step.name, "name": step.name, "current_version_id": step.name + "-old"}
                    for step in project._steps
                }
                self.function_versions: dict[str, dict[str, Any]] = {}
                self.function_writes = 0
                self.landed: list[str] = []
                self.workflow_writes = 0
                self.fault_name = targets[105]

            def response(self, payload: Any, status: int = 200) -> Any:
                result = requests.Response()
                result.status_code = status
                result._content = json.dumps(payload).encode()
                return result

            def request(self, method: str, path: str, **kwargs: Any) -> Any:
                category = path.split("/")[1]
                self.calls[f"{method} {category}"] += 1
                payload = kwargs.get("json", {})
                if method == "GET":
                    if path == "/projects":
                        if self.scenario == "lookup_500" and not self.fired:
                            self.fired = True
                            return self.response({"detail": "injected project lookup failure"}, 500)
                        return self.response([{"id": "p", "name": project.name, "description": project.description}])
                    if path == "/projects/p/functions":
                        return self.response(list(self.functions.values()))
                    if path == "/projects/p/workflows":
                        return self.response(list(self.workflows.values()))
                    if path == "/projects/p/asgi-apps":
                        return self.response([{"id": "dashboard", "name": "dashboard"}])
                    if category == "secrets":
                        return self.response({"secret_refs": {"KEY": "offline-secret-reference"}})
                    if category in {"buckets", "volumes"}:
                        return self.response({"id": path.split("/")[-1], "name": path.split("/")[-1]})
                    if category == "functions":
                        parts = path.split("/")
                        if len(parts) == 5 and parts[3] == "versions":
                            return self.response(self.function_versions[parts[4]])
                        return self.response(self.functions[parts[2]])
                    if category == "workflows":
                        parts = path.split("/")
                        if len(parts) == 5 and parts[3] == "versions":
                            return self.response(self.versions[parts[4]])
                        return self.response(self.workflows[parts[2]])
                if method == "PATCH" and category == "functions":
                    name = path.split("/")[-1]
                    self.function_writes += 1
                    version_id = name + "-new"
                    self.function_versions[version_id] = {
                        **copy.deepcopy(payload),
                        "id": version_id,
                        "function_id": name,
                    }
                    self.functions[name].update(
                        {
                            k: copy.deepcopy(v)
                            for k, v in payload.items()
                            if not k.startswith(("git_", "repo_"))
                            and k not in {"environment", "source_path", "source_mode"}
                        },
                        current_version_id=version_id,
                    )
                    image_spec = self.functions[name].get("image_spec")
                    if isinstance(image_spec, dict) and image_spec.get("kind") == "python":
                        image_spec.setdefault("runtime", "python")
                    if self.scenario == "step_lost_response" and self.function_writes == 2:
                        self.fired = True
                        raise requests.ConnectionError("injected shared-step connection reset after commit")
                    return self.response(self.functions[name])
                if method == "PATCH" and category == "asgi-apps":
                    return self.response({"id": "dashboard", "name": "dashboard", **payload})
                if (method == "PATCH" and category == "workflows") or (
                    method == "POST" and path == "/projects/p/workflows"
                ):
                    self.workflow_writes += 1
                    self.timeouts.add(kwargs["timeout"])
                    name = payload.get("name", path.split("/")[-1])
                    fault = name == self.fault_name and not self.fired
                    if fault and self.scenario == "connect_timeout":
                        self.fired = True
                        raise requests.ConnectTimeout("injected before connection")
                    if name == self.fault_name and self.scenario == "unconfirmed_500":
                        self.fired = True
                        return self.response({"detail": "injected uncommitted server failure"}, 500)
                    # A 45-second registration exceeds the old 30-second timeout.
                    if fault and self.scenario == "slow_write" and kwargs["timeout"] < 45:
                        self.fired = True
                        raise requests.ReadTimeout("simulated 45-second workflow write")
                    previous = self.workflows.get(name, {"id": name, "name": name})
                    version_id = name + "-new"
                    resource = {
                        **previous,
                        **{k: copy.deepcopy(v) for k, v in payload.items() if k in RESOURCE_FIELDS},
                        "current_version_id": version_id,
                    }
                    if payload.get("mode") == "job":
                        resource.update(isolation=None, run_type="long")
                    self.workflows[name] = resource
                    self.versions[version_id] = {
                        "id": version_id,
                        "workflow_id": name,
                        **{k: copy.deepcopy(v) for k, v in payload.items() if k in VERSION_FIELDS},
                    }
                    self.landed.append(name)
                    if fault and self.scenario in {"update_lost_response", "create_lost_response"}:
                        self.fired = True
                        raise requests.ReadTimeout("injected after successful commit")
                    return self.response(resource)
                raise AssertionError(f"Unexpected verification request: {method} {path}")

        outcomes: list[dict[str, Any]] = []
        try:
            for scenario in (
                "clean",
                "slow_write",
                "connect_timeout",
                "lookup_500",
                "step_lost_response",
                "update_lost_response",
                "create_lost_response",
                "unconfirmed_500",
            ):
                server = ControlPlane(scenario)
                patches.enter_context(
                    patch.object(
                        rb.Client,
                        "_http_request",
                        lambda _self, method, path, _server=server, **kwargs: _server.request(method, path, **kwargs),
                    )
                )
                started = time.monotonic()
                error = None
                try:
                    project.deploy(environment="dev")
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                report = getattr(project, "deployment_report", None)
                workflow_statuses = (
                    Counter(r.status for r in report.results if r.target_type == "workflow") if report else {}
                )
                outcome = {
                    "scenario": scenario,
                    "workflow_count": len(targets),
                    "landed": len(server.landed),
                    "workflow_writes": server.workflow_writes,
                    "function_writes": server.function_writes,
                    "fault_injected": server.fired,
                    "unique_landed": len(set(server.landed)),
                    "workflow_statuses": dict(workflow_statuses),
                    "requests": sum(server.calls.values()),
                    "request_categories": dict(server.calls),
                    "workflow_timeouts": sorted(server.timeouts),
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                    "error": error,
                    "fault_workflow": server.fault_name,
                }
                outcomes.append(outcome)
                print(json.dumps(outcome), flush=True)
        finally:
            patches.close()
        result = {
            "grid_commit": module["COMMIT_SHA"],
            "toolkit_source": rb.__file__,
            "live_http_blocked": True,
            "metadata_cached_only_during_import": True,
            "outcomes": outcomes,
        }
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        if not args.record_only:
            for outcome in outcomes:
                assert outcome["workflow_timeouts"] == [300], outcome
                assert outcome["landed"] == outcome["unique_landed"], outcome
                assert outcome["requests"] < len(targets) + 48, outcome
                if outcome["scenario"] not in {"clean", "slow_write"}:
                    assert outcome["fault_injected"], outcome
                if outcome["scenario"] == "unconfirmed_500":
                    assert outcome["landed"] == 105 and outcome["workflow_writes"] == 106, outcome
                    assert outcome["workflow_statuses"] == {
                        "succeeded": 105,
                        "uncertain": 1,
                        "unattempted": len(targets) - 106,
                    }, outcome
                    assert outcome["error"] and outcome["error"].startswith("DeploymentError:"), outcome
                else:
                    assert outcome["error"] is None and outcome["landed"] == len(targets), outcome
                    assert outcome["function_writes"] == len(project._steps), outcome
                    assert outcome["workflow_statuses"] == {"succeeded": len(targets)}, outcome
                    extra_attempt = int(outcome["scenario"] == "connect_timeout")
                    assert outcome["workflow_writes"] == len(targets) + extra_attempt, outcome


if __name__ == "__main__":
    main()
