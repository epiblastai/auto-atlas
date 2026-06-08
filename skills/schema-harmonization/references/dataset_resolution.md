# Dataset table resolution

Every dataset directory carries a `DatasetSchema` table, staged with **one row per feature space**. Its identity columns (`dataset_uid`, `feature_space`) are already filled from `collection.json`. Harmonization fills the table's *descriptive* metadata and records the dataset's publication link, and leaves the automatic and ingestion-deferred columns alone.

The `DatasetSchema` row describes the dataset as a whole, so the same dataset-level value is written to every per-feature-space row. Its columns fall into three groups, each handled differently.

## 1. Descriptive metadata — fill it

The free-text and accession-style descriptors of the dataset — for example an accession database and id, and a dataset description — come from the dataset's own metadata: the collection/dataset manifest, an accession record (e.g. a GEO series or sample), or publication text coalesced into the package. Fill them with audited ops, under the same discipline as any nullable field:

## 2. The publication registry key — record its join key

The dataset's link to its publication row is a registry key (`publication_uid`). As with every registry key, harmonization does **not** fill the uid; it records the natural join key so finalization can resolve it once uids exist:

- On the dataset table, write `publication_uid_PublicationSchema_join` holding the publication's natural key (an accession, a DOI, whatever identifies the publication row).
- On the publication table, expose the matching key as `PublicationSchema_join`.

A collection is associated with a single publication, so this key is typically one constant value across all dataset rows. Record it anyway — the link is then explicit and resolves through the same mechanism as every other registry key, rather than being a special case.

## 3. Automatic and summary columns — leave them

Two kinds of column on this table are not harmonization's to fill:

- **Automatic columns.** `dataset_uid` is stamped from `collection.json` at staging; `zarr_group` is assigned by finalization. Both are deterministic — no decision or source to record — so do not write them.
- **Summary columns.** A field the schema marks with `SummaryField` is an aggregate of a target table's column — for example a row count, or unique-value rollups of obs columns such as organism or tissue. These are computed at ingestion time, **after** the obs rows are final, not during harmonization — the staged scaffold does not even create them. Do not add or fill a `SummaryField`-marked column here; the marker in the schema is the signal to skip it.

## Rules

- Fill descriptive metadata; never fill `dataset_uid`, `zarr_group`, or any `SummaryField`-marked column.
- Write the same dataset-level value to every per-feature-space row of the table.
- Record the publication link as a `*_join` natural key, never as a uid.
- A `SummaryField`-marked column is filled downstream at ingestion; skipping it here is correct, not an omission.
- Apply every fill as an audited transaction, like all harmonization.
