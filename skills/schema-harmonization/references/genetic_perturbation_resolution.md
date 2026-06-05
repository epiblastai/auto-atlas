# Genetic perturbation resolution

Resolve genetic perturbation targets and their associated fields — the reagents, target genes, genomic locations, control status, and perturbation modality that describe what was perturbed in each row. These typically live in collection-level library tables or per-accession reagent manifests.

For resolving gene identifiers themselves (feature/`var`-level symbol and Ensembl ID canonicalization of an expression matrix), see **references/gene_resolution.md** instead.

## Task description

The expected input is a LanceDB URL and table name along with a target homeobox schema file. The name of the table must correspond to one of the schema classes in the provided file.

A single table may mix several kinds of perturbation evidence that resolve through different tools:

1. **Target gene names/symbols** — a named gene the reagent perturbs (e.g. "TP53", "BRCA1").
2. **Guide RNA sequences** — raw CRISPR guide sequences. Aligned via BLAT to recover genomic coordinates, then annotated with the overlapping gene and target context. 20bp is the minimum for reliable BLAT resolution and generally works well, because guides are designed to match the reference genome exactly and uniquely.
3. **Genomic coordinates** — pre-computed target regions (e.g. enhancer/promoter-targeting screens). Annotated with overlapping genes and context directly, without BLAT.

Separately, a row may carry a free-text **perturbation modality** (the technique used — knockout, interference, activation, overexpression, knockdown, etc.) that needs normalizing, and a **control status** that determines whether the row has a perturbation target at all.

This reference is designed to guide you through the specific resolution considerations for genetic perturbations.

## Resolution Strategy

1. **Resolution succeeds** (sufficient confidence, `resolved_value` is not None) → use the canonical values from the resolver result (e.g. a `GuideRnaResolution`'s coordinates and intended gene, or a `GeneResolution`'s symbol and Ensembl ID).
2. **Resolution fails** (gene name unresolved, guide fails BLAT, coordinates overlap no gene) → keep the original value where one exists.
3. **Control labels → None** — non-targeting / scramble / vehicle labels become None in target fields; they inform control-status fields, not target fields.

## Rules

- **Organism as scientific name.** Resolve common organism names to scientific names with `resolve_organisms()` rather than hardcoding mappings, and pass the organism through to the gene and guide resolvers.
<!--- I don't believe there's an auditable operation that can do row splitting yet. It's especially challenging because it requires changing the shape of the table with repeated values when a row is split. -->
- **One perturbation per row.** Each accession-level row must represent exactly one reagent/target. Combinatorial perturbations encoded in a single row (e.g. `guideA|guideB`) must be split into multiple rows before resolution.
- **Controls are not perturbations.** Use the control-detection helpers to identify control rows. If it's non-targeting, intergenic, or another control type their genetic target, if a field in the schema, should be null.
- **Missing ≠ control.** A null or empty target does not imply a negative control.
- **Don't guess required guide-level fields.** If the schema requires a guide sequence, coordinates, or strand and the data lacks them, stop and ask the user rather than fabricating values unless the user explicitly approves nulls.
- **Deduplicate before BLAT.** Guide sequences repeat across many rows and BLAT is rate-limited — resolve the distinct set, then map results back.
- **Ambiguity → ask.** If delimiters, control labels, or join keys are ambiguous, ask the user instead of guessing.

## Tools

Two tiers of tooling are available. **Batch resolvers** make external lookups, return a `ResolutionReport`, and can be driven by `scripts/apply_resolution_pass.py` to emit auditable curation ops. **Local helpers** are deterministic pure-Python functions you call inline when building custom transactions.

```python
from auto_atlas import (
    resolve_genes,
    resolve_guide_sequences,
    annotate_genomic_coordinates,
    resolve_organisms,
    is_control_label,
    detect_control_labels,
    detect_negative_control_type,
    parse_combinatorial_perturbations,
    classify_perturbation_method,
)
from auto_atlas.assemblies import get_assembly_report
from auto_atlas.types import GeneResolution, GuideRnaResolution, ResolutionReport
```

### Batch resolvers

| Tool | Input | What it finds (resolver result fields) | Use it to fill |
|------|-------|-----------------------------------------|----------------|
| `resolve_genes(values, organism="human", input_type="auto")` | Gene symbols or Ensembl IDs | canonical `symbol`, `ensembl_gene_id`, `organism`, `ncbi_gene_id` | any field holding a **named target gene** |
| `resolve_guide_sequences(sequences, organism="human")` | Guide RNA sequences (≥20bp) | `chromosome`, `target_start`, `target_end`, `target_strand`, `intended_gene_name`, `intended_ensembl_gene_id`, `target_context`, `assembly`, `blat_pct_match` | **genomic location**, **strand**, **targeted gene**, or **target context** fields, derived from raw guides via BLAT |
| `annotate_genomic_coordinates(coordinates, organism="human")` | Dicts of `chromosome`/`start`/`end`/(`strand`) | same `GuideRnaResolution` fields, **without BLAT** | the same fields when coordinates are **already known** and only gene-overlap/context annotation is needed |

