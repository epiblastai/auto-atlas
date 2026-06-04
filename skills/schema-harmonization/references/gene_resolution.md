# Gene resolution

Resolve gene identifiers in feature dataframes — typically the var index of a gene expression or chromatin accessibility matrix. Maps gene symbols and Ensembl IDs to canonical identifiers using the `auto_atlas` suite.

For genetic perturbation target resolution (obs-level: control detection, combinatorial splitting, guide RNA alignment, perturbation method classification), see **references/genetic_perturbation_resolution.md** instead.

## Task description

The expected input is a LanceDB URL and table name along with a target homeobox schema file. The name of the table must correspond to one of the schema classes in the provided file, modulo any feature space suffixes.

This reference is designed to guide you through the specific resolution considerations for gene symbols and Ensembl IDs.

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
