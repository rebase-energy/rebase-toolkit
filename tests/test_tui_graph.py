"""The graphs behind the TUI's `i` pane, read without a terminal or plotui."""

from __future__ import annotations

from rebase.brand import (
    BRAND_AMBER,
    BRAND_BRIGHT_GREEN,
    BRAND_CORAL_RED,
    BRAND_MAIN_GREEN,
    BRAND_MEDIUM_GRAY,
    BRAND_SLATE_BLUE,
)
from rebase.tui_graph import (
    EDGE_BRIGHT,
    GraphEdge,
    GraphNode,
    GraphSpec,
    dim,
    edge_colours,
    edge_summary,
    highlight_colours,
    node_colours,
    node_index,
    node_shapes,
    step_dag,
    trigger_dag,
    upstream_nodes,
    upstream_summary,
)

CHAIN = {
    "schema_version": 1,
    "nodes": [
        {"node_key": "fetch", "name": "fetch", "input_bindings": {}, "upstream_node_keys": []},
        {
            "node_key": "clean",
            "name": "clean-up",
            "input_bindings": {"raw": {"type": "node_output", "node_key": "fetch"}},
            "upstream_node_keys": ["fetch"],
        },
        {
            "node_key": "publish",
            "name": "publish",
            # Publishes what `fetch` returned; `clean` is only there because it ran before.
            "input_bindings": {
                "payload": {"type": "dict", "items": {"raw": {"type": "node_output", "node_key": "fetch"}}}
            },
            "upstream_node_keys": ["clean", "fetch", "dropped"],
        },
    ],
}


def _edges(spec: GraphSpec) -> list[tuple[str, str, str]]:
    return [(spec.nodes[e.src].key, spec.nodes[e.dst].key, e.kind) for e in spec.edges]


def test_step_dag_tells_data_edges_from_ordering_edges() -> None:
    spec = step_dag(CHAIN, None, title="chain")

    assert [node.label for node in spec.nodes] == ["fetch", "clean-up", "publish"]
    assert all(node.kind == "step" for node in spec.nodes)
    assert _edges(spec) == [
        ("fetch", "clean", "data"),
        ("clean", "publish", "order"),
        ("fetch", "publish", "data"),
    ]
    # `dropped` names no node, so it draws nothing rather than a node nobody declared.
    assert node_index(spec, "dropped") is None


def test_step_dag_colours_nodes_by_step_run_status_by_key_then_name() -> None:
    steps = [
        {"node_key": "fetch", "name": "fetch", "status": "failed", "attempt": 1},
        {"node_key": "fetch", "name": "fetch", "status": "succeeded", "attempt": 2},
        # The way older run rows arrive: a name and no key.
        {"name": "clean-up", "status": "running"},
    ]
    spec = step_dag(CHAIN, steps, title="chain")

    assert [node.state for node in spec.nodes] == ["succeeded", "running", "unrun"]
    assert node_colours(spec) == [BRAND_MAIN_GREEN, BRAND_BRIGHT_GREEN, BRAND_MEDIUM_GRAY]
    assert edge_colours(spec) == [EDGE_BRIGHT, BRAND_MEDIUM_GRAY, EDGE_BRIGHT]
    assert node_shapes(spec) == ["rounded", "rounded", "rounded"]
    # Colours are not structure: the same graph coloured differently repaints in place.
    assert spec.structure == step_dag(CHAIN, None, title="chain").structure


def test_step_dag_without_a_run_draws_the_definition_as_neutral() -> None:
    assert [node.state for node in step_dag(CHAIN, None, title="chain").nodes] == ["defined"] * 3
    assert [node.state for node in step_dag(CHAIN, [], title="chain").nodes] == ["unrun"] * 3
    assert node_colours(step_dag(CHAIN, None, title="chain")) == [BRAND_MEDIUM_GRAY] * 3


def test_step_dag_tolerates_no_graph() -> None:
    assert step_dag(None, None, title="none").nodes == ()
    assert step_dag({"nodes": "nope"}, None, title="none").nodes == ()
    assert step_dag({"nodes": [{"name": "keyless"}, "junk"]}, None, title="none").nodes == ()


