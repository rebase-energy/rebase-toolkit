"""Warm-pool options: a setup hook per worker process (``setup=`` and
``rb.worker_state()``), a queue ``priority=``, and ``reuse=`` on maps. Each is
shipped only when declared, so a definition without them is byte-identical to
one written before they existed."""

from __future__ import annotations

import asyncio
import copy
import importlib
import json
import platform as platform_module
import sys
import types
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import requests
from http_stub import patch_client_http

import rebase as rb
from rebase import runtime
from tests.test_client import _compiled_graph

client_module = importlib.import_module("rebase.client")
cli = importlib.import_module("rebase.cli")

SETUP_RUNS: list[str] = []
FLAKY_ATTEMPTS: list[int] = []


def load_table() -> dict[str, list[int]]:
    SETUP_RUNS.append("load_table")
    return {"rows": [1, 2, 3]}


def warm() -> int:
    return 3


def optional_arguments(path: str = "model.bin", *args: Any, **kwargs: Any) -> str:
    return path


def needs_argument(path: str) -> str:
    return path


async def async_warm() -> int:
    return 1


def flaky() -> str:
    FLAKY_ATTEMPTS.append(1)
    if len(FLAKY_ATTEMPTS) == 1:
        raise RuntimeError("bundle not on disk yet")
    return "ready"


def asks_for_itself() -> Any:
    return rb.worker_state()


def make_nested():
    def nested() -> int:
        return 1

    return nested


class Holder:
    def method(self) -> int:
        return 1

    @staticmethod
    def static() -> int:
        return 1


def target(x: int = 0) -> dict:
    return {"x": x}


def square(x: int) -> dict:
    return {"value": x * x}


@rb.function(project="checks", name="registered")
def registered() -> int:
    return 1


def response(payload: Any, status: int = 200) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode()
    return result


@pytest.fixture(autouse=True)
def fresh_process(monkeypatch):
    """A process that has run no setup hook and is not a pool worker."""
    monkeypatch.setattr(runtime, "_worker_states", {})
    # None in sys.modules makes the import fail, wherever the suite runs.
    monkeypatch.setitem(sys.modules, "app.pool.handlers", None)
    SETUP_RUNS.clear()
    FLAKY_ATTEMPTS.clear()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(rb.Function, "_source_metadata_for_deploy", lambda *args: {})
    monkeypatch.setattr(rb.Workflow, "_source_metadata_for_deploy", lambda *args: {})
    monkeypatch.setattr(rb.Client, "_http_request", lambda *args, **kwargs: pytest.fail("unexpected HTTP request"))
    return rb.Client(api_key="test", api_url="https://example.invalid", workspace_id="test", environment_name="dev")


# --- declaration --------------------------------------------------------------


def test_setup_and_priority_are_recorded_on_functions_and_steps() -> None:
    project = rb.project("declared")
    function = project.function(setup=warm, priority="batch")(target)
    step = project.step(setup=optional_arguments, priority="interactive")(square)
    public_function = rb.function(setup=warm, priority="standard")(target)
    public_step = rb.step(setup=warm)(square)
    plain = project.function()(square)

    assert function.setup is warm
    assert function.setup_spec == {"source": "def warm() -> int:\n    return 3\n", "entrypoint": "warm"}
    assert function.priority == "batch"
    assert step.setup_spec is not None and step.setup_spec["entrypoint"] == "optional_arguments"
    assert step.priority == "interactive"
    assert public_function.setup_spec == function.setup_spec and public_function.priority == "standard"
    assert public_step.setup_spec == function.setup_spec and public_step.priority is None
    assert plain.setup is None and plain.setup_spec is None and plain.priority is None


