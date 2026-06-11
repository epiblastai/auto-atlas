"""Registries for reference content stored in the Auto Atlas reference DB."""

from enum import StrEnum


class OntologyRegistry(StrEnum):
    """Ontology prefixes loaded into the unified ``ontology_terms`` table."""

    CL = "CL"
    UBERON = "UBERON"
    MONDO = "MONDO"
    NCBITAXON = "NCBITaxon"
    EFO = "EFO"
    HSAPDV = "HsapDv"
    MMUSDV = "MmusDv"
    HANCESTRO = "HANCESTRO"


class CrossReferenceDbRegistry(StrEnum):
    """Identifier authority names represented in the reference DB."""

    ENSEMBL = "ENSEMBL"
    ENSEMBL_BIOMART = "Ensembl BioMart"
    GENCODE = "GENCODE"
    NCBI_GENE = "NCBI Gene"
    NCBI_TAXONOMY = "NCBI Taxonomy"
    UNIPROT = "UniProt"
    PUBCHEM = "PubChem"
    CELLOSAURUS = "Cellosaurus"
    DOI = "DOI"
    PUBMED = "PubMed"
    GENBANK = "GenBank"
    REFSEQ = "RefSeq"
    INCHI = "InChI"
    CHEMBL = "ChEMBL"


def parse_ontology(value: str) -> OntologyRegistry:
    """Parse a schema ``ontology_name`` string into :class:`OntologyRegistry`."""
    try:
        return OntologyRegistry(value)
    except ValueError as exc:
        known = ", ".join(sorted(member.value for member in OntologyRegistry))
        raise ValueError(f"Unknown ontology {value!r}. Known ontologies: {known}") from exc


def parse_crossref(value: str) -> CrossReferenceDbRegistry:
    """Parse a schema ``database_name`` string into :class:`CrossReferenceDbRegistry`."""
    try:
        return CrossReferenceDbRegistry(value)
    except ValueError as exc:
        known = ", ".join(sorted(member.value for member in CrossReferenceDbRegistry))
        raise ValueError(
            f"Unknown cross-reference database {value!r}. Known databases: {known}"
        ) from exc
