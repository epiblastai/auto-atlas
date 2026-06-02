"""Build column replacements from resolver output."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from auto_atlas.curation.types import ColumnReplacement
from auto_atlas.types import Resolution, ResolutionReport


def _values_equal(a: Any, b: Any) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return str(a) == str(b)


def propose_column_replacements(
    current_values: list[Any],
    report: ResolutionReport,
    *,
    column: str,
    tool: str,
    reason: str,
    resolved_value_fn: Callable[[Resolution], Any | None],
) -> list[ColumnReplacement]:
    """Derive deduplicated find-and-replace operations from a resolution report.

    Zips ``current_values`` with ``report.results``. For each row, uses
    ``resolved_value_fn`` to pick which part of the :class:`~auto_atlas.types.Resolution`
    becomes the replacement ``new_value`` for ``column`` (e.g. ``lambda r: r.symbol``
    for ``gene_symbol``, ``lambda r: r.ensembl_gene_id`` for ``ensembl_gene_id``).
    One report can therefore drive multiple columns with different callbacks.

    Skips a row when the callback returns ``None`` (unresolved or no value for this
    column) or when the new value equals the current cell. Collapses duplicate
    ``(column, old_value, new_value)`` keys; when several rows share a pair, keeps
    metadata from the highest-confidence resolution.
    """
    if len(current_values) != len(report.results):
        raise ValueError(
            f"current_values length ({len(current_values)}) must match "
            f"report.results length ({len(report.results)})"
        )

    best: dict[tuple[str, Any, Any], ColumnReplacement] = {}

    for current, resolution in zip(current_values, report.results, strict=True):
        new_value = resolved_value_fn(resolution)
        if new_value is None:
            continue
        if _values_equal(current, new_value):
            continue

        key = (column, current, new_value)
        candidate = ColumnReplacement(
            column=column,
            old_value=current,
            new_value=new_value,
            tool=tool,
            reason=reason,
            confidence=resolution.confidence,
            source=resolution.source,
            alternatives=list(resolution.alternatives),
            input_value=resolution.input_value,
        )

        existing = best.get(key)
        if existing is None or (resolution.confidence or 0.0) > (existing.confidence or 0.0):
            best[key] = candidate

    return list(best.values())
