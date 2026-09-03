"""The graphs the TUI's `i` pane draws, built from data the TUI already holds.

Two graphs, chosen by where the cursor is:

- `step_dag`: the steps inside one workflow, from the compiled `step_graph` on its
  version, coloured by a run's step statuses. The compiler adds an ordering edge
  between consecutive steps whether or not one consumes the other's output, so most
  workflows draw as a chain; the edges that carry data are told apart from the ones
  that only fix the order, which is what makes the drawing say more than the table.
- `trigger_dag`: the project's workflows and what fires them — other workflows
  (`OnWorkflow`) and datasets (`OnUpdate`). The API exposes each workflow's own
  trigger and never the reverse ("who listens to me"), so the edges are inverted here.

Pure functions and frozen dataclasses, no textual or plotui: the pane hands a
`GraphSpec` to plotui, and the tests read one without a terminal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from rebase.brand import (
    BRAND_AMBER,
    BRAND_MAIN_GREEN,
    BRAND_MEDIUM_GRAY,
    BRAND_SLATE_BLUE,
    status_colour,
)

NodeKind = Literal["step", "workflow", "dataset"]
EdgeKind = Literal["data", "order", "trigger_workflow", "trigger_dataset"]

#: What an unrun step is called: a node of the graph no step run has reached yet.
UNRUN = "unrun"
#: A step drawn from the workflow's definition alone, with no run to colour it by.
DEFINED = "defined"

#: The screen's own foreground; an edge that matters is drawn in it.
EDGE_BRIGHT = "#E8F0ED"
#: The screen's background; unlit nodes are blended toward it when a path is lit.
BACKGROUND = "#101412"

WORKFLOW_STATE_COLOURS: dict[str, str] = {
    "active": BRAND_MAIN_GREEN,
    "paused": BRAND_AMBER,
    "disabled": BRAND_MEDIUM_GRAY,
    #: A trigger source outside this project, or one the project no longer has.
    "external": BRAND_SLATE_BLUE,
}
DATASET_COLOUR = BRAND_SLATE_BLUE

NODE_SHAPES: dict[NodeKind, str] = {"step": "rounded", "workflow": "box", "dataset": "diamond"}
EXTERNAL_SHAPE = "ellipse"


@dataclass(frozen=True)
class GraphNode:
    key: str
    label: str
    kind: NodeKind
    #: A run status for a step, `active`/`paused`/`disabled`/`external` for a
    #: workflow, `dataset` for a dataset.
    state: str


@dataclass(frozen=True)
class GraphEdge:
    src: int
    dst: int
    kind: EdgeKind
    #: A trigger the workflow has switched off still draws, dimmed.
    active: bool = True


@dataclass(frozen=True)
class GraphSpec:
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    title: str
    rankdir: Literal["TB", "LR"] = "TB"

    @property
    def structure(self) -> tuple[Any, ...]:
        """What a layout depends on. Two specs that share it differ only in colour."""
        return (
            tuple((node.key, node.label, node.kind) for node in self.nodes),
            tuple((edge.src, edge.dst, edge.kind) for edge in self.edges),
            self.rankdir,
        )

    @property
    def edge_pairs(self) -> list[tuple[int, int]]:
        return [(edge.src, edge.dst) for edge in self.edges]


# ---- colours and shapes -------------------------------------------------------


def node_colour(node: GraphNode) -> str:
    if node.kind == "step":
        return status_colour(node.state)
    if node.kind == "dataset":
        return DATASET_COLOUR
    return WORKFLOW_STATE_COLOURS.get(node.state, BRAND_MEDIUM_GRAY)


def edge_colour(edge: GraphEdge) -> str:
    """Data and trigger edges are the graph's point; ordering edges are its footnote."""
    if edge.kind == "order" or not edge.active:
        return BRAND_MEDIUM_GRAY
    return EDGE_BRIGHT


def node_shape(node: GraphNode) -> str:
    if node.kind == "workflow" and node.state == "external":
        return EXTERNAL_SHAPE
    return NODE_SHAPES[node.kind]


def node_colours(spec: GraphSpec) -> list[str]:
    return [node_colour(node) for node in spec.nodes]


def edge_colours(spec: GraphSpec) -> list[str]:
    return [edge_colour(edge) for edge in spec.edges]


def node_shapes(spec: GraphSpec) -> list[str]:
    return [node_shape(node) for node in spec.nodes]


def dim(colour: str, *, amount: float = 0.65) -> str:
    """*colour* blended toward the background: still itself, but out of the way."""
    fg = _rgb(colour)
    bg = _rgb(BACKGROUND)
    mixed = tuple(round(f + (b - f) * amount) for f, b in zip(fg, bg, strict=True))
    return "#{:02x}{:02x}{:02x}".format(*mixed)


