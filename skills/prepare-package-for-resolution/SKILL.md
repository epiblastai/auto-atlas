---
name: prepare-package-for-resolution
description: Use after create-data-package when a coalesced data package and homeobox schema file are ready. Stages per-dataset OBS and VAR and collection-level LIBRARY tables into Lance.
---

# Prepare package for resolution

## Scope: raw staging only

This skill loads raw tables into Lance and names them after homeobox schema classes. It does not download files, standardize columns, or ingest arrays. Run `create-data-package` first.

Tables are named after schema classes (e.g. `GeneticPerturbationSchema`) but their **columns are kept exactly as found in the source file**. This skill does not rename, reshape, or otherwise align columns to the schema's fields, so a staged table will usually not conform to its schema yet. That is expected. Evolving these raw tables into the final schema-aligned form is the job of other downstream skills.

Do not stage publication or `DatasetSchema` tables here — those rows are created when an ingestion script is written.

## Expected input

- A coalesced collection with `collection.json` at the root
- A schema file path from the user (ask if missing)

## Workflow

### 1. Stage OBS and VAR

Unless the user already said whether they trust the schema file, ask. Use `--parse-mode runtime` only if they do; otherwise omit the flag (defaults to safe AST parsing).

```
python scripts/stage_lance_tables.py <collection_root> \
  --schema <path/to/schema.py> \
  [--parse-mode runtime] \
  [--obs-class CellIndex]
```

If the script fails because the schema defines multiple obs tables, ask the user which class to use and re-run with `--obs-class`.

### 2. Stage LIBRARY tables

Collection-level `LIBRARY` files (in `collection.json` → `shared_files`) may be staged into `<collection_root>/lance_db/`. Read the schema and decide which CamelCase table each library file belongs to (e.g. `GeneticPerturbationSchema`). If more than one table is plausible, ask the user.

Run once per library file:

```
python scripts/stage_library_table.py <collection_root> \
  --library <path/to/library.csv> \
  --table <SchemaClassName>
```

Supported formats: `.parquet`, `.csv`, `.tsv`, `.tsv.gz`, `.xlsx`.

#### Multi-sheet `.xlsx` files

A single `.xlsx` library file may contain many sheets, and often only one (or a few) are actual reference tables. The script loads one sheet per run (`--sheet-name`, default is the first sheet), so never assume the default sheet is the right one.

Before staging, inspect the workbook: list the sheet names and preview each sheet's columns. Then stage only the sheet(s) that are genuine reference/library tables, choosing the matching schema class for each:

```
python scripts/stage_library_table.py <collection_root> \
  --library <path/to/library.xlsx> \
  --table <SchemaClassName> \
  --sheet-name <SheetName>
```

If it is unclear which sheet is the library, or which schema table a sheet maps to, ask the user rather than guessing.

## Scripts

| Script | Usage | Purpose |
|--------|-------|---------|
| `scripts/stage_lance_tables.py` | `python scripts/stage_lance_tables.py <collection_root> --schema <schema.py> [--parse-mode runtime] [--obs-class NAME]` | Stage OBS/VAR into per-dataset `lance_db/` |
| `scripts/stage_library_table.py` | `python scripts/stage_library_table.py <collection_root> --library <file> --table <SchemaClassName> [--sheet-name SHEET]` | Stage one LIBRARY file into collection `lance_db/` |
