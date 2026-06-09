"""Ingest a finalized :class:`~auto_atlas.collection.Collection` into a homeobox atlas.

This is step 6 of the auto_atlas pipeline (after ``finalize-tables``). A finalized
collection already has, on disk:

- ``<root>/lance_db/`` — collection-level registry-key tables (donors, perturbations,
  publications, …), named by schema class, with ``uid`` assigned.
- ``<root>/<dataset>/lance_db/`` — a finalized **bare obs table** (e.g. ``CellIndex``)
  carrying resolved ``uid`` / ``dataset_uid`` / registry keys / derived fields; a
  per-dataset **feature registry / var table** (e.g. ``GenomicFeatureSchema``) in local
  matrix-column order with registry ``uid``; a **dataset table** (a ``DatasetSchema``
  subclass) with one row per feature space; and — for multimodal datasets only — per
  feature-space obs tables ``<ObsClass>_<fs>`` carrying a stamped ``uid`` linking back
  to the bare table. Every obs table carries ``obs_index`` — the original per-modality
  DATA-array row barcode.

What is *not* on disk is the array data written to zarr and the obs pointer columns that
reference it. That is this module's job.

Design choices (see plan):

- We do **not** use ``homeobox.ingestion.add_anndata_batch`` for the obs write because it
  regenerates ``uid`` and writes only one pointer field per call. Instead we reuse
  homeobox's low-level zarr writers and pointer builders, then assemble the obs rows
  ourselves from the finalized bare obs table — preserving the finalized identity and
  supporting a single multimodal obs row carrying multiple modality pointers.
- **Writing zarr is independent of obs order.** A reader streams a DATA matrix in its
  native row order; we write that straight to zarr and build a minimal pointer table keyed
  by ``obs_index`` (the DATA file's own row barcodes). We then *merge* that table onto the
  obs tables by ``obs_index`` to attach the finalized ``uid``, and finally map ``uid`` onto
  the bare obs. No row reordering, no alignment context threaded into readers.

The only thing that varies wildly across datasets is how raw DATA files are loaded. That is
factored into the :class:`DataReader` abstraction. Only ``.h5ad`` is built in; everything
else (mtx, csv/tsv, COO, …) is a user-supplied reader registered via
:meth:`CollectionIngestor.register_reader`.

No ``pathlib``: paths are plain strings joined with ``os.path`` so s3 urls keep working.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import lancedb
import numpy as np
import pyarrow as pa
import scipy.sparse as sp

# Low-level homeobox primitives we reuse rather than the high-level batch functions.
from homeobox.atlas import RaggedAtlas, create_or_open_atlas
from homeobox.group_specs import get_spec
from homeobox.ingestion import (
    _CHUNK_ELEMS,
    _SHARD_ELEMS,
    _make_sparse_pointer,
    _write_dense_batched,
    _write_sparse_batched,
)
from homeobox.parser import parse_schema_module
from homeobox.pointer_types import DenseZarrPointer, SparseZarrPointer

from auto_atlas.collection import Collection, FileTypeTag
from auto_atlas.types import SchemaInfo
from auto_atlas.util import load_schema_info

LANCE_DB_DIR = "lance_db"
OBS_INDEX_COLUMN = "obs_index"
UID_COLUMN = "uid"


# ===========================================================================
# Reader abstraction
# ===========================================================================


@dataclass(frozen=True)
class ReaderContext:
    """Everything a reader needs to load one feature space of one dataset.

    The reader's contract is narrow and **order-free**: stream the DATA matrix in whatever
    native row order the files have, and report, per matrix row, the ``obs_index`` barcode
    that identifies it (so the ingestor can later merge pointers onto obs). The ``var`` table
    is one row per matrix column carrying the registry ``uid``. The ingestor handles zarr
    layout, identity, pointers, registries and obs assembly.
    """

    dataset_name: str
    feature_space: str
    data_files: list[str]
    var_files: list[str]
    # The per-dataset feature registry / var table (local matrix-column order + ``uid``).
    var_table: pa.Table | None
    pointer_type: type  # SparseZarrPointer | DenseZarrPointer
    registry_schema: type | None  # feature registry class, or None (e.g. image_tiles)


@dataclass
class LoadedMatrix:
    """A reader's output: a DATA matrix in native row order plus its row identities.

    Exactly one of ``csr`` / ``dense`` is set, matching the feature space's pointer type.
    ``row_ids`` are the ``obs_index`` barcodes, one per matrix row, in matrix-row order.
    ``var`` is one row per matrix column with a ``uid`` column matching the feature registry.
    """

    var: pa.Table
    row_ids: list[str]
    csr: sp.csr_matrix | None = None
    dense: np.ndarray | None = None

    @property
    def matrix(self) -> Any:
        return self.csr if self.csr is not None else self.dense

    @property
    def n_rows(self) -> int:
        return len(self.row_ids)

    def validate(self, ctx: ReaderContext) -> None:
        mat = self.matrix
        if mat is None:
            raise ValueError(f"{ctx.dataset_name}/{ctx.feature_space}: reader returned no matrix")
        if mat.shape[0] != len(self.row_ids):
            raise ValueError(
                f"{ctx.dataset_name}/{ctx.feature_space}: matrix has {mat.shape[0]} rows, "
                f"but row_ids has {len(self.row_ids)} entries"
            )
        if mat.shape[1] != self.var.num_rows:
            raise ValueError(
                f"{ctx.dataset_name}/{ctx.feature_space}: matrix has {mat.shape[1]} columns, "
                f"but var has {self.var.num_rows} rows"
            )
        if UID_COLUMN not in self.var.column_names:
            raise ValueError(
                f"{ctx.dataset_name}/{ctx.feature_space}: var table is missing a {UID_COLUMN!r} column"
            )


@runtime_checkable
class DataReader(Protocol):
    """Loads raw DATA for one feature space into a :class:`LoadedMatrix`."""

    def can_read(self, ctx: ReaderContext) -> bool: ...

    # TODO: Should add batched loading with an iter instead
    # of loading everything as once
    def load(self, ctx: ReaderContext) -> LoadedMatrix: ...


class H5adReader:
    """Built-in reader for ``.h5ad`` DATA files (read in backed mode).

    Assumes exactly one ``.h5ad`` data file for the feature space, with ``X`` already
    cell x feature. The matrix is taken in its native row order; ``row_ids`` are
    ``adata.obs_names``. ``var`` rows come from ``ctx.var_table`` (local matrix-column
    order + registry ``uid``).
    """

    def can_read(self, ctx: ReaderContext) -> bool:
        return len(self._h5ads(ctx)) == 1

    @staticmethod
    def _h5ads(ctx: ReaderContext) -> list[str]:
        return [p for p in ctx.data_files if p.endswith(".h5ad")]

    def load(self, ctx: ReaderContext) -> LoadedMatrix:
        import anndata as ad

        h5ads = self._h5ads(ctx)
        if len(h5ads) != 1:
            raise ValueError(
                f"{ctx.dataset_name}/{ctx.feature_space}: H5adReader expects exactly one "
                f".h5ad data file, found {h5ads}"
            )
        if ctx.var_table is None:
            raise ValueError(
                f"{ctx.dataset_name}/{ctx.feature_space}: H5adReader needs a var table "
                "(per-dataset feature registry) for column->uid mapping"
            )

        adata = ad.read_h5ad(h5ads[0], backed="r")
        # TODO: This defeats the purpose of backed mode
        X = adata.X[:]  # materialize in native order
        # TODO: Confused by why we would want this?
        # Don't we just want `range(len(adata))`, what are these ids?
        row_ids = [str(name) for name in adata.obs_names]

        loaded = LoadedMatrix(var=ctx.var_table, row_ids=row_ids)
        if ctx.pointer_type is SparseZarrPointer:
            loaded.csr = X if isinstance(X, sp.csr_matrix) else sp.csr_matrix(X)
        elif ctx.pointer_type is DenseZarrPointer:
            loaded.dense = np.asarray(X.todense() if sp.issparse(X) else X)
        else:
            raise NotImplementedError(
                f"H5adReader does not support pointer type {ctx.pointer_type!r}"
            )
        loaded.validate(ctx)
        return loaded


# ===========================================================================
# Schema introspection
# ===========================================================================


# TODO: This is literally homeobox.schema.PointerField, no reason to recreate it.
@dataclass
class _ObsPointer:
    field_name: str
    feature_space: str
    registry_class: str | None  # feature registry schema class name, or None


@dataclass
class _SchemaModel:
    """Resolved schema facts the ingestor needs, derived from the schema module."""

    info: SchemaInfo
    obs_class: str
    dataset_class: str
    pointers: list[_ObsPointer]
    # Collection-level registry-key tables to copy (entity/table kinds).
    registry_key_classes: list[str]

    @property
    def obs_cls(self) -> type:
        return self.info.live_class(self.obs_class)

    @property
    def dataset_cls(self) -> type:
        return self.info.live_class(self.dataset_class)

    def feature_space_registry(self) -> dict[str, type]:
        """``{feature_space: live registry class}`` for pointers that have a registry."""
        out: dict[str, type] = {}
        for p in self.pointers:
            if p.registry_class is None:
                continue
            cls = self.info.live_class(p.registry_class)
            if cls is not None:
                out[p.feature_space] = cls
        return out

    def pointer_for(self, feature_space: str) -> _ObsPointer:
        for p in self.pointers:
            if p.feature_space == feature_space:
                return p
        raise KeyError(f"No obs pointer field for feature space {feature_space!r}")


def _resolve_schema(schema_path: str) -> _SchemaModel:
    info = load_schema_info(schema_path)
    parsed = parse_schema_module(info.module)

    obs = parsed.get("obs")
    dataset = parsed.get("dataset")
    if obs is None or dataset is None:
        raise ValueError("schema must declare exactly one obs table and one dataset table")

    pointers: list[_ObsPointer] = []
    for fobj in obs.get("fields", []):
        pmeta = fobj.get("pointer")
        if not pmeta:
            continue
        pointers.append(
            _ObsPointer(
                field_name=fobj["name"],
                feature_space=pmeta["feature_space"],
                registry_class=pmeta.get("feature_registry_schema"),
            )
        )

    registry_key_classes = [
        name for name, kind in info.kinds.items() if kind in {"entity", "table"}
    ]

    return _SchemaModel(
        info=info,
        obs_class=obs["class_name"],
        dataset_class=dataset["class_name"],
        pointers=pointers,
        registry_key_classes=registry_key_classes,
    )


# ===========================================================================
# CollectionIngestor
# ===========================================================================


@dataclass
class IngestReport:
    datasets_ingested: list[str] = field(default_factory=list)
    datasets_skipped: list[str] = field(default_factory=list)
    rows_per_feature_space: dict[str, int] = field(default_factory=dict)
    features_registered: dict[str, int] = field(default_factory=dict)
    registry_tables_copied: dict[str, int] = field(default_factory=dict)

    def __str__(self) -> str:
        lines = ["Ingestion report:"]
        lines.append(f"  datasets ingested: {self.datasets_ingested}")
        if self.datasets_skipped:
            lines.append(f"  datasets skipped (already present): {self.datasets_skipped}")
        lines.append(f"  rows per feature space: {self.rows_per_feature_space}")
        lines.append(f"  features registered: {self.features_registered}")
        lines.append(f"  registry tables copied: {self.registry_tables_copied}")
        return "\n".join(lines)


class CollectionIngestor:
    """Add a finalized collection and all its datasets to a homeobox atlas.

    Typical use::

        ing = CollectionIngestor(root_dir, schema_path, atlas_path)
        ing.register_reader(MyMtxReader(), feature_space="gene_expression")  # if needed
        report = ing.run()
    """

    def __init__(
        self,
        root_dir: str,
        schema_path: str,
        atlas_path: str,
        *,
        obs_table_name: str | None = None,
        zarr_layer: str = "counts",
        store_kwargs: dict | None = None,
        skip_existing: bool = True,
        write_csc: bool = False,
        dry_run: bool = False,
    ) -> None:
        self.root_dir = os.fspath(root_dir)
        self.atlas_path = os.fspath(atlas_path)
        self.zarr_layer = zarr_layer
        self.store_kwargs = store_kwargs
        self.skip_existing = skip_existing
        self.write_csc = write_csc
        self.dry_run = dry_run

        self.collection = Collection.from_json(os.path.join(self.root_dir, "collection.json"))
        self.schema = _resolve_schema(schema_path)
        self.obs_table_name = obs_table_name or self.schema.obs_class

        # Built-in readers come last; user readers take precedence (see _resolve_reader).
        self._builtin_readers: list[DataReader] = [H5adReader()]
        self._global_readers: list[DataReader] = []
        self._fs_readers: dict[str, DataReader] = {}
        self._dataset_readers: dict[str, DataReader] = {}

        self._atlas: RaggedAtlas | None = None

    # -- reader registration ------------------------------------------------

    def register_reader(
        self,
        reader: DataReader,
        *,
        dataset: str | None = None,
        feature_space: str | None = None,
    ) -> None:
        """Register a custom reader. More specific scopes win (dataset > fs > global)."""
        if dataset is not None:
            self._dataset_readers[dataset] = reader
        elif feature_space is not None:
            self._fs_readers[feature_space] = reader
        else:
            self._global_readers.append(reader)

    def _resolve_reader(self, ctx: ReaderContext) -> DataReader:
        if ctx.dataset_name in self._dataset_readers:
            return self._dataset_readers[ctx.dataset_name]
        if ctx.feature_space in self._fs_readers:
            return self._fs_readers[ctx.feature_space]
        for reader in (*self._global_readers, *self._builtin_readers):
            if reader.can_read(ctx):
                return reader
        raise ValueError(
            f"No reader can handle {ctx.dataset_name}/{ctx.feature_space}; "
            f"data files: {ctx.data_files}. Register a custom DataReader."
        )

    # -- run ----------------------------------------------------------------

    def run(self) -> IngestReport:
        report = IngestReport()
        self._atlas = self._open_atlas()

        self._copy_registry_tables(report)
        self._register_features(report)

        existing = self._existing_dataset_uids()
        for name in self.collection.datasets:
            dataset = self.collection._datasets[name]
            if self.skip_existing and dataset.uid in existing:
                print(f"== {name}: dataset_uid {dataset.uid} already in atlas, skipping ==")
                report.datasets_skipped.append(name)
                continue
            print(f"== ingesting {name} ==")
            self._ingest_dataset(name, dataset, report)
            report.datasets_ingested.append(name)

        print(report)
        return report

    # -- atlas setup --------------------------------------------------------

    def _open_atlas(self) -> RaggedAtlas:
        registry_schemas = self.schema.feature_space_registry()
        return create_or_open_atlas(
            self.atlas_path,
            obs_schemas={self.obs_table_name: self.schema.obs_cls},
            dataset_table_name=self.schema.dataset_class,
            dataset_schema=self.schema.dataset_cls,
            registry_schemas=registry_schemas,
            store_kwargs=self.store_kwargs,
        )

    def _atlas_db(self) -> lancedb.DBConnection:
        return lancedb.connect(os.path.join(self.atlas_path, LANCE_DB_DIR))

    def _existing_dataset_uids(self) -> set[str]:
        try:
            df = self._atlas._dataset_table.search().select(["dataset_uid"]).to_polars()
        except Exception:
            return set()
        if df.is_empty():
            return set()
        return set(df["dataset_uid"].to_list())

    def _copy_registry_tables(self, report: IngestReport) -> None:
        """Copy collection-level registry-key tables into the atlas (dedup on uid)."""
        coll_db_path = os.path.join(self.root_dir, LANCE_DB_DIR)
        if not os.path.isdir(coll_db_path):
            return
        src = lancedb.connect(coll_db_path)
        names = set(src.list_tables().tables)
        dst = self._atlas_db()
        dst_names = set(dst.list_tables().tables)

        for cls in self.schema.registry_key_classes:
            if cls not in names:
                continue
            arrow = src.open_table(cls).to_arrow()
            print(f"  registry-key table {cls}: {arrow.num_rows} row(s)")
            report.registry_tables_copied[cls] = arrow.num_rows
            if self.dry_run:
                continue
            if cls not in dst_names:
                dst.create_table(cls, data=arrow)
            else:
                (
                    dst.open_table(cls)
                    .merge_insert(on=UID_COLUMN)
                    .when_not_matched_insert_all()
                    .execute(arrow)
                )

    def _register_features(self, report: IngestReport) -> None:
        """Register feature registries per feature space, unioned across datasets."""
        registries = self.schema.feature_space_registry()
        for feature_space, registry_cls in registries.items():
            records = []
            for name in self.collection.datasets:
                tbl = self._open_dataset_table(name, registry_cls.__name__)
                if tbl is None:
                    continue
                df = tbl.to_arrow().to_pandas()
                records.extend(registry_cls(**row.to_dict()) for _, row in df.iterrows())
            if not records:
                continue
            print(f"  register_features({feature_space}): {len(records)} record(s)")
            if self.dry_run:
                report.features_registered[feature_space] = 0
                continue
            n_new = self._atlas.register_features(feature_space, records)
            report.features_registered[feature_space] = n_new
            print(f"    {n_new} new")

    # -- per-dataset ingestion ---------------------------------------------

    def _ingest_dataset(self, name: str, dataset: Any, report: IngestReport) -> None:
        bare = self._open_dataset_table(name, self.obs_table_name)
        if bare is None:
            raise ValueError(f"{name}: no finalized obs table {self.obs_table_name!r}")
        bare_obs = bare.to_arrow()

        # field_name -> {uid: pointer dict}, merged in from each feature space's zarr write.
        pointer_by_field: dict[str, dict[str, dict]] = {}
        for feature_space in dataset.feature_spaces:
            ctx = self._build_context(name, dataset, feature_space)
            reader = self._resolve_reader(ctx)
            loaded = reader.load(ctx)
            loaded.validate(ctx)

            uid_to_pointer = self._write_feature_space(name, dataset, ctx, loaded)
            field_name = self.schema.pointer_for(feature_space).field_name
            pointer_by_field[field_name] = uid_to_pointer
            report.rows_per_feature_space[feature_space] = (
                report.rows_per_feature_space.get(feature_space, 0) + loaded.n_rows
            )

        if self.dry_run:
            return

        obs_arrow = self._assemble_obs(bare_obs, pointer_by_field)
        self._atlas.add_obs_records(obs_arrow, obs_table_name=self.obs_table_name)
        print(f"  added {obs_arrow.num_rows} obs row(s)")

    def _build_context(self, name: str, dataset: Any, feature_space: str) -> ReaderContext:
        pointer = self.schema.pointer_for(feature_space)
        spec = get_spec(feature_space)
        registry_cls = (
            self.schema.info.live_class(pointer.registry_class) if pointer.registry_class else None
        )

        data_files = dataset.files_for(tag=FileTypeTag.DATA, feature_space=feature_space)
        var_files = dataset.files_for(tag=FileTypeTag.VAR, feature_space=feature_space)
        var_tbl = (
            self._open_dataset_table(name, registry_cls.__name__)
            if registry_cls is not None
            else None
        )
        var_table = var_tbl.to_arrow() if var_tbl is not None else None

        return ReaderContext(
            dataset_name=name,
            feature_space=feature_space,
            data_files=data_files,
            var_files=var_files,
            var_table=var_table,
            pointer_type=spec.pointer_type,
            registry_schema=registry_cls,
        )

    def _write_feature_space(
        self, name: str, dataset: Any, ctx: ReaderContext, loaded: LoadedMatrix
    ) -> dict[str, dict]:
        """Register the dataset row, write zarr in native order, and merge pointers to ``uid``.

        Returns ``{uid: pointer dict}`` — the per-feature-space pointer for each finalized obs
        row that has this modality. Rows lacking the modality simply don't appear in the map.
        """
        spec = get_spec(ctx.feature_space)
        dataset_record = self._build_dataset_record(name, dataset, ctx)
        zarr_group = dataset_record.zarr_group

        if self.dry_run:
            return {}

        # 1. Register the dataset row (+ feature layout where the fs has a var registry).
        if spec.has_var_df:
            import polars as pl

            self._atlas.register_dataset(dataset_record, var_df=pl.from_arrow(loaded.var))
        else:
            self._atlas.register_dataset(dataset_record)

        # 2. Write the matrix to zarr in native row order; build one pointer per matrix row.
        group = self._atlas.create_zarr_group(zarr_group)
        adata = self._as_anndata(loaded)
        if ctx.pointer_type is SparseZarrPointer:
            chunk_shape, shard_shape = (_CHUNK_ELEMS,), (_SHARD_ELEMS,)
            starts, ends = _write_sparse_batched(
                group, adata, self.zarr_layer, chunk_shape, shard_shape, spec
            )
            row_pointers = _make_sparse_pointer(zarr_group, starts, ends).to_pylist()
        elif ctx.pointer_type is DenseZarrPointer:
            n_vars = adata.n_vars
            chunk_shape = (max(1, _CHUNK_ELEMS // max(1, n_vars)), n_vars)
            shard_rows = (
                max(chunk_shape[0], (_SHARD_ELEMS // n_vars // chunk_shape[0]) * chunk_shape[0])
                if n_vars
                else chunk_shape[0]
            )
            shard_shape = (max(shard_rows, chunk_shape[0]), n_vars)
            _write_dense_batched(group, adata, self.zarr_layer, chunk_shape, shard_shape, spec)
            row_pointers = [{"zarr_group": zarr_group, "position": i} for i in range(loaded.n_rows)]
        else:
            raise NotImplementedError(f"unsupported pointer type {ctx.pointer_type!r}")

        # 3. Minimal pointer table keyed by the DATA file's own row identity (obs_index).
        pointer_by_obs_index = dict(zip(loaded.row_ids, row_pointers, strict=True))

        # 4. Merge onto the obs table that carries this modality's obs_index to attach uid.
        return self._merge_pointers_to_uid(name, ctx.feature_space, pointer_by_obs_index)

    def _merge_pointers_to_uid(
        self, name: str, feature_space: str, pointer_by_obs_index: dict[str, dict]
    ) -> dict[str, dict]:
        """Join ``{obs_index: pointer}`` onto the modality's obs table to get ``{uid: pointer}``.

        The modality's obs table is the suffixed ``<ObsClass>_<fs>`` table for multimodal
        datasets, or the bare obs table for single-modality datasets. Both carry ``obs_index``
        (the DATA-array row barcode) and the finalized ``uid``.
        """
        suffixed = self._open_dataset_table(name, f"{self.obs_table_name}_{feature_space}")
        src = (
            suffixed
            if suffixed is not None
            else self._open_dataset_table(name, self.obs_table_name)
        )
        arrow = src.to_arrow()
        for col in (OBS_INDEX_COLUMN, UID_COLUMN):
            if col not in arrow.column_names:
                raise ValueError(
                    f"{name}/{feature_space}: obs table is missing {col!r}; "
                    f"available: {arrow.column_names}"
                )

        obs_index = arrow.column(OBS_INDEX_COLUMN).to_pylist()
        uids = arrow.column(UID_COLUMN).to_pylist()

        uid_to_pointer: dict[str, dict] = {}
        missing: list[str] = []
        for bc, uid in zip(obs_index, uids, strict=True):
            pointer = pointer_by_obs_index.get(bc)
            if pointer is None:
                missing.append(bc)
                continue
            uid_to_pointer[str(uid)] = pointer
        if missing:
            raise ValueError(
                f"{name}/{feature_space}: {len(missing)} obs row(s) have an obs_index with no "
                f"matching DATA row; examples: {missing[:5]}"
            )
        return uid_to_pointer

    def _build_dataset_record(self, name: str, dataset: Any, ctx: ReaderContext) -> Any:
        """Reuse the finalized DatasetSchema row for this fs, filling SummaryFields from obs."""
        dataset_cls = self.schema.dataset_cls
        tbl = self._open_dataset_table(name, self.schema.dataset_class)
        if tbl is None:
            raise ValueError(f"{name}: no dataset table {self.schema.dataset_class!r}")
        df = tbl.to_arrow().to_pandas()
        rows = df[df["feature_space"] == ctx.feature_space]
        if rows.empty:
            raise ValueError(
                f"{name}: dataset table has no row for feature_space={ctx.feature_space!r}"
            )
        record_data = rows.iloc[0].to_dict()
        record_data = self._fill_summary_fields(name, record_data)
        return dataset_cls(**record_data)

    def _fill_summary_fields(self, name: str, record_data: dict) -> dict:
        """Fill DatasetSchema SummaryFields (unique/count) from the bare obs table."""
        summaries = self.schema.info.summary_fields.get(self.schema.dataset_class, [])
        if not summaries:
            return record_data
        bare = self._open_dataset_table(name, self.obs_table_name)
        obs = bare.to_arrow()
        for s in summaries:
            if s.target_field not in obs.column_names:
                continue
            values = obs.column(s.target_field).to_pylist()
            if s.op == "count":
                record_data[s.field_name] = len(values)
            elif s.op == "unique":
                seen = sorted({v for v in values if v is not None})
                record_data[s.field_name] = seen
        return record_data

    # -- obs assembly -------------------------------------------------------

    def _assemble_obs(
        self, bare_obs: pa.Table, pointer_by_field: dict[str, dict[str, dict]]
    ) -> pa.Table:
        """Build an obs arrow table matching the atlas schema, preserving finalized identity.

        Pointer columns are merged onto the bare obs by ``uid``: each ``has_<field>`` flag and
        each pointer struct comes straight from the per-feature-space ``{uid: pointer}`` maps.
        """
        arrow_schema = self.schema.obs_cls.to_arrow_schema()
        n = bare_obs.num_rows
        bare_uids = [str(u) for u in bare_obs.column(UID_COLUMN).to_pylist()]
        columns: dict[str, pa.Array] = {}

        pointer_field_names = {p.field_name for p in self.schema.pointers}
        has_flags = {f"has_{p.field_name}": p.field_name for p in self.schema.pointers}

        for fname in arrow_schema.names:
            ftype = arrow_schema.field(fname).type
            if fname in pointer_field_names:
                uid_to_pointer = pointer_by_field.get(fname, {})
                values = [uid_to_pointer.get(uid) for uid in bare_uids]
                columns[fname] = pa.array(values, type=ftype)
            elif fname in has_flags:
                uid_to_pointer = pointer_by_field.get(has_flags[fname], {})
                columns[fname] = pa.array(
                    [uid in uid_to_pointer for uid in bare_uids], type=pa.bool_()
                )
            elif fname in bare_obs.column_names:
                columns[fname] = bare_obs.column(fname).cast(ftype)
            else:
                columns[fname] = pa.nulls(n, type=ftype)

        return pa.table(columns, schema=arrow_schema)

    # -- anndata + lance helpers --------------------------------------------

    @staticmethod
    def _as_anndata(loaded: LoadedMatrix) -> Any:
        import anndata as ad

        var = loaded.var.to_pandas()
        adata = ad.AnnData(X=loaded.matrix, var=var)
        adata.var.index = adata.var.index.astype(str)
        return adata

    def _open_dataset_table(self, dataset_name: str, table_name: str):
        db_path = os.path.join(self.root_dir, dataset_name, LANCE_DB_DIR)
        if not os.path.isdir(db_path):
            return None
        db = lancedb.connect(db_path)
        if table_name not in db.list_tables().tables:
            return None
        return db.open_table(table_name)
