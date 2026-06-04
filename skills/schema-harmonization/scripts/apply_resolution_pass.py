"""Apply one resolver pass to one Lance column.

Run once per (column, field) pair. Example: symbol on ``target_gene``, then
Ensembl IDs with ``--column ensembl_gene_id --source-column target_gene``.

    python skills/schema-harmonization/scripts/apply_resolution_pass.py \\
        <lance_db> --table T --tool resolve_genes --column target_gene \\
        --field symbol --reason "standardize symbols" --organism human

    python ... --dry-run   # audit only, no Lance writes

Tools: ``--list-tools``. Optional kwargs: ``--organism``, ``--input-type``.
Built-in tools are listed in ``auto_atlas.tool_registry``.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import lancedb
import pandas as pd

from auto_atlas import CurationApplicator, CurationTransaction, default_audit_db_path
from auto_atlas.curation.types import ApplyResult
from auto_atlas.tool_registry import RESOLVER_TOOLS, list_resolver_tools
from auto_atlas.types import Resolution, ResolutionReport


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _skipped() -> Resolution:
    return Resolution(
        input_value="",
        resolved_value=None,
        confidence=0.0,
        source="skipped",
    )


def resolution_report_for_column(
    values: list[Any],
    tool: str,
    *,
    resolver_kwargs: dict[str, Any] | None = None,
) -> ResolutionReport:
    """Resolve distinct non-null cell values and expand back to one result per row."""
    spec = RESOLVER_TOOLS.get(tool)
    if spec is None:
        raise ValueError(
            f"Unknown tool {tool!r}. Known tools: {', '.join(list_resolver_tools())}"
        )

    normalized = [_optional_str(v) for v in values]
    unique = list(dict.fromkeys(s for s in normalized if s is not None))
    kwargs = dict(resolver_kwargs or {})

    if not unique:
        return ResolutionReport(
            tool=tool,
            total=len(values),
            resolved=0,
            unresolved=len(values),
            ambiguous=0,
            results=[_skipped()] * len(values),
        )

    partial = spec.fn(**{spec.values_param: unique, **kwargs})
    if not isinstance(partial, ResolutionReport):
        raise TypeError(f"{tool} did not return ResolutionReport")

    lookup = {r.input_value: r for r in partial.results}
    missing = [s for s in unique if s not in lookup]
    if missing:
        raise ValueError(f"{tool} did not return results for: {missing[:5]}")

    aligned = [lookup[s] if s is not None else _skipped() for s in normalized]
    resolved_count = sum(1 for r in aligned if r.resolved_value is not None)
    ambiguous_count = sum(1 for r in aligned if len(r.alternatives) > 1)
    return ResolutionReport(
        tool=partial.tool,
        total=len(aligned),
        resolved=resolved_count,
        unresolved=len(aligned) - resolved_count,
        ambiguous=ambiguous_count,
        results=aligned,
    )


def _read_column(lance_db_path: str, table_name: str, column: str) -> list[Any]:
    table = lancedb.connect(os.fspath(lance_db_path)).open_table(table_name)
    arrow = table.to_arrow()
    if column not in arrow.column_names:
        raise ValueError(
            f"Column {column!r} not in {table_name!r}. Available: {list(arrow.column_names)}"
        )
    return arrow.column(column).to_pylist()


def apply_resolution_pass(
    lance_db_path: str,
    *,
    table_name: str,
    tool: str,
    column: str,
    field: str,
    reason: str,
    source_column: str | None = None,
    resolver_kwargs: dict[str, Any] | None = None,
    allowed_columns: set[str] | None = None,
    dry_run: bool = False,
) -> ApplyResult | None:
    """Run a registered resolver on ``source_column`` and apply replacements to ``column``."""
    source_column = source_column or column
    source_values = _read_column(lance_db_path, table_name, source_column)
    target_values = (
        source_values
        if column == source_column
        else _read_column(lance_db_path, table_name, column)
    )
    if len(source_values) != len(target_values):
        raise ValueError(
            f"{source_column!r} ({len(source_values)} rows) and {column!r} "
            f"({len(target_values)} rows) differ in length"
        )

    report = resolution_report_for_column(
        source_values, tool, resolver_kwargs=resolver_kwargs
    )
    print(
        f"Resolver {report.tool}: {report.resolved}/{report.total} resolved, "
        f"{report.unresolved} unresolved"
    )
    if report.unresolved_values:
        sample = report.unresolved_values[:15]
        print(f"  Unresolved sample ({len(report.unresolved_values)} total): {sample}")

    ops = report.propose_column_replacements(
        target_values,
        column=column,
        reason=reason,
        resolution_field_name=field,
    )
    print(f"  {column} <- {field}: {len(ops)} ReplaceValue op(s)")
    if not ops:
        return None

    allowed = allowed_columns if allowed_columns is not None else {column}
    txn = CurationTransaction(table_name=table_name, changes=ops)
    applicator = CurationApplicator(
        lance_db_path, audit_db_path=default_audit_db_path(lance_db_path)
    )
    try:
        return applicator.apply(txn, dry_run=dry_run, allowed_columns=allowed)
    finally:
        applicator.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lance_db_path")
    parser.add_argument("--table", required=True)
    parser.add_argument("--tool", help="Registered resolver name")
    parser.add_argument("--list-tools", action="store_true")
    parser.add_argument("--column", help="Column to update")
    parser.add_argument(
        "--source-column",
        default=None,
        help="Column to resolve (default: same as --column)",
    )
    parser.add_argument(
        "--field",
        help="Resolution attribute for new values (e.g. symbol, ensembl_gene_id, resolved_value)",
    )
    parser.add_argument("--reason", required=False)
    parser.add_argument("--organism", default=None)
    parser.add_argument("--input-type", default=None, dest="input_type")
    parser.add_argument("--allowed-columns", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.list_tools:
        for name in list_resolver_tools():
            print(name)
        return

    missing = [
        flag
        for flag, val in (
            ("--tool", args.tool),
            ("--column", args.column),
            ("--field", args.field),
            ("--reason", args.reason),
        )
        if not val
    ]
    if missing:
        parser.error(f"required when not using --list-tools: {', '.join(missing)}")

    resolver_kwargs: dict[str, object] = {}
    if args.organism is not None:
        resolver_kwargs["organism"] = args.organism
    if args.input_type is not None:
        resolver_kwargs["input_type"] = args.input_type

    allowed_columns = None
    if args.allowed_columns:
        allowed_columns = {c.strip() for c in args.allowed_columns.split(",") if c.strip()}

    result = apply_resolution_pass(
        os.fspath(args.lance_db_path),
        table_name=args.table,
        tool=args.tool,
        column=args.column,
        field=args.field,
        reason=args.reason,
        source_column=args.source_column,
        resolver_kwargs=resolver_kwargs or None,
        allowed_columns=allowed_columns,
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
