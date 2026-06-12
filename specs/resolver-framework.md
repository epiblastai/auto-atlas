# Resolver framework

Proposal for a shared API that unifies the `resolve_*` functions in `auto_atlas`.
Today each resolver reimplements overlapping logic with different subsets of the
full pipeline. This spec describes the superset of steps and proposes a
`ResolverPipeline` class with pluggable stage protocols.

## Motivation

Registered resolvers (`resolve_genes`, `resolve_proteins`, `resolve_molecules`,
`resolve_guide_sequences`, `resolve_*` ontology wrappers) all:

- Accept a batch of input strings plus optional context (organism, input type).
- Return a `ResolutionReport` with one `Resolution` subclass per input.
- Perform some combination of normalization, local lookup, external lookup,
  disambiguation, enrichment, and (rarely) cache write-back.

Only `resolve_guide_sequences` implements the full “cache → API → write-back”
loop. Others omit API fallbacks, cache write-back, or even LanceDB batch lookup
(ontologies preload in-memory indices; molecule SMILES/CID paths skip LanceDB).

A shared framework would make new resolvers declarative, reduce duplicated
batching/disambiguation/report code, and make optional stages explicit.

## Superset pipeline

Every resolver is a composition of the stages below. Stages are optional per
resolver; the framework provides no-op defaults.

```
 inputs + context
       │
       ▼
┌──────────────────┐
│ 1. Preprocess    │  normalize casing, strip salts, canonicalize SMILES,
│                  │  map organism common_name → scientific_name, etc.
└────────┬─────────┘
         ▼
┌──────────────────┐
│ 2. Classify      │  route values to lanes (symbol vs ensembl_id, name vs
│                  │  smiles vs cid, entity-specific shortcuts)
└────────┬─────────┘
         ▼
┌──────────────────┐
│ 3. Deduplicate   │  resolve unique keys once; retain original casing/order map
└────────┬─────────┘
         ▼
┌──────────────────┐
│ 4. Local lookup  │  LanceDB batch WHERE, in-memory index, or FTS
└────────┬─────────┘
         ▼
┌──────────────────┐
│ 5. Disambiguate  │  pick best hit among multiple cache matches; set confidence
│                  │  and alternatives
└────────┬─────────┘
         ▼
┌──────────────────┐
│ 6. Enrich        │  secondary LanceDB join (alias hit → primary record)
└────────┬─────────┘
         ▼
┌──────────────────┐
│ 7. Fallback      │  ordered cascade for cache misses (external APIs, fuzzy
│    cascade       │  search, hardcoded tables, multi-step sub-pipelines)
└────────┬─────────┘
         ▼
┌──────────────────┐
│ 8. Build result  │  construct typed Resolution subclass per key
└────────┬─────────┘
         ▼
┌──────────────────┐
│ 9. Cache write   │  optional: persist API hits for future local lookups
└────────┬─────────┘
         ▼
┌──────────────────┐
│ 10. Re-expand    │  map unique-key results back to full input list
└────────┬─────────┘
         ▼
┌──────────────────┐
│ 11. Report       │  aggregate resolved / unresolved / ambiguous counts
└──────────────────┘
```

### Stage notes

| Stage | Present in | Absent from |
|-------|-----------|-------------|
| Preprocess | all | — |
| Classify | genes (`auto`), molecules (`input_type`), ontologies (entity) | proteins, guide RNAs |
| Deduplicate | guide RNAs, batch molecule names | per-value loops (ontology, smiles/cid) |
| Local lookup | genes, proteins, molecules (name), guide RNAs, ontologies | molecules (smiles/cid) |
| Disambiguate | genes, proteins, molecules (title vs synonym) | guide RNAs (single BLAT hit) |
| Enrich | genes, proteins, molecules | guide RNAs (fields set in one pass) |
| Fallback cascade | molecules (PubChem → ChEMBL), guide RNAs (BLAT → Ensembl), cell lines (FTS) | genes, proteins |
| Cache write-back | guide RNAs only | everything else |
| Special short-circuit | controls, sex (hardcoded PATO) | — |

### Shared conventions (preserve across framework)

