---
name: data-preparer
description: Use when a user provides a URL or database accession code and a target homeobox schema file and wants to align and prepare the dataset for ingestion against that schema. Covers instructions for downloading files, organizing them properly, and orchestrating resolver sub-agents that perform metadata standardization.
---

# Data Preparer

## Scope

This skill handles the full data preparation pipeline prior to writing an actual ingestion script. It does NOT handle assembling standardized CSVs, adding data to LanceDB, or writing Zarr arrays.

> **This is a preparation and orchestration skill only.** Other skills exist for:
> - Reading, parsing, or inspecting supplementary files (e.g., guide libraries, reagent libraries, compound libraries). This skill does not provide the requisite detail to understand such files and you must choose an appropriate resolver and delegate to them instead.
> - Making decisions about how resolvers should interpret data (e.g., how dual-guide pairs map to schema rows, how control labels are detected, how transcript IDs map to target context). The resolver skills describe how to do this properly, this skill does not. You shouldn't make any assumptions or decisions on these points.
>
> Your job when executing this skill is to relay column names, file paths, and delimiters, etc. When in doubt, pass more context to resolvers rather than trying to interpret anything yourself.

## Scripts

You have access to scripts that can be used for common tasks. All paths are relative to this skill directory.

| Script | Usage | Purpose |
|--------|-------|---------|
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
3. **Foreign key classes** — These inherit directly from `LanceModel`. These tables are referenced by either the obs table or a feature registry table through a foreign key.

Our goal is to fill out each of the schemas and fields that apply to the provided GEO dataset, which will always include the obs class but may involve only a subset of the feature registry and foreign key classes in the schema file. If a field's purpose is unclear from its name, type, docstring or in-line comments, ask the user.

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

### 5. Download and organize files by experiment

Download the necessary files from GEO (be sure to use long enough timeouts for large files):

```
python scripts/download_geo_file.py <accession> <filename> [dest_dir]
```

Default destination: `/tmp/geo_agent/<accession>/`. Some GEO datasets have multiple files in a single tar archive -- extract it. If there are multiple versions of the same dataset, possibly indicated by terms like "filtered", "processed", or "validated", prefer these analysis-ready artifacts to the raw version. Ask the user if unsure.

Next group the files into subdirectories by experiment. Simply use `mv` to move the files into the correct subdirectory, no `cp` or `ln -s` for symlinks. Depending on the file formats and whether the assay is unimodal or multimodal, we may have multiple files bundled together in the same subdirectory. Do not create separate subdirectories for modalities captured in the same experiment.

### 6. Create raw obs and var dataframes

Each of the subdirectories should have dataframes that correspond to obs-level and, typically, var-level metadata as well. These dataframe might be csv or tsv or inside of an h5ad file. In either case, write new csv files with suffix `_{feature_space}_raw_obs.csv` and `_{feature_space}_raw_var.csv`, where the feature space might be "gene_expression", "chromatin_accessibility, "protein_abundance", etc. There shouldn't be more than 1 obs or var csv per modality.

You should not remove any columns from the original dataframes, but you may add additional fields that were discovered from the GEO metadata or the downloaded publication text. For example, the raw dataframes might not include global metadata like organism, cell type, or donor information. If that information is in the metadata or publication, create new columns relevant to the schema. Do not worry about standardizing the terms that you find because that is delegated to the resolver subagents.

For any obs fields that need only pass-through or type coercion (e.g., batch_id, replicate, well_position, days_in_vitro), write them to `{fs}_fragment_preparer_obs.csv` using the schema field names directly. For multimodal experiments, also run barcode reconciliation:

```
python scripts/reconcile_barcodes.py <experiment_dir>
```

### 7. Create global feature and foreign key tables

Before launching resolvers, create accession-level `_raw.csv` files that consolidate data across all experiments for entities that need global resolution.

**For each feature registry schema** (e.g., `GenomicFeatureSchema`):

1. Concatenate per-experiment `{fs}_raw_var.csv` files
2. Add columns: `var_index` (the var index value), `experiment_subdir`, `source_var_column`
3. Deduplicate on `var_index`
4. Write `{SchemaClassName}_raw.csv` at accession level (e.g., `GenomicFeatureSchema_raw.csv`)

**For each foreign key schema** (e.g., `GeneticPerturbationSchema`, `SmallMoleculeSchema`):

1. Extract relevant columns from obs across all experiments
2. Add a key column (e.g., `reagent_id`) for mapping back
3. Deduplicate on key
4. Write `{SchemaClassName}_raw.csv` at accession level

Column misalignment across datasets is OK — union of columns with NaN fills.

**Enrich `_raw.csv` with supplementary data.** Before handing off to resolvers, add supplementary info (e.g., publication metadata, global experimental variables, etc.) into `_raw.csv`. **`_raw.csv` contains all available information in unstandardized form.** The preparer never calls resolution functions; the resolver never hunts for supplementary files.

**Naming convention:** Use the full schema class name, examples might be: `GenomicFeatureSchema`, `GeneticPerturbationSchema`, `SmallMoleculeSchema`, `BiologicPerturbationSchema`, or `ProteinSchema`.

### 8. Delegate resolution to resolver subagents

Feature registries (var) and foreign key tables are resolved across ALL experiments in one pass. Same entity in multiple experiments gets one UID.

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
    Read the skill file at .claude/skills/<resolver-name>/SKILL.md and follow its workflow.

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

- Finalized global tables: `{SchemaClassName}.parquet` for each feature registry, foreign key schema, and publication schema
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
