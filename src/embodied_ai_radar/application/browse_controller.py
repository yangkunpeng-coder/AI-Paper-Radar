from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from embodied_ai_radar.application.library_service import LibraryService
from embodied_ai_radar.domain.library import LibraryPaper, LibraryViewCounts
from embodied_ai_radar.domain.research_domains import TOPIC_ALL


@dataclass(frozen=True, slots=True)
class BrowseQuery:
    """Complete SQL-backed browse state, independent from Flet controls."""

    updated_after: datetime
    domain_key: str
    topic_key: str = TOPIC_ALL
    favorites_only: bool = False
    sort_mode: str = "interest"


@dataclass(frozen=True, slots=True)
class BrowseSnapshot:
    entries: tuple[LibraryPaper, ...]
    counts: LibraryViewCounts


class BrowseController:
    """Application-layer facade for paged paper browsing and bulk reads.

    UI code owns interaction state and stale-request guards; this controller owns
    translating one immutable browse query into repository calls. Keeping that
    wiring outside Flet makes pagination/filter semantics directly testable.
    """

    def __init__(self, library: LibraryService) -> None:
        self.library = library

    def load_page(
        self,
        query: BrowseQuery,
        *,
        limit: int = 24,
        offset: int = 0,
    ) -> list[LibraryPaper]:
        return self.library.since(
            query.updated_after,
            limit=limit,
            offset=offset,
            domain_key=query.domain_key,
            topic_key=query.topic_key,
            favorites_only=query.favorites_only,
            sort_mode=query.sort_mode,
        )

    def counts(self, query: BrowseQuery) -> LibraryViewCounts:
        return self.library.counts_since(
            query.updated_after,
            domain_key=query.domain_key,
            topic_key=query.topic_key,
            favorites_only=query.favorites_only,
        )

    def load_snapshot(self, query: BrowseQuery, *, limit: int = 24) -> BrowseSnapshot:
        return BrowseSnapshot(
            entries=tuple(self.load_page(query, limit=limit)),
            counts=self.counts(query),
        )

    def load_all(self, query: BrowseQuery, *, batch_size: int = 500) -> list[LibraryPaper]:
        return self.library.all_since(
            query.updated_after,
            batch_size=batch_size,
            domain_key=query.domain_key,
            topic_key=query.topic_key,
            favorites_only=query.favorites_only,
            sort_mode=query.sort_mode,
        )
