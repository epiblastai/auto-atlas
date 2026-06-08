---
name: geo-data-preparer
description: Use when a user provides a GEO accession and a target homeobox schema file and wants to align and prepare the dataset for ingestion against that schema. Covers listing and downloading GEO files as well as file classification, metadata creation, and delegation to resolver sub-agents for metadata resolution to ontologies and databases.
---

# GEO Data Preparer

## Scope

This skill handles the full pre-ingestion pipeline:

1. **Listing** supplementary files for a GEO accession
2. **Downloading** selected files via FTP
3. **Classifying** files (e.g., h5ad vs matrix + companion files)
4. **Writing metadata.json** that stores GEO series or sample metadata
5. **Creating global tables** for feature registries and registry keys
6. **Delegating** metadata standardization to resolver sub-agents

It does NOT handle assembling standardized CSVs, adding data to LanceDB, or writing Zarr arrays. Those responsibilities belong to the `geo-data-curator` skill. 

> **HARD BOUNDARY: This is a preparation and orchestration skill only.** Other skills exist for:
> - Reading, parsing, or inspecting supplementary files (e.g., guide libraries, reagent libraries, compound libraries). This skill does not provide the requisite detail to understand such files and you must choose an appropriate resolver and delegate to them instead.
> - Making decisions about how resolvers should interpret data (e.g., how dual-guide pairs map to schema rows, how control labels are detected, how transcript IDs map to target context). The resolver skills describe how to do this properly, this skill does not. You shouldn't make any assumptions or decisions on these points.
>
> Your job when executing this skill is to relay column names, file paths, and delimiters, etc. When in doubt, pass more context to resolvers rather than trying to interpret anything yourself.

## Scripts

You have access to scripts that can be used for common tasks. All paths are relative to this skill directory.

| Script | Usage | Purpose |
|--------|-------|---------|
| `scripts/list_geo_files.py` | `python scripts/list_geo_files.py GSE123456` | List supplementary files for any GEO accession (GSE or GSM) |
| `scripts/download_geo_file.py` | `python scripts/download_geo_file.py GSE123456 file.h5ad [dest_dir]` | Download a supplementary file via FTP (default dest: `/tmp/geo_agent/<accession>/`) |
| `scripts/write_metadata_json.py` | `python scripts/write_metadata_json.py <experiment_dir> <accession>` | Fetch GEO metadata and write metadata.json in the experiment directory |
| `scripts/reconcile_barcodes.py` | `python scripts/reconcile_barcodes.py <experiment_dir>` | Reconcile barcodes across modalities; writes `multimodal_barcode` to each feature space's preparer fragment |

## Workflow

### 1. List and identify data files for the provided GEO accession

If the accession code is for a series or superseries (GSE prefix) series record, look for preprocessed and filtered files or a single large tar file, these are generally preferable. However, if the series level has no files or only summary statistics, then you should check the sample-level for the real data. If there are many sample records its best to process them one at a time to avoid confusion. If very many, you should ask the user how they would like to proceed.

Currently, we support the following file formats (which may be in `.tar` files):

| Format | Action |
|--------|--------|
| `.h5ad` | Already AnnData — keep as-is, set `anndata` field |
| `.h5` (10x HDF5) | Set `matrix_file` field; can be read with `scanpy.read_10x_h5()` for validation |
| `.mtx` / `.mtx.gz` (Market Matrix) | Set `matrix_file` field; companions go to `cell_metadata`/`var_metadata` |
| `.tsv` / `.tsv.gz` | Sometimes used for protein abundance which is not sparse |
| `_fragments.tsv.gz` / `.bed.gz` / `.bed` | Fragment files — per-cell chromatin accessibility regions. Columns: `(chrom, start, end, barcode)` (4-col) or `(chrom, start, end, barcode, count)` (5-col, 10x format) |
| `.bw` (bigWig) | Not supported. Per-sample coverage tracks, not per-cell data. Skip and note in output. |
| Peak matrices (cells × peaks) | Not supported for chromatin accessibility ingestion. Skip and note in output. |
| `.rds` | Not supported. Skip and note in output. |

If the file formats present on the GEO record fall outside of this list, raise it to the user.

**mtx bundles:** When you see `.mtx.gz` files, look for companion `barcodes.tsv.gz` and `features.tsv.gz` (or `genes.tsv.gz`) files. These form a single dataset. If the mtx bundle files are in a tar/gz archive, download and extract it first.

**Multimodal datasets:** Watch out for file naming patterns that indicate multiple modalities from the same experiment (e.g., `*_cDNA_*` and `*_ADT_*` for CITE-seq, `*_RNA_*` and `*_ATAC_*` for multiome). We will want to group these files together later.

