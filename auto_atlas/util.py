"""Shared internal utilities."""

import uuid

# Matches the uid used in homeobox
_AA_NS = uuid.UUID("b3e7a9f1-6c2d-4a8b-9f01-3d5e7a2b8c4f")


def make_stable_uid(*identity_values: str) -> str:
    """Deterministic 16-char hex UID from identity values.

    Same inputs always produce the same UID. Used for entity deduplication
    across datasets (genes, proteins, molecules, perturbations, publications).
    """
    return uuid.uuid5(_AA_NS, "|".join(identity_values)).hex[:16]


def sql_escape(s: str) -> str:
    """Escape single quotes for LanceDB SQL string literals."""
    return s.replace("'", "''")