- **Batch chunk size**: 500 for LanceDB `IN` clauses.
- **Unresolved stub**: `resolved_value=None`, `confidence=0.0`, `source="none"`.
- **Ambiguous**: standardize on `len(alternatives) > 0` — `alternatives` holds the
  *non-chosen* targets, so one alternative already means two viable candidates.
  This unifies a current divergence and fixes a latent bug: today
  `resolve_genes`/`resolve_proteins` count with `> 0` while
  `resolve_molecules`/ontologies count with `> 1`, **and**
  `ResolutionReport.ambiguous_values` filters with `> 1` — so a gene with exactly
  one alternative is counted as ambiguous but omitted from `ambiguous_values`. The
  framework must use one threshold (`> 0`) for both the per-report `ambiguous`
  count and the `ambiguous_values` property; fix the property as part of Phase 1.
- **Cache write of misses**: `CacheSink.to_record` may return a record for an
  *unresolved* result to memoize the negative lookup (guide RNAs cache failed
  BLAT/Ensembl resolutions so they are not retried). Returning `None` skips the
  write entirely. Preserve guide RNA's miss-caching behavior on migration.
- **Input fidelity**: `input_value` preserves caller's original string; lookup keys
  may be normalized separately.
- **Rate limiting**: applied inside fallback steps that call external services,
  not at the framework level.

## Core types

```python
from dataclasses import dataclass, field
from typing import Callable, Generic, Protocol, TypeVar

from auto_atlas.types import Resolution, ResolutionReport

R = TypeVar("R", bound=Resolution)


@dataclass(frozen=True)
class ResolverContext:
    """Per-run context passed to every stage."""

    organism: str | None = None
    tool: str = "resolve_unknown"
    # Extensible bag for entity, min_similarity, assembly, etc.
    extras: dict[str, object] = field(default_factory=dict)


@dataclass
class LookupHit:
    """Intermediate match before disambiguation / enrichment."""

    key: str  # normalized lookup key
    original: str  # caller's string for this key
    candidates: list[dict]  # raw rows or API payloads
    source: str  # e.g. "lancedb", "reference_db_synonym"


@dataclass
class Disambiguation:
    """Outcome of picking among a hit's candidates.

    ``chosen`` is the *winning raw candidate* (a row dict or API payload from
    ``LookupHit.candidates``), not a flattened string — the result builder reads
    typed fields off it (``ensembl_gene_id``, ``pubchem_cid``, ``is_title``, …).
    ``chosen=None`` means no acceptable pick; the builder emits an unresolved stub.
    """

    chosen: dict | None
    confidence: float
    source: str  # provenance of the chosen candidate (e.g. "lancedb")
    alternatives: list[str] = field(default_factory=list)


@dataclass
class PipelineState(Generic[R]):
    """Mutable state threaded through pipeline stages."""

    inputs: list[str]
    context: ResolverContext
    # normalized_key → original (first-seen casing)
    key_map: dict[str, str] = field(default_factory=dict)
    # normalized_key → LookupHit or None (miss)
    local_hits: dict[str, LookupHit | None] = field(default_factory=dict)
    # normalized_key → fully built resolution
    results: dict[str, R] = field(default_factory=dict)
    # records to write back to cache (optional)
    cache_writes: list[dict] = field(default_factory=list)
```

Existing `Resolution` subclasses and `ResolutionReport` remain the public output
types. The framework does not introduce a parallel result hierarchy.

## API design

A `ResolverPipeline` class owns stage registration and execution. Stages implement
small protocols; the pipeline enforces ordering and provides typed hooks. Optional
fields default to no-op, so a cache-only resolver omits `fallbacks` and
`cache_sink` rather than branching internally.

