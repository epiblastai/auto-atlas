# Auditable curation

Harmonization mutates staged Lance tables through **audited transactions**, not ad hoc edits. Each planned change is a `CurationOp` with provenance; ops are batched into a `CurationTransaction` and applied with `CurationApplicator`, which updates Lance and records the batch in a SQLite audit database. Use this reference for how to apply changes.

## Where the data lives

- **Lance tables** — staged by `prepare-package-for-resolution` (or equivalent). Collection-level foreign keys: `<collection_root>/lance_db/`. Per-dataset obs/var: `<dataset_dir>/lance_db/`. Table names match schema class names (e.g. `GeneticFeaturenSchema`, `CellIndex`).
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

## Script for running automatic resolution tools

`scripts/apply_resolution_pass.py` runs one registered resolver on one column. Use `--list-tools` for names (`resolve_genes`, `resolve_cell_types`, …). Run once per field; use `--dry-run` before committing.

```bash
python scripts/apply_resolution_pass.py \
  <path/to/lance_db> \
  --table GeneticFeaturenSchema \
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

**Scripts vs custom code** — Use `apply_resolution_pass.py` (or equivalent) when a single column’s distinct values can be resolved and written back in place. Use **custom Python** for intermediate steps: renaming raw columns, building staging columns with `value_sql`, copying across columns with `CASE`/`COALESCE`, or applying one `ResolutionReport` to multiple schema fields. Those steps must still go through `CurationApplicator`; do not mutate Lance directly.

**Basic steps**

1. **Plan** — Decide the Lance `table_name` and list of `CurationOp` instances (and whether a resolution script runs before or after structural ops).
2. **Optional dry run** — `applicator.apply(txn, dry_run=True)` records the transaction and ops in the audit DB but does **not** mutate Lance. Use this to validate ops and provenance before committing. Resolution scripts support `--dry-run` the same way.
3. **Apply** — Open an applicator, apply with column guardrails, check the result, close.

```python
lance_path = "<path/to/lance_db>"
audit_path = default_audit_db_path(lance_path)

txn = CurationTransaction(
    table_name="GeneticFeaturenSchema",
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

### Example: Ensembl IDs and symbols on one table

Raw table has `gene_id` (Ensembl) and `gene_name` (common name). Target schema uses `ensembl_id` and `gene_symbol`. Some rows have null `gene_id` but a usable `gene_name`.

The script always resolves and replaces **in the same column** you pass to `--column`. Plan around that: either update schema columns in place, or resolve a staging column and copy results back with `SetColumn`.

| Phase | What to do |
|-------|------------|
| Align names | `RenameColumn(column="gene_id", new_name="ensembl_id", …)` (and rename `gene_name` → `gene_symbol`). |
| Resolve Ensembl | Script on `ensembl_id` with `--resolution-field-name ensembl_gene_id`. No-op pairs (resolved value already equals the distinct old value) emit no `ReplaceValue` ops. |
| Null Ensembl fallback | Custom transaction: `SetColumn(column="ensembl_id", value_sql="CASE WHEN ensembl_id IS NULL THEN gene_name ELSE ensembl_id END", …)` — symbols temporarily sit in `ensembl_id` for null rows only. |
| Resolve symbols as IDs | Script on `ensembl_id` again (`ensembl_gene_id`, often with `--input-type auto`) so coalesced symbols canonicalize to Ensembl IDs. |
| Resolve symbols | Script on `gene_symbol` with `--resolution-field-name symbol`. |
| Cleanup | Drop any staging columns if you used them instead of in-place coalesce. |

```bash
# After rename transaction is applied
python skills/schema-harmonization/scripts/apply_resolution_pass.py \
  <path/to/lance_db> \
  --table GeneticFeaturenSchema \
  --tool resolve_genes \
  --column ensembl_id \
  --resolution-field-name ensembl_gene_id \
  --reason "canonicalize Ensembl gene IDs" \
  --organism human
```

```python
# Null Ensembl rows: copy symbol into ensembl_id for a second resolve pass
txn = CurationTransaction(
    table_name="GeneticFeaturenSchema",
    changes=[
        SetColumn(
            column="ensembl_id",
            value_sql="CASE WHEN ensembl_id IS NULL THEN gene_name ELSE ensembl_id END",
            tool="schema_align",
            reason="use symbol as resolve input where Ensembl is missing",
        ),
    ],
)
```

**Staging column variant** — If you prefer not to overwrite `ensembl_id` with symbols, add `gene_resolve_input` via `AddColumn` + `value_sql`, run the script on that column, then copy back with `SetColumn(column="ensembl_id", value_sql="CASE WHEN ensembl_id IS NULL THEN gene_resolve_input ELSE ensembl_id END", …)` and drop the staging column. The copy-back step exists only because the script does not write to other columns.

**When custom `propose_column_replacements` helps** — If you resolve in Python (not the script) and want one `ResolutionReport` to drive `ReplaceValue` ops on both `ensembl_id` and `gene_symbol` in a single transaction, call `propose_column_replacements` twice with the same `distinct` list and report. That is optional; separate script passes on each column are usually enough.

**Limitations to plan around** — `ReplaceValue` sets one `new_value` for every row matching `old_value`; it cannot map the same bad Ensembl ID to different symbols on different rows. Failed **non-null** Ensembl IDs with inconsistent `gene_name` values need an explicit agent decision (filter, manual ops, or row-level `SetColumn` expressions), not an implicit script fallback.

## Conventions for agents

- **One logical step → one transaction** — e.g. “resolve gene symbols on this table” or “rename raw columns for schema alignment”. Split unrelated tables or independent phases into separate transactions.
- **Never edit Lance outside the applicator** for harmonization work; otherwise the audit trail will not match reality. Assertions checked later will raise if direct edits are discovered. If this restriction presents problems, raise it to the user and do not proceed without their guidance.
- **Always set `tool`** to the resolver or script name (e.g. `"resolve_ontology_terms"`, `"schema_align"`).
- **Dry-run first** when a transaction is large, spans many `ReplaceValue` ops, or mixes structural and value ops.
