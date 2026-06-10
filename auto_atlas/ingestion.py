"""Ingest a finalized :class:`~auto_atlas.collection.Collection` into homeobox.

This is the final write step after auto-atlas has coalesced and finalized a
collection. At this point the package already owns the collection-level registry
tables, per-dataset obs tables, feature registries, and dataset rows. This
module's job is to translate that package state into homeobox ingestion calls:

- copy collection-level registry-key tables into the atlas;
- register feature registries;
- resolve each DATA file into a streaming homeobox ``Reader``;
- map DATA row identities onto finalized obs row positions; and
- let :class:`homeobox.ingestion.Ingestor` write arrays and stamp pointers.

Auto-atlas remains responsible for collection and data-package semantics. The
zarr write path, pointer construction, dataset registration, and final obs insert
are delegated to homeobox.

No ``pathlib``: paths are plain strings joined with ``os.path`` so s3 urls keep
working.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol

import lancedb
import numpy as np
import pandas as pd
import pyarrow as pa
from homeobox.atlas import RaggedAtlas, create_or_open_atlas
from homeobox.group_specs import get_spec
from homeobox.ingestion import AnnDataReader, Ingestor, Reader
from homeobox.parser import parse_schema_module

from auto_atlas.collection import Collection, FileTypeTag
from auto_atlas.types import SchemaInfo
from auto_atlas.util import load_schema_info

LANCE_DB_DIR = "lance_db"
OBS_INDEX_COLUMN = "obs_index"
UID_COLUMN = "uid"
DATASET_UID_COLUMN = "dataset_uid"


# ===========================================================================
# Source abstraction
# ===========================================================================


@dataclass(frozen=True)
class ReaderContext:
    """Everything needed to prepare one feature space of one dataset."""

    dataset_name: str
    feature_space: str
    data_files: list[str]
    var_files: list[str]
    # The per-dataset feature registry / var table in local matrix-column order.
    var_table: pa.Table | None
    registry_schema: type | None


@dataclass
class PreparedSource:
    """A streaming matrix source plus the metadata homeobox needs to ingest it.

    ``row_ids`` are DATA-native row identities in the same order emitted by
    ``reader``. Auto-atlas maps them to integer obs positions and passes those
    positions to ``Ingestor.write_array(obs_indices=...)``.
    """

    reader: Reader
    row_ids: list[str]
    n_rows: int
    n_vars: int
    var_df: pd.DataFrame | None = None
    layer_mapping: dict[str, str] = field(default_factory=dict)


class DataSourceResolver(Protocol):
    """Prepare raw DATA files for homeobox ingestion."""

    def can_read(self, ctx: ReaderContext) -> bool: ...

    def prepare(self, ctx: ReaderContext) -> PreparedSource: ...


@dataclass
class _FeatureIngestPlan:
    feature_space: str
    field_name: str
    source: PreparedSource
    dataset_record: Any
    obs_indices: np.ndarray


# ===========================================================================
# Schema introspection
# ===========================================================================


@dataclass
class _ObsPointer:
    field_name: str
    feature_space: str
    registry_class: str | None


@dataclass
class _SchemaModel:
    """Resolved schema facts the ingestor needs, derived from the schema module."""

    info: SchemaInfo
    obs_class: str
    dataset_class: str
    pointers: list[_ObsPointer]
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
    """Add a finalized collection and all its datasets to a homeobox atlas."""

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

        self._global_readers: list[DataSourceResolver] = []
        self._fs_readers: dict[str, DataSourceResolver] = {}
        self._dataset_readers: dict[str, DataSourceResolver] = {}

        self._atlas: RaggedAtlas | None = None

    # -- reader registration ------------------------------------------------

    def register_reader(
        self,
        reader: DataSourceResolver,
        *,
        dataset: str | None = None,
        feature_space: str | None = None,
    ) -> None:
        """Register a custom DATA source resolver.

        Resolvers prepare a homeobox ``Reader`` and report the DATA row ids in
        emitted order. More specific scopes win: dataset > feature space >
        global.
        """
        if dataset is not None:
            self._dataset_readers[dataset] = reader
        elif feature_space is not None:
            self._fs_readers[feature_space] = reader
        else:
            self._global_readers.append(reader)

    def _resolve_source(self, ctx: ReaderContext) -> PreparedSource:
        if ctx.dataset_name in self._dataset_readers:
            return self._prepare_source(self._dataset_readers[ctx.dataset_name], ctx)
        if ctx.feature_space in self._fs_readers:
            return self._prepare_source(self._fs_readers[ctx.feature_space], ctx)
        for reader in self._global_readers:
            if reader.can_read(ctx):
                return self._prepare_source(reader, ctx)
        return self._prepare_h5ad_source(ctx)

    def _prepare_source(self, resolver: DataSourceResolver, ctx: ReaderContext) -> PreparedSource:
        if not resolver.can_read(ctx):
            raise ValueError(
                f"Registered reader cannot handle {ctx.dataset_name}/{ctx.feature_space}; "
                f"data files: {ctx.data_files}"
            )
        return self._validate_source(ctx, resolver.prepare(ctx))

    def _prepare_h5ad_source(self, ctx: ReaderContext) -> PreparedSource:
        h5ads = [p for p in ctx.data_files if p.endswith(".h5ad")]
        if len(h5ads) != 1:
            raise ValueError(
                f"No reader can handle {ctx.dataset_name}/{ctx.feature_space}; "
                f"expected exactly one .h5ad DATA file for the built-in path, found {h5ads}. "
                "Register a custom DataSourceResolver."
            )

        import anndata as ad

        adata = ad.read_h5ad(h5ads[0], backed="r")
        return self._validate_source(
            ctx,
            PreparedSource(
                reader=AnnDataReader(adata),
                row_ids=[str(name) for name in adata.obs_names],
                n_rows=adata.n_obs,
                n_vars=adata.n_vars,
                var_df=ctx.var_table.to_pandas() if ctx.var_table is not None else None,
                layer_mapping={"X": self.zarr_layer},
            ),
        )

    def _validate_source(self, ctx: ReaderContext, source: PreparedSource) -> PreparedSource:
        spec = get_spec(ctx.feature_space)
        if source.n_rows != len(source.row_ids):
            raise ValueError(
                f"{ctx.dataset_name}/{ctx.feature_space}: source reports {source.n_rows} rows, "
                f"but row_ids has {len(source.row_ids)} entries"
            )
        if not source.layer_mapping:
            raise ValueError(
                f"{ctx.dataset_name}/{ctx.feature_space}: source must provide a layer_mapping"
            )

        var_df = source.var_df
        if spec.has_var_df:
            if var_df is None:
                raise ValueError(
                    f"{ctx.dataset_name}/{ctx.feature_space}: feature space requires a var_df"
                )
            if len(var_df) != source.n_vars:
                raise ValueError(
                    f"{ctx.dataset_name}/{ctx.feature_space}: source reports {source.n_vars} "
                    f"variables, but var_df has {len(var_df)} rows"
                )
            if UID_COLUMN not in var_df.columns:
                raise ValueError(
                    f"{ctx.dataset_name}/{ctx.feature_space}: var_df is missing {UID_COLUMN!r}"
                )
        else:
            var_df = None

        return PreparedSource(
            reader=source.reader,
            row_ids=[str(row_id) for row_id in source.row_ids],
            n_rows=source.n_rows,
            n_vars=source.n_vars,
            var_df=var_df,
            layer_mapping=dict(source.layer_mapping),
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
            df = self._atlas._dataset_table.search().select([DATASET_UID_COLUMN]).to_polars()
        except Exception:
            return set()
        if df.is_empty():
            return set()
        return set(df[DATASET_UID_COLUMN].to_list())

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

        plans: list[_FeatureIngestPlan] = []
        for feature_space in dataset.feature_spaces:
            ctx = self._build_context(name, dataset, feature_space)
            source = self._resolve_source(ctx)
            dataset_record = self._build_dataset_record(name, dataset, ctx)
            field_name = self.schema.pointer_for(feature_space).field_name
            obs_indices = self._obs_indices_for_source(
                name, feature_space, source.row_ids, bare_obs
            )

            plans.append(
                _FeatureIngestPlan(
                    feature_space=feature_space,
                    field_name=field_name,
                    source=source,
                    dataset_record=dataset_record,
                    obs_indices=obs_indices,
                )
            )
            report.rows_per_feature_space[feature_space] = (
                report.rows_per_feature_space.get(feature_space, 0) + source.n_rows
            )

        if self.dry_run:
            return

        obs_df = self._prepare_obs_df(bare_obs, plans)
        ingestor = Ingestor(self._atlas, obs_df=obs_df, obs_table_name=self.obs_table_name)
        for plan in plans:
            ingestor.write_array(
                plan.source.reader,
                field_name=plan.field_name,
                layer_mapping=plan.source.layer_mapping,
                dataset_record=plan.dataset_record,
                n_vars=plan.source.n_vars,
                var_df=plan.source.var_df,
                required_pointer_type=get_spec(plan.feature_space).pointer_type,
                obs_indices=plan.obs_indices,
            )
        n_rows = ingestor.write_obs_records()
        print(f"  added {n_rows} obs row(s)")

    def _build_context(self, name: str, dataset: Any, feature_space: str) -> ReaderContext:
        pointer = self.schema.pointer_for(feature_space)
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
            registry_schema=registry_cls,
        )

    def _obs_indices_for_source(
        self,
        name: str,
        feature_space: str,
        row_ids: list[str],
        bare_obs: pa.Table,
    ) -> np.ndarray:
        """Map DATA row ids to integer positions in the finalized bare obs table."""
        for col in (UID_COLUMN,):
            if col not in bare_obs.column_names:
                raise ValueError(f"{name}: bare obs table is missing {col!r}")

        bare_uids = [str(uid) for uid in bare_obs.column(UID_COLUMN).to_pylist()]
        self._raise_duplicate_values(name, self.obs_table_name, UID_COLUMN, bare_uids)
        bare_uid_to_pos = {uid: i for i, uid in enumerate(bare_uids)}

        modality_obs = self._modality_obs_arrow(name, feature_space)
        for col in (OBS_INDEX_COLUMN, UID_COLUMN):
            if col not in modality_obs.column_names:
                raise ValueError(
                    f"{name}/{feature_space}: obs table is missing {col!r}; "
                    f"available: {modality_obs.column_names}"
                )

        obs_index_values = [
            str(value) for value in modality_obs.column(OBS_INDEX_COLUMN).to_pylist()
        ]
        modality_uids = [str(uid) for uid in modality_obs.column(UID_COLUMN).to_pylist()]
        self._raise_duplicate_values(
            name, f"{self.obs_table_name}_{feature_space}", OBS_INDEX_COLUMN, obs_index_values
        )

        obs_index_to_position: dict[str, int] = {}
        unknown_uids: list[str] = []
        for obs_index, uid in zip(obs_index_values, modality_uids, strict=True):
            pos = bare_uid_to_pos.get(uid)
            if pos is None:
                unknown_uids.append(uid)
                continue
            obs_index_to_position[obs_index] = pos
        if unknown_uids:
            raise ValueError(
                f"{name}/{feature_space}: {len(unknown_uids)} modality obs uid(s) are not "
                f"present in the bare obs table; examples: {unknown_uids[:5]}"
            )

        row_ids = [str(row_id) for row_id in row_ids]
        self._raise_duplicate_values(name, feature_space, "DATA row id", row_ids)
        missing_from_data = [
            obs_index for obs_index in obs_index_values if obs_index not in row_ids
        ]
        if missing_from_data:
            raise ValueError(
                f"{name}/{feature_space}: {len(missing_from_data)} obs row(s) have an "
                f"{OBS_INDEX_COLUMN!r} with no matching DATA row; "
                f"examples: {missing_from_data[:5]}"
            )
        missing_from_obs = [row_id for row_id in row_ids if row_id not in obs_index_to_position]
        if missing_from_obs:
            raise ValueError(
                f"{name}/{feature_space}: {len(missing_from_obs)} DATA row id(s) have no "
                f"matching {OBS_INDEX_COLUMN!r} in the finalized obs table; "
                f"examples: {missing_from_obs[:5]}"
            )

        return np.array([obs_index_to_position[row_id] for row_id in row_ids], dtype=np.int64)

    def _modality_obs_arrow(self, name: str, feature_space: str) -> pa.Table:
        suffixed_name = f"{self.obs_table_name}_{feature_space}"
        suffixed = self._open_dataset_table(name, suffixed_name)
        if suffixed is not None:
            return suffixed.to_arrow()
        bare = self._open_dataset_table(name, self.obs_table_name)
        if bare is None:
            raise ValueError(f"{name}: no finalized obs table {self.obs_table_name!r}")
        return bare.to_arrow()

    @staticmethod
    def _raise_duplicate_values(
        dataset_name: str, table_name: str, column_name: str, values: list[str]
    ) -> None:
        seen = set()
        duplicates = []
        for value in values:
            if value in seen:
                duplicates.append(value)
            else:
                seen.add(value)
        if duplicates:
            raise ValueError(
                f"{dataset_name}/{table_name}: column {column_name!r} has duplicate "
                f"value(s); examples: {duplicates[:5]}"
            )

    def _prepare_obs_df(self, bare_obs: pa.Table, plans: list[_FeatureIngestPlan]) -> pd.DataFrame:
        """Build the obs frame consumed by homeobox.Ingestor."""
        obs_df = bare_obs.to_pandas()
        arrow_schema = self.schema.obs_cls.to_arrow_schema()
        schema_names = set(arrow_schema.names)

        for pointer in self.schema.pointers:
            flag_name = f"has_{pointer.field_name}"
            if flag_name in schema_names:
                obs_df[flag_name] = False

        for plan in plans:
            flag_name = f"has_{plan.field_name}"
            if flag_name not in obs_df.columns:
                continue
            values = np.asarray(obs_df[flag_name].fillna(False), dtype=bool)
            values[plan.obs_indices] = True
            obs_df[flag_name] = values

        return obs_df

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

    def _open_dataset_table(self, dataset_name: str, table_name: str):
        db_path = os.path.join(self.root_dir, dataset_name, LANCE_DB_DIR)
        if not os.path.isdir(db_path):
            return None
        db = lancedb.connect(db_path)
        if table_name not in db.list_tables().tables:
            return None
        return db.open_table(table_name)