```python
class Preprocessor(Protocol):
    def __call__(self, value: str, ctx: ResolverContext) -> str: ...


class LocalLookup(Protocol[R]):
    def lookup(
        self, keys: list[str], ctx: ResolverContext
    ) -> dict[str, LookupHit | None]: ...


class Disambiguator(Protocol):
    def pick(self, hit: LookupHit, ctx: ResolverContext) -> Disambiguation: ...


class ResultBuilder(Protocol[R]):
    """Build the typed ``Resolution`` for a key from its disambiguation.

    Owns *both* the resolved and the unresolved path: ``picked is None`` (a miss
    that survived every fallback) yields the unresolved stub, and that stub still
    carries resolver-specific context fields (``organism`` for genes/proteins,
    ``ontology_name`` for ontologies). This replaces the underspecified
    ``resolution_factory`` — a bare factory could not thread those context fields.
    """

    def build(
        self,
        key: str,
        original: str,
        picked: Disambiguation | None,
        ctx: ResolverContext,
    ) -> R: ...


class Enricher(Protocol[R]):
    # Batch over all results: genes/proteins enrich via one secondary lookup
    # keyed by the resolved id, so a per-item hook would fan out into N queries.
    def enrich(self, results: dict[str, R], ctx: ResolverContext) -> dict[str, R]: ...


class Fallback(Protocol[R]):
    def try_resolve(self, key: str, original: str, ctx: ResolverContext) -> R | None: ...


class CacheSink(Protocol[R]):
    # Returning a record for an *unresolved* result memoizes the miss (guide RNAs
    # cache negative BLAT/Ensembl lookups today); return None only to skip writes.
    def to_record(self, result: R, ctx: ResolverContext) -> dict | None: ...
    # The sink owns persistence (it knows its table + record schema), keeping the
    # pipeline decoupled from the reference DB.
    def write(self, records: list[dict]) -> None: ...


@dataclass
class ResolverPipeline(Generic[R]):
    tool: str
    result_builder: ResultBuilder[R]  # builds resolved results and unresolved stubs

    preprocessor: Preprocessor | None = None
    prescan_fallbacks: list[Fallback[R]] = field(default_factory=list)
    local_lookup: LocalLookup[R] | None = None
    disambiguator: Disambiguator | None = None
    enricher: Enricher[R] | None = None
    fallbacks: list[Fallback[R]] = field(default_factory=list)
    cache_sink: CacheSink[R] | None = None

    def resolve(self, values: list[str], *, tool: str | None = None, **ctx_kwargs) -> ResolutionReport:
        # ``tool`` overrides the report/provenance label per call, so resolvers
        # that share one pipeline under several public names (the ontology
        # wrappers: resolve_cell_types, resolve_tissues, …) each stamp their own.
        ctx = ResolverContext(tool=tool or self.tool, **ctx_kwargs)
        state = self._init_state(values, ctx)
        state = self._run_preprocess(state)
        state = self._run_deduplicate(state)
        state = self._run_prescan_fallbacks(state)
        state = self._run_local_lookup(state)
        state = self._run_disambiguate_and_build(state)  # disambiguate → ResultBuilder
        state = self._run_enrich(state)
        state = self._run_fallbacks(state)
        state = self._run_cache_write(state)
        return self._reexpand_and_report(state)  # stage 10 (re-expand) + stage 11 (report)
```

Implementation lives in `auto_atlas/resolvers/`. Thin public `resolve_*` functions
remain the stable entry points and delegate to a module-level pipeline instance.

### Declaring a resolver

```python
# auto_atlas/genes.py — two lanes, routed in plain Python (no classifier stage)

gene_symbol_pipeline = ResolverPipeline[GeneResolution](
    tool="resolve_genes",
    result_builder=GeneSymbolResultBuilder(),  # carries organism onto resolved + stub
    preprocessor=lowercase_strip,
    local_lookup=AliasLookup(GENOMIC_FEATURE_ALIASES_TABLE, "ensembl_gene_id"),
    disambiguator=CanonicalAliasDisambiguator(),
    enricher=GeneFeatureEnricher(),
)

gene_ensembl_pipeline = ResolverPipeline[GeneResolution](
    tool="resolve_genes",
    result_builder=GeneEnsemblResultBuilder(),
    local_lookup=GeneEnsemblLookup(),  # detects organism per Ensembl prefix, then queries
)

def resolve_genes(values, organism="human", input_type="auto") -> ResolutionReport:
    # classify by input shape, run each lane, merge positionally back into order
    symbol_idx, ensembl_idx = _split_lanes(values, input_type)
    results = [None] * len(values)
    if symbol_idx:
        report = gene_symbol_pipeline.resolve([values[i] for i in symbol_idx], organism=organism, ...)
        for i, res in zip(symbol_idx, report.results, strict=True):
            results[i] = res
    if ensembl_idx:
        report = gene_ensembl_pipeline.resolve([values[i] for i in ensembl_idx], organism=organism)
        for i, res in zip(ensembl_idx, report.results, strict=True):
            results[i] = res
    ...
```

```python
# auto_atlas/resolvers/guide_rna.py

guide_pipeline = ResolverPipeline[GuideRnaResolution](
    tool="resolve_guide_sequences",
    result_builder=GuideRnaResultBuilder(),
    preprocessor=uppercase_key,
    local_lookup=GuideRnaCacheLookup(),
    fallbacks=[BlatEnsemblFallback()],
    cache_sink=GuideRnaCacheSink(),  # to_record returns a row even for misses
)

def resolve_guide_sequences(sequences, organism="human") -> ResolutionReport:
    return guide_pipeline.resolve(sequences, organism=organism)
```

