"""Priority-ordered stitching of time series layers.

:func:`stitch` composes pandas Series or DataFrames from a list of :class:`Layer`
entries: the first layer whose window covers a timestamp and whose value is non-null
wins, lower-priority layers only fill the gaps. Windows are expressed as offsets
relative to an issue time ("PT0H" = from the issue time onwards) or absolute
timezone-aware datetimes, so one call expresses "freshest forecast for the future,
history for the past, climatology as fallback". :class:`Exclude` entries block
fallback inside a window without contributing data — deliberate nulls that lower
layers must not fill (e.g. masking a storm week out of training data).

pandas is imported lazily inside functions so this module stays importable with the
toolkit's core (pandas-free) dependencies.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from rebase.client import RebaseWorkflowError
from rebase.timing import Duration, _coerce_issue_time, parse_offset


class StitchError(RebaseWorkflowError):
    """Raised when layers cannot be stitched (bad windows, indices or data kinds)."""


_EXCLUDED = "excluded"

_Bound = str | timedelta | Duration | datetime | None


class Layer:
    """One prioritised data source with an optional ``[start, end)`` window."""

    def __init__(
        self,
        data: Any,
        *,
        start: _Bound = None,
        end: _Bound = None,
        name: str | None = None,
    ) -> None:
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise ValueError("Layer name must be a non-empty string when given")
        self.data = data
        self.start = _coerce_bound(start, field_name="start")
        self.end = _coerce_bound(end, field_name="end")
        self.name = name
        self._label = name or "layer"


class Exclude:
    """A window in which the output stays null and lower layers are never consulted."""

    def __init__(self, *, start: _Bound = None, end: _Bound = None, name: str | None = None) -> None:
        if start is None and end is None:
            raise ValueError(
                "Exclude requires start= and/or end=; an unbounded Exclude would blank everything below it"
            )
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise ValueError("Exclude name must be a non-empty string when given")
        self.start = _coerce_bound(start, field_name="start")
        self.end = _coerce_bound(end, field_name="end")
        self.name = name
        self._label = name or "exclude"


def _coerce_bound(value: _Bound, *, field_name: str) -> Duration | datetime | None:
    """Normalise a window bound to a relative Duration, an aware datetime, or None."""
    if value is None or isinstance(value, datetime):
        return value
    return parse_offset(value, field_name=field_name)


def stitch(
    layers: Sequence[Layer | Exclude],
    *,
    issue_time: datetime | str | None = None,
    index: Any = None,
    return_sources: bool = False,
) -> Any:
    """Stitch prioritised layers into one Series/DataFrame; first non-null value wins.

    Windows are ``[start, end)`` (end-exclusive). Relative bounds resolve against
    ``issue_time``; with ``return_sources=True`` a same-shaped frame of layer names
    (or ``"excluded"``) is returned alongside the result for provenance.
    """
    try:
        import pandas as pd
    except ImportError:  # pragma: no cover - exercised only without pandas
        raise ImportError("stitch() requires pandas; install it with: pip install pandas") from None

    entries = list(layers)
    if not any(isinstance(entry, Layer) for entry in entries):
        raise ValueError("stitch requires at least one Layer")
    for position, entry in enumerate(entries):
        if not isinstance(entry, (Layer, Exclude)):
            raise TypeError(f"stitch entries must be Layer or Exclude instances; got {type(entry).__name__}")
        entry._label = entry.name or f"layer{position}"

    moment = _coerce_issue_time(issue_time) if issue_time is not None else None

    data_layers = [entry for entry in entries if isinstance(entry, Layer)]
    is_frame = isinstance(data_layers[0].data, pd.DataFrame)
    for layer in data_layers:
        if not isinstance(layer.data, (pd.Series, pd.DataFrame)):
            raise TypeError(f"Layer {layer._label!r} data must be a pandas Series or DataFrame")
        if isinstance(layer.data, pd.DataFrame) is not is_frame:
            raise StitchError("cannot stitch Series with DataFrame layers")

    windows = {id(entry): _resolve_window(entry, moment) for entry in entries}
    sliced = {id(layer): _sliced_layer_data(pd, layer, windows[id(layer)]) for layer in data_layers}

    union = _union_index(pd, [frame.index for frame in sliced.values()])
    if index is not None:
        index = _coerce_target_index(pd, index)

    if is_frame:
        result, sources = _stitch_frames(pd, entries, sliced, windows, union)
    else:
        result, sources = _stitch_series(pd, entries, sliced, windows, union)

    if index is not None:
        result = result.reindex(index)
        sources = sources.reindex(index)
    sources = sources.astype("string")

    if return_sources:
        return result, sources
    return result


def _resolve_window(entry: Layer | Exclude, moment: datetime | None) -> tuple[datetime | None, datetime | None]:
    resolved = []
    for bound in (entry.start, entry.end):
        if bound is None:
            resolved.append(None)
        elif isinstance(bound, datetime):
            if bound.tzinfo is None:
                raise StitchError(f"layer {entry._label!r} has a naive datetime bound; bounds must be timezone-aware")
            resolved.append(bound)
        else:
            if moment is None:
                raise StitchError(f"layer {entry._label!r} uses a relative window but no issue_time was provided")
            resolved.append(bound.add_to(moment))
    return resolved[0], resolved[1]


def _sliced_layer_data(pd: Any, layer: Layer, window: tuple[datetime | None, datetime | None]) -> Any:
    data = layer.data
    if not isinstance(data.index, pd.DatetimeIndex):
        raise StitchError(f"layer {layer._label!r} must have a DatetimeIndex; got {type(data.index).__name__}")
    if data.index.tz is None:
        raise StitchError(f"layer {layer._label!r} has a naive DatetimeIndex; localise it (e.g. tz_localize('UTC'))")
    data = data.tz_convert("UTC")
    duplicates = int(data.index.duplicated().sum())
    if duplicates:
        raise StitchError(
            f"layer {layer._label!r} has {duplicates} duplicate index entries; deduplicate before stitching"
        )
    start, end = window
    if start is not None:
        data = data[data.index >= start]
    if end is not None:
        data = data[data.index < end]
    return data


def _union_index(pd: Any, indices: list[Any]) -> Any:
    union = pd.DatetimeIndex([], tz="UTC")
    for idx in indices:
        union = union.union(idx)
    return union.sort_values()


def _coerce_target_index(pd: Any, index: Any) -> Any:
    if not isinstance(index, pd.DatetimeIndex):
        raise TypeError(f"index= must be a pandas DatetimeIndex; got {type(index).__name__}")
    if index.tz is None:
        raise StitchError("index= has naive timestamps; localise it (e.g. tz_localize('UTC'))")
    return index.tz_convert("UTC")


def _coverage(pd: Any, union: Any, window: tuple[datetime | None, datetime | None]) -> Any:
    start, end = window
    mask = pd.Series(True, index=union)
    if start is not None:
        mask &= union >= start
    if end is not None:
        mask &= union < end
    return mask


def _stitch_series(pd: Any, entries: list, sliced: dict, windows: dict, union: Any) -> tuple[Any, Any]:
    values = pd.Series(pd.NA, index=union, dtype=object)
    sources = pd.Series(pd.NA, index=union, dtype=object)
    filled = pd.Series(False, index=union)
    blocked = pd.Series(False, index=union)
    for entry in entries:
        coverage = _coverage(pd, union, windows[id(entry)])
        if isinstance(entry, Exclude):
            blocked |= coverage & ~filled
            continue
        candidate = sliced[id(entry)].reindex(union)
        fillable = coverage & ~filled & ~blocked & candidate.notna()
        values = values.mask(fillable, candidate)
        sources = sources.mask(fillable, entry._label)
        filled |= fillable
    sources = sources.mask(blocked & ~filled, _EXCLUDED)
    return values.infer_objects(), sources


def _stitch_frames(pd: Any, entries: list, sliced: dict, windows: dict, union: Any) -> tuple[Any, Any]:
    columns: list[Any] = []
    for entry in entries:
        if isinstance(entry, Layer):
            columns.extend(column for column in entry.data.columns if column not in columns)
    values = pd.DataFrame(pd.NA, index=union, columns=columns, dtype=object)
    sources = pd.DataFrame(pd.NA, index=union, columns=columns, dtype=object)
    filled = pd.DataFrame(False, index=union, columns=columns)
    blocked = pd.DataFrame(False, index=union, columns=columns)
    for entry in entries:
        row_coverage = _coverage(pd, union, windows[id(entry)])
        coverage = pd.DataFrame({column: row_coverage for column in columns})
        if isinstance(entry, Exclude):
            blocked |= coverage & ~filled
            continue
        candidate = sliced[id(entry)].reindex(index=union, columns=columns)
        fillable = coverage & ~filled & ~blocked & candidate.notna()
        values = values.mask(fillable, candidate)
        sources = sources.mask(fillable, entry._label)
        filled |= fillable
    sources = sources.mask(blocked & ~filled, _EXCLUDED)
    return values.infer_objects(), sources
