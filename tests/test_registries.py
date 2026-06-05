from auto_atlas.registries import CrossReferenceDbRegistry, OntologyRegistry


def test_ontology_registry_values() -> None:
    assert {ontology.value for ontology in OntologyRegistry} == {
        "CL",
        "UBERON",
        "MONDO",
        "NCBITaxon",
        "EFO",
        "HsapDv",
        "MmusDv",
        "HANCESTRO",
    }


def test_cross_reference_db_registry_values() -> None:
    assert {db.value for db in CrossReferenceDbRegistry} == {
        "ENSEMBL",
        "Ensembl BioMart",
        "GENCODE",
        "NCBI Gene",
        "NCBI Taxonomy",
        "UniProt",
        "PubChem",
        "Cellosaurus",
    }