@pytest.mark.parametrize(
    "setup,message",
    [
        (lambda: 1, "not a lambda"),
        (Holder().method, "not a method"),
        (Holder.static, "not a method"),
        (make_nested(), "at module level"),
        (async_warm, "async setup hooks are not supported yet"),
        (needs_argument, "must take no arguments; it requires path"),
        (platform_module.python_version, "same file"),
        (print, "defined with def; got builtin_function_or_method"),
        (registered, "is the Rebase target 'registered'.*remove its decorator"),
    ],
)
def test_setup_is_checked_where_it_is_declared(setup, message) -> None:
    project = rb.project("checks")
    with pytest.raises(TypeError, match=message):
        project.function(setup=setup)(target)
    with pytest.raises(TypeError, match=message):
        project.step(setup=setup)(target)
    with pytest.raises(TypeError, match=message):
        rb.function(setup=setup)(target)


def test_setup_source_must_be_readable() -> None:
    namespace: dict[str, Any] = {"__name__": __name__}
    exec("def generated():\n    return 1\n", namespace)
    with pytest.raises(rb.RebaseWorkflowError, match="Could not read source for setup hook 'generated'"):
        rb.project("checks").function(setup=namespace["generated"])(target)


def test_priority_and_reuse_values_are_checked(client, monkeypatch) -> None:
    project = rb.Project("checks", client=client)
    with pytest.raises(ValueError, match="priority must be 'interactive', 'standard' or 'batch'"):
        project.function(priority="urgent")(target)
    with pytest.raises(ValueError, match="priority must be"):
        project.step(priority="low")(target)
    function = project.function()(square)
    with pytest.raises(rb.RebaseWorkflowError, match="reuse must be True, False or None; got str"):
        not_a_bool: Any = "yes"
        list(function.map([1], reuse=not_a_bool))

    def build(project: Any) -> None:
        @project.step(name="each")
        def each(site: str) -> dict:
            return {"site": site}

        @project.workflow(name="w")
        def w(sites: list) -> list:
            return each.map(sites, reuse=1)

    with pytest.raises(rb.RebaseWorkflowError, match="reuse must be True, False or None; got int"):
        _compiled_graph(monkeypatch, build)


# --- what is shipped ------------------------------------------------------------


def test_step_nodes_carry_setup_priority_and_reuse_only_when_declared(monkeypatch) -> None:
    def build(project: Any) -> None:
        @project.step(name="plain")
        def plain() -> list:
            return ["a", "b"]

        @project.step(name="warmed", setup=warm, priority="interactive")
        def warmed(site: str) -> dict:
            return {"site": site}

        @project.step(name="each")
        def each(site: str) -> dict:
            return {"site": site}

        @project.workflow(name="w")
        def w() -> dict:
            sites = plain()
            return {
                "warmed": warmed.map(sites, reuse=True),
                "each": each.map(sites),
                "fresh": each.map(sites, reuse=False),
                "single": warmed("x"),
            }

    nodes = {node["node_key"]: node for node in _compiled_graph(monkeypatch, build)["nodes"]}
    setup = {"source": "def warm() -> int:\n    return 3\n", "entrypoint": "warm"}
    for key in ("plain", "each"):
        assert not {"setup", "priority", "reuse"} & nodes[key].keys()
    assert nodes["warmed"]["kind"] == "map"
    assert (nodes["warmed"]["setup"], nodes["warmed"]["priority"], nodes["warmed"]["reuse"]) == (
        setup,
        "interactive",
        True,
    )
    assert nodes["each_2"]["reuse"] is False and "setup" not in nodes["each_2"]
    assert nodes["warmed_2"]["setup"] == setup and nodes["warmed_2"]["priority"] == "interactive"
    assert "reuse" not in nodes["warmed_2"]


@pytest.mark.parametrize("reuse", [None, True, False])
def test_map_requests_carry_reuse_only_when_chosen(monkeypatch, reuse) -> None:
    observed: list[dict[str, Any]] = []

    def fake_stream_request(method: str, path: str, **kwargs: Any):
        observed.append(kwargs["json"])
        yield {"type": "item", "index": 0, "status": "succeeded", "result": {"value": 1}}

    client = rb.Client(api_key="rbw_test", api_url="https://workflows.example.com")
    monkeypatch.setattr(client, "stream_request", fake_stream_request)
    function = rb.Function(square, project="default", client=client)
    function.id = "function-id"
    step = rb.Step(square, project="default", client=client, function_id="step-id")

    assert list(function.map([1], reuse=reuse)) == [{"value": 1}]
    assert list(step.map([1], reuse=reuse)) == [{"value": 1}]  # outside a body: Function.map
    for payload in observed:
        assert ("reuse" in payload) == (reuse is not None)
        assert payload.get("reuse") == reuse


