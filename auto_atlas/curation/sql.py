"""SQL helpers for Lance table update predicates."""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from auto_atlas.util import sql_escape


def format_sql_literal(value: Any, field_type: pa.DataType) -> str:
    """Format a Python value as a Lance SQL literal for the given Arrow type."""
    if value is None:
        raise ValueError("format_sql_literal does not accept None; use IS NULL predicates instead")

    if pa.types.is_string(field_type) or pa.types.is_large_string(field_type):
        return f"'{sql_escape(str(value))}'"

    if pa.types.is_boolean(field_type):
        return "true" if value else "false"

    if pa.types.is_integer(field_type):
        return str(int(value))

    if pa.types.is_floating(field_type):
        return str(float(value))

    return f"'{sql_escape(str(value))}'"


def build_where_clause(column: str, old_value: Any, field_type: pa.DataType) -> str:
    """Build a SQL WHERE predicate for a find-and-replace operation."""
    if old_value is None:
        return f"{column} IS NULL"
    literal = format_sql_literal(old_value, field_type)
    return f"{column} = {literal}"