def _rgb(colour: str) -> tuple[int, int, int]:
    value = colour.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def highlight_colours(spec: GraphSpec, lit: list[bool]) -> tuple[list[str], list[str]]:
    """Colours with everything outside *lit* dimmed, edges lit only end to end."""
    nodes = [node_colour(node) if lit[i] else dim(node_colour(node)) for i, node in enumerate(spec.nodes)]
    edges = [edge_colour(edge) if lit[edge.src] and lit[edge.dst] else dim(edge_colour(edge)) for edge in spec.edges]
    return nodes, edges


# ---- reading a graph ------------------------------------------------------------


def node_index(spec: GraphSpec, key: str) -> int | None:
    for i, node in enumerate(spec.nodes):
        if node.key == key:
            return i
    return None


def upstream_nodes(spec: GraphSpec, i: int) -> list[bool]:
    """Every node *i* waits on, transitively, and *i* itself."""
    lit = [False] * len(spec.nodes)
    if not 0 <= i < len(spec.nodes):
        return lit
    incoming: dict[int, list[int]] = {}
    for edge in spec.edges:
        incoming.setdefault(edge.dst, []).append(edge.src)
    pending = [i]
    while pending:
        current = pending.pop()
        if lit[current]:
            continue
        lit[current] = True
        pending.extend(incoming.get(current, []))
    return lit


def upstream_summary(spec: GraphSpec, i: int, lit: list[bool]) -> str:
    """One line for the readout: `normalize waits on 2: fetch, clean`."""
    if not 0 <= i < len(spec.nodes):
        return ""
    label = spec.nodes[i].label
    names = [node.label for j, node in enumerate(spec.nodes) if lit[j] and j != i]
    if not names:
        return f"{label} waits on nothing"
    return f"{label} waits on {len(names)}: {', '.join(names)}"


def edge_summary(spec: GraphSpec, i: int) -> str:
    if not 0 <= i < len(spec.edges):
        return ""
    edge = spec.edges[i]
    kind = {
        "data": "data",
        "order": "ordering only",
        "trigger_workflow": "runs after",
        "trigger_dataset": "runs on update",
    }[edge.kind]
    if not edge.active:
        kind += ", inactive"
    return f"{spec.nodes[edge.src].label} → {spec.nodes[edge.dst].label} ({kind})"


# ---- the step graph of one workflow --------------------------------------------


def _bound_node_keys(binding: Any) -> set[str]:
    """The `node_output` keys anywhere inside a binding tree.

    Mirrors the SDK's `_binding_node_keys`, but walks any nesting rather than the
    three container types the compiler emits: the graph is stored JSON and read back
    here, and a stricter reader would drop an edge over a shape it did not expect.
    """
    keys: set[str] = set()
    if isinstance(binding, dict):
        if binding.get("type") == "node_output" and binding.get("node_key") is not None:
            keys.add(str(binding["node_key"]))
        for value in binding.values():
            keys.update(_bound_node_keys(value))
    elif isinstance(binding, (list, tuple)):
        for item in binding:
            keys.update(_bound_node_keys(item))
    return keys


def _literal_inputs(node: dict[str, Any]) -> list[str]:
    """The literal arguments a node was called with, in signature order."""
    bindings = node.get("input_bindings")
    if not isinstance(bindings, dict):
        return []
    values: list[str] = []
    for binding in bindings.values():
        if isinstance(binding, dict) and binding.get("type") == "literal":
            value = binding.get("value")
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                values.append(str(value))
    return values


def _step_labels(entries: list[dict[str, Any]]) -> list[str]:
    """Each node's label: its name, plus its literal arguments when the name repeats.

    A step called twelve times over different quarters and tiers is twelve nodes with
    one name. The literals are what set them apart — `match_quarter · current ·
    fortnox` — and only a repeated name pays for them; a step called once keeps its
    name alone, as its row in the timeline does.
    """
    names = [str(raw.get("name") or raw["node_key"]) for raw in entries]
    repeated = {name for name in names if names.count(name) > 1}
    labels: list[str] = []
    for name, raw in zip(names, entries, strict=True):
        literals = _literal_inputs(raw) if name in repeated else []
        labels.append(" · ".join([name, *literals]) if literals else name)
    return labels


def _step_states(step_runs: list[dict[str, Any]] | None) -> tuple[dict[str, str], dict[str, str]]:
    """Status by `node_key` and by `name`, the latest attempt of each winning."""
    by_key: dict[str, tuple[int, str]] = {}
    by_name: dict[str, tuple[int, str]] = {}
    for step in step_runs or []:
        if not isinstance(step, dict):
            continue
        status = str(step.get("status") or UNRUN)
        attempt = step.get("attempt")
        rank = int(attempt) if isinstance(attempt, int) else 0
        for index, field in ((by_key, "node_key"), (by_name, "name")):
            value = step.get(field)
            if value is None:
                continue
            key = str(value)
            if key not in index or index[key][0] <= rank:
                index[key] = (rank, status)
    return {k: v[1] for k, v in by_key.items()}, {k: v[1] for k, v in by_name.items()}


