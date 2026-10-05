from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Protocol

from ai_paper_analyzer.domain.library import (
    LibraryPaper,
    LibraryViewCounts,
    PaperUserState,
    SyncState,
)
from ai_paper_analyzer.domain.llm import PaperAIAnalysis
from ai_paper_analyzer.domain.models import RankedPaper
from ai_paper_analyzer.domain.relevance import RANKING_ALGORITHM_VERSION, rank_paper
from ai_paper_analyzer.domain.research_domains import (
    DOMAIN_EMBODIED,
    PaperResearchTaxonomy,
)


class PaperLibraryRepository(Protocol):
    def initialize(self) -> None: ...

    def get_app_meta(self, key: str) -> str | None: ...

    def set_app_meta(self, key: str, value: str) -> None: ...

    def upsert_ranked_papers(
        self,
        papers: Sequence[RankedPaper],
        *,
        domain_key: str = DOMAIN_EMBODIED,
        topic_keys_by_arxiv_id: Mapping[str, Sequence[str]] | None = None,
        classification_source: str = "rule",
    ) -> None: ...

    def load_recent(
        self,
        *,
        limit: int = 500,
        offset: int = 0,
        domain_key: str | None = None,
        topic_key: str | None = None,
        favorites_only: bool = False,
        sort_mode: str = "interest",
    ) -> list[LibraryPaper]: ...

    def load_since(
        self,
        updated_after: datetime,
        *,
        limit: int = 5000,
        offset: int = 0,
        domain_key: str | None = None,
        topic_key: str | None = None,
        favorites_only: bool = False,
        sort_mode: str = "interest",
    ) -> list[LibraryPaper]: ...

    def load_all_since(
        self,
        updated_after: datetime,
        *,
        batch_size: int = 500,
        domain_key: str | None = None,
        topic_key: str | None = None,
        favorites_only: bool = False,
        sort_mode: str = "interest",
    ) -> list[LibraryPaper]: ...

    def count_since(
        self,
        updated_after: datetime,
        *,
        domain_key: str | None = None,
        topic_key: str | None = None,
        favorites_only: bool = False,
    ) -> LibraryViewCounts: ...

    def get_taxonomies(
        self,
        arxiv_ids: Sequence[str],
    ) -> dict[str, PaperResearchTaxonomy]: ...

    def get_sync_state(self, *, domain_key: str = DOMAIN_EMBODIED) -> SyncState: ...

    def get_domain_paper_versions(self, *, domain_key: str = DOMAIN_EMBODIED) -> dict[str, str]: ...

    def begin_sync(
        self, *, domain_key: str = DOMAIN_EMBODIED, attempted_at: datetime | None = None
    ) -> SyncState: ...

    def record_sync_checkpoint(
        self,
        *,
        from_date: date,
        until_date: date,
        domain_key: str = DOMAIN_EMBODIED,
        completed_at: datetime | None = None,
    ) -> SyncState: ...

    def finish_sync(self, *, domain_key: str = DOMAIN_EMBODIED) -> SyncState: ...

    def cancel_sync(
        self, *, domain_key: str = DOMAIN_EMBODIED, cancelled_at: datetime | None = None
    ) -> SyncState: ...

    def fail_sync(self, message: str, *, domain_key: str = DOMAIN_EMBODIED) -> SyncState: ...

    def record_sync_success(
        self,
        *,
        from_date: date,
        until_date: date,
        domain_key: str = DOMAIN_EMBODIED,
        completed_at: datetime | None = None,
    ) -> SyncState: ...

    def get_states(self, arxiv_ids: Sequence[str]) -> dict[str, PaperUserState]: ...

    def set_favorite(self, arxiv_id: str, value: bool) -> PaperUserState: ...

    def set_read(self, arxiv_id: str, value: bool) -> PaperUserState: ...

    def save_analyses(
        self,
        *,
        model: str,
        domain_key: str = DOMAIN_EMBODIED,
        analyses: Mapping[str, PaperAIAnalysis],
    ) -> None: ...

    def save_analyses_with_topics(
        self,
        *,
        model: str,
        domain_key: str = DOMAIN_EMBODIED,
        analyses: Mapping[str, PaperAIAnalysis],
        classification_source: str = "deepseek-domain-v1",
    ) -> None: ...

    def load_analyses(
        self,
        arxiv_ids: Sequence[str],
        *,
        domain_key: str = DOMAIN_EMBODIED,
    ) -> dict[str, PaperAIAnalysis]: ...

    def replace_analysis_topics(
        self,
        *,
        domain_key: str,
        topic_keys_by_arxiv_id: Mapping[str, Sequence[str]],
        classification_source: str = "deepseek-domain-v1",
    ) -> None: ...

    def update_author_affiliations(
        self,
        arxiv_id: str,
        affiliations: Sequence[Sequence[str]],
        *,
        source: str,
    ) -> None: ...


