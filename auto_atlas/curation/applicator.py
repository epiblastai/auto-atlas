"""Apply audited find-and-replace operations to Lance tables."""

from __future__ import annotations

import os
from typing import Any

import lancedb
import pyarrow as pa

from auto_atlas.curation.audit import CurationAuditStore, default_audit_db_path
from auto_atlas.curation.sql import build_where_clause
from auto_atlas.curation.types import (
    AppliedChange,
    ApplyResult,
    CurationTransaction,
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
        schema = table.schema
        field_types = {name: schema.field(name).type for name in schema.names}

        for change in transaction.changes:
            if change.column not in field_types:
                raise ValueError(
                    f"Column '{change.column}' not found in table '{table_name}'. "
                    f"Available: {list(field_types)}"
                )
            if allowed_columns is not None and change.column not in allowed_columns:
                raise ValueError(
                    f"Column '{change.column}' is not in allowed_columns: {sorted(allowed_columns)}"
                )

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
                    replacement=repl,
                    change_id=cid,
                    rows_updated=0,
                    lance_version=None,
                )
                for cid, repl in zip(change_ids, transaction.changes, strict=True)
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

        try:
            for change_id, change in zip(change_ids, transaction.changes, strict=True):
                where = build_where_clause(
                    change.column,
                    change.old_value,
                    field_types[change.column],
                )
                update_values = self._coerce_update_value(
                    change.new_value,
                    field_types[change.column],
                )
                result = table.update(where=where, values={change.column: update_values})
                self._audit.record_applied_change(
                    change_id,
                    rows_updated=result.rows_updated,
                    lance_version=result.version,
                )
                applied_changes.append(
                    AppliedChange(
                        replacement=change,
                        change_id=change_id,
                        rows_updated=result.rows_updated,
                        lance_version=result.version,
                    )
                )
            status = TransactionStatus.APPLIED
        except Exception as exc:
            error = str(exc)
            status = (
                TransactionStatus.PARTIAL if applied_changes else TransactionStatus.FAILED
            )

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