def test_update_function_sends_setup_and_priority_only_when_given(client, monkeypatch) -> None:
    bodies: list[dict[str, Any]] = []
    patch_client_http(monkeypatch, lambda method, url, **kwargs: bodies.append(kwargs["json"]) or response({"id": "f"}))
    spec = {"source": "def warm():\n    return 3\n", "entrypoint": "warm"}

    client.update_function("f", source_code="def f(): pass")
    client.update_function("f", setup=spec, priority="batch")
    client.update_function("f", setup=None, priority=None)  # explicit None clears

    assert not {"setup", "priority"} & bodies[0].keys()
    assert (bodies[1]["setup"], bodies[1]["priority"]) == (spec, "batch")
    assert bodies[2]["setup"] is None and bodies[2]["priority"] is None


@pytest.mark.parametrize("echoed", [True, False])
def test_a_lost_write_is_confirmed_only_when_the_server_echoes_the_declared_options(client, echoed) -> None:
    spec = {"source": "def warm():\n    return 3\n", "entrypoint": "warm"}
    payload = {"source_code": "def f(): pass", "setup": spec, "priority": "batch", "environment": "dev"}
    resource: dict[str, Any] = {"id": "f", "source_code": "def f(): pass"}
    if echoed:
        resource.update(setup=spec, priority="batch")
    # Missing evidence is never a match: a server that drops the fields cannot confirm them.
    assert (client._confirmed_definition(resource, payload, target_type="function") is not None) is echoed


# --- deploy against a server that stores what it knows -----------------------------


class Platform:
    """Functions and workflows as the API stores them, with comparison as the API
    does it: every key sent must be stored and equal. With ``echoes=False`` the
    server accepts the warm-pool fields but neither stores nor echoes them."""

    FIELDS = ("setup", "priority")
    NODE_FIELDS = ("setup", "priority", "reuse")

    def __init__(self, *, echoes: bool) -> None:
        self.echoes = echoes
        self.stored: dict[str, dict[str, dict[str, Any]]] = {"functions": {}, "workflows": {}}
        self.writes: list[tuple[str, str]] = []
        self.bodies: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.versions: Counter[str] = Counter()

    def _kept(self, payload: dict[str, Any]) -> dict[str, Any]:
        kept = {key: copy.deepcopy(value) for key, value in payload.items() if key != "environment"}
        if not self.echoes:
            for key in self.FIELDS:
                kept.pop(key, None)
            for node in (kept.get("step_graph") or {}).get("nodes", []):
                for key in self.NODE_FIELDS:
                    node.pop(key, None)
        return kept

    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        payload = kwargs.get("json") or {}
        parts = path.strip("/").split("/")
        if path == "/projects":
            return response([{"id": "p", "name": "fleet"}])
        if parts[-1] == "compare-deployments":
            rows = []
            for item in payload["items"]:
                stored = self.stored[parts[0]][item["id"]]
                definition = {key: value for key, value in item["definition"].items() if key != "environment"}
                unchanged = all(key in stored and stored[key] == value for key, value in definition.items())
                rows.append({"id": item["id"], "unchanged": unchanged, "resource": stored if unchanged else None})
            return response({"items": rows})
        if method == "GET":
            if parts[0] == "projects":
                return response(list(self.stored[parts[-1]].values()))
            return response(self.stored[parts[0]][parts[1]])
        kind = parts[-1] if parts[0] == "projects" else parts[0]
        name = payload["name"] if method == "POST" else parts[1]
        self.writes.append((method, f"/{kind}/{name}"))
        self.bodies.setdefault((kind, name), []).append(copy.deepcopy(payload))
        previous = self.stored[kind].get(name)
        kept = self._kept(payload)
        if previous is None or any(previous.get(key) != value for key, value in kept.items()):
            self.versions[name] += 1
        resource = {**(previous or {}), **kept, "id": name, "name": name}
        resource["current_version_id"] = f"{name}-v{self.versions[name]}"
        self.stored[kind][name] = resource
        return response(resource)


