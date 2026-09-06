"""The right-hand pane `i` opens in the TUI, drawing a `GraphSpec` with plotui.

plotui renders the graph as a terminal image (Kitty graphics, or iTerm2's inline
images), which is why it is optional: it needs a native wheel and a terminal that can
show one. The pane imports it on first use, and says what to install or which
terminal to use when it cannot draw, rather than costing `rebase tui` the dependency
everywhere else.

The pane is docked, not modal: the tables beside it keep the focus and the keys, and
it follows the cursor. A cursor move that keeps the same graph only recolours it —
plotui repaints in place — and a change of graph lays a new one out.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.containers import Vertical
from textual.widgets import Static

from rebase._optional import optional_module
from rebase.brand import BRAND_BRIGHT_GREEN, BRAND_MEDIUM_GRAY
from rebase.tui_graph import (
    GraphSpec,
    edge_colours,
    edge_summary,
    highlight_colours,
    layout_spacing,
    node_colour,
    node_colours,
    node_shapes,
    upstream_nodes,
    upstream_summary,
)

MISSING_PLOTUI_NOTICE = (
    'The graph pane draws with plotui, which is not installed.\n\nInstall it with:  pip install "rebase-toolkit[graph]"'
)
UNSUPPORTED_TERMINAL_NOTICE = (
    "plotui draws the graph as an image, and this terminal cannot show one.\n\n"
    "Kitty, Ghostty, iTerm2, WezTerm and Konsole can. Under tmux, set\n"
    "`allow-passthrough on` and run with PLOTUI_RENDER=direct."
)
OLD_PLOTUI_NOTICE = (
    'The installed plotui predates directed graphs.\n\nUpgrade it with:  pip install -U "rebase-toolkit[graph]"'
)
NOTHING_TO_DRAW_NOTICE = "Nothing to draw here."
READOUT_HINT = "hover or click a step to light what it waits on"

STATE_LABELS = {"unrun": "not run", "defined": "step (open a run to colour it)", "external": "elsewhere"}


def load_plotui() -> tuple[Any, Any]:
    """`plotui` and `plotui.textual`, or the ImportError that names the extra."""
    return optional_module("plotui", "graph"), optional_module("plotui.textual", "graph")


class GraphPane(Vertical):
    DEFAULT_CSS = f"""
    GraphPane {{
        dock: right;
        width: 45%;
        min-width: 40;
        height: 100%;
        display: none;
        padding: 0 1;
        background: #101412;
        border-left: solid {BRAND_BRIGHT_GREEN};
    }}
    GraphPane #graph-title {{
        height: 1;
        color: {BRAND_BRIGHT_GREEN};
        text-style: bold;
    }}
    GraphPane #graph-legend {{
        height: 1;
    }}
    GraphPane #graph-notice {{
        height: 1fr;
        color: {BRAND_MEDIUM_GRAY};
        padding: 1 0;
        display: none;
    }}
    GraphPane #graph-body {{
        height: 1fr;
    }}
    GraphPane #graph-readout {{
        height: 2;
        color: {BRAND_MEDIUM_GRAY};
    }}
    """

    def __init__(self) -> None:
        super().__init__(id="graph-pane")
        self.can_focus = False
        self._modules: tuple[Any, Any] | None = None
        self._spec: GraphSpec | None = None
        self._plot: Any = None
        self._widget: Any = None
        self._handle: int | None = None
        self._base: tuple[list[str], list[str]] | None = None
        self._notice: str | None = None
        #: How often a graph was laid out afresh, and how often only repainted. The
        #: tests read these to check a cursor move costs a repaint and not a layout.
        self.rebuilds = 0
        self.recolours = 0

    def compose(self):
        yield Static("", id="graph-title")
        yield Static("", id="graph-legend")
        yield Static("", id="graph-notice")
        yield Vertical(id="graph-body")
        yield Static("", id="graph-readout")

    # ---- what is on show --------------------------------------------------------

    @property
    def spec(self) -> GraphSpec | None:
        return self._spec

    @property
    def notice(self) -> str | None:
        """The notice on show, or None while a graph is."""
        return self._notice

    def show_notice(self, text: str, *, title: str = "") -> None:
        # `Text`, not `str`, everywhere the pane writes: a bare string is content markup
        # to Textual, and `[graph]` in the install hint reads as a style tag and vanishes.
        self.query_one("#graph-title", Static).update(Text(title))
        self.query_one("#graph-legend", Static).update("")
        self.query_one("#graph-readout", Static).update("")
        notice = self.query_one("#graph-notice", Static)
        notice.update(Text(text))
        notice.display = True
        self.query_one("#graph-body").display = False
        self._notice = text

    def clear(self) -> None:
        """Forget the graph: the next `show` lays out afresh, whatever it is asked for.

        This is also what takes the picture off the screen. plotui paints the graph
        as a terminal image, which hiding the pane leaves in place; unmounting the
        widget is what makes plotui delete it.
        """
        self._spec = None
        self._plot = None
        self._widget = None
        self._handle = None
        self._base = None
        self.query_one("#graph-body").remove_children()
        self.show_notice("")

    def show(self, spec: GraphSpec, *, selected: int | None = None, hint: str | None = None) -> None:
        """Draw *spec*, repainting in place when only its colours changed.

        *hint* replaces the readout's standing line, for a graph that needs a word of
        explanation — a trigger graph with nothing to connect, say.
        """
        modules = self._plotui()
        if modules is None:
            self.show_notice(MISSING_PLOTUI_NOTICE, title=spec.title)
            return
        core, textual_mod = modules
        if not hasattr(core, "LayeredLayout") or not hasattr(core.Plot, "add_graph2d"):
            self.show_notice(OLD_PLOTUI_NOTICE, title=spec.title)
            return
        if textual_mod.detect_render_mode() == "unsupported":
            self.show_notice(UNSUPPORTED_TERMINAL_NOTICE, title=spec.title)
            return
        if not spec.nodes:
            self.show_notice(NOTHING_TO_DRAW_NOTICE, title=spec.title)
            return
        self.query_one("#graph-title", Static).update(Text(spec.title))
        self.query_one("#graph-legend", Static).update(self._legend(spec))
        self.query_one("#graph-readout", Static).update(Text(hint or READOUT_HINT))
        self.query_one("#graph-notice").display = False
        self.query_one("#graph-body").display = True
        self._notice = None
        base = (node_colours(spec), edge_colours(spec))
        if self._widget is not None and self._spec is not None and spec.structure == self._spec.structure:
            self._spec = spec
            self._base = base
            self._widget.set_graph_colors(self._handle, *base)
            self._select(selected)
            self.recolours += 1
            return
        self._spec = spec
        self._base = base
        edges = spec.edge_pairs
        labels = [node.label for node in spec.nodes]
        try:
            # The labels size the boxes, so the layout keeps them from touching.
            layout = core.LayeredLayout(
                len(spec.nodes), edges, rankdir=spec.rankdir, labels=labels, **layout_spacing(spec.rankdir)
            )
        except TypeError:
            # plotui 0.5.0 lays out without labels, and its boxes overlap.
            self.show_notice(OLD_PLOTUI_NOTICE, title=spec.title)
            return
        xs, ys = layout.positions()
        plot = core.Plot()
        self._handle = plot.add_graph2d(
            xs,
            ys,
            edges,
            labels=labels,
            node_colors=base[0],
            edge_colors=base[1],
            node_shapes=node_shapes(spec),
            routes=layout.routes(),
            # Unnamed on purpose: a named trace gets a legend chip inside the plot, and
            # the title line above already says what the graph is.
        )
        self._plot = plot
        widget = textual_mod.PlotWidget(plot, pickable=True, crosshair=False)
        widget.can_focus = False
        self._widget = widget
        body = self.query_one("#graph-body")
        body.remove_children()
        body.mount(widget)
        self._select(selected)
        self.rebuilds += 1

    def _select(self, selected: int | None) -> None:
        if self._plot is None:
            return
        self._plot.set_selected(None if selected is None else ("node", selected))
        if self._widget is not None:
            self._widget.invalidate()

    def _plotui(self) -> tuple[Any, Any] | None:
        if self._modules is None:
            try:
                self._modules = load_plotui()
            except ImportError:
                return None
        return self._modules

    @staticmethod
    def _legend(spec: GraphSpec) -> Text:
        legend = Text()
        seen: list[tuple[str, str]] = []
        for node in spec.nodes:
            entry = (STATE_LABELS.get(node.state, node.state), node_colour(node))
            if entry not in seen:
                seen.append(entry)
        for label, colour in seen:
            if legend:
                legend.append("  ")
            legend.append("■ ", style=colour)
            legend.append(label, style=BRAND_MEDIUM_GRAY)
        if spec.caption:
            legend.append(f"  {spec.caption}", style=BRAND_MEDIUM_GRAY)
        return legend

    # ---- hover and click ----------------------------------------------------------

    def on_plot_widget_element_hovered(self, message: Any) -> None:
        self._light(message.element)

    def on_plot_widget_element_picked(self, message: Any) -> None:
        self._light(message.element)

    def _light(self, element: tuple[str, int] | None) -> None:
        """Light the path a node waits on, name an edge, or put the colours back."""
        spec = self._spec
        if spec is None or self._widget is None or self._base is None:
            return
        readout = self.query_one("#graph-readout", Static)
        if element is not None and element[0] == "node":
            lit = upstream_nodes(spec, element[1])
            self._widget.set_graph_colors(self._handle, *highlight_colours(spec, lit))
            readout.update(Text(upstream_summary(spec, element[1], lit)))
            return
        self._widget.set_graph_colors(self._handle, *self._base)
        readout.update(Text(edge_summary(spec, element[1]) if element is not None else READOUT_HINT))
