# Homeobox concepts for schema design

Short reference for the homeobox mechanics that shape a `schema.py`. Read the
SKILL for the workflow and class-selection rules; this covers the underlying
model. Confirm specifics against the live package when they matter.

## Feature spaces and specs

A **feature space** is a registered `FeatureSpaceSpec` pairing a name with a
pointer type, a zarr layout, and a `has_var_df` flag. Pointer fields on obs
tables reference a feature space by name; the spec's `pointer_type` must match
the field's annotation, and this is checked at class-definition time.

`has_var_df` decides whether the space has a **feature axis**:

- `has_var_df=True` → there is a feature axis, so provide a `FeatureBaseSchema`
  registry for it and list it in `registry_schemas` (e.g. genes, proteins).
- `has_var_df=False` → no feature axis, so **no registry** (e.g. raw image
  tiles or image volumes). Do not put it in `registry_schemas`.

Builtin spaces exist (gene_expression, chromatin_accessibility,
protein_abundance, image_features, image_tiles, discrete_image, …). Importing
`homeobox` registers them; check the registry rather than assuming. Custom
spaces are added with `register_spec(...)`.

## Pointer types

Every obs table subclasses `HoxBaseSchema` and declares ≥1 pointer field with
`PointerField.declare(feature_space=...)`. The annotation picks how rows
address data in the zarr group:

| Pointer | Addresses | Use for |
|---------|-----------|---------|
| `SparseZarrPointer` | a ravelled row range (`start`/`end`) | sparse matrices (CSR/CSC) |
| `DenseZarrPointer` | a single row position | dense per-row vectors |
| `DiscreteSpatialPointer` | an N-D box (`min_corner`/`max_corner`) | crops/tiles into image volumes |

`DiscreteSpatialPointer` corners apply to the **leading** axes of the array;
trailing axes are sliced in full. Order the stored array so the boxed axes come
first.

Notes that recur:

- Use `| None` and never set a pointer to bare `None`; multimodal rows omit
  modalities they lack. Each pointer also gets an auto `has_<field>` flag.
- A field name may differ from its `feature_space`, so one space can back
  several columns (e.g. `cycle1_*`, `cycle2_*`).

## Stable UIDs

`StableUIDBaseSchema` / `FeatureBaseSchema` give an entity a deterministic
`uid` derived from one canonical identifier, so the same entity dedupes across
ingestion runs. Mark **at most one** field with `StableUIDField.declare(...)`;
for composite identity, add a derived field and mark that.

How assignment actually works (a common gotcha):

- The deterministic `uid` is assigned by the **bulk** `Schema.compute_stable_uids(df)`
  path during ingestion. Populate any derived stable-identity column first.
- The instance-time validator only **checks** consistency — it does not assign.
  So constructing an object directly with a non-null stable field requires
  passing `uid=make_stable_uid(<identity>)`; otherwise it keeps a random uid
  (and raises if the stable field is set but the uid doesn't match).
- A null stable field falls back to a random uid — fine for unresolved entities,
  but they won't dedupe.

## Datasets table

`DatasetSchema` is the inventory of ingested data. Its primary key is
`zarr_group` (one row per modality write); `dataset_uid` is the logical id
shared across the modalities of one multimodal batch and referenced by
`HoxBaseSchema.dataset_uid`. Auto-managed fields (`dataset_uid`, `zarr_group`,
`feature_space`, `n_rows`, `layout_uid`, `created_at`) are filled by ingestion —
subclass only to add provenance.

## Foreign keys

Homeobox enforces no relational constraints. Make references explicit: name
them `*_uid` / `*_uids`, and comment each with its target table. For
polymorphic references, also store a type column that says which table each uid
belongs to.

## Validating a schema

Build a temporary atlas with `create_or_open_atlas(...)` passing the obs,
dataset, and registry schemas. This exercises class-definition checks, pointer/
feature-space matching, and Arrow/Lance schema generation without ingesting
data. Ensure feature-space specs used by pointer fields are registered first.