def _deploy_source(*, options: bool, table: int = 3, priority: str = "batch") -> str:
    function_options = f'setup=warm, priority="{priority}"' if options else ""
    map_options = ", reuse=True" if options else ""
    return f"""
import rebase as rb

project = rb.project("fleet")


def warm():
    return {{"table": {table}}}


@project.function({function_options})
def predict(x: int = 0) -> dict:
    import rebase as rb

    return {{"y": x * rb.worker_state()["table"]}}


@project.step({function_options})
def score(site: str) -> dict:
    return {{"site": site}}


@project.workflow()
def nightly(sites: list = ["a", "b"]) -> list:
    return score.map(sites{map_options})
"""


@pytest.fixture
def fleet(client, monkeypatch, tmp_path):
    """Deploy a file as `rebase deploy` loads it, against a `Platform`."""
    servers: list[Platform] = []

    def serve(echoes: bool) -> Platform:
        servers.append(Platform(echoes=echoes))
        monkeypatch.setattr(
            rb.Client, "_http_request", lambda self, method, path, **kwargs: servers[-1].request(method, path, **kwargs)
        )
        return servers[-1]

    def deploy(source: str) -> Any:
        path = tmp_path / "fleet.py"
        path.write_text(source, encoding="utf-8")
        module = cli._load_module(path)
        module.project.deploy()
        return module.project

    monkeypatch.setattr(client_module, "_default_client", client)
    return serve, deploy


def _node(platform: Platform, key: str) -> dict[str, Any]:
    graph = platform.bodies[("workflows", "nightly")][-1]["step_graph"]
    return next(node for node in graph["nodes"] if node["node_key"] == key)


def test_redeploy_without_options_writes_nothing_new_on_a_server_that_omits_them(fleet) -> None:
    serve, deploy = fleet
    platform = serve(echoes=False)
    deploy(_deploy_source(options=False))
    platform.writes.clear()

    project = deploy(_deploy_source(options=False))

    # A function is always sent and the server decides; it mints nothing new.
    assert platform.writes == [("PATCH", "/functions/predict")]
    assert platform.versions == Counter({"predict": 1, "score": 1, "nightly": 1})
    assert project.deployment_report.counts["unchanged"] == 2  # the step and the workflow
    for bodies in platform.bodies.values():
        for body in bodies:
            assert not {"setup", "priority"} & body.keys()
            for node in (body.get("step_graph") or {}).get("nodes", []):
                assert not {"setup", "priority", "reuse"} & node.keys()


def test_declared_options_are_unchanged_on_redeploy_and_rewritten_when_they_change(fleet) -> None:
    serve, deploy = fleet
    platform = serve(echoes=True)
    deploy(_deploy_source(options=True))

    setup = {"source": 'def warm():\n    return {"table": 3}\n', "entrypoint": "warm"}
    created = platform.bodies[("functions", "predict")][0]
    assert (created["setup"], created["priority"]) == (setup, "batch")
    # A step's options travel in its node, not in the function it is registered as.
    assert not {"setup", "priority"} & platform.bodies[("functions", "score")][0].keys()
    assert (_node(platform, "score")["setup"], _node(platform, "score")["priority"]) == (setup, "batch")
    assert _node(platform, "score")["reuse"] is True

    platform.writes.clear()
    project = deploy(_deploy_source(options=True))
    assert platform.writes == [("PATCH", "/functions/predict")]
    assert platform.versions == Counter({"predict": 1, "score": 1, "nightly": 1})
    assert project.deployment_report.counts["unchanged"] == 2

    platform.writes.clear()
    deploy(_deploy_source(options=True, table=30))  # the setup hook's source changed
    assert ("PATCH", "/workflows/nightly") in platform.writes
    assert platform.versions == Counter({"predict": 2, "score": 1, "nightly": 2})
    assert "30" in _node(platform, "score")["setup"]["source"]

    deploy(_deploy_source(options=True, table=30, priority="interactive"))
    assert platform.versions == Counter({"predict": 3, "score": 1, "nightly": 3})
    assert platform.stored["functions"]["predict"]["priority"] == "interactive"
    assert _node(platform, "score")["priority"] == "interactive"


