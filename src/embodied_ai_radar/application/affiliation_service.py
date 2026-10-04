from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from embodied_ai_radar.domain.models import RankedPaper
from embodied_ai_radar.domain.affiliations import AffiliationLookup


class AffiliationSource(Protocol):
    def enrich(self, papers) -> dict[str, AffiliationLookup]: ...


class AffiliationRepository(Protocol):
    def update_author_affiliations(
        self,
        arxiv_id: str,
        affiliations: Sequence[Sequence[str]],
        *,
        source: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class AffiliationEnrichmentSummary:
    requested: int
    updated: int


class AffiliationService:
    def __init__(self, source: AffiliationSource, repository: AffiliationRepository) -> None:
        self.source = source
        self.repository = repository

    def enrich(self, papers: Sequence[RankedPaper]) -> AffiliationEnrichmentSummary:
        unique: dict[str, RankedPaper] = {item.paper.arxiv_id: item for item in papers}
        lookups: Mapping[str, AffiliationLookup] = self.source.enrich(
            [item.paper for item in unique.values()]
        )
        for arxiv_id, lookup in lookups.items():
            self.repository.update_author_affiliations(
                arxiv_id,
                lookup.affiliations,
                source=lookup.source,
            )
        return AffiliationEnrichmentSummary(requested=len(unique), updated=len(lookups))