### 2. Read the schema file

This skill's validation workflow is driven by a **user-provided Python schema file**. The schema defines the tables and fields to populate with data.

The user must provide the schema file path as part of their task prompt. Example:

> "Prepare GSE123456 using the schema at `some/path/schema.py`"

If no schema was provided, ask the user for the path before going any further. Read the Python file and identify:

1. **The obs schema classes** — These inherits from `HoxBaseSchema`. If there is more than one, you user will need to specify which to use, do not assume.
2. **Feature registry classes** — These inherit from `FeatureBaseSchema` and correspond to var-level fields per feature space supported by an atlas.
3. **Registry key classes** — These inherit directly from `LanceModel`. These tables are referenced by either the obs table or a feature registry table through a registry key.

Our goal is to fill out each of the schemas and fields that apply to the provided GEO dataset, which will always include the obs class but may involve only a subset of the feature registry and registry key classes in the schema file. If a field's purpose is unclear from its name, type, docstring or in-line comments, ask the user.

### 3. Download and read GEO metadata

Download the metadata from the GEO series or sample records:

```
python scripts/write_metadata_json.py /tmp/geo_agent/<accession> <accession>
```

You may need to run this multiple times. Sometimes when the data is stored at the series level, it still references a sample record (e.g., the filename contains a GSM id). In this case, download the metadata from the series and from the referenced sample ids.

Read the relevant json files. These often include helpful information about how to use the files and high-level metadata like organism or assay.

### 4. (Optional) Download and parse the publication

If any of the schemas are storing publication-related data (like PubMed ids, publication text, or figures), then you may launch a subagent with the `publication-resolver` skill. Provide it with a publication title, PMID, DOI, or author names and search terms so that it can find the paper in PubMed. Also provide the schema module path and any publication-related schema class names for which the resolver should be responsible. Often the requisite identifier information is found in the GEO metadata json files that you downloaded in the previous step.

The publication resolver produces validated parquet files with UIDs already assigned, following the same pattern as other resolvers. The `uid` is included in `publication.json` for downstream reference.

In some cases the GEO record does not have a clear publication reference. Do not guess at the publication, stop and ask the user to provide it directly. There is nothing worse than a hallucinated citation.

If you have questions about the data in later steps, the publication is a good place to find answers before asking the user.

### 5. Download and organize files into a Collection

Download the necessary files from GEO (be sure to use long enough timeouts for large files):

```
python scripts/download_geo_file.py <accession> <filename> [dest_dir]
```

Default destination: `/tmp/geo_agent/<accession>/`. Some GEO datasets have multiple files in a single tar archive -- extract it. If there are multiple versions of the same dataset, possibly indicated by terms like "filtered", "processed", or "validated", prefer these analysis-ready artifacts to the raw version. Ask the user if unsure.

Organize files with `auto_atlas.collection`. Create one `Dataset` per experiment, add each file with the appropriate `FileTypeTag` (and feature space for obs/var/data files), add the datasets to a `Collection`, then `coalesce()` to lay out the directory structure on disk and `dumps()` the manifest to the root directory.

- A `Dataset` is one experiment. Multimodal modalities from the same experiment go in the SAME dataset, distinguished by `feature_space` — do not split them.
- Tag files with `FileTypeTag`: `DATA` for matrices (h5ad, mtx, etc.), `OBS`/`VAR` for metadata tables, `LIBRARY` for reagent/guide/donor libraries, `OTHER` for free-form informational files (READMEs, protocols).
- **For `.h5ad` data files, run `extract_h5ad_obs_var` (from `auto_atlas.util`) BEFORE tagging and adding files.** It writes `_obs.csv` and `_var.csv` next to the h5ad. Add the h5ad as `DATA` and the two extracted CSVs as `OBS`/`VAR` (same feature space), so the obs/var tables get coalesced and recorded in the manifest alongside the matrix.
<!---Feature space name mostly doesn't matter during curation, so long as it properly keeps obs and var from getting confused with each other. These do not need to be aligned to the homeobox feature space registry.-->
- Set `feature_space` (e.g. `gene_expression`, `protein_abundance`, `chromatin_accessibility`) on obs/var/data files; omit it for shared libraries and informational files.
- Files shared across datasets (e.g. one guide library used by every experiment) are added to the `Collection` via `add_file`, not to an individual `Dataset`.

