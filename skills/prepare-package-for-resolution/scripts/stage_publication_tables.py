"""Stage publication metadata from ``publication.json`` into collection ``lance_db/``.

Reads the collection's ``publication.json`` sidecar (typically under
``other_files/`` after coalesce) and writes one or two Lance tables at
``<collection_root>/lance_db/``. Only run this when the target schema defines
collection-level publication registry tables.

Field mapping follows ``publication.json`` exactly — only keys present in the
file are written. Top-level keys other than ``text_data`` go to the
publication table; ``text_data.section_title`` / ``text_data.section_text``
become section rows. Staged columns usually will not yet conform to the
homeobox schema; downstream skills align and finalize them.

Three modes (provide at least one schema argument):

1. Publication registry only (``--pub-schema``):
   One row with all top-level fields except ``text_data``.
2. Publication + sections (``--pub-schema`` and ``--pub-section-schema``):
   One publication row plus one section row per ``text_data`` entry.
3. Denormalized sections only (``--pub-section-schema``):
   Section rows with top-level publication fields repeated on each row.

Usage:
    python scripts/stage_publication_tables.py <collection_root> \\
        [--pub-schema PublicationSchema] \\
        [--pub-section-schema PublicationSectionSchema] \\
        [--publication-json PATH]

Arguments:
    collection_root       Root directory of a coalesced collection
    --pub-schema          CamelCase Lance table for the publication registry
    --pub-section-schema  CamelCase Lance table for publication text sections
    --publication-json    Path to publication.json (default: discover from manifest)
"""

from __future__ import annotations

import argparse
import json
import os

import lancedb
import pandas as pd

from auto_atlas.collection import FileTypeTag

COLLECTION_MANIFEST = "collection.json"
LANCE_DB_DIR = "lance_db"
PUBLICATION_FILENAME = "publication.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stage publication.json into collection-level lance_db."
    )
    parser.add_argument("collection_root", help="Root directory of a coalesced collection")
    parser.add_argument(
        "--pub-schema",
        help="CamelCase Lance table name for the publication registry (e.g. PublicationSchema)",
    )
    parser.add_argument(
        "--pub-section-schema",
        help=(
            "CamelCase Lance table name for publication sections (e.g. PublicationSectionSchema)"
        ),
    )
    parser.add_argument(
        "--publication-json",
        help="Path to publication.json (absolute or relative to collection root)",
    )
    args = parser.parse_args(argv)
    if not args.pub_schema and not args.pub_section_schema:
        parser.error("Provide --pub-schema and/or --pub-section-schema")
    return args


def resolve_path(collection_root: str, path: str) -> str:
    if os.path.isabs(path):
        return path
    return os.path.join(collection_root, path)


def find_publication_json(collection_root: str, override: str | None) -> str:
    if override is not None:
        path = resolve_path(collection_root, override)
        if not os.path.isfile(path):
            raise FileNotFoundError(f"publication.json not found: {path}")
        return path

    manifest_path = os.path.join(collection_root, COLLECTION_MANIFEST)
    if os.path.isfile(manifest_path):
        with open(manifest_path) as f:
            payload = json.load(f)
        for entry in payload.get("shared_files", []):
            path = entry.get("path", "")
            if os.path.basename(path) != PUBLICATION_FILENAME:
                continue
            resolved = resolve_path(collection_root, path)
            if os.path.isfile(resolved):
                return resolved

    for candidate in (
        os.path.join(collection_root, "other_files", PUBLICATION_FILENAME),
        os.path.join(collection_root, PUBLICATION_FILENAME),
    ):
        if os.path.isfile(candidate):
            return candidate

    raise FileNotFoundError(
        f"Could not find {PUBLICATION_FILENAME} under {collection_root}. "
        "Pass --publication-json or add the file to the collection first."
    )


def warn_if_not_tagged_publication(collection_root: str, publication_path: str) -> None:
    manifest_path = os.path.join(collection_root, COLLECTION_MANIFEST)
    if not os.path.isfile(manifest_path):
        return
    with open(manifest_path) as f:
        payload = json.load(f)
    abs_publication = os.path.abspath(publication_path)
    for entry in payload.get("shared_files", []):
        if entry.get("tag") != str(FileTypeTag.OTHER):
            continue
        tagged = os.path.abspath(resolve_path(collection_root, entry["path"]))
        if tagged == abs_publication:
            return
    print(f"warning: {publication_path} is not listed as an OTHER file in {COLLECTION_MANIFEST}")


def load_publication_json(path: str) -> dict:
    with open(path) as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return payload


def extract_pub_fields(publication: dict) -> dict:
    """Top-level publication.json fields, excluding ``text_data``."""
    return {key: value for key, value in publication.items() if key != "text_data"}


def build_section_rows(publication: dict, *, denormalize: bool) -> list[dict]:
    text_data = publication.get("text_data") or {}
    titles = text_data.get("section_title") or []
    texts = text_data.get("section_text") or []

    if len(titles) != len(texts):
        raise ValueError(
            "text_data.section_title and text_data.section_text must have the same length "
            f"({len(titles)} vs {len(texts)})"
        )

    pub_fields = extract_pub_fields(publication) if denormalize else {}

    rows: list[dict] = []
    for title, text in zip(titles, texts, strict=False):
        row = {"section_title": title, "section_text": text}
        if denormalize:
            row.update(pub_fields)
        rows.append(row)
    return rows


def stage_table(db: lancedb.DBConnection, table_name: str, df: pd.DataFrame) -> None:
    db.create_table(table_name, data=df, mode="overwrite")
    print(f"{table_name}: {len(df)} row(s), {len(df.columns)} column(s)")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    collection_root = os.path.abspath(args.collection_root)
    publication_path = find_publication_json(collection_root, args.publication_json)
    warn_if_not_tagged_publication(collection_root, publication_path)
    publication = load_publication_json(publication_path)

    lance_path = os.path.join(collection_root, LANCE_DB_DIR)
    os.makedirs(lance_path, exist_ok=True)
    db = lancedb.connect(lance_path)

    print(f"Loaded {publication_path}")

    if args.pub_schema:
        pub_df = pd.DataFrame([extract_pub_fields(publication)])
        stage_table(db, args.pub_schema, pub_df)

    if args.pub_section_schema:
        denormalize = args.pub_schema is None
        section_rows = build_section_rows(publication, denormalize=denormalize)
        if not section_rows:
            print("warning: no text sections found in publication.json text_data")
        section_df = pd.DataFrame(section_rows)
        stage_table(db, args.pub_section_schema, section_df)

    print(f"-> {lance_path}")


if __name__ == "__main__":
    main()
