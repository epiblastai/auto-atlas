# Auditable curation

Harmonization mutates staged Lance tables through **audited transactions**, not ad hoc edits. Each planned change is a `CurationOp` with provenance; ops are batched into a `CurationTransaction` and applied with `CurationApplicator`, which updates Lance and records the batch in a SQLite audit database. Use this reference for how to apply changes.

## Where the data lives

- **Lance tables** — staged by `prepare-package-for-resolution` (or equivalent). Collection-level foreign keys: `<collection_root>/lance_db/`. Per-dataset obs/var: `<dataset_dir>/lance_db/`. Table names match schema class names (e.g. `GeneticPerturbationSchema`, `CellIndex`).
- **Audit database** — defaults to a sibling of the Lance directory: `<parent_of_lance_db>/curation_audit.db`. 

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

## CurationOps (what to use when)

| Op | Use for |
|---|---|
| `ReplaceValue` | Find-and-replace specific cell values in one column (`old_value` → `new_value`). Typical output of resolvers when only some rows change. |
| `SetColumn` | Overwrite every row of an existing column (`new_value` constant, or `value_sql` per row). |
| `AddColumn` | Introduce a new column: constant `value`, per-row `value_sql`, or null-initialized column via `data_type`. |
| `RenameColumn` | Rename a raw column toward a schema field (`column` → `new_name`). |
| `DropColumn` | Remove a non-schema column during finalization. |
| `CastColumn` | Coerce a column to a schema type (`data_type` Arrow alias, e.g. `"string"`, `"int64"`). |

Every op requires `column` and `tool`. Also set provenance when you have it: `reason`, `confidence`, `source`, `alternatives`, `input_value` (the value passed into a resolver, if it differs from the cell).

Ops in a single transaction run **in order**. You can depend on earlier ops in the same batch (e.g. `AddColumn` then `SetColumn` on that column). Validation runs up front against the simulated post-op schema; nothing is written if validation fails.

## From resolver output to ops

Call `ResolutionReport.propose_column_replacements` to build `ReplaceValue` ops from distinct old values:

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

`report.tool` is set by the resolver (e.g. `"resolve_genes"`) and is copied onto each `ReplaceValue` as provenance. Lance matches each op's `old_value` in the column (find-and-replace), not by row index.

Pick a different `resolution_field_name` per target column (e.g. `"ensembl_gene_id"`). Unresolved values and no-op replacements are skipped automatically.

Combine proposed ops with structural ops (`AddColumn`, `RenameColumn`, etc.) in one `CurationTransaction` when they belong to the same harmonization step.

## Thin Lance pass script

`skills/schema-harmonization/scripts/apply_resolution_pass.py` runs one registered resolver on one column. Use `--list-tools` for names (`resolve_genes`, `resolve_cell_types`, …). Run once per field; use `--dry-run` before committing.

```bash
python skills/schema-harmonization/scripts/apply_resolution_pass.py \
  <path/to/lance_db> \
  --table GeneticPerturbationSchema \
  --tool resolve_genes \
  --column target_gene \
  --resolution-field-name symbol \
  --reason "standardize gene symbols" \
  --organism human \
  --dry-run
```

The script only resolves and replaces within the same column. To populate one column from another (e.g. copy symbols into a staging column, or replace failed Ensembl IDs from a symbol column), do that first with explicit `AddColumn` / `SetColumn` / `ReplaceValue` transactions so the audit trail records every step. Then run a resolution pass on the column that holds the values being resolved.

Built-in tool names are listed with `--list-tools` (see `auto_atlas.tool_registry`).

## Apply workflow

1. **Plan** — Decide the Lance `table_name` and list of `CurationOp` instances.
2. **Optional dry run** — `applicator.apply(txn, dry_run=True)` records the transaction and ops in the audit DB but does **not** mutate Lance. Use this to validate ops and provenance before committing.
3. **Apply** — Open an applicator, apply with column guardrails, check the result, close.

```python
lance_path = "<path/to/lance_db>"
audit_path = default_audit_db_path(lance_path)

txn = CurationTransaction(
    table_name="GeneticPerturbationSchema",
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

## Conventions for agents

- **One logical step → one transaction** — e.g. “resolve gene symbols on this table” or “rename raw columns for schema alignment”. Split unrelated tables or independent phases into separate transactions.
- **Never edit Lance outside the applicator** for harmonization work; otherwise the audit trail will not match reality. If this presents problems, raise it to the user and do not proceed without their guidance.
- **Always set `tool`** to the resolver or script name (e.g. `"resolve_ontology_terms"`, `"schema_align"`).
- **Prefer `allowed_columns`** scoped to the schema fields you are harmonizing in that step.
- **Dry-run first** when a transaction is large, spans many `ReplaceValue` ops, or mixes structural and value ops.
