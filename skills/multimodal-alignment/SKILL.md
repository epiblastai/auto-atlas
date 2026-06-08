---
name: multimodal-alignment
description: Use after prepare-package-for-resolution on multimodal datasets. Reconciles per-modality cell barcodes across feature-space obs tables and writes a canonical multimodal_barcode column via audited curation.
---

# Multimodal alignment

Multimodal datasets (CITE-seq, NEAT-seq, Multiome, …) stage one obs table per feature space. The same physical cell can appear under different barcode string formats across modalities — GEX barcodes with a `-1` well suffix, ADT exports without it, ATAC fragments with a lane prefix like `lane1#ACGTACGT-1`. This skill picks the normalization that maximizes cross-modality overlap and writes a shared **`multimodal_barcode`** on every obs row so downstream steps can join cells across feature spaces.

Run this **after** `prepare-package-for-resolution` has staged obs tables into each dataset's `lance_db/`. It does not read raw matrix or fragment files; barcodes come from the staged **`obs_index`** column.

## Input

- **Per-dataset LanceDB** at `<collection_root>/<dataset>/lance_db/`, as produced by `prepare-package-for-resolution`.
- **Obs schema class name** (e.g. `CellIndex`). Table names follow the staging convention:
  - Single feature space → bare class name (`CellIndex`)
  - Multiple feature spaces → `{obs_class}_{feature_space}` (e.g. `CellIndex_gene_expression`, `CellIndex_protein_abundance`)

## When to run

Run only when a dataset has **two or more** feature-space obs tables. Single-modality datasets are skipped automatically.

## Audit model

All mutations go through audited transactions — **never edit Lance directly**.

- A planned change is a **`CurationOp`** with provenance (`tool`, `reason`, …).
- Ops are batched into a **`CurationTransaction`** (table name + ordered list of ops).
- A **`CurationApplicator`** applies the transaction: it updates Lance and records the batch in a SQLite audit database (defaults to `<parent_of_lance_db>/curation_audit.db`).

Each obs table gets one transaction:

1. **`AddColumn`** — null-initialize `multimodal_barcode` (`data_type="string"`) when the column is not already present.
2. **`MergeColumns`** — keyed on `obs_index`, fill `multimodal_barcode` from the reconciled raw→canonical mapping.

Re-runs are safe: the script skips `AddColumn` when the column exists and re-applies the keyed merge.

**Dry run** — pass `--dry-run` (or `applicator.apply(txn, dry_run=True)`) to record the transaction in the audit DB without mutating Lance. Use this while iterating on overlap.

**Apply** — restrict writes with `allowed_columns` set to `multimodal_barcode`; check `result.status` (`applied`, `failed`, `partial`, or `pending` on dry run) and `result.error` before continuing. For the full curation API (`AddColumn`, `MergeColumns`, `ReplaceValue`, …), read `auto_atlas.curation` for more detail.

## Workflow

**Start with the script.** `reconcile_barcodes.py` accelerates the most common barcode reconciliations — exact match, stripping well/lane prefixes and suffixes, reverse complement, and picking whichever maximizes overlap. Run it first on every multimodal dataset.

If overlap stays poor after iteration, the built-in normalizations may not be enough (e.g. dataset-specific transforms, whitelist filtering, or multi-step pipelines). You may then write a custom script that still applies changes through `CurationApplicator`; treat that as the exception, not the default.

For each multimodal dataset in the collection:

```bash
python scripts/reconcile_barcodes.py \
  <collection_root>/<dataset>/lance_db \
  --obs-class CellIndex \
  --dry-run
```

Remove `--dry-run` to apply. The script prints per-modality barcode counts, the chosen normalization, overlap statistics, and per-table apply status.

### Normalizations tried (in order)

| Name | Transform |
|------|-----------|
| `exact` | identity |
| `strip_suffix` | drop `-1` well suffix |
| `strip_prefix` | drop `lane#` prefix |
| `strip_both` | prefix then suffix |
| `reverse_complement` | DNA reverse complement of the sequence before any suffix |

The script picks whichever yields the largest **minimum** pairwise overlap across feature spaces (not just overlap with one reference modality).

### Interpreting output

Getting a good match may take iteration. If overlap is low, re-check feature-space pairing and whether `obs_index` values need correction upstream, then re-run the script. The goal is **high cross-modality overlap** before you apply — dry-run, adjust, and re-run until the statistics look right. Only after that fails to converge should you reach for a custom normalization script.

- **`common barcodes`** — cells present in every modality after normalization.
- **`unmatched`** per feature space — barcodes unique to that modality after normalization.
- **`WARNING: <50% overlap`** — likely a file-pairing or modality-mismatch problem; investigate before continuing.

Unmatched cells still receive a `multimodal_barcode` (the normalized form of their raw `obs_index`, or the raw value when no mapping applies). They simply will not join across modalities.

## Scripts

| Script | Usage | Purpose |
|--------|-------|---------|
| `scripts/reconcile_barcodes.py` | `python scripts/reconcile_barcodes.py <lance_db> --obs-class CellIndex [--dry-run]` | Reconcile barcodes and write `multimodal_barcode` on all feature-space obs tables |

## Downstream

`multimodal_barcode` is a curation column, not necessarily a schema field. Harmonization may rename or map it if the target schema declares a barcode field; otherwise it remains as a join key.

After harmonization, the **finalize-tables** skill's `join_feature_space_obs.py` merges the per-feature-space obs tables into one table named after the obs schema class (outer join on `multimodal_barcode`), then drops the suffixed source tables.
