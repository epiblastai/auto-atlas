"""Datatypes for auditable Lance table curation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


class TransactionStatus(StrEnum):
    """Lifecycle of a curation transaction in the audit store."""

    PENDING = "pending"
    APPLIED = "applied"
    PARTIAL = "partial"  # some replacements applied before failure
    FAILED = "failed"


@dataclass
class ColumnReplacement:
    """One find-and-replace operation on a Lance table column."""

    # Core replacement operation values
    column: str
    old_value: Any
    new_value: Any

    # Essential metadata for justification
    tool: str
    reason: str = ""

    # Additional metadata, when applicable
    confidence: float | None = None
    source: str | None = None
    alternatives: list[str] = field(default_factory=list)
    # When using a resolution tool, this is the value that was given
    # to the tool to resolve. It might be different from old value if
    # some transformation like stripping a prefix or suffix occurred prior
    # to resolution.
    input_value: str | None = None


@dataclass
class CurationTransaction:
    """Batch of column replacements applied in a single apply() call."""

    # Target Lance table and planned replacements
    table_name: str
    changes: list[ColumnReplacement]

    # Assigned when the transaction is created; used by the audit store
    transaction_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    # Updated during apply(); optional caller context (organism, dry_run, etc.)
    status: TransactionStatus = TransactionStatus.PENDING
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AppliedChange:
    """Result of applying a single ColumnReplacement."""

    # Intent and link to the curation_changes audit row
    replacement: ColumnReplacement
    change_id: int

    # Outcome of this table.update() call
    rows_updated: int
    # Lance table version after this step. This can help during
    # debugging to see what the state of a table was immediately before
    # applying a change instead of what it was at the start of a whole
    # transaction.
    lance_version: int | None


@dataclass
class ApplyResult:
    """Result of applying a CurationTransaction."""

    transaction_id: str  # foreign key to CurationTransaction
    status: TransactionStatus

    # Checkout this Lance version to undo the entire transaction
    lance_version_before: int | None

    # One entry per successful replacement (shorter than changes if apply failed)
    applied_changes: list[AppliedChange] = field(default_factory=list)

    # True when audit rows were written but Lance was not updated
    dry_run: bool = False
    # Set when apply stops on the first exception (see status PARTIAL/FAILED)
    error: str | None = None
