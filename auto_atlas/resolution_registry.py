"""Map ontology and cross-reference authorities to resolver tools.

Bindings are explicit: one :class:`ResolverBinding` per :class:`OntologyRegistry` or
:class:`CrossReferenceDbRegistry` member. Harmonization scripts look up bindings here;
they do not infer tools from field names.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from auto_atlas.registries import CrossReferenceDbRegistry, OntologyRegistry

ResolutionMode = Literal["single", "custom", "none"]


class OntologyEntity(StrEnum):
    """Supported ontology entity types for CELLxGENE-compatible resolution."""

    CELL_TYPE = "cell_type"
    CELL_LINE = "cell_line"
    TISSUE = "tissue"
    DISEASE = "disease"
    ORGANISM = "organism"
    ASSAY = "assay"
    DEVELOPMENT_STAGE = "development_stage"
    ETHNICITY = "ethnicity"
    SEX = "sex"


ENTITY_TO_PREFIXES: dict[OntologyEntity, list[str]] = {
    OntologyEntity.CELL_TYPE: ["CL"],
    OntologyEntity.TISSUE: ["UBERON"],
    OntologyEntity.DISEASE: ["MONDO"],
    OntologyEntity.ORGANISM: ["NCBITaxon"],
    OntologyEntity.ASSAY: ["EFO"],
    OntologyEntity.DEVELOPMENT_STAGE: ["HsapDv", "MmusDv"],
    OntologyEntity.ETHNICITY: ["HANCESTRO"],
}

ENTITY_TO_ONTOLOGY_NAME: dict[OntologyEntity, str] = {
    OntologyEntity.CELL_TYPE: "Cell Ontology",
    OntologyEntity.CELL_LINE: "Cellosaurus",
    OntologyEntity.TISSUE: "UBERON",
    OntologyEntity.DISEASE: "MONDO",
    OntologyEntity.ORGANISM: "NCBITaxon",
    OntologyEntity.ASSAY: "EFO",
    OntologyEntity.DEVELOPMENT_STAGE: "HsapDv",
    OntologyEntity.ETHNICITY: "HANCESTRO",
    OntologyEntity.SEX: "PATO",
}

DEVELOPMENT_STAGE_ORGANISM_PREFIX: dict[str, str] = {
    "human": "HsapDv",
    "homo_sapiens": "HsapDv",
    "mouse": "MmusDv",
    "mus_musculus": "MmusDv",
}


@dataclass(frozen=True)
class ResolverBinding:
    """How a schema authority resolves in single-column mode."""

    tool: str
    resolution_field: str = "resolved_value"
    resolver_kwargs: dict[str, Any] = field(default_factory=dict)
    requires_organism: bool = False
    mode: ResolutionMode = "single"
    ontology_entity: OntologyEntity | None = None


_CROSSREF_NONE = ResolverBinding(tool="", mode="none")

ONTOLOGY_BINDINGS: dict[OntologyRegistry, ResolverBinding] = {
    OntologyRegistry.CL: ResolverBinding(tool="resolve_cell_types"),
    OntologyRegistry.UBERON: ResolverBinding(tool="resolve_tissues"),
    OntologyRegistry.MONDO: ResolverBinding(tool="resolve_diseases"),
    OntologyRegistry.NCBITAXON: ResolverBinding(tool="resolve_organisms"),
    OntologyRegistry.EFO: ResolverBinding(tool="resolve_assays"),
    OntologyRegistry.HANCESTRO: ResolverBinding(
        tool="resolve_ontology_terms",
        mode="custom",
        ontology_entity=OntologyEntity.ETHNICITY,
    ),
    OntologyRegistry.HSAPDV: ResolverBinding(
        tool="resolve_ontology_terms",
        mode="custom",
        ontology_entity=OntologyEntity.DEVELOPMENT_STAGE,
        requires_organism=True,
    ),
    OntologyRegistry.MMUSDV: ResolverBinding(
        tool="resolve_ontology_terms",
        mode="custom",
        ontology_entity=OntologyEntity.DEVELOPMENT_STAGE,
        requires_organism=True,
    ),
}

CROSSREF_BINDINGS: dict[CrossReferenceDbRegistry, ResolverBinding] = {
    CrossReferenceDbRegistry.ENSEMBL: ResolverBinding(
        tool="resolve_genes",
        resolution_field="ensembl_gene_id",
        resolver_kwargs={"input_type": "ensembl_id"},
    ),
    CrossReferenceDbRegistry.GENCODE: ResolverBinding(
        tool="resolve_genes",
        resolution_field="ensembl_gene_id",
        resolver_kwargs={"input_type": "ensembl_id"},
    ),
    CrossReferenceDbRegistry.UNIPROT: ResolverBinding(
        tool="resolve_proteins",
        resolution_field="uniprot_id",
    ),
    CrossReferenceDbRegistry.PUBCHEM: ResolverBinding(
        tool="resolve_molecules",
        resolution_field="pubchem_cid",
        resolver_kwargs={"input_type": "cid"},
    ),
    CrossReferenceDbRegistry.CELLOSAURUS: ResolverBinding(tool="resolve_cell_lines"),
    CrossReferenceDbRegistry.ENSEMBL_BIOMART: _CROSSREF_NONE,
    CrossReferenceDbRegistry.NCBI_GENE: _CROSSREF_NONE,
    CrossReferenceDbRegistry.NCBI_TAXONOMY: _CROSSREF_NONE,
    CrossReferenceDbRegistry.DOI: _CROSSREF_NONE,
    CrossReferenceDbRegistry.PUBMED: _CROSSREF_NONE,
    CrossReferenceDbRegistry.GENBANK: _CROSSREF_NONE,
    CrossReferenceDbRegistry.REFSEQ: _CROSSREF_NONE,
    CrossReferenceDbRegistry.INCHI: _CROSSREF_NONE,
    CrossReferenceDbRegistry.CHEMBL: _CROSSREF_NONE,
}


def ontology_binding(ontology: OntologyRegistry) -> ResolverBinding:
    """Return the resolver binding for an ontology authority."""
    try:
        return ONTOLOGY_BINDINGS[ontology]
    except KeyError as exc:
        raise KeyError(f"No resolver binding for ontology {ontology!r}") from exc


def crossref_binding(database: CrossReferenceDbRegistry) -> ResolverBinding:
    """Return the resolver binding for a cross-reference database authority."""
    try:
        return CROSSREF_BINDINGS[database]
    except KeyError as exc:
        raise KeyError(f"No resolver binding for cross-reference database {database!r}") from exc


def validate_bindings(resolver_tools: dict[str, object]) -> None:
    """Verify every registry member has a binding and single-mode tools are registered."""
    missing_ontology = set(OntologyRegistry) - set(ONTOLOGY_BINDINGS)
    if missing_ontology:
        raise RuntimeError(
            f"OntologyRegistry members missing bindings: {sorted(missing_ontology, key=str)}"
        )

    missing_crossref = set(CrossReferenceDbRegistry) - set(CROSSREF_BINDINGS)
    if missing_crossref:
        raise RuntimeError(
            f"CrossReferenceDbRegistry members missing bindings: "
            f"{sorted(missing_crossref, key=str)}"
        )

    for ontology, binding in ONTOLOGY_BINDINGS.items():
        if binding.mode == "single" and binding.tool not in resolver_tools:
            raise RuntimeError(
                f"Ontology {ontology.value!r} binding tool {binding.tool!r} "
                f"is not registered in RESOLVER_TOOLS"
            )

    for database, binding in CROSSREF_BINDINGS.items():
        if binding.mode == "single" and binding.tool not in resolver_tools:
            raise RuntimeError(
                f"Cross-reference {database.value!r} binding tool {binding.tool!r} "
                f"is not registered in RESOLVER_TOOLS"
            )
