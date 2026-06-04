"""Apply one resolver pass to one Lance column.

Resolves distinct values in ``--column`` and applies find-and-replace ops on that
same column. Run once per ``--resolution-field-name``. Cross-column workflows (e.g. filling Ensembl
IDs from symbols) must be separate audited steps — ``AddColumn``, ``SetColumn``, or
explicit ``ReplaceValue`` ops — before running this script on the target column.

    python skills/schema-harmonization/scripts/apply_resolution_pass.py \\
        <lance_db> --table T --tool resolve_genes --column target_gene \\
        --resolution-field-name symbol --reason "standardize symbols" --organism human

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


def resolve_distinct_values(
    values: list[Any],
    tool: str,
    *,
    resolver_kwargs: dict[str, Any] | None = None,
) -> ResolutionReport:
    """Resolve distinct non-null cell values; return the tool's ``ResolutionReport``."""
    spec = RESOLVER_TOOLS.get(tool)
    if spec is None:
        raise ValueError(
            f"Unknown tool {tool!r}. Known tools: {', '.join(list_resolver_tools())}"
        )

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

    report = resolve_distinct_values(
        column_values, tool, resolver_kwargs=resolver_kwargs
    )
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
    parser.add_argument("--reason", required=False)
    parser.add_argument("--organism", default=None)
    parser.add_argument("--input-type", default=None, dest="input_type")
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
            ("--resolution-field-name", args.resolution_field_name),
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