def test_a_declared_option_the_server_does_not_echo_reads_as_changed(fleet) -> None:
    serve, deploy = fleet
    platform = serve(echoes=False)
    deploy(_deploy_source(options=True))
    platform.writes.clear()
    deploy(_deploy_source(options=True))
    # The API must store and echo what it accepts, or every deploy rewrites.
    assert ("PATCH", "/workflows/nightly") in platform.writes
    assert platform.versions["nightly"] == 1  # the fake drops the fields, so its own copy did not change


@pytest.mark.parametrize("echoes", [True, False])
def test_removing_a_declared_option_clears_only_what_the_server_echoes(fleet, echoes) -> None:
    serve, deploy = fleet
    platform = serve(echoes=echoes)
    deploy(_deploy_source(options=True))
    deploy(_deploy_source(options=False))

    update = platform.bodies[("functions", "predict")][-1]
    if echoes:
        assert update["setup"] is None and update["priority"] is None
        assert platform.stored["functions"]["predict"]["setup"] is None
        # The graph is replaced whole, so the node simply loses them.
        assert not {"setup", "priority", "reuse"} & _node(platform, "score").keys()
    else:
        # Never sent undeclared to a server that does not echo them.
        assert not {"setup", "priority"} & update.keys()


# --- worker_state() ---------------------------------------------------------------


def test_worker_state_runs_the_hook_once_per_process_across_local_calls() -> None:
    project = rb.project("local")

    @project.function(setup=load_table)
    def count() -> int:
        return len(rb.worker_state()["rows"])

    @project.step(setup=load_table)
    def first_row() -> int:
        return rb.worker_state()["rows"][0]

    @project.function(setup=load_table)
    def ignores_state() -> int:
        return 0

    assert ignores_state() == 0
    assert SETUP_RUNS == []  # lazily, on the first call that asks
    assert [count(), count(), first_row(), count()] == [3, 3, 1, 3]
    # One state per hook: functions and steps sharing it share one, as they share a pool template.
    assert SETUP_RUNS == ["load_table"]


def test_worker_state_serves_nested_and_async_local_calls() -> None:
    project = rb.project("local")

    @project.function(name="helper")
    def helper() -> list:
        return rb.worker_state()["rows"]

    @project.function(setup=load_table)
    def outer() -> list:
        return helper()

    @project.function(setup=load_table)
    async def total() -> int:
        await asyncio.sleep(0)
        return sum(rb.worker_state()["rows"])

    # A call without its own hook, made from one with a hook, sees that call's state.
    assert outer() == [1, 2, 3]
    assert asyncio.run(total()) == 6
    assert SETUP_RUNS == ["load_table"]


def test_a_hook_that_raises_runs_again_on_the_next_call() -> None:
    @rb.function(project="local", setup=flaky)
    def ready() -> str:
        return rb.worker_state()

    with pytest.raises(RuntimeError, match="not on disk yet"):
        ready()
    assert ready() == "ready" and ready() == "ready"
    assert len(FLAKY_ATTEMPTS) == 2


def test_worker_state_without_a_setup_hook_is_a_clear_error() -> None:
    @rb.function(project="local", name="bare")
    def bare() -> Any:
        return rb.worker_state()

    with pytest.raises(rb.RebaseWorkflowError, match="'bare' declares no setup hook"):
        bare()
    with pytest.raises(rb.RebaseWorkflowError, match="call it inside a function or step declared with setup="):
        rb.worker_state()

    @rb.function(project="local", setup=asks_for_itself)
    def circular() -> Any:
        return rb.worker_state()

    with pytest.raises(rb.RebaseWorkflowError, match="'asks_for_itself' called rb.worker_state"):
        circular()