Each returns a `ResolutionReport` (`total`, `resolved`, `unresolved`, `ambiguous`, `results`), with one `Resolution` per input value.

**Only `resolve_genes` and `resolve_guide_sequences` are registered with `apply_resolution_pass.py`.** `annotate_genomic_coordinates` is a custom Python step.

**Applying resolver output — the single-field caveat.** `resolve_genes` maps cleanly onto the script: one canonical field, one `--resolution-field-name`, one `ReplaceValue` pass (exactly as in **references/gene_resolution.md**). Guide and coordinate resolution instead produce **many correlated fields from one expensive, rate-limited call**, so driving the script once per field would re-run BLAT each time. Prefer a **custom transaction**: call the resolver once on the distinct guides, then build a multi-column `SetColumn`/`AddColumn` transaction from the single `ResolutionReport`. See **references/auditable_curation.md**.

```python
# Resolve by guide RNA sequence — dedupe first, BLAT is rate-limited
guides = raw_df["<guide_col>"].dropna().unique().tolist()
report = resolve_guide_sequences(guides, organism="human")
print(f"Resolved: {report.resolved}/{report.total}, Ambiguous: {report.ambiguous}")
```

```python
# Annotate pre-computed coordinates — skips BLAT
coordinates = [
    {
        "chromosome": row["<chr_col>"],
        "start": int(row["<start_col>"]),
        "end": int(row["<end_col>"]),
        "strand": row.get("<strand_col>"),
    }
    for _, row in raw_df[raw_df["<chr_col>"].notna()].iterrows()
]
report = annotate_genomic_coordinates(coordinates, organism="human")
```

After inferring coordinates or target context for a large screen, spot-check 3–5 guides with `resolve_guide_sequences()` to confirm the mapping.

### Local helpers

These take plain values and return plain values — use them inside `SetColumn`/`AddColumn` expressions or to decide which rows to touch.

| Helper | Returns | Use it for |
|--------|---------|------------|
| `is_control_label(value)` / `detect_control_labels(values)` | `bool` / `list[bool]` | deciding which rows are controls so their **target fields** become None |
| `detect_negative_control_type(value)` | a canonical control-type string, or `None` | populating a **control-type / negative-control** field |
| `parse_combinatorial_perturbations(value)` | `list[str]` of individual targets (splits on `+ & ; \| ,`) | detecting and splitting **combinatorial** reagents into one-per-row |
| `classify_perturbation_method(value)` | a normalized perturbation-modality classification, or `None` | normalizing a free-text **perturbation modality/technique** field |

### Chromosome naming conversion

BLAT and `GuideRnaResolution` return **UCSC** chromosome names (e.g. `chr1`). A target schema may expect a different representation (bare `1`, a GenBank or RefSeq accession). Convert with `get_assembly_report()` rather than hardcoding mappings, and check the target schema's docstring/comment for the expected convention:

```python
report = get_assembly_report("human", "GRCh38")
seq = report.lookup("chr1")   # accepts UCSC, bare, GenBank, or RefSeq names
seq.genbank_accession  # "CM000663.2"
seq.ucsc_name          # "chr1"
seq.sequence_name      # "1"
```

## Sourcing fields

Where a field can come from more than one place, prefer the most authoritative source and fall back in order:

- **Guide sequence fields** — prefer a supplementary guide library or reagent manifest, joined on a reagent/guide key. If multiple joins are possible, prefer the one that preserves one reagent per row and document the join key.
- **Library / screen-identifier fields** — prefer the library metadata file itself, then raw columns, then publication text.
- **Genomic location fields** (chromosome, start, end, strand) — prefer explicit columns from a library or manifest; otherwise infer from `resolve_guide_sequences()` or `annotate_genomic_coordinates()`. If absent, deterministically parse coordinates from reagent IDs only when the identifier format encodes them. Convert chromosome naming with `get_assembly_report()`.
- **Target-context fields** — prefer explicit annotation from the library; otherwise infer from the guide/coordinate resolvers.
- **Cross-reference (UID / foreign-key) fields** that point at a record in another table — populate only when the target can be mapped unambiguously to a record already available to the workflow; otherwise leave null and justify it in the report.
- **Perturbation-modality fields** — the technique is sometimes in a library file and sometimes only in collection-level metadata such as the publication; normalize whatever string you find with `classify_perturbation_method()`.

## Worked example: combinatorial genetic perturbation library

> _TODO: worked example pending the row-splitting operation discussed above — combinatorial reagents need an auditable op that can change table shape (split one row into many with repeated values). To be written once that op exists._