<!---This doesn't provide an example of how to handle the case of an "anndata-dataframe" where the metadata is in the same csv as features, which are named by column. In that cases we need to reason about the split. One place this comes up is CellProfiler features.-->
<!---We shouldn't use temp directories for this, because we don't want to lose work on shutdown.-->
```python
from auto_atlas.collection import Collection, Dataset, FileTypeTag
from auto_atlas.util import extract_h5ad_obs_var

collection = Collection(root_dir="/tmp/geo_agent/<accession>")

hepg2 = Dataset("HepG2")

# h5ad: extract obs/var to CSVs BEFORE tagging, then add all three.
gex_h5ad = ".../GSE..._HepG2.h5ad"
gex_obs, gex_var = extract_h5ad_obs_var(gex_h5ad)  # -> ..._obs.csv, ..._var.csv
hepg2.add_file(gex_h5ad, FileTypeTag.DATA, "gene_expression")
hepg2.add_file(gex_obs, FileTypeTag.OBS, "gene_expression")
hepg2.add_file(gex_var, FileTypeTag.VAR, "gene_expression")

# shared, collection-level library referenced by multiple datasets
collection.add_file(".../guide_library.csv", FileTypeTag.LIBRARY)

collection.coalesce(copy=False)  # moves dataset files under root/<name>/, OTHER files under root/other_files/
with open("/tmp/geo_agent/<accession>/collection.json", "w") as f:
    f.write(collection.dumps())
```

After `coalesce()`, dataset files live in `root/<dataset_name>/`, shared files in `root/`, and `OTHER` files in `root/other_files/`. The `collection.json` manifest records every file with its tag and feature space and is the source of truth for the steps that follow.

### 6. Stage obs and var into per-dataset Lance tables

Each dataset's obs- and var-level metadata is loaded into a Lance database at `<dataset_dir>/lance_db/`.

The OBS and VAR tables were already extracted and tagged in step 5 (h5ad via `extract_h5ad_obs_var`; mtx bundles and standalone tables tagged directly). Use `Dataset.files_for(tag=..., feature_space=...)` (or `collection.json`) to find the OBS and VAR file for each feature space — there should be at most one of each per feature space, and their paths now point at the coalesced locations.

Load each table into the dataset's `lance_db/` with ALL of its original columns — do not drop, rename, or standardize anything. 

- Name each table by the CamelCase schema class it will populate: the obs schema class (from step 2) for obs tables, and the feature registry class (e.g. `GenomicFeatureSchema`, `ProteinSchema`) for var tables.
- Keep the cell index / var index as an explicit column so rows can be linked downstream.
- When a dataset has multiple feature spaces sharing the same obs schema class, qualify the obs table name with the feature space (e.g. `CellIndex_gene_expression`) to avoid collisions.

<!---This should either be a script or it could be combined with the previous step. If we have a feature_space to registry_schema_name mapping then naming is automatic from tags. Obs naming can be handled similarly. 99% of the time there's one schema for obs, so wouldn't necessarily need a mapping.-->
```python
import lancedb
import pandas as pd

db = lancedb.connect("<dataset_dir>/lance_db")

(obs_csv,) = hepg2.files_for(tag=FileTypeTag.OBS, feature_space="gene_expression")
obs_df = pd.read_csv(obs_csv, index_col=0).reset_index(names="obs_index")
db.create_table("CellIndex_gene_expression", data=obs_df, mode="overwrite")

(var_csv,) = hepg2.files_for(tag=FileTypeTag.VAR, feature_space="gene_expression")
var_df = pd.read_csv(var_csv, index_col=0).reset_index(names="var_index")
db.create_table("GenomicFeatureSchema", data=var_df, mode="overwrite")
```

### 7. Create collection-level informational tables

Registry key tables (genetic perturbations, small molecules, donors, etc.) describe entities shared across the whole collection — they derive from the collection-level `LIBRARY` files and from columns in the per-dataset obs tables. Stage them globally in a single Lance database at the collection root, `<root_dir>/lance_db/`, one table per registry key schema, named by the CamelCase schema class (e.g. `GeneticPerturbationSchema`, `SmallMoleculeSchema`).

For each registry key schema:

1. Gather the relevant columns from the shared `LIBRARY` file(s) and/or from obs across all datasets
2. Add a key column (e.g. `reagent_id`) for mapping back to obs
3. Deduplicate on the key
4. Enrich with supplementary information from the GEO metadata and publication — the preparer gathers everything in unstandardized form so the resolver never hunts for supplementary files
5. Write the table into the collection-level `lance_db/`

Column misalignment across sources is OK — union of columns with NaN fills.

```python
import lancedb

db = lancedb.connect("<root_dir>/lance_db")
db.create_table("GeneticPerturbationSchema", data=fk_df, mode="overwrite")
```

Feature (var) registries are NOT created here — they are staged per dataset in step 6, since deterministic UIDs let the atlas registry deduplicate features across datasets on ingestion.

### 8. Delegate resolution to resolver subagents