def test_rebase_run_local_runs_the_hook_once_for_the_whole_run(tmp_path: Path, capsys) -> None:
    workflow_file = tmp_path / "rows.py"
    workflow_file.write_text(
        """
import rebase as rb

project = rb.project("local")
RUNS = []


def load_rows():
    RUNS.append(1)
    return {"rows": [10, 20, 30]}


@project.step(setup=load_rows)
def row(i: int) -> dict:
    import rebase as rb

    return {"value": rb.worker_state()["rows"][i], "setup_runs": len(RUNS)}


@project.workflow()
def rows() -> dict:
    return {"first": row(0), "last": row(2)}
""",
        encoding="utf-8",
    )

    assert cli.main(["run", str(workflow_file), "--local"]) == 0
    result = json.loads(capsys.readouterr().out.split("locally...", 1)[1])
    assert result == {"first": {"value": 10, "setup_runs": 1}, "last": {"value": 30, "setup_runs": 1}}


def test_worker_state_inside_the_pool_delegates_to_the_backend(monkeypatch) -> None:
    handlers = types.SimpleNamespace(worker_state=lambda: {"rows": [7]})
    monkeypatch.setitem(sys.modules, "app.pool.handlers", handlers)
    assert rb.worker_state() == {"rows": [7]}

    @rb.function(project="pool", setup=load_table)
    def first() -> int:
        import rebase as rb

        return rb.worker_state()["rows"][0]

    # A pool worker executes the shipped source, not the decorated object.
    namespace: dict[str, Any] = {}
    exec(compile(first.source_code or "", "<rebase-function first>", "exec"), namespace)
    assert namespace["first"]() == 7
    assert SETUP_RUNS == []
    # When the SDK runs the function itself it answers from the declared hook.
    assert first() == 1 and SETUP_RUNS == ["load_table"]


def test_a_broken_backend_import_is_not_mistaken_for_no_pool(monkeypatch) -> None:
    monkeypatch.delitem(sys.modules, "app.pool.handlers")

    def import_module(name: str) -> Any:
        raise ModuleNotFoundError("No module named 'pydantic'", name="pydantic")

    monkeypatch.setattr(runtime, "importlib", types.SimpleNamespace(import_module=import_module))
    with pytest.raises(ModuleNotFoundError, match="pydantic"):
        rb.worker_state()


# --- rebase deploy --plan ------------------------------------------------------------


def _plan(client, monkeypatch, tmp_path: Path, capsys, source: str) -> str:
    path = tmp_path / "plan.py"
    path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(client_module, "_default_client", client)
    monkeypatch.setattr(cli, "Client", lambda: client)
    monkeypatch.setattr(cli, "_environment_policy", lambda *args: {})
    monkeypatch.setattr(cli, "_policy_requires_gitops", lambda *args: False)
    assert cli.main(["deploy", str(path), "--plan"]) == 0
    return capsys.readouterr().out


def test_deploy_plan_shows_declared_execution_options(client, monkeypatch, tmp_path, capsys) -> None:
    output = _plan(client, monkeypatch, tmp_path, capsys, _deploy_source(options=True, priority="interactive"))
    lines = output.splitlines()

    def row(target: str) -> str:
        return next(line for line in lines if f" {target} " in line)

    assert "Deploy Plan" in output and "Execution options" in output
    assert "function" in row("fleet/predict") and "warm" in row("fleet/predict")
    assert "interactive" in row("fleet/predict")
    assert "step" in row("fleet/score") and "warm" in row("fleet/score")
    assert "map" in row("fleet/nightly/score") and "True" in row("fleet/nightly/score")


def test_deploy_plan_without_options_prints_only_the_plan(client, monkeypatch, tmp_path, capsys) -> None:
    output = _plan(client, monkeypatch, tmp_path, capsys, _deploy_source(options=False))
    assert "Deploy Plan" in output
    assert "Execution options" not in output
