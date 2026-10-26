"""The step graph's second schema: dependencies follow the data, steps fan out
with map, branch with when, order with after, and carry their own retry
backoff, size and runtime."""

from __future__ import annotations

from typing import Any

import pytest

import rebase as rb
from rebase.client import RebaseWorkflowError
from tests.test_client import _compiled_graph


def _nodes(monkeypatch, build) -> dict[str, dict[str, Any]]:
    return {node["node_key"]: node for node in _compiled_graph(monkeypatch, build)["nodes"]}


def test_map_fans_a_step_out_over_another_steps_output(monkeypatch) -> None:
    def build(project: Any) -> None:
        @project.step(name="list-sites")
        def list_sites() -> list:
            return ["a", "b"]

        @project.step(name="fetch-site", retries=1, retry_backoff=10)
        def fetch_site(site: str, day: str) -> dict:
            return {"site": site}

        @project.step(name="combine")
        def combine(parts: list) -> dict:
            return {"n": len(parts)}

        @project.workflow(name="per-site")
        def per_site(day: str = "today") -> dict:
            sites = list_sites()
            parts = fetch_site.map(sites, day=day, max_concurrency=4)
            return combine(parts)

    nodes = _nodes(monkeypatch, build)
    fan = nodes["fetch_site"]
    assert fan["kind"] == "map"
    assert fan["items_binding"] == {"type": "node_output", "node_key": "list_sites"}
    assert fan["input_bindings"] == {"site": {"type": "map_item"}, "day": {"type": "parameter", "name": "day"}}
    assert fan["upstream_node_keys"] == ["list_sites"] and fan["max_concurrency"] == 4
    assert fan["retry_policy"] == {
        "retries": 1,
        "backoff_seconds": 10.0,
        "backoff_multiplier": 2.0,
        "max_backoff_seconds": 160.0,
    }
    assert nodes["combine"]["input_bindings"] == {"parts": {"type": "node_output", "node_key": "fetch_site"}}


def test_map_refuses_a_mixed_literal_list_and_an_unknown_parameter(monkeypatch) -> None:
    def build_mixed(project: Any) -> None:
        @project.step(name="one")
        def one() -> dict:
            return {}

        @project.step(name="each")
        def each(item: dict) -> dict:
            return item

        @project.workflow(name="mixed")
        def mixed() -> dict:
            return {"x": each.map([one(), {"literal": True}])}

    with pytest.raises(RebaseWorkflowError, match="cannot mix in step outputs"):
        _compiled_graph(monkeypatch, build_mixed)

    def build_unknown(project: Any) -> None:
        @project.step(name="each")
        def each(item: dict) -> dict:
            return item

        @project.workflow(name="unknown")
        def unknown(items: list) -> dict:
            return {"x": each.map(items, parameter="nope")}

    with pytest.raises(RebaseWorkflowError, match="no parameter 'nope'"):
        _compiled_graph(monkeypatch, build_unknown)


def test_when_conditions_a_step_and_rb_when_switches_between_two(monkeypatch) -> None:
    def build(project: Any) -> None:
        @project.step(name="quick")
        def quick(x: int) -> dict:
            return {"x": x}

        @project.step(name="slow")
        def slow(x: int) -> dict:
            return {"x": x}

        @project.step(name="clean")
        def clean(v: dict) -> dict:
            return v

        @project.step(name="use")
        def use(v: dict) -> dict:
            return v

        @project.workflow(name="branching")
        def branching(fast: bool = True, dirty: bool = False, x: int = 1) -> dict:
            picked = rb.when(fast, then=quick(x), otherwise=slow(x))
            cleaned = clean.when(dirty, equals=True)(picked)
            return {"used": use(picked), "cleaned": cleaned}

    nodes = _nodes(monkeypatch, build)
    cond = {"binding": {"type": "parameter", "name": "fast"}, "op": "truthy"}
    assert nodes["quick"]["condition"] == {**cond, "negate": False}
    assert nodes["slow"]["condition"] == {**cond, "negate": True}
    switch = nodes["when"]
    assert switch["kind"] == "switch" and switch["condition"] == cond
    assert (switch["then_node_key"], switch["otherwise_node_key"]) == ("quick", "slow")
    assert switch["upstream_node_keys"] == ["quick", "slow"]
    assert nodes["clean"]["condition"] == {
        "binding": {"type": "parameter", "name": "dirty"},
        "op": "eq",
        "value": True,
        "negate": False,
    }
    assert nodes["use"]["input_bindings"] == {"v": {"type": "node_output", "node_key": "when"}}


def test_when_needs_a_traced_value_and_plain_branches(monkeypatch) -> None:
    def build(project: Any) -> None:
        @project.step(name="a")
        def a() -> dict:
            return {}

        @project.workflow(name="bad")
        def bad() -> dict:
            return {"x": rb.when(True, then=a(), otherwise=a())}

    with pytest.raises(RebaseWorkflowError, match="a step's output or a workflow parameter"):
        _compiled_graph(monkeypatch, build)


def test_steps_carry_their_own_size_and_runtime_over_the_workflows(monkeypatch) -> None:
    def build(project: Any) -> None:
        @project.step(name="small")
        def small() -> dict:
            return {}

        @project.step(name="big", cpu=4, memory="8Gi")
        def big(v: dict) -> dict:
            return v

        @project.workflow(name="sized", mode="job", resources={"gpu": "none"})
        def sized() -> dict:
            return big(small())

    nodes = _nodes(monkeypatch, build)
    assert nodes["small"]["resource_policy"] == {"gpu": "none"}
    assert nodes["big"]["resource_policy"] == {"gpu": "none", "cpu": "4", "memory": "8Gi"}
    assert nodes["big"]["runtime"] == "job"


def test_step_options_are_validated_at_declaration() -> None:
    project = rb.project("opts")
    with pytest.raises(ValueError, match="retry_backoff"):
        project.step(retry_backoff=-1)(lambda: None)
    with pytest.raises(ValueError, match="runtime"):
        project.step(runtime="lambda")(lambda: None)


def test_after_and_when_outside_a_body_are_refused() -> None:
    project = rb.project("outside")

    @project.step(name="s")
    def s() -> dict:
        return {}

    with pytest.raises(RebaseWorkflowError, match="only meaningful inside a workflow body"):
        rb.when(None, then=None, otherwise=None)  # type: ignore[arg-type]
    with pytest.raises(RebaseWorkflowError, match="takes the results of other step calls"):
        s.after("not a promise")
