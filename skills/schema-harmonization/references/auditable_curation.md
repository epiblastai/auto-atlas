# Auditable curation

Deep reference for the curation/apply API. The skill body (`SKILL.md`) covers the audit model, the `CurationOp` menu, the apply workflow, and conventions; this file holds the applicator API surface, the Python-resolver path, and the general constraints of the resolution script. All harmonization mutations go through `CurationApplicator` — never edit Lance directly.

## Imports

```python
from auto_atlas import (
    AddColumn,
    CastColumn,
    CurationApplicator,
    CurationAuditStore,
    CurationTransaction,
    DropColumn,
    OpKind,
    RenameColumn,
    ReplaceValue,
    ResolutionReport,
    SetColumn,
    TransactionStatus,
    default_audit_db_path,
)
```

## Applying a transaction

```python
lance_path = "<path/to/lance_db>"
audit_path = default_audit_db_path(lance_path)

txn = CurationTransaction(
    table_name="GeneticFeatureSchema",
    changes=[...],  # list[CurationOp]
    metadata={"organism": "human"},  # optional caller context
)

applicator = CurationApplicator(lance_path, audit_db_path=audit_path)
try:
    result = applicator.apply(
        txn,
        allowed_columns={"target_gene", "ensembl_gene_id"},  # recommended
    )
finally:
    applicator.close()
```

**`allowed_columns`** — Restrict which columns may be mutated. Renames are checked against the **new** name. `DropColumn` is exempt so finalization can remove any non-schema column. Omit only when you intentionally need unrestricted writes.

**`ApplyResult`** — Inspect `result.status` (`applied`, `failed`, `partial`, or `pending` for dry run), `result.applied_changes` (per-op `rows_updated` and `lance_version`), and `result.error` on failure. `result.lance_version_before` is the Lance version to restore if you need to undo the whole transaction.

## From resolver output to ops (Python path)

The resolution-pass script is the happy path for resolving a single column in place. When you resolve in Python instead — e.g. to drive one `ResolutionReport` into multiple schema fields — build `ReplaceValue` ops with `propose_column_replacements`:

```python
distinct = list(dict.fromkeys(gene_symbols))  # values sent to the resolver
report = resolve_genes(distinct, organism="human")
ops = report.propose_column_replacements(
    distinct,                # same distinct old values, aligned with report.results
    column="gene_symbol",
    reason="standardize gene symbols",
    resolution_field_name="symbol",
)
```

`report.tool` (e.g. `"resolve_genes"`) is copied onto each `ReplaceValue` as provenance. Lance matches each op's `old_value` in the column (find-and-replace), not by row index. Unresolved values and no-op replacements are skipped automatically. Pick a different `resolution_field_name` per target column (e.g. `"ensembl_gene_id"`); call it twice with the same `distinct` list and report to populate two columns in one transaction. Combine the resulting ops with structural ops (`AddColumn`, `RenameColumn`, …) in one `CurationTransaction` when they belong to the same step.

## Resolution-script constraints

`scripts/apply_resolution_pass.py` runs one registered resolver on one column. These constraints are **general** — they apply to every resolution domain, not just genes — and they shape how you sequence ops:

- **Same column only.** The script resolves and writes back **within the same `--column`**. To populate one column from another — copy symbols into a staging column, or replace failed IDs from a symbol column — do that first with explicit `AddColumn` / `SetColumn` / `ReplaceValue` transactions (so the audit trail records every step), then run the pass on the column that holds the values being resolved.
- **Staging-column pattern.** When you do not want to overwrite a schema column in place, add a staging column via `AddColumn` + `value_sql`, run the script on it, copy results back with `SetColumn` (`CASE`/`COALESCE`), then `DropColumn` the staging column. This pattern exists only because the script does not write to other columns.
- **One `new_value` per `old_value`.** `ReplaceValue` sets a single `new_value` for every row matching `old_value`; it cannot map the same bad identifier to different values on different rows. Cases that need row-specific mapping require explicit row-level `SetColumn` expressions or an agent decision, not an implicit script fallback.

For a full worked example exercising these constraints (Ensembl IDs and symbols on one table), see **references/gene_resolution.md**.
