"""Registry of resolution tools for harmonization scripts.

Tool names match :attr:`~auto_atlas.types.ResolutionReport.tool` on resolver output.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from auto_atlas.genes import resolve_genes
from auto_atlas.guide_rna import resolve_guide_sequences
from auto_atlas.molecules import resolve_molecules
from auto_atlas.ontologies import (
    resolve_assays,
    resolve_cell_lines,
    resolve_cell_types,
    resolve_diseases,
    resolve_organisms,
    resolve_tissues,
)
from auto_atlas.proteins import resolve_proteins
from auto_atlas.types import ResolutionReport


@dataclass(frozen=True)
class ResolverTool:
    fn: Callable[..., ResolutionReport]
    values_param: str = "values"


RESOLVER_TOOLS: dict[str, ResolverTool] = {
    "resolve_genes": ResolverTool(resolve_genes),
    "resolve_proteins": ResolverTool(resolve_proteins),
    "resolve_molecules": ResolverTool(resolve_molecules),
    "resolve_guide_sequences": ResolverTool(resolve_guide_sequences, values_param="sequences"),
    "resolve_cell_types": ResolverTool(resolve_cell_types),
    "resolve_tissues": ResolverTool(resolve_tissues),
    "resolve_diseases": ResolverTool(resolve_diseases),
    "resolve_organisms": ResolverTool(resolve_organisms),
    "resolve_assays": ResolverTool(resolve_assays),
    "resolve_cell_lines": ResolverTool(resolve_cell_lines),
}


def list_resolver_tools() -> list[str]:
    return sorted(RESOLVER_TOOLS)


from auto_atlas.resolution_registry import validate_bindings

validate_bindings(RESOLVER_TOOLS)
