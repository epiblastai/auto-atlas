"""Datatypes for auditable Lance table curation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar
from uuid import uuid4


class TransactionStatus(StrEnum):
    """Lifecycle of a curation transaction in the audit store."""

    PENDING = "pending"
    APPLIED = "applied"
    PARTIAL = "partial"  # some operations applied before failure
    FAILED = "failed"


class OpKind(StrEnum):
    """Discriminator for the column operations the applicator supports."""

    REPLACE_VALUE = "replace_value"  # find-and-replace specific cell values
    SET_COLUMN = "set_column"  # overwrite every row of a column
    ADD_COLUMN = "add_column"  # introduce a new column
    RENAME_COLUMN = "rename_column"  # rename a column (e.g. raw name -> schema field)
    DROP_COLUMN = "drop_column"  # remove a column (e.g. non-schema raw columns)
    CAST_COLUMN = "cast_column"  # change a column's data type


@dataclass(kw_only=True)
class CurationOp:
    """Base for one auditable column operation.

    Carries the provenance shared by every operation. Subclasses add the
    payload specific to their :class:`OpKind`. ``column`` is the column the
    operation is *about* (the operated column, or the new column for an add).
    """

    # Class-level discriminator; set by each subclass.
    kind: ClassVar[OpKind]

    # Target column and justification metadata (shared by all ops).
    column: str
    tool: str
    reason: str = ""
    confidence: float | None = None
    source: str | None = None
    alternatives: list[str] = field(default_factory=list)
    # When using a resolution tool, this is the value that was given
    # to the tool to resolve. It might be different from old value if
    # some transformation like stripping a prefix or suffix occurred prior
    # to resolution.
    input_value: str | None = None


@dataclass(kw_only=True)
class ReplaceValue(CurationOp):
    """Find-and-replace specific cell values in a column (matched on old_value)."""

    kind: ClassVar[OpKind] = OpKind.REPLACE_VALUE

    old_value: Any
    new_value: Any


@dataclass(kw_only=True)
class SetColumn(CurationOp):
    """Overwrite every row of an existing column.

    Provide either ``new_value`` (a constant applied to all rows) or
    ``value_sql`` (a SQL expression evaluated per row, may reference other
    columns). Useful when a resolver replaces a whole raw column wholesale
    (e.g. resolved ``organism`` overwrites the raw ``organism`` column).
    """

    kind: ClassVar[OpKind] = OpKind.SET_COLUMN

    new_value: Any = None
    value_sql: str | None = None


@dataclass(kw_only=True)
class AddColumn(CurationOp):
    """Add a new column to the table.

    Exactly one of three modes:
    - ``value``: a constant applied to all rows.
    - ``value_sql``: a SQL expression evaluated per row (may reference columns).
    - neither, with ``data_type`` set: a null-initialized column of that type.
    """

    kind: ClassVar[OpKind] = OpKind.ADD_COLUMN

    value: Any = None
    value_sql: str | None = None
    # Serialized Arrow type alias (e.g. "int64", "string"). Optional for a
    # constant/expression add; required when null-initializing.
    data_type: str | None = None


@dataclass(kw_only=True)
class RenameColumn(CurationOp):
    """Rename a column. ``column`` is the source name; ``new_name`` the target."""

    kind: ClassVar[OpKind] = OpKind.RENAME_COLUMN

    new_name: str


@dataclass(kw_only=True)
class DropColumn(CurationOp):
    """Remove a column from the table."""

    kind: ClassVar[OpKind] = OpKind.DROP_COLUMN


@dataclass(kw_only=True)
class CastColumn(CurationOp):
    """Coerce a column to a new data type (e.g. on finalization to parquet)."""

    kind: ClassVar[OpKind] = OpKind.CAST_COLUMN

    # Serialized Arrow type alias (e.g. "int64", "double", "string", "bool").
    data_type: str


@dataclass
class CurationTransaction:
    """Batch of column operations applied in a single apply() call."""

    # Target Lance table and planned operations
    table_name: str
    changes: list[CurationOp]

    # Assigned when the transaction is created; used by the audit store
    transaction_id: str = field(default_factory=lambda: uuid4().hex)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    # Updated during apply(); optional caller context (organism, dry_run, etc.)
    status: TransactionStatus = TransactionStatus.PENDING
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AppliedChange:
    """Result of applying a single :class:`CurationOp`."""

    # Intent and link to the curation_changes audit row
    operation: CurationOp
    change_id: int

    # Rows affected by row-level ops (replace/set); None for schema-only ops
    # (add/rename/drop/cast).
    rows_updated: int | None
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

    # One entry per successful operation (shorter than changes if apply failed)
    applied_changes: list[AppliedChange] = field(default_factory=list)

    # True when audit rows were written but Lance was not updated
    dry_run: bool = False
    # Set when apply stops on the first exception (see status PARTIAL/FAILED)
    error: str | None = None