```python
# auto_atlas/resolvers/molecules.py — control short-circuit before LanceDB

molecule_name_pipeline = ResolverPipeline[MoleculeResolution](
    tool="resolve_molecules",
    result_builder=MoleculeResultBuilder(),
    preprocessor=clean_compound_name,
    prescan_fallbacks=[ControlCompoundFallback()],
    local_lookup=CompoundSynonymLookup(),
    disambiguator=TitlePreferenceDisambiguator(),
    enricher=CompoundSmilesEnricher(),
    fallbacks=[PubChemFallback(), ChemblFallback()],
)
```

### Built-in lookup strategies

Only one `LocalLookup` turned out to be genuinely shared across resolvers; the
rest were specific enough that a hand-written class per module was clearer than a
parameterized abstraction. As-built:

| Class | Backing | Used for |
|-------|---------|----------|
| `AliasLookup(table_name, id_column)` | alias table, chunked `IN`, grouped by `alias` | genes (symbol lane), proteins |
| `GeneEnsemblLookup` | features table, organism detected per Ensembl prefix | genes (ensembl lane) |
| `CompoundSynonymLookup` | synonyms + compounds tables | molecules (name lane) |
| `OntologyTermLookup` / `CellLineLookup` | `functools.lru_cache` name/synonym index | ontology terms, cell lines |
| `SexLookup` | static `_SEX_TERMS` dict | sex terms |
| `GuideRnaCacheLookup` | guide_rnas table (negative cache) | guide RNAs |

`AliasLookup` is the shared one: it reads `ctx.extras["scientific_name"]`,
queries the alias `table_name`, groups rows by `alias`, and returns a `LookupHit`
whose candidates carry `{id, is_canonical}` for `CanonicalAliasDisambiguator`.

> The original spec proposed a single generic `LanceDBBatchLookup(table_name,
> key_column, partition, select, group_by)` to back every lookup. During
> migration this never paid off — each lookup needed enough table-specific shape
> (grouping, id columns, version stripping, per-prefix organism detection) that
> the generic form would have been configured into the same code anyway. The
> genes ensembl lane in particular owns its `detect_organism_from_ensembl_ids` →
> group-by-organism loop directly in `GeneEnsemblLookup` rather than expressing
> it as a `partition` callback. The abstraction was dropped.

### Built-in fallback patterns

| Class | Behavior |
|-------|----------|
| `ChainedFallback` | try fallbacks in order until one returns non-None |
| `RateLimitedFallback` | wraps a `Fallback` with `@rate_limited` |
| `MultiStepFallback` | internal sub-pipeline (BLAT → Ensembl for guide RNAs) |
| `ShortCircuitFallback` | runs via `prescan_fallbacks` before local lookup (controls, hardcoded sex) |

### Shared components to extract first

These utilities are the first migration targets; they back the pipeline stages
above:

1. **`deduplicate_keys(state, normalizer)`** — builds `key_map` and unique key list.
2. **`AliasLookup(table_name, id_column)`** — 500-chunk `IN` queries with
   `sql_escape`, grouped by `alias`; shared by genes (symbol lane) and proteins.
3. **`CanonicalAliasDisambiguator`** — `is_canonical` flag logic shared by genes
   and proteins (confidence 1.0 / 0.9 / 0.7, sorted alternatives), returning a
   `Disambiguation` whose `chosen` is the winning alias row.
4. **`reexpand_and_report`** — input-order list + `ResolutionReport` counts, using
   the unified `len(alternatives) > 0` ambiguity threshold.
5. **`CacheSink` protocol** — optional write-back; default no-op.

## Mapping current resolvers

The "Lane split" column is *not* a pipeline stage — resolvers that handle several
input shapes route in plain Python in their public function (one pipeline per
lane), then merge results positionally. There is no `classifier` stage.

| Resolver | Preprocess | Lane split | Local | Disambiguate | Enrich | Fallback | Cache write |
|----------|------------|------------|-------|--------------|--------|----------|-------------|
| `resolve_genes` | lowercase | symbol/ensembl | alias + feature tables | canonical | features | — | — |
| `resolve_proteins` | lowercase | — | alias table | canonical | proteins | — | — |
| `resolve_molecules` (name) | clean_compound_name | control skip | synonyms + compounds | title preference | SMILES | PubChem → ChEMBL | — |
| `resolve_molecules` (smiles) | RDKit canonicalize | — | — | — | — | PubChem | — |
| `resolve_molecules` (cid) | int parse | — | — | — | — | PubChem | — |
| `resolve_guide_sequences` | uppercase key | — | guide_rnas | — | — | BLAT → Ensembl | guide_rnas |
| `resolve_ontology_terms` | strip/lower | entity | memory index | — | — | FTS (cell lines) | — |
| `resolve_ontology_terms` (sex) | strip/lower | — | hardcoded | — | — | — | — |

