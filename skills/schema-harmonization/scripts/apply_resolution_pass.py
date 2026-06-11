"""Apply one resolver pass to a Lance table.

Two modes:

**Single column** (default) — resolves distinct values in ``--column`` and applies
find-and-replace ops on that same column. Run once per ``--resolution-field-name``.
Cross-column workflows (e.g. filling Ensembl IDs from symbols) must be separate
audited steps — ``AddColumn``, ``SetColumn``, or explicit ``ReplaceValue`` ops —
before running this script on the target column.

    python skills/schema-harmonization/scripts/apply_resolution_pass.py \\
        <lance_db> --table T --tool resolve_genes --column target_gene \\
        --resolution-field-name symbol --reason "standardize symbols" --organism human

**Fan-out** (``--fanout``) — resolves distinct values of ``--key-column`` once and
fans the many correlated fields of each resolution out to several target columns in
a single keyed merge (``MergeColumns``). Use it for multi-field resolvers like
``resolve_guide_sequences`` where one expensive call fills coordinates, strand,
intended gene, and context. Target columns that don't exist yet are auto-created
(null-initialized, type inferred from the resolved values). Map each resolution
field to its column with repeated ``--map FIELD:COLUMN``:

    python ... --table GeneticPerturbationSchema --tool resolve_guide_sequences \\
        --fanout --key-column guide_sequence \\
        --map target_start:target_start --map target_end:target_end \\
        --map target_strand:target_strand --map intended_gene_name:intended_gene_name \\
        --reason "resolve guide targets via BLAT" --organism human

    python ... --dry-run   # validate and report only; no Lance or audit writes

Tools: ``--list-tools``. Optional kwargs: ``--organism``, ``--input-type``.
Built-in tools are listed in ``auto_atlas.registry``.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import lancedb
import pandas as pd

from auto_atlas import AddColumn, CurationApplicator, CurationTransaction, default_audit_db_path
from auto_atlas.curation.sql import infer_arrow_type
from auto_atlas.curation.types import ApplyResult
from auto_atlas.registry import RESOLVER_TOOLS, list_resolver_tools
from auto_atlas.types import ResolutionReport


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _distinct_non_null(values: list[Any]) -> list[str]:
    return list(dict.fromkeys(s for s in (_optional_str(v) for v in values) if s is not None))


def _parse_field_map(items: list[str]) -> dict[str, str]:
    """Parse repeated ``FIELD:COLUMN`` strings into a resolution-field -> column map."""
    mapping: dict[str, str] = {}
    for item in items:
        field, sep, column = item.partition(":")
        field, column = field.strip(), column.strip()
        if not sep or not field or not column:
            raise ValueError(f"--map expects FIELD:COLUMN, got {item!r}")
        mapping[field] = column
    return mapping


def resolve_distinct_values(
    values: list[Any],
    tool: str,
    *,
    resolver_kwargs: dict[str, Any] | None = None,
) -> ResolutionReport:
    """Resolve distinct non-null cell values; return the tool's ``ResolutionReport``."""
    spec = RESOLVER_TOOLS.get(tool)
    if spec is None:
        raise ValueError(f"Unknown tool {tool!r}. Known tools: {', '.join(list_resolver_tools())}")

    distinct = _distinct_non_null(values)
    if not distinct:
        return ResolutionReport(
            tool=tool,
            total=0,
            resolved=0,
            unresolved=0,
            ambiguous=0,
            results=[],
        )

    kwargs = dict(resolver_kwargs or {})
    report = spec.fn(**{spec.values_param: distinct, **kwargs})
    if not isinstance(report, ResolutionReport):
        raise TypeError(f"{tool} did not return ResolutionReport")
    return report


def _read_column(lance_db_path: str, table_name: str, column: str) -> list[Any]:
    table = lancedb.connect(os.fspath(lance_db_path)).open_table(table_name)
    arrow = table.to_arrow()
    if column not in arrow.column_names:
        raise ValueError(
            f"Column {column!r} not in {table_name!r}. Available: {list(arrow.column_names)}"
        )
    return arrow.column(column).to_pylist()


