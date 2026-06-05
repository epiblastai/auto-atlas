"""Apply audited column operations to Lance tables."""

from __future__ import annotations

import os
from typing import Any

import lancedb
import pyarrow as pa

from auto_atlas.curation.audit import CurationAuditStore, default_audit_db_path
from auto_atlas.curation.sql import (
    arrow_alias_to_sql_cast,
    arrow_type_from_alias,
    build_add_column_expr,
    build_where_clause,
)
from auto_atlas.curation.types import (
    AddColumn,
    AppliedChange,
    ApplyResult,
    CastColumn,
    CurationOp,
    CurationTransaction,
    DropColumn,
    OpKind,
    RenameColumn,
    ReplaceValue,
    SetColumn,
    TransactionStatus,
)


class CurationApplicator:
    """Apply curation transactions to Lance tables with SQLite audit logging."""

    def __init__(
        self,
        lance_db_path: str | os.PathLike[str],
        audit_db_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self.audit_db_path = (
            os.fspath(audit_db_path) if audit_db_path else default_audit_db_path(lance_db_path)
        )
        self._audit = CurationAuditStore(self.audit_db_path)
        self._db = lancedb.connect(os.fspath(lance_db_path))

    def close(self) -> None:
        self._audit.close()

    def get_revert_version(self, transaction_id: str) -> int | None:
        return self._audit.get_revert_version(transaction_id)

    def apply(
        self,
        transaction: CurationTransaction,
        *,
        dry_run: bool = False,
        allowed_columns: set[str] | None = None,
    ) -> ApplyResult:
        table_name = transaction.table_name

        table = self._db.open_table(table_name)
        self._validate(transaction, table, allowed_columns)

        lance_version_before = table.version
        transaction.status = TransactionStatus.PENDING
        if dry_run:
            transaction.metadata = {**transaction.metadata, "dry_run": True}

        change_ids = self._audit.insert_pending_transaction(
            transaction,
            lance_version_before=lance_version_before,
        )

        if dry_run:
            applied = [
                AppliedChange(
                    operation=op,
                    change_id=cid,
                    rows_updated=None,
                    lance_version=None,
                )
                for cid, op in zip(change_ids, transaction.changes, strict=True)
            ]
            self._audit.finalize_transaction(
                transaction.transaction_id,
                status=TransactionStatus.PENDING,
            )
            return ApplyResult(
                transaction_id=transaction.transaction_id,
                status=TransactionStatus.PENDING,
                lance_version_before=lance_version_before,
                applied_changes=applied,
                dry_run=True,
            )

        applied_changes: list[AppliedChange] = []
        error: str | None = None
        field_types = self._field_types(table)

        try:
            for change_id, change in zip(change_ids, transaction.changes, strict=True):
                rows_updated, version = self._execute(change, table, field_types)
                if change.kind not in (OpKind.REPLACE_VALUE, OpKind.SET_COLUMN):
                    # Schema-altering ops change the columns/types; refresh.
                    field_types = self._field_types(table)
                self._audit.record_applied_change(
                    change_id,
                    rows_updated=rows_updated,
                    lance_version=version,
                )
                applied_changes.append(
                    AppliedChange(
                        operation=change,
                        change_id=change_id,
                        rows_updated=rows_updated,
                        lance_version=version,
                    )
                )
            status = TransactionStatus.APPLIED
        except Exception as exc:
            error = str(exc)
            status = TransactionStatus.PARTIAL if applied_changes else TransactionStatus.FAILED

        self._audit.finalize_transaction(
            transaction.transaction_id,
            status=status,
        )

        return ApplyResult(
            transaction_id=transaction.transaction_id,
            status=status,
            lance_version_before=lance_version_before,
            applied_changes=applied_changes,
            dry_run=False,
            error=error,
        )

    @staticmethod
    def _field_types(table: Any) -> dict[str, pa.DataType]:
        schema = table.schema
        return {name: schema.field(name).type for name in schema.names}

    def _validate(
        self,
        transaction: CurationTransaction,
        table: Any,
        allowed_columns: set[str] | None,
    ) -> None:
        """Check every op up front against the (simulated) evolving schema.

        Walking changes in order lets intra-transaction dependencies validate
        correctly (e.g. add a column then set it). Nothing is recorded or
        mutated if validation fails. Drops are exempt from ``allowed_columns``
        since finalization must be free to remove any non-schema column.
        """
        columns = set(self._field_types(table))

        for change in transaction.changes:
            kind = change.kind
            if kind is OpKind.ADD_COLUMN:
                if change.column in columns:
                    raise ValueError(
                        f"Column '{change.column}' already exists in table "
                        f"'{transaction.table_name}'; use SetColumn to overwrite it."
                    )
            else:
                if change.column not in columns:
                    raise ValueError(
                        f"Column '{change.column}' not found in table "
                        f"'{transaction.table_name}'. Available: {sorted(columns)}"
                    )

            if kind is OpKind.RENAME_COLUMN and change.new_name in columns:
                raise ValueError(
                    f"Cannot rename '{change.column}' to '{change.new_name}': "
                    f"a column with that name already exists."
                )

            gated = change.new_name if kind is OpKind.RENAME_COLUMN else change.column
            if (
                allowed_columns is not None
                and kind is not OpKind.DROP_COLUMN
                and gated not in allowed_columns
            ):
                raise ValueError(
                    f"Column '{gated}' is not in allowed_columns: {sorted(allowed_columns)}"
                )

            # Simulate the schema change for subsequent ops.
            if kind is OpKind.ADD_COLUMN:
                columns.add(change.column)
            elif kind is OpKind.DROP_COLUMN:
                columns.discard(change.column)
            elif kind is OpKind.RENAME_COLUMN:
                columns.discard(change.column)
                columns.add(change.new_name)

    def _execute(
        self,
        change: CurationOp,
        table: Any,
        field_types: dict[str, pa.DataType],
    ) -> tuple[int | None, int | None]:
        """Run one op against the Lance table; return (rows_updated, version)."""
        if isinstance(change, ReplaceValue):
            where = build_where_clause(
                change.column,
                change.old_value,
                field_types[change.column],
            )
            value = self._coerce_update_value(change.new_value, field_types[change.column])
            result = table.update(where=where, values={change.column: value})
            return result.rows_updated, result.version

        if isinstance(change, SetColumn):
            if change.value_sql is not None:
                result = table.update(values_sql={change.column: change.value_sql})
            else:
                value = self._coerce_update_value(change.new_value, field_types[change.column])
                result = table.update(values={change.column: value})
            return result.rows_updated, result.version

        if isinstance(change, AddColumn):
            if change.value_sql is not None:
                result = table.add_columns({change.column: change.value_sql})
            elif change.value is not None:
                expr = build_add_column_expr(change.value, change.data_type)
                result = table.add_columns({change.column: expr})
            elif change.data_type is not None:
                field = pa.field(change.column, arrow_type_from_alias(change.data_type))
                result = table.add_columns(field)
            else:
                raise ValueError(
                    f"AddColumn for '{change.column}' needs value, value_sql, or data_type."
                )
            return None, self._version_after(result, table)

        if isinstance(change, RenameColumn):
            result = table.alter_columns({"path": change.column, "rename": change.new_name})
            return None, self._version_after(result, table)

        if isinstance(change, CastColumn):
            # Lance's alter_columns only re-types within a family, so coerce via
            # a SQL cast into a temp column, then drop the original and rename.
            # The recast column moves to the end of the schema.
            sql_type = arrow_alias_to_sql_cast(change.data_type)
            tmp = f"__cast_{change.column}"
            table.add_columns({tmp: f"cast({change.column} as {sql_type})"})
            table.drop_columns([change.column])
            result = table.alter_columns({"path": tmp, "rename": change.column})
            return None, self._version_after(result, table)

        if isinstance(change, DropColumn):
            result = table.drop_columns([change.column])
            return None, self._version_after(result, table)

        raise ValueError(f"Unsupported operation: {type(change).__name__}")

    @staticmethod
    def _version_after(result: Any, table: Any) -> int | None:
        # add_columns/drop_columns expose .version; alter_columns does not, so
        # fall back to the table's current version.
        version = getattr(result, "version", None)
        return version if version is not None else table.version

    @staticmethod
    def _coerce_update_value(value: Any, field_type: Any) -> Any:
        if value is None:
            return None
        if pa.types.is_boolean(field_type):
            return bool(value)
        if pa.types.is_integer(field_type):
            return int(value)
        if pa.types.is_floating(field_type):
            return float(value)
        return str(value)
