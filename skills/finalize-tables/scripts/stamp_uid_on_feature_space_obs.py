"""Stamp finalized ``uid`` values onto per-feature-space obs tables.

After ``join_feature_space_obs.py`` has written the bare obs table and
``assign_uids`` has assigned a per-row ``uid`` on it, this script copies those
``uid`` values onto each ``{obs_class}_{feature_space}`` table by joining on
``multimodal_barcode``. Ingestion can then look up a modality's DATA row index
by finding the row with the matching ``uid`` in the corresponding feature-space
table (row position is preserved from staging).

Suffixed tables are not finalized; they only receive ``uid`` for this lookup.

Usage:
    python scripts/stamp_uid_on_feature_space_obs.py <lance_db> --obs-class CellIndex [--dry-run]

    python scripts/stamp_uid_on_feature_space_obs.py <collection_root> \\
        --obs-class CellIndex [--dataset NAME] [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import sys

import lancedb
import pandas as pd
import pyarrow as pa
from join_feature_space_obs import (
    JOIN_KEY,
    _dataset_lance_dirs,
    assert_unique_multimodal_barcode,
    suffixed_obs_tables,
)

from auto_atlas.util import is_null

UID_COLUMN = "uid"


def _barcode_to_uid(joined: pd.DataFrame, obs_class: str) -> dict[object, str]:
    assert_unique_multimodal_barcode(joined, obs_class)
    mapping: dict[object, str] = {}
    for barcode, uid in zip(joined[JOIN_KEY], joined[UID_COLUMN], strict=True):
        if is_null(barcode):
            raise ValueError(f"{obs_class}: null {JOIN_KEY!r} after join; expected unique barcodes")
        if is_null(uid):
            raise ValueError(f"{obs_class}: null {UID_COLUMN!r} for {JOIN_KEY}={barcode!r}")
        mapping[barcode] = str(uid)
    return mapping


def stamp_uid_on_feature_space_obs(
    lance_path: str,
    *,
    obs_class: str,
    dry_run: bool = False,
) -> bool:
    """Stamp ``uid`` on suffixed obs tables in one dataset ``lance_db``. Returns False if skipped."""
    lance_path = os.path.abspath(lance_path)
    tables_by_space = suffixed_obs_tables(lance_path, obs_class)
    if len(tables_by_space) < 2:
        return False

    db = lancedb.connect(lance_path)
    existing = set(db.list_tables().tables)
    if obs_class not in existing:
        raise ValueError(
            f"Joined obs table {obs_class!r} not found in {lance_path!r}. "
            f"Run join_feature_space_obs first. Available: {sorted(existing)}"
        )

    joined = db.open_table(obs_class).to_arrow().to_pandas()
    for column in (JOIN_KEY, UID_COLUMN):
        if column not in joined.columns:
            raise ValueError(
                f"{obs_class}: column {column!r} missing; "
                f"run join_feature_space_obs and assign_uids first. "
                f"Available: {list(joined.columns)}"
            )

    barcode_to_uid = _barcode_to_uid(joined, obs_class)
    print(f"{lance_path}: stamping {UID_COLUMN} on {len(tables_by_space)} feature-space table(s)")

    for table_name in tables_by_space.values():
        df = db.open_table(table_name).to_arrow().to_pandas()
        if JOIN_KEY not in df.columns:
            raise ValueError(f"Column {JOIN_KEY!r} not in {table_name!r}")
        assert_unique_multimodal_barcode(df, table_name)

        uids: list[str | None] = []
        missing: list[object] = []
        for barcode in df[JOIN_KEY]:
            if is_null(barcode):
                missing.append(barcode)
                uids.append(None)
                continue
            uid = barcode_to_uid.get(barcode)
            if uid is None:
                missing.append(barcode)
                uids.append(None)
            else:
                uids.append(uid)

        if missing:
            sample = missing[:5]
            raise ValueError(
                f"{table_name}: {len(missing)} row(s) have no {UID_COLUMN} in joined "
                f"{obs_class!r} for {JOIN_KEY!r}; examples: {sample}"
            )

        df[UID_COLUMN] = uids
        print(f"  {table_name}: stamped {len(uids)} {UID_COLUMN}(s)")

        if dry_run:
            continue

        arrow = pa.Table.from_pandas(df, preserve_index=False)
        db.create_table(table_name, data=arrow, mode="overwrite")

    if dry_run:
        print("(dry run — Lance not mutated)")

    return True


def stamp_collection(
    collection_root: str,
    *,
    obs_class: str,
    dataset: str | None = None,
    dry_run: bool = False,
) -> int:
    """Stamp uid on feature-space obs tables for every matching dataset. Returns stamp count."""
    stamped = 0
    for dataset_name, lance_path in _dataset_lance_dirs(collection_root, dataset):
        print(f"\n{dataset_name}/")
        if stamp_uid_on_feature_space_obs(lance_path, obs_class=obs_class, dry_run=dry_run):
            stamped += 1
    return stamped


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        help="Dataset lance_db directory or collection root (with collection.json)",
    )
    parser.add_argument(
        "--obs-class",
        required=True,
        dest="obs_class",
        help="Obs schema class name (e.g. CellIndex)",
    )
    parser.add_argument("--dataset", help="Limit to one dataset when path is a collection root")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    path = os.path.abspath(args.path)
    manifest = os.path.join(path, "collection.json")
    if os.path.isfile(manifest):
        stamp_collection(
            path,
            obs_class=args.obs_class,
            dataset=args.dataset,
            dry_run=args.dry_run,
        )
        return

    if not os.path.isdir(path):
        print(f"path not found: {path}", file=sys.stderr)
        sys.exit(1)

    stamp_uid_on_feature_space_obs(path, obs_class=args.obs_class, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
