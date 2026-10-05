from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from threading import Event
from typing import Protocol

from ai_paper_analyzer.domain.domain_relevance import (
    classify_topic_keys,
    rank_paper_for_domain,
)
from ai_paper_analyzer.domain.library import ArxivHarvestProgress, base_arxiv_id
from ai_paper_analyzer.domain.llm import PaperAIAnalysis
from ai_paper_analyzer.domain.models import Paper, RankedPaper
from ai_paper_analyzer.domain.research_domains import DOMAIN_EMBODIED, get_research_domain


class RecentPaperSource(Protocol):
    def fetch_recent(
        self,
        *,
        categories: tuple[str, ...],
        max_results: int = 150,
    ) -> list[Paper]: ...


class DateRangePaperSource(Protocol):
    def fetch_date_range(
        self,
        *,
        from_date: date,
        until_date: date,
        categories: tuple[str, ...],
        cancel_event: Event | None = None,
        progress_callback: Callable[[ArxivHarvestProgress], None] | None = None,
    ) -> list[Paper]: ...


class PaperAnalyzer(Protocol):
    def analyze_papers(
        self,
        papers: Sequence[RankedPaper],
        *,
        domain_key: str = DOMAIN_EMBODIED,
    ) -> dict[str, PaperAIAnalysis]: ...


@dataclass(frozen=True, slots=True)
class RadarResult:
    fetched_count: int
    papers: tuple[RankedPaper, ...]
    source_note: str = ""
    topic_keys_by_arxiv_id: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


class RadarService:
    def __init__(self, source: RecentPaperSource, *, minimum_score: int = 30) -> None:
        self.source = source
        self.minimum_score = minimum_score

    def refresh(
        self,
        *,
        max_results: int = 150,
        domain_key: str = DOMAIN_EMBODIED,
    ) -> RadarResult:
        domain = get_research_domain(domain_key)
        papers = self.source.fetch_recent(
            categories=domain.sync_categories,
            max_results=max_results,
        )
        return self.rank_papers(papers, domain_key=domain.key)

    def sync_range(
        self,
        *,
        from_date: date,
        until_date: date,
        domain_key: str = DOMAIN_EMBODIED,
        cancel_event: Event | None = None,
        progress_callback: Callable[[ArxivHarvestProgress], None] | None = None,
        skip_existing_ids: Collection[str] | None = None,
    ) -> RadarResult:
        fetch_range = getattr(self.source, "fetch_date_range", None)
        if fetch_range is None:
            raise TypeError("Paper source does not support date-range synchronization")
        domain = get_research_domain(domain_key)
        kwargs = {
            "from_date": from_date,
            "until_date": until_date,
            "categories": domain.sync_categories,
        }
        if cancel_event is not None:
            kwargs["cancel_event"] = cancel_event
        if progress_callback is not None:
            kwargs["progress_callback"] = progress_callback
        papers = fetch_range(**kwargs)
        # The streaming consumer reserves IDs as it ranks page payloads. Read
        # this collection AFTER harvesting to avoid ranking those papers twice.
        candidates = (
            [paper for paper in papers if base_arxiv_id(paper.arxiv_id) not in skip_existing_ids]
            if skip_existing_ids is not None
            else papers
        )
        ranked = self.rank_papers(candidates, domain_key=domain.key)
        return RadarResult(
            fetched_count=len(papers),
            papers=ranked.papers,
            source_note=ranked.source_note,
            topic_keys_by_arxiv_id=ranked.topic_keys_by_arxiv_id,
        )

    async def refresh_async(
        self,
        *,
        max_results: int = 150,
        domain_key: str = DOMAIN_EMBODIED,
    ) -> RadarResult:
        return await asyncio.to_thread(
            self.refresh,
            max_results=max_results,
            domain_key=domain_key,
        )

    async def sync_range_async(
        self,
        *,
        from_date: date,
        until_date: date,
        domain_key: str = DOMAIN_EMBODIED,
        cancel_event: Event | None = None,
        progress_callback: Callable[[ArxivHarvestProgress], None] | None = None,
        skip_existing_ids: Collection[str] | None = None,
    ) -> RadarResult:
        return await asyncio.to_thread(
            self.sync_range,
            from_date=from_date,
            until_date=until_date,
            domain_key=domain_key,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
            skip_existing_ids=skip_existing_ids,
        )

    async def analyze_async(
        self,
        analyzer: PaperAnalyzer,
        papers: Sequence[RankedPaper],
        *,
        domain_key: str = DOMAIN_EMBODIED,
    ) -> dict[str, PaperAIAnalysis]:
        return await asyncio.to_thread(
            analyzer.analyze_papers,
            papers,
            domain_key=domain_key,
        )

    def _rank_result(
        self,
        papers: Sequence[Paper],
        *,
        domain_key: str,
    ) -> RadarResult:
        """Backward-compatible alias for older tests/callers."""
        return self.rank_papers(papers, domain_key=domain_key)

    def rank_papers(
        self,
        papers: Sequence[Paper],
        *,
        domain_key: str,
    ) -> RadarResult:
        ranked = [rank_paper_for_domain(paper, domain_key) for paper in papers]
        relevant = [paper for paper in ranked if paper.score >= self.minimum_score]
        relevant.sort(
            key=lambda item: (item.interest_score, item.score, item.paper.updated_at),
            reverse=True,
        )
        topics = {
            item.paper.arxiv_id: classify_topic_keys(item.paper, domain_key)
            for item in relevant
        }
        return RadarResult(
            fetched_count=len(papers),
            papers=tuple(relevant),
            source_note=getattr(self.source, "last_fetch_note", ""),
            topic_keys_by_arxiv_id=topics,
        )
