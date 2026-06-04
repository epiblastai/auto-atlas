---
name: schema-harmonization
description: Use this skill to harmonize raw collection- and dataset-level tables in a data package to a homeobox schema. Covers the resolution procedure for adding new columns, replacing values, and using ontology and database resolution tools.
---

# Schema harmonization

Harmonize the raw Lance tables in a data package so they conform to a target homeobox schema: align raw columns to schema fields and resolve raw values to canonical identifiers (genes, ontology terms, proteins, …). Every change is applied as an **audited transaction**, never an ad hoc edit to Lance.

## Input

- A **LanceDB** location and table name. Collection-level foreign keys live in `<collection_root>/lance_db/`; per-dataset obs/var live in `<dataset_dir>/lance_db/`. Table names match schema class names (e.g. `GeneticFeaturenSchema`, `CellIndex`), modulo feature-space suffixes.
- A **target homeobox schema file**. The table name must correspond to one of its schema classes.

## The audit model (read before mutating anything)

All harmonization mutates staged Lance tables through audited transactions:

- A planned change is a **`CurationOp`** with provenance.
- Ops are batched into a **`CurationTransaction`** (table name + ordered list of ops).
- A **`CurationApplicator`** applies the transaction: it updates Lance and records the batch in a SQLite audit database (defaults to `<parent_of_lance_db>/curation_audit.db`).

**Never edit Lance outside the applicator** for harmonization work — the audit trail must match reality, and later assertions raise if direct edits are found. If this restriction blocks you, raise it to the user; do not work around it.

## CurationOps

| Op | Use for |
|---|---|
| `ReplaceValue` | Find-and-replace specific cell values in one column (`old_value` → `new_value`). Typical resolver output when only some rows change. |
| `SetColumn` | Overwrite every row of an existing column (`new_value` constant, or `value_sql` per row). |
| `AddColumn` | Introduce a new column: constant `value`, per-row `value_sql`, or null-initialized via `data_type`. |
| `RenameColumn` | Rename a raw column toward a schema field (`column` → `new_name`). |
| `DropColumn` | Remove a non-schema column during finalization. |
| `CastColumn` | Coerce a column to a schema type (`data_type` Arrow alias, e.g. `"string"`, `"int64"`). |

Every op requires `column` and `tool`. Also set provenance when you have it: `reason`, `confidence`, `source`, `alternatives`, `input_value`. Ops in a transaction run **in order**, so later ops can depend on earlier ones (e.g. `AddColumn` then `SetColumn` on that column). Validation runs up front against the simulated post-op schema; nothing is written if it fails.

## Apply workflow

**Script vs custom code** — Use `scripts/apply_resolution_pass.py` when a single column's distinct values can be resolved and written back in place. Use custom Python (still through `CurationApplicator`) for everything else: renaming raw columns, building staging columns with `value_sql`, copying across columns with `CASE`/`COALESCE`, or driving one `ResolutionReport` into multiple schema fields.

1. **Plan** — decide the table name and ordered `CurationOp` list (and whether a resolution pass runs before or after structural ops).
2. **Dry run** — `applicator.apply(txn, dry_run=True)` (or `--dry-run` on the script) records the transaction in the audit DB but does **not** mutate Lance. Use it to validate ops and provenance, especially for large or mixed transactions.
3. **Apply** — open an applicator, apply with `allowed_columns` guardrails, check the result, close.

Resolution-pass script (one resolver, one column; `--list-tools` for names like `resolve_genes`, `resolve_cell_types`):

```bash
python skills/schema-harmonization/scripts/apply_resolution_pass.py \
  <path/to/lance_db> \
  --table GeneticFeaturenSchema \
  --tool resolve_genes \
  --column target_gene \
  --resolution-field-name symbol \
  --reason "standardize gene symbols" \
  --organism human \
  --dry-run
```

See **references/auditable_curation.md** for the applicator API (imports, `ApplyResult`, `allowed_columns` semantics, `propose_column_replacements`) and the general constraints of the resolution script.

## Resolving values

Resolution maps raw values to canonical identifiers per domain. Shared principles: **never NaN unless there is genuinely no value**, and **flag resolution status** (e.g. a boolean `resolved` column). Per-domain references hold the specific considerations and worked examples:

- **references/gene_resolution.md** — gene symbols and Ensembl IDs (var-level), with a full worked example.
- Other domains (ontology terms, proteins, molecules, genetic perturbation targets) follow the same pattern in their own references under `references/`.

## Conventions

- **One logical step → one transaction** (e.g. "resolve gene symbols on this table", "rename raw columns for alignment"). Split unrelated tables or independent phases into separate transactions.
- **Always set `tool`** to the resolver or script name (e.g. `"resolve_ontology_terms"`, `"schema_align"`).
- **Dry-run first** for large transactions or ones that mix structural and value ops.
- **Never edit Lance outside the applicator** (see the audit model above).
