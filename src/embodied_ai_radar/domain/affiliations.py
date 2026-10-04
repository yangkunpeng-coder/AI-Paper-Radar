from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AffiliationLookup:
    arxiv_id: str
    affiliations: tuple[tuple[str, ...], ...]
    source: str
