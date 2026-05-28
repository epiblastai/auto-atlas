---
name: geo-data-downloader
description: Use when given a GEO accession (with a GSE or GSM prefix) for ingestion into a homeobox atlas. Covers listing, selecting, and downloading GEO files and writing metadata.
---

# GEO Data Downloader

## Scope

This skill describes the process for finding and downloading data from the Gene Expression Omnibus (GEO).

## Scripts

You have access to scripts that can be used for common tasks. All paths are relative to this skill directory.

| Script | Usage | Purpose |
|--------|-------|---------|
| `scripts/list_geo_files.py` | `python scripts/list_geo_files.py GSE123456` | List supplementary files for any GEO accession (GSE or GSM) |
| `scripts/download_geo_file.py` | `python scripts/download_geo_file.py GSE123456 file.h5ad [dest_dir]` | Download a supplementary file via FTP (default dest: `/tmp/geo_agent/<accession>/`) |
| `scripts/write_metadata_json.py` | `python scripts/write_metadata_json.py <experiment_dir> <accession>` | Fetch GEO metadata and write metadata.json in the experiment directory |

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

### 2. Download and read GEO metadata

Download the metadata from the GEO series or sample records:

```
python scripts/write_metadata_json.py /tmp/geo_agent/<accession> <accession>
```

You may need to run this multiple times. Sometimes when the data is stored at the series level, it still references a sample record (e.g., the filename contains a GSM id). In this case, download the metadata from the series and from the referenced sample ids.

Read the relevant json files. These often include helpful information about how to use the files and high-level metadata like organism or assay.

### 3. Download files

Download the necessary files from GEO (be sure to use long enough timeouts for large files):

```
python scripts/download_geo_file.py <accession> <filename> [dest_dir]
```

Default destination: `/tmp/geo_agent/<accession>/`. Some GEO datasets have multiple files in a single tar archive -- extract it. If there are multiple versions of the same dataset, possibly indicated by terms like "filtered", "processed", or "validated", prefer these analysis-ready artifacts to the raw version. Ask the user if unsure.