def _table_columns(lance_db_path: str, table_name: str) -> list[str]:
    table = lancedb.connect(os.fspath(lance_db_path)).open_table(table_name)
    return list(table.schema.names)


def _add_column_ops_for_missing(
    rows: list[dict[str, Any]],
    *,
    existing: set[str],
    field_to_column: dict[str, str],
    tool: str,
) -> list[AddColumn]:
    """Null-init AddColumn ops for target columns that don't exist yet.

    The merge can only update existing columns, so any target column missing from
    the table is created first. Its Arrow type is inferred from the values it will
    receive (``infer_arrow_type(None)`` falls back to string for all-null columns).
    """
    add_ops: list[AddColumn] = []
    planned: set[str] = set()
    for field_name, column in field_to_column.items():
        if column in existing or column in planned:
            continue
        planned.add(column)
        first = next((row[column] for row in rows if row.get(column) is not None), None)
        add_ops.append(
            AddColumn(
                column=column,
                data_type=str(infer_arrow_type(first)),
                tool=tool,
                reason=f"stage {column} for {field_name} fan-out from {tool}",
            )
        )
    return add_ops


def apply_resolution_pass(
    lance_db_path: str,
    *,
    table_name: str,
    tool: str,
    column: str,
    resolution_field_name: str,
    reason: str,
    resolver_kwargs: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> ApplyResult | None:
    """Resolve distinct values in ``column`` and apply replacements in that column."""
    column_values = _read_column(lance_db_path, table_name, column)

    report = resolve_distinct_values(column_values, tool, resolver_kwargs=resolver_kwargs)
    print(
        f"Resolver {report.tool}: {report.resolved}/{report.total} resolved, "
        f"{report.unresolved} unresolved"
    )
    if report.unresolved_values:
        sample = report.unresolved_values[:15]
        print(f"  Unresolved sample ({len(report.unresolved_values)} total): {sample}")

    distinct = _distinct_non_null(column_values)
    ops = report.propose_column_replacements(
        distinct,
        column=column,
        reason=reason,
        resolution_field_name=resolution_field_name,
    )
    print(f"  {column} <- {resolution_field_name}: {len(ops)} ReplaceValue op(s)")
    if not ops:
        return None

    txn = CurationTransaction(table_name=table_name, changes=ops)
    applicator = CurationApplicator(
        lance_db_path, audit_db_path=default_audit_db_path(lance_db_path)
    )
    try:
        return applicator.apply(txn, dry_run=dry_run, allowed_columns={column})
    finally:
        applicator.close()


def apply_resolution_fanout(
    lance_db_path: str,
    *,
    table_name: str,
    tool: str,
    key_column: str,
    field_to_column: dict[str, str],
    reason: str,
    resolver_kwargs: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> ApplyResult | None:
    """Resolve distinct ``key_column`` values once and fan fields out to columns."""
    key_values = _read_column(lance_db_path, table_name, key_column)

    report = resolve_distinct_values(key_values, tool, resolver_kwargs=resolver_kwargs)
    print(
        f"Resolver {report.tool}: {report.resolved}/{report.total} resolved, "
        f"{report.unresolved} unresolved"
    )
    if report.unresolved_values:
        sample = report.unresolved_values[:15]
        print(f"  Unresolved sample ({len(report.unresolved_values)} total): {sample}")

    distinct = _distinct_non_null(key_values)
    op = report.propose_keyed_columns(
        distinct,
        key_column=key_column,
        field_to_column=field_to_column,
        reason=reason,
    )
    targets = sorted(field_to_column.values())
    if op is None:
        print(f"  merge on {key_column} -> {targets}: nothing resolved")
        return None
    print(f"  merge on {key_column} -> {targets}: {len(op.rows)} keyed row(s)")

    existing = set(_table_columns(lance_db_path, table_name))
    add_ops = _add_column_ops_for_missing(
        op.rows, existing=existing, field_to_column=field_to_column, tool=tool
    )
    if add_ops:
        print(f"  auto-creating columns: {[a.column for a in add_ops]}")

    txn = CurationTransaction(table_name=table_name, changes=[*add_ops, op])
    applicator = CurationApplicator(
        lance_db_path, audit_db_path=default_audit_db_path(lance_db_path)
    )
    try:
        allowed = set(field_to_column.values()) | {key_column}
        return applicator.apply(txn, dry_run=dry_run, allowed_columns=allowed)
    finally:
        applicator.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lance_db_path")
    parser.add_argument("--table", required=True)
    parser.add_argument("--tool", help="Registered resolver name")
    parser.add_argument("--list-tools", action="store_true")
    parser.add_argument("--column", help="Column to resolve and update")
    parser.add_argument(
        "--resolution-field-name",
        help="Resolution attribute for new values (e.g. symbol, ensembl_gene_id, resolved_value)",
    )
    parser.add_argument(
        "--fanout",
        action="store_true",
        help="Fan one resolution out to many columns via a keyed merge",
    )
    parser.add_argument(
        "--key-column",
        dest="key_column",
        help="(fanout) Column whose distinct values are resolved and joined on",
    )
    parser.add_argument(
        "--map",
        action="append",
        dest="field_map",
        default=[],
        metavar="FIELD:COLUMN",
        help="(fanout) Map a resolution field to a target column; repeatable",
    )
    parser.add_argument("--reason", required=False)
    parser.add_argument("--organism", default=None)
    parser.add_argument("--input-type", default=None, dest="input_type")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.list_tools:
        for name in list_resolver_tools():
            print(name)
        return

    resolver_kwargs: dict[str, object] = {}
    if args.organism is not None:
        resolver_kwargs["organism"] = args.organism
    if args.input_type is not None:
        resolver_kwargs["input_type"] = args.input_type

    if args.fanout:
        field_to_column = _parse_field_map(args.field_map)
        missing = [
            flag
            for flag, val in (
                ("--tool", args.tool),
                ("--key-column", args.key_column),
                ("--reason", args.reason),
                ("--map", field_to_column),
            )
            if not val
        ]
        if missing:
            parser.error(f"required with --fanout: {', '.join(missing)}")

        result = apply_resolution_fanout(
            os.fspath(args.lance_db_path),
            table_name=args.table,
            tool=args.tool,
            key_column=args.key_column,
            field_to_column=field_to_column,
            reason=args.reason,
            resolver_kwargs=resolver_kwargs or None,
            dry_run=args.dry_run,
        )
    else:
        missing = [
            flag
            for flag, val in (
                ("--tool", args.tool),
                ("--column", args.column),
                ("--resolution-field-name", args.resolution_field_name),
                ("--reason", args.reason),
            )
            if not val
        ]
        if missing:
            parser.error(f"required when not using --list-tools: {', '.join(missing)}")

        result = apply_resolution_pass(
            os.fspath(args.lance_db_path),
            table_name=args.table,
            tool=args.tool,
            column=args.column,
            resolution_field_name=args.resolution_field_name,
            reason=args.reason,
            resolver_kwargs=resolver_kwargs or None,
            dry_run=args.dry_run,
        )

    if result is None:
        print("No changes proposed.")
        return

    print(f"Status: {result.status.value}")
    if result.error:
        print(f"Error: {result.error}", file=sys.stderr)
        sys.exit(1)
    for applied in result.applied_changes:
        op = applied.operation
        print(f"  {op.kind.value}: {op.column} rows_updated={applied.rows_updated}")
    if args.dry_run:
        print("(dry run — Lance not mutated)")


if __name__ == "__main__":
    main()