## Registry integration

`ResolverTool` in `registry.py` already wraps callables. The existing field is
`fn` (not `resolve`), and `apply_resolution_pass.py` invokes it as
`spec.fn(**{spec.values_param: distinct, **kwargs})` — keep that name so the
consumer is untouched. After migration, add an optional pipeline handle for
introspection only:

```python
@dataclass(frozen=True)
class ResolverTool:
    fn: Callable[..., ResolutionReport]
    values_param: str = "values"
    pipeline: ResolverPipeline | None = None  # introspection / docs generation
```

`apply_resolution_pass.py` and fan-out mode remain unchanged; they depend on
`ResolutionReport` shape and `ResolverTool.fn` / `values_param`, not resolver
internals.

## Migration plan

1. **Phase 1** — Extract shared utilities (`deduplicate_keys`, batch lookup,
   disambiguator, report builder) without changing public APIs. Also unify the
   ambiguity threshold: switch every resolver's `ambiguous` count and
   `ResolutionReport.ambiguous_values` to `len(alternatives) > 0` (the lone
   behavior change in this phase).
2. **Phase 2** — Reimplement `resolve_proteins` on the pipeline (smallest
   API surface; validates cache-only path).
3. **Phase 3** — Migrate `resolve_genes`, `resolve_molecules`, ontology
   wrappers, `resolve_guide_sequences`.
4. **Phase 4** — Delete duplicated per-module batching code; add introspection
   helper (`describe_resolver("resolve_genes")` → stage list).

Public function signatures (`resolve_genes(values, organism=..., input_type=...)`,
etc.) stay stable through Phase 3. Internal implementation swaps under them.

## Non-goals

- **Unifying `resolve_sequence_names`** — returns `list[dict]`, not
  `ResolutionReport`; out of scope unless a separate `AssemblyResolver` is added.
- **Wiring `ols.py` into ontology resolvers** — desirable fallback, but not
  required for the framework itself; add as a `Fallback` implementation later.
- **Automatic cache write-back for molecules** — possible future `CacheSink`,
  but not assumed by default (reference tables are curated imports, not runtime
  mutation).
- **Async / parallel API calls** — keep sequential execution with rate
  limiting; parallelization is a later optimization.

## Resolved design decisions

These were open questions in earlier drafts; the API above now commits to them.

1. **Multi-input resolvers route in plain Python, not via a `classifier`
   stage.** The earlier draft proposed per-key lane labels carried in
   `PipelineState`; in practice the resolvers that handle several input shapes
   (genes, molecules, ontologies) compose one pipeline per lane and split/merge
   in their public function. The genes per-Ensembl-ID organism grouping lives
   inside `GeneEnsemblLookup`. The `classifier` stage was never used and was
   removed.
2. **Disambiguation returns the chosen candidate, not a string.** `Disambiguator`
   yields a `Disambiguation` carrying the winning raw row; `ResultBuilder.build`
   reads typed fields off it. This keeps the `ensembl_gene_id` / `pubchem_cid` /
   `is_title` payload that a flattened `(str, float, list)` tuple would drop.
3. **Result building owns both resolved and unresolved output**, replacing the
   bare `resolution_factory`. The builder receives `ctx`, so unresolved stubs keep
   their resolver-specific context fields (`organism`, `ontology_name`).
4. **Enrichment is a batch stage**, not per-item: `enrich(results, ctx)` receives
   the whole result map, because genes/proteins enrich with a single secondary
   lookup keyed by the resolved id — a per-item hook would fan out into one query
   per result. It may mutate in place and return the same map.

## Open questions

1. **Testing** — each `Fallback` and `LocalLookup` gets fixture-driven unit
   tests; pipeline integration tests mock LanceDB and HTTP.
2. **`describe_resolver` surface** — Phase 4 introspection reads stage instances
   off the pipeline. Open whether it emits a flat stage list or a richer tree
   (lanes, fallback order); defer until a consumer needs it.