def step_dag(
    step_graph: dict[str, Any] | None,
    step_runs: list[dict[str, Any]] | None,
    *,
    title: str,
) -> GraphSpec:
    """The steps of a workflow version, coloured by a run of it.

    Nodes keep the graph's order. A step run matches a node by `node_key`, or by
    `name` when the run rows carry no key; a node no run has reached is `unrun`. With
    *step_runs* None there is no run at all — the workflow as deployed — and every
    node is `defined`, which is drawn as neutral rather than as "not run". An
    upstream the graph never declares as a node is skipped rather than invented.
    """
    raw_nodes = (step_graph or {}).get("nodes")
    if not isinstance(raw_nodes, list):
        return GraphSpec(nodes=(), edges=(), title=title)
    by_key, by_name = _step_states(step_runs)
    resting = DEFINED if step_runs is None else UNRUN
    entries = [raw for raw in raw_nodes if isinstance(raw, dict) and raw.get("node_key")]
    nodes: list[GraphNode] = []
    index: dict[str, int] = {}
    for raw, label in zip(entries, _step_labels(entries), strict=True):
        key = str(raw["node_key"])
        name = str(raw.get("name") or key)
        state = by_key.get(key) or by_name.get(name) or resting
        index[key] = len(nodes)
        nodes.append(GraphNode(key=key, label=label, kind="step", state=state))
    edges: list[GraphEdge] = []
    for dst, raw in enumerate(entries):
        upstream = raw.get("upstream_node_keys")
        if not isinstance(upstream, list):
            continue
        data_keys = _bound_node_keys(raw.get("input_bindings"))
        seen: set[int] = set()
        for key in upstream:
            src = index.get(str(key))
            if src is None or src in seen:
                continue
            seen.add(src)
            edges.append(GraphEdge(src=src, dst=dst, kind="data" if str(key) in data_keys else "order"))
    return GraphSpec(nodes=tuple(nodes), edges=tuple(edges), title=title)


# ---- the trigger graph of a project ---------------------------------------------


def workflow_key(project_name: str, name: str) -> str:
    """How a workflow is addressed in a trigger: `project/workflow`."""
    return name if "/" in name else f"{project_name}/{name}"


def dataset_key(name: str) -> str:
    return f"dataset:{name}"


def _workflow_state(workflow: dict[str, Any]) -> str:
    if workflow.get("enabled") is False:
        return "disabled"
    if workflow.get("paused"):
        return "paused"
    return "active"


def trigger_dag(workflows: list[dict[str, Any]], *, project_name: str) -> GraphSpec:
    """The project's workflows and the workflows and datasets that fire them.

    A bare source name means a workflow of the same project, as the platform reads it.
    A source the project does not have — another project's workflow, or one since
    deleted — becomes an `external` node so the edge still has somewhere to start.
    """
    nodes: list[GraphNode] = []
    index: dict[str, int] = {}

    def add(node: GraphNode) -> int:
        if node.key not in index:
            index[node.key] = len(nodes)
            nodes.append(node)
        return index[node.key]

    rows = [item for item in workflows if isinstance(item, dict) and item.get("name")]
    for item in rows:
        name = str(item["name"])
        add(GraphNode(key=workflow_key(project_name, name), label=name, kind="workflow", state=_workflow_state(item)))
    edges: list[GraphEdge] = []
    for item in rows:
        trigger = item.get("trigger")
        if not isinstance(trigger, dict):
            continue
        dst = index[workflow_key(project_name, str(item["name"]))]
        active = trigger.get("active") is not False
        if trigger.get("type") == "on_workflow" and trigger.get("source"):
            source = str(trigger["source"])
            key = workflow_key(project_name, source)
            label = source if key not in index and "/" in source else key.rsplit("/", 1)[-1]
            src = add(GraphNode(key=key, label=label, kind="workflow", state="external"))
            edges.append(GraphEdge(src=src, dst=dst, kind="trigger_workflow", active=active))
        elif trigger.get("type") == "on_update":
            datasets = trigger.get("datasets")
            for dataset in datasets if isinstance(datasets, list) else []:
                name = str(dataset)
                src = add(GraphNode(key=dataset_key(name), label=name, kind="dataset", state="dataset"))
                edges.append(GraphEdge(src=src, dst=dst, kind="trigger_dataset", active=active))
    title = f"{project_name} · triggers" if project_name else "triggers"
    return GraphSpec(nodes=tuple(nodes), edges=tuple(edges), title=title)
