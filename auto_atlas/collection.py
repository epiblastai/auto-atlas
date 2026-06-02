import json
import os
import shutil
from dataclasses import dataclass
from enum import StrEnum


class FileTypeTag(StrEnum):
    # Use this tag for files that contain row-level metadata
    OBS = "obs"
    # Use this tag for files that contain column-level metadata
    VAR = "var"
    # Use this tag for files that contain actual array data
    DATA = "data"
    # Use this tag for files that contain libraries like names of
    # small molecule perturbation, guide RNAs, or donor information.
    # Libraries have a column that can be linked to another column in
    # an OBS or VAR file.
    LIBRARY = "library"


@dataclass(frozen=True)
class TaggedFile:
    """A file path together with its tag and (optionally) the feature space it
    belongs to. Feature space is set for obs/var/data files of a given modality
    (e.g. "gene_expression", "protein_abundance") and may be omitted for shared
    library files that are not specific to a single modality."""

    path: str
    tag: FileTypeTag
    feature_space: str | None = None

    def to_dict(self) -> dict:
        return {"path": self.path, "tag": str(self.tag), "feature_space": self.feature_space}


class Dataset:
    def __init__(self, dataset_name: str) -> None:
        self.dataset_name = dataset_name
        self._tagged_files: dict[str, TaggedFile] = {}

    @property
    def files(self) -> list[str]:
        return list(self._tagged_files.keys())

    @property
    def feature_spaces(self) -> list[str]:
        # Distinct, sorted feature spaces present across this dataset's files.
        spaces = {tf.feature_space for tf in self._tagged_files.values()}
        spaces.discard(None)
        return sorted(spaces)

    def add_file(
        self,
        file_path: str,
        tag: FileTypeTag,
        feature_space: str | None = None,
    ) -> None:
        if file_path in self._tagged_files:
            raise ValueError(f"file_path {file_path} has already be added!")

        self._tagged_files[file_path] = TaggedFile(file_path, tag, feature_space)

    def files_for(
        self,
        tag: FileTypeTag | None = None,
        feature_space: str | None = None,
    ) -> list[str]:
        # List files, optionally filtered by tag and/or feature space. A filter
        # left as None places no constraint on that axis.
        return [
            tf.path
            for tf in self._tagged_files.values()
            if (tag is None or tf.tag == tag)
            and (feature_space is None or tf.feature_space == feature_space)
        ]

    def _rename_file(self, old_path: str, new_path: str) -> None:
        # Internal: rewrite a tracked path (used by Collection.coalesce_datasets
        # after physically moving/copying files).
        tf = self._tagged_files.pop(old_path)
        self._tagged_files[new_path] = TaggedFile(new_path, tf.tag, tf.feature_space)

    def _to_dict(self) -> dict:
        return {"files": [tf.to_dict() for tf in self._tagged_files.values()]}


class Collection:
    """We do not make accomodation for general free-form metadata files.
    For example, sample preparation protocols, dataset READMEs, or publication
    texts. Instead datasets and collections are for tables and arrays only.
    """

    def __init__(self, root_dir: str):
        os.makedirs(root_dir, exist_ok=True)
        self.root_dir = root_dir
        # Files within each dataset are independent of each other
        self._datasets: dict[str, Dataset] = {}
        self._coalesced_datasets: set[str] = set()

        # Collection-level files MUST be shared across all datasets
        # Though not all datasets are required to reference a shared file
        self._shared_tagged_files: dict[str, TaggedFile] = {}

    @property
    def datasets(self) -> list[str]:
        return list(self._datasets.keys())

    def add_dataset(self, dataset: Dataset) -> None:
        if dataset.dataset_name in self._datasets:
            raise ValueError(f"dataset {dataset.dataset_name} has already be added!")

        self._datasets[dataset.dataset_name] = dataset

    def add_file(
        self,
        file_path: str,
        tag: FileTypeTag,
        feature_space: str | None = None,
    ) -> None:
        if file_path in self._shared_tagged_files:
            raise ValueError(f"file_path {file_path} has already be added!")

        self._shared_tagged_files[file_path] = TaggedFile(file_path, tag, feature_space)

    def coalesce_datasets(self, copy: bool = True) -> None:
        # Copies or moves the files in each not-yet-coalesced dataset to
        # root_dir / dataset_name, keeping the original filenames. Tracked paths
        # are rewritten to the new locations. This is a local-filesystem
        # operation (shutil does not handle s3 urls).
        for name, dataset in self._datasets.items():
            if name in self._coalesced_datasets:
                continue

            dest_dir = os.path.join(self.root_dir, name)
            os.makedirs(dest_dir, exist_ok=True)

            # Map source -> dest first so we can catch collisions before moving
            # anything (two files sharing a basename, or an existing dest).
            moves: list[tuple[str, str]] = []
            seen_dests: set[str] = set()
            for src in dataset.files:
                dest = os.path.join(dest_dir, os.path.basename(src))
                if dest in seen_dests:
                    raise ValueError(
                        f"basename collision in dataset {name}: multiple files map to {dest}"
                    )
                if os.path.exists(dest):
                    raise ValueError(f"destination {dest} already exists; refusing to overwrite")
                seen_dests.add(dest)
                moves.append((src, dest))

            for src, dest in moves:
                if copy:
                    shutil.copy2(src, dest)
                else:
                    shutil.move(src, dest)
                dataset._rename_file(src, dest)

            self._coalesced_datasets.add(name)

    def dumps(self) -> str:
        # Creates a string with json that lists file paths and their tags,
        # including dataset subdirectories. Only coalesced datasets may be
        # dumped, so the manifest always reflects organized, on-disk locations.
        uncoalesced = [name for name in self._datasets if name not in self._coalesced_datasets]
        if uncoalesced:
            raise ValueError(
                f"datasets {uncoalesced} have not been coalesced; "
                f"run coalesce_datasets() before dumps()"
            )

        payload = {
            "root_dir": self.root_dir,
            "shared_files": [tf.to_dict() for tf in self._shared_tagged_files.values()],
            "datasets": {
                name: dataset._to_dict() for name, dataset in self._datasets.items()
            },
        }
        return json.dumps(payload, indent=2)