def test_trigger_dag_inverts_bare_and_qualified_sources() -> None:
    workflows = [
        {"name": "forecast", "enabled": True, "paused": False},
        {
            "name": "publish",
            "enabled": True,
            "paused": True,
            "trigger": {"type": "on_workflow", "source": "forecast", "on": "success"},
        },
        {
            "name": "report",
            "enabled": False,
            "trigger": {"type": "on_workflow", "source": "trading/settle", "on": "completion", "active": False},
        },
    ]
    spec = trigger_dag(workflows, project_name="energy")

    assert [(node.key, node.label, node.state) for node in spec.nodes] == [
        ("energy/forecast", "forecast", "active"),
        ("energy/publish", "publish", "paused"),
        ("energy/report", "report", "disabled"),
        ("trading/settle", "trading/settle", "external"),
    ]
    assert spec.edges == (
        GraphEdge(src=0, dst=1, kind="trigger_workflow", active=True),
        GraphEdge(src=3, dst=2, kind="trigger_workflow", active=False),
    )
    assert node_colours(spec) == [BRAND_MAIN_GREEN, BRAND_AMBER, BRAND_MEDIUM_GRAY, BRAND_SLATE_BLUE]
    assert edge_colours(spec) == [EDGE_BRIGHT, BRAND_MEDIUM_GRAY]
    assert node_shapes(spec) == ["box", "box", "box", "ellipse"]
    assert spec.title == "energy · triggers"


def test_trigger_dag_shares_one_dataset_node_between_listeners() -> None:
    workflows = [
        {"name": "a", "trigger": {"type": "on_update", "datasets": ["prices", "wind"], "require": "all"}},
        {"name": "b", "trigger": {"type": "on_update", "datasets": ["prices"]}},
        {"name": "c", "trigger": "not a trigger"},
    ]
    spec = trigger_dag(workflows, project_name="energy")

    assert [(node.key, node.kind) for node in spec.nodes] == [
        ("energy/a", "workflow"),
        ("energy/b", "workflow"),
        ("energy/c", "workflow"),
        ("dataset:prices", "dataset"),
        ("dataset:wind", "dataset"),
    ]
    assert _edges(spec) == [
        ("dataset:prices", "energy/a", "trigger_dataset"),
        ("dataset:wind", "energy/a", "trigger_dataset"),
        ("dataset:prices", "energy/b", "trigger_dataset"),
    ]
    assert node_shapes(spec)[3:] == ["diamond", "diamond"]


def test_upstream_nodes_and_summary_follow_the_edges_back() -> None:
    spec = step_dag(CHAIN, None, title="chain")

    assert upstream_nodes(spec, 2) == [True, True, True]
    assert upstream_nodes(spec, 1) == [True, True, False]
    assert upstream_nodes(spec, 0) == [True, False, False]
    assert upstream_nodes(spec, 7) == [False, False, False]
    assert upstream_summary(spec, 2, upstream_nodes(spec, 2)) == "publish waits on 2: fetch, clean-up"
    assert upstream_summary(spec, 0, upstream_nodes(spec, 0)) == "fetch waits on nothing"
    assert edge_summary(spec, 1) == "clean-up → publish (ordering only)"
    assert edge_summary(spec, 9) == ""


def test_highlight_colours_dim_what_is_not_lit() -> None:
    spec = GraphSpec(
        nodes=(
            GraphNode(key="a", label="a", kind="step", state="succeeded"),
            GraphNode(key="b", label="b", kind="step", state="failed"),
        ),
        edges=(GraphEdge(src=0, dst=1, kind="data"),),
        title="ab",
    )
    nodes, edges = highlight_colours(spec, [True, False])

    assert nodes == [BRAND_MAIN_GREEN, dim(BRAND_CORAL_RED)]
    assert edges == [dim(EDGE_BRIGHT)]
    assert dim(BRAND_CORAL_RED) != BRAND_CORAL_RED
    assert dim("#101412") == "#101412"


def test_step_dag_tells_repeated_steps_apart_by_their_literal_arguments() -> None:
    graph = {
        "nodes": [
            {"node_key": "resolve", "name": "resolve", "input_bindings": {}, "upstream_node_keys": []},
            {
                "node_key": "match",
                "name": "match",
                "input_bindings": {
                    "quarters": {"type": "node_output", "node_key": "resolve"},
                    "which": {"type": "literal", "value": "current"},
                    "tier": {"type": "literal", "value": "fortnox"},
                    "after": {"type": "literal", "value": None},
                },
                "upstream_node_keys": ["resolve"],
            },
            {
                "node_key": "match_2",
                "name": "match",
                "input_bindings": {
                    "quarters": {"type": "node_output", "node_key": "resolve"},
                    "which": {"type": "literal", "value": "previous"},
                    "tier": {"type": "literal", "value": "fortnox"},
                    "after": {"type": "node_output", "node_key": "match"},
                },
                "upstream_node_keys": ["match", "resolve"],
            },
        ]
    }
    spec = step_dag(graph, None, title="repeat")

    assert [node.label for node in spec.nodes] == ["resolve", "match · current · fortnox", "match · previous · fortnox"]
    # Same step run status still matches by name when a run row carries no key.
    coloured = step_dag(graph, [{"name": "match", "status": "succeeded"}], title="repeat")
    assert [node.state for node in coloured.nodes] == ["unrun", "succeeded", "succeeded"]
