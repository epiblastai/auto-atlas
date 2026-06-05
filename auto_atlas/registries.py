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
