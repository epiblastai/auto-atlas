"""SQLite audit store for curation transactions."""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

from auto_atlas.curation.types import (
    AppliedChange,
    ColumnReplacement,
    CurationTransaction,
    TransactionStatus,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS curation_transactions (
    transaction_id       TEXT PRIMARY KEY,
    table_name           TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    lance_version_before INTEGER,
    status               TEXT NOT NULL,
    metadata_json        TEXT
);

CREATE TABLE IF NOT EXISTS curation_changes (
    change_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id      TEXT NOT NULL REFERENCES curation_transactions(transaction_id),
    table_name          TEXT NOT NULL,
    column_name         TEXT NOT NULL,
    old_value           TEXT,
    new_value           TEXT,
    tool                TEXT NOT NULL,
    reason              TEXT,
    confidence          REAL,
    source              TEXT,
    alternatives_json   TEXT,
    input_value         TEXT,
    rows_updated        INTEGER,
    lance_version       INTEGER,
    apply_order         INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_changes_txn ON curation_changes(transaction_id);
CREATE INDEX IF NOT EXISTS idx_changes_column ON curation_changes(table_name, column_name);
"""


def _is_remote_path(path: str) -> bool:
    return path.startswith(("s3://", "gs://", "az://"))


def default_audit_db_path(lance_db_path: str | os.PathLike[str]) -> str:
    """Return the default audit DB path co-located with a Lance DB directory."""
    lance_dir = os.fspath(lance_db_path).rstrip(os.sep) or os.fspath(lance_db_path)
    if not _is_remote_path(lance_dir):
        lance_dir = os.path.abspath(lance_dir)
    return os.path.join(os.path.dirname(lance_dir), "curation_audit.db")


class CurationAuditStore:
    """Persistent SQLite store for curation audit records."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = os.fspath(path)
        if not _is_remote_path(self.path):
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def insert_pending_transaction(
        self,
        transaction: CurationTransaction,
        *,
        lance_version_before: int | None,
    ) -> list[int]:
        """Insert a pending transaction and its changes. Returns change row ids."""
        self._conn.execute(
            """
            INSERT INTO curation_transactions (
                transaction_id, table_name, created_at,
                lance_version_before, status, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                transaction.transaction_id,
                transaction.table_name,
                transaction.created_at,
                lance_version_before,
                transaction.status.value,
                json.dumps(transaction.metadata) if transaction.metadata else None,
            ),
        )
        change_ids: list[int] = []
        for order, change in enumerate(transaction.changes):
            cur = self._conn.execute(
                """
                INSERT INTO curation_changes (
                    transaction_id, table_name, column_name, old_value, new_value,
                    tool, reason, confidence, source, alternatives_json, input_value,
                    rows_updated, lance_version, apply_order
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transaction.transaction_id,
                    transaction.table_name,
                    change.column,
                    change.old_value,
                    change.new_value,
                    change.tool,
                    change.reason,
                    change.confidence,
                    change.source,
                    json.dumps(change.alternatives) if change.alternatives else None,
                    change.input_value,
                    None,
                    None,
                    order,
                ),
            )
            change_ids.append(cur.lastrowid)
        self._conn.commit()
        return change_ids

    def record_applied_change(
        self,
        change_id: int,
        *,
        rows_updated: int,
        lance_version: int | None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE curation_changes
            SET rows_updated = ?, lance_version = ?
            WHERE change_id = ?
            """,
            (rows_updated, lance_version, change_id),
        )
        self._conn.commit()

    def finalize_transaction(
        self,
        transaction_id: str,
        *,
        status: TransactionStatus,
    ) -> None:
        self._conn.execute(
            """
            UPDATE curation_transactions
            SET status = ?
            WHERE transaction_id = ?
            """,
            (status.value, transaction_id),
        )
        self._conn.commit()

    def get_revert_version(self, transaction_id: str) -> int | None:
        row = self._conn.execute(
            "SELECT lance_version_before FROM curation_transactions WHERE transaction_id = ?",
            (transaction_id,),
        ).fetchone()
        if row is None:
            return None
        return row["lance_version_before"]

    def get_transaction(self, transaction_id: str) -> dict[str, Any] | None:
        txn = self._conn.execute(
            "SELECT * FROM curation_transactions WHERE transaction_id = ?",
            (transaction_id,),
        ).fetchone()
        if txn is None:
            return None
        changes = self._conn.execute(
            """
            SELECT * FROM curation_changes
            WHERE transaction_id = ?
            ORDER BY apply_order
            """,
            (transaction_id,),
        ).fetchall()
        return {
            "transaction": dict(txn),
            "changes": [dict(c) for c in changes],
        }

    def list_transactions(
        self,
        *,
        table_name: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if table_name is not None:
            clauses.append("table_name = ?")
            params.append(table_name)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self._conn.execute(
            f"""
            SELECT * FROM curation_transactions
            {where}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def load_pending_changes(self, transaction_id: str) -> list[tuple[int, ColumnReplacement]]:
        rows = self._conn.execute(
            """
            SELECT * FROM curation_changes
            WHERE transaction_id = ?
            ORDER BY apply_order
            """,
            (transaction_id,),
        ).fetchall()
        result: list[tuple[int, ColumnReplacement]] = []
        for row in rows:
            alternatives = json.loads(row["alternatives_json"]) if row["alternatives_json"] else []
            replacement = ColumnReplacement(
                column=row["column_name"],
                old_value=row["old_value"],
                new_value=row["new_value"],
                tool=row["tool"],
                reason=row["reason"] or "",
                confidence=row["confidence"],
                source=row["source"],
                alternatives=alternatives,
                input_value=row["input_value"],
            )
            result.append((row["change_id"], replacement))
        return result

    def build_applied_change(self, change_id: int, replacement: ColumnReplacement) -> AppliedChange:
        row = self._conn.execute(
            "SELECT rows_updated, lance_version FROM curation_changes WHERE change_id = ?",
            (change_id,),
        ).fetchone()
        return AppliedChange(
            replacement=replacement,
            change_id=change_id,
            rows_updated=row["rows_updated"] or 0,
            lance_version=row["lance_version"],
        )