Feature registries (var) and registry key tables are resolved across ALL experiments in one pass. Same entity in multiple experiments gets one UID.

Launch relevant resolvers for each global `_raw.csv`:

| Input | Resolver Skill | Output |
|-------|---------------|--------|
| `GenomicFeatureSchema_raw.csv` | `gene-resolver` | `GenomicFeatureSchema_resolved.csv` |
| `ProteinSchema_raw.csv` | `protein-resolver` | `ProteinSchema_resolved.csv` |
| `GeneticPerturbationSchema_raw.csv` | `genetic-perturbation-resolver` | `GeneticPerturbationSchema_resolved.csv` |
| `SmallMoleculeSchema_raw.csv` | `molecule-resolver` | `SmallMoleculeSchema_resolved.csv` |
| `ProteinSchema_raw.csv` | `protein-resolver` | `ProteinSchema_resolved.csv` |

**Prompt template for resolvers:**

```
Agent tool call:
  prompt: |
    Read the skill file at skills/<resolver-name>/SKILL.md and follow its workflow.

    Context:
    - Accession directory: <accession_dir>
    - Schema file: <schema_path>
    - Input: <SchemaClassName>_raw.csv
    - Output: <SchemaClassName>_resolved.csv (with UIDs assigned via make_uid())
```

Avoid giving the resolver skill any instructions about how to resolve the data. It already knows the correct procedure, such instructions in your prompt might contradict the skill.

**For the gene-resolver specifically**, also provide experiment subdirectories and feature space so it can write per-experiment standardized var CSVs (step 3 in the gene-resolver workflow):

```
    Additional context for per-experiment standardized var writing:
    - Experiment directories: [list of experiment subdirectory paths]
    - Feature space: <feature_space> (e.g., "gene_expression")
```

**For the ontology-resolver, genetic-perturbation-resolver, and molecule-resolver specifically**, also provide experiment subdirectories so it can write obs fragments:

```
    Additional context for obs-level fragment writing:
    - Experiment directories: [list of experiment subdirectory paths]
    - Feature space: <feature_space> (e.g., "gene_expression")
    - Schema: <ObsLevelSchemaClassName>
    - Columns you are responsible for: [list of columns in ObsLevelSchemaClassName]
```

All resolvers, except for `publication-resolver` which must be run first, may run in parallel. However, avoid running more than 2 or 3 resolvers agents at a time as this can cause resource contention.

**Note:** The ontology resolver operates per-experiment (writing `{fs}_fragment_ontology_obs.csv` directly in each experiment directory), unlike other resolvers which write global accession-level tables.

### 9. Verification

After all resolvers complete, verify that the expected output files exist:

- Finalized global tables: `{SchemaClassName}.parquet` for each feature registry, registry key schema, and publication schema
- Per-experiment: raw obs/var CSVs, resolver fragment obs CSVs (e.g., ontology fragments), preparer fragment obs CSVs
- Accession-level: `metadata.json`, `publication.json` (with `publication_uid`), `PublicationSchema.parquet`

The preparer is now complete. Hand off to the `geo-data-curator` skill for assembly, validation, and ingestion.

## Directory Layout

```
/tmp/geo_agent/GSE264667/
├── GenomicFeatureSchema_raw.csv                        # resolver input
├── GenomicFeatureSchema_resolved.csv                   # resolver intermediate (with UIDs + raw columns)
├── GenomicFeatureSchema.parquet                        # finalized, type-coerced parquet
├── GeneticPerturbationSchema_raw.csv                   # resolver input
├── GeneticPerturbationSchema_resolved.csv              # resolver intermediate (with UIDs + raw columns)
├── GeneticPerturbationSchema.parquet                   # finalized, type-coerced parquet
├── PublicationSchema.parquet                           # finalized publication table (from publication-resolver)
├── PublicationSectionSchema.parquet                    # finalized publication sections (optional)
├── publication.json                                    # backward-compatible sidecar (includes publication_uid)
├── GSE264667_metadata.json
├── HepG2/
│   ├── GSE264667_HepG2.h5ad
│   ├── gene_expression_raw_obs.csv                     # all obs columns from the h5ad + metadata
│   ├── gene_expression_raw_var.csv                     # all var columns from the h5ad
│   ├── gene_expression_standardized_var.csv            # var index + global_feature_uid (from gene-resolver)
│   ├── gene_expression_fragment_preparer_obs.csv       # pass-through fields (batch_id, etc.)
│   ├── gene_expression_fragment_ontology_obs.csv       # ontology-resolved fields (organism, assay, etc.)
│   ├── gene_expression_fragment_perturbation_obs.csv   # perturbation UIDs, control flags (from genetic-perturbation-resolver)
├── Jurkat/
│   └── ...
```