class LibraryService:
    def __init__(self, repository: PaperLibraryRepository) -> None:
        self.repository = repository

    def initialize(self) -> None:
        self.repository.initialize()

    def remember(
        self,
        papers: Sequence[RankedPaper],
        *,
        domain_key: str = DOMAIN_EMBODIED,
        topic_keys_by_arxiv_id: Mapping[str, Sequence[str]] | None = None,
        classification_source: str = "rule",
    ) -> None:
        self.repository.upsert_ranked_papers(
            papers,
            domain_key=domain_key,
            topic_keys_by_arxiv_id=topic_keys_by_arxiv_id,
            classification_source=classification_source,
        )

    def rerank_existing(self, *, limit: int = 10000) -> int:
        entries = self.repository.load_recent(limit=limit, domain_key=DOMAIN_EMBODIED)
        changed: list[RankedPaper] = []
        for entry in entries:
            reranked = rank_paper(entry.ranked.paper)
            if (
                reranked.score != entry.ranked.score
                or reranked.tags != entry.ranked.tags
                or reranked.matched_terms != entry.ranked.matched_terms
                or reranked.interest_score != entry.ranked.interest_score
                or reranked.interest_tags != entry.ranked.interest_tags
                or reranked.interest_matched_terms != entry.ranked.interest_matched_terms
            ):
                changed.append(reranked)
        self.repository.upsert_ranked_papers(changed, domain_key=DOMAIN_EMBODIED)
        return len(changed)

    def rerank_if_needed(
        self,
        *,
        algorithm_version: str = RANKING_ALGORITHM_VERSION,
        limit: int = 10000,
    ) -> int | None:
        """Re-rank the legacy embodied score only when its algorithm changes.

        ``None`` means the stored library already matches this ranking version.
        The version marker is written only after a successful rerank, so a failed
        maintenance pass is retried on the next launch.
        """

        key = "embodied_ranking_algorithm_version"
        if self.repository.get_app_meta(key) == algorithm_version:
            return None
        changed = self.rerank_existing(limit=limit)
        self.repository.set_app_meta(key, algorithm_version)
        return changed

    def recent(
        self,
        *,
        limit: int = 500,
        offset: int = 0,
        domain_key: str | None = None,
        topic_key: str | None = None,
        favorites_only: bool = False,
        sort_mode: str = "interest",
    ) -> list[LibraryPaper]:
        return self.repository.load_recent(
            limit=limit,
            offset=offset,
            domain_key=domain_key,
            topic_key=topic_key,
            favorites_only=favorites_only,
            sort_mode=sort_mode,
        )

    def since(
        self,
        updated_after: datetime,
        *,
        limit: int = 5000,
        offset: int = 0,
        domain_key: str | None = None,
        topic_key: str | None = None,
        favorites_only: bool = False,
        sort_mode: str = "interest",
    ) -> list[LibraryPaper]:
        return self.repository.load_since(
            updated_after,
            limit=limit,
            offset=offset,
            domain_key=domain_key,
            topic_key=topic_key,
            favorites_only=favorites_only,
            sort_mode=sort_mode,
        )

    def all_since(
        self,
        updated_after: datetime,
        *,
        batch_size: int = 500,
        domain_key: str | None = None,
        topic_key: str | None = None,
        favorites_only: bool = False,
        sort_mode: str = "interest",
    ) -> list[LibraryPaper]:
        """Load an export from one repository snapshot; normal browsing stays paged."""

        if batch_size < 1:
            raise ValueError("batch_size must be > 0")
        return self.repository.load_all_since(
            updated_after,
            batch_size=batch_size,
            domain_key=domain_key,
            topic_key=topic_key,
            favorites_only=favorites_only,
            sort_mode=sort_mode,
        )

    def counts_since(
        self,
        updated_after: datetime,
        *,
        domain_key: str | None = None,
        topic_key: str | None = None,
        favorites_only: bool = False,
    ) -> LibraryViewCounts:
        return self.repository.count_since(
            updated_after,
            domain_key=domain_key,
            topic_key=topic_key,
            favorites_only=favorites_only,
        )

    def taxonomies_for(
        self,
        papers: Sequence[RankedPaper],
    ) -> dict[str, PaperResearchTaxonomy]:
        return self.repository.get_taxonomies([item.paper.arxiv_id for item in papers])

    def sync_state(self, *, domain_key: str = DOMAIN_EMBODIED) -> SyncState:
        return self.repository.get_sync_state(domain_key=domain_key)

    def domain_paper_versions(self, *, domain_key: str = DOMAIN_EMBODIED) -> dict[str, str]:
        """Return stable arXiv IDs already classified into one domain.

        Sync uses this snapshot as an O(1) membership map so overlap/category
        duplicates never re-enter ranking and SQLite upsert work for the same
        domain during a normal pull.
        """

        return self.repository.get_domain_paper_versions(domain_key=domain_key)

    def begin_sync(
        self,
        *,
        domain_key: str = DOMAIN_EMBODIED,
        attempted_at: datetime | None = None,
    ) -> SyncState:
        return self.repository.begin_sync(domain_key=domain_key, attempted_at=attempted_at)

    def record_sync_checkpoint(
        self,
        *,
        from_date: date,
        until_date: date,
        domain_key: str = DOMAIN_EMBODIED,
        completed_at: datetime | None = None,
    ) -> SyncState:
        return self.repository.record_sync_checkpoint(
            from_date=from_date,
            until_date=until_date,
            domain_key=domain_key,
            completed_at=completed_at,
        )

    def finish_sync(self, *, domain_key: str = DOMAIN_EMBODIED) -> SyncState:
        return self.repository.finish_sync(domain_key=domain_key)

    def cancel_sync(
        self,
        *,
        domain_key: str = DOMAIN_EMBODIED,
        cancelled_at: datetime | None = None,
    ) -> SyncState:
        return self.repository.cancel_sync(domain_key=domain_key, cancelled_at=cancelled_at)

    def fail_sync(
        self,
        message: str,
        *,
        domain_key: str = DOMAIN_EMBODIED,
    ) -> SyncState:
        return self.repository.fail_sync(message, domain_key=domain_key)

    def record_sync_success(
        self,
        *,
        from_date: date,
        until_date: date,
        domain_key: str = DOMAIN_EMBODIED,
        completed_at: datetime | None = None,
    ) -> SyncState:
        return self.repository.record_sync_success(
            from_date=from_date,
            until_date=until_date,
            domain_key=domain_key,
            completed_at=completed_at,
        )

    def states_for(self, papers: Sequence[RankedPaper]) -> dict[str, PaperUserState]:
        return self.repository.get_states([item.paper.arxiv_id for item in papers])

    def set_favorite(self, arxiv_id: str, value: bool) -> PaperUserState:
        return self.repository.set_favorite(arxiv_id, value)

    def set_read(self, arxiv_id: str, value: bool) -> PaperUserState:
        return self.repository.set_read(arxiv_id, value)

    def save_analyses(
        self,
        *,
        model: str,
        domain_key: str = DOMAIN_EMBODIED,
        analyses: Mapping[str, PaperAIAnalysis],
    ) -> None:
        self.repository.save_analyses(
            model=model,
            domain_key=domain_key,
            analyses=analyses,
        )

    def save_analyses_with_topics(
        self,
        *,
        model: str,
        domain_key: str = DOMAIN_EMBODIED,
        analyses: Mapping[str, PaperAIAnalysis],
        classification_source: str = "deepseek-domain-v1",
    ) -> None:
        """Persist a batch and its classifications in one repository transaction."""
        self.repository.save_analyses_with_topics(
            model=model,
            domain_key=domain_key,
            analyses=analyses,
            classification_source=classification_source,
        )

    def analyses_for(
        self,
        papers: Sequence[RankedPaper],
        *,
        domain_key: str = DOMAIN_EMBODIED,
    ) -> dict[str, PaperAIAnalysis]:
        return self.repository.load_analyses(
            [item.paper.arxiv_id for item in papers],
            domain_key=domain_key,
        )

    def replace_analysis_topics(
        self,
        *,
        domain_key: str,
        topic_keys_by_arxiv_id: Mapping[str, Sequence[str]],
        classification_source: str = "deepseek-domain-v1",
    ) -> None:
        self.repository.replace_analysis_topics(
            domain_key=domain_key,
            topic_keys_by_arxiv_id=topic_keys_by_arxiv_id,
            classification_source=classification_source,
        )

    def update_author_affiliations(
        self,
        arxiv_id: str,
        affiliations: Sequence[Sequence[str]],
        *,
        source: str,
    ) -> None:
        self.repository.update_author_affiliations(
            arxiv_id,
            affiliations,
            source=source,
        )
