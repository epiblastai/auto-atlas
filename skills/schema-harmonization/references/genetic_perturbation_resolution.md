# Genetic perturbation resolution

Resolve genetic perturbation targets and schema fields, typically defined in collection-level library tables.

Covers resolution of three input types that may co-exist in a single dataset:

1. **Gene names/symbols** — Target gene names (e.g., "TP53", "BRCA1").
2. **Guide RNA sequences** — Raw guide sequences from CRISPR screens. Aligns via BLAT to get genomic coordinates, as needed, then annotates with overlapping genes and target context. 20bp is the minimum for BLAT resolution and generally works great for resolving guide RNAs. This is because guide RNAs are chosen to exactly match the reference genome and to be unique within it.
3. **Genomic coordinates** — Pre-computed target regions (e.g., enhancer/promoter-targeting screens). Annotates with overlapping genes and target context without BLAT.
4. **Perturbation type** - Should resolve information about the perturbation type like whether it's CRISPRi, an ORF, or an siRNA. This information is sometimes found in library files or will be in collection-level metadata files like the publication.

## Task description

The expected input is a LanceDB URL and table name along with a target homeobox schema file. The name of the table must correspond exactly to one of the schema classes in the provided file.

This reference is designed to guide you through the specific resolution considerations for genetic perturbations.

## Resolution Strategy

All resolved columns follow the same principle: **never NaN unless there is genuinely no value**, and **always flag resolution status with a boolean `resolved` column.**

1. **Resolution succeeds** (`confidence >= 0.5`, `resolved_value` is not None) → use the canonical value from `GeneResolution` (e.g., `.symbol`, `.ensembl_gene_id`). Set `resolved=True`.
2. **NaN only when no value exists** — e.g., a gene has no symbol at all.

## Rules

- **Organism as scientific name.** Use `resolve_organisms()` to map common names to scientific names. Do not hardcode organism mappings.
- **Strip version suffixes** from Ensembl IDs before resolution (split on `.`).
- **Resolve per organism** when multiple organisms are detected (barnyard experiments).
- **Old Ensembl versions:** If a large fraction of Ensembl IDs fail, attempt recovery via gene symbols.
- **Output columns may overwrite raw columns.** In particular, resolved `organism` replaces any raw `organism` column.

## Imports

```python
from auto_atlas import (
    resolve_genes,
    resolve_guide_sequences,
    annotate_genomic_coordinates,
    is_control_label,
    detect_control_labels,
    detect_negative_control_type,
    parse_combinatorial_perturbations,
    classify_perturbation_method,
    GeneticPerturbationType,
)
from auto_atlas.assemblies import get_assembly_report
from auto_atlas.types import GeneResolution, GuideRnaResolution, ResolutionReport
from homeobox.schema import make_uid
```

## Worked example: Combinatorial genetic perturbation library

## Core Constraints

<!---I don't believe there's an auditable operation that can do this yet. It's especially challenging because it requires changing the shape of the table with repeated values when a row is split.-->
- **One perturbation per row.** Each accession-level row must represent exactly one reagent. If the library has combinatorial perturbations, where the combination is a single row, then these must be split into two or more rows.
- **Negative controls are not perturbations.** Control labels map to `None` in perturbation target fields, you can use the helper functions for detecting controls to decide.
- **Do not guess required guide-level fields.** If the schema requires `guide_sequence`, coordinates, or strand and the data is missing, stop and ask the user unless they explicitly approve nulls.

## Resolution Workflow

1. **Load & inspect** — reads the raw CSV, identifies columns
2. **Control detection** — `detect_control_labels` on the gene column, plus numbered-prefix check
3. **Optional row splitting** — if a reagent column contains paired entries such as `guideA|guideB`, split that column first so the output becomes one reagent per row
4. **Classify perturbation method** — `classify_perturbation_method` on the method string
5. **Resolve genes** — `resolve_genes` on unique non-control targets; if `--ensembl-column` is present, report mismatches and let the resolver's current Ensembl IDs take precedence unless the dataset explicitly requires a pinned release
6. **Build output** — maps results to schema fields, assigns UIDs, writes CSV

- `guide_sequence`:
  - Prefer a supplementary guide library or reagent manifest.
  - Join on `reagent_id`, guide ID, or other dataset-specific reagent keys.
  - If multiple possible joins exist, prefer the one that preserves one reagent per row and document the join key.
- `library_name`:
  - Prefer the library metadata file itself, then raw columns, then publication text if needed.
- `target_chromosome`:
  - BLAT and `GuideRnaResolution` return UCSC chromosome names (e.g., `chr1`). The schema may expect a different representation such as a GenBank accession (e.g., `CM000663.2`). Use `get_assembly_report()` from `auto_atlas.assemblies` to convert:
    ```python
    from auto_atlas.assemblies import get_assembly_report
    report = get_assembly_report("human", "GRCh38")
    seq = report.lookup("chr1")  # accepts UCSC, bare, GenBank, or RefSeq names
    seq.genbank_accession  # "CM000663.2"
    seq.ucsc_name          # "chr1"
    seq.sequence_name      # "1"
    ```
  - Check the target schema's docstring/comment for the expected naming convention. Populate `target_chromosome` accordingly using the appropriate `AssemblySequence` field.
- `target_start`, `target_end`, `target_strand`:
  - Prefer explicit columns from a guide library or manifest.
  - If absent, deterministically parse coordinates from reagent IDs when the identifier format encodes them.
- `target_context`:
  - Prefer explicit annotation from the library.
  - Otherwise infer from `resolve_guide_sequences()` or `annotate_genomic_coordinates()`.
  - For transcript-targeted CRISPRi screens, `promoter` is an acceptable fallback only when supported by the dataset design.
- `target_sequence_uid`:
  - Populate when the target sequence can be mapped unambiguously to a `ReferenceSequenceSchema` record already available to the workflow.
  - Otherwise leave null and justify it in the report.

### A7. Resolve by guide RNA sequence (if applicable)

```python
guide_col = "<guide_sequence_column>"
unique_guides = raw_df[guide_col].dropna().unique().tolist()
report = resolve_guide_sequences(unique_guides, organism="human")
print(f"Resolved: {report.resolved}/{report.total}, Ambiguous: {report.ambiguous}")
```

Deduplicate guide sequences before BLAT-backed resolution because guides are reused across many cells and BLAT is rate-limited. After inferring coordinates or target context, spot-check 3-5 guides with `resolve_guide_sequences()`. 

### A8. Resolve by genomic coordinates (if applicable)

```python
coordinates = []
for _, row in raw_df[raw_df["<chr_col>"].notna()].iterrows():
    coordinates.append({
        "chromosome": row["<chr_col>"],
        "start": int(row["<start_col>"]),
        "end": int(row["<end_col>"]),
        "strand": row.get("<strand_col>"),
    })

report = annotate_genomic_coordinates(coordinates, organism="human")
```

---

## Resolution Strategy

All resolved columns follow the same principle: **never NaN unless there is genuinely no value**, and **always flag resolution status with a boolean `resolved` column.**

1. **Resolution succeeds** → use canonical values. Set `resolved=True`.
2. **Resolution fails** (gene name unresolved, guide fails BLAT, coordinates have no gene overlap) → keep original values where possible, set `resolved=False`.
3. **NaN only when no value exists** — e.g., a cell has no perturbation target.
4. **Control labels → None** — "non-targeting", "NegCtrl0", etc. become None in perturbation columns (they inform `is_negative_control`, not the gene field).

NaN or missing perturbation values do not imply control.

- If delimiters or control labels are ambiguous, ask the user instead of guessing.
