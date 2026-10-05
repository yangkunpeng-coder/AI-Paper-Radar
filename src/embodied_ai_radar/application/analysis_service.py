from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence

from embodied_ai_radar.application.library_service import LibraryService
from embodied_ai_radar.application.radar_service import PaperAnalyzer, RadarService
from embodied_ai_radar.domain.llm import PaperAIAnalysis
from embodied_ai_radar.domain.models import RankedPaper
from embodied_ai_radar.domain.research_domains import DOMAIN_EMBODIED


class AnalysisService:
    """Coordinate domain-aware analysis and persistence outside the Flet page."""

    def __init__(self, radar: RadarService, library: LibraryService) -> None:
        self.radar = radar
        self.library = library

    async def analyze_and_persist(
        self,
        analyzer: PaperAnalyzer,
        papers: Sequence[RankedPaper],
        *,
        model: str,
        domain_key: str = DOMAIN_EMBODIED,
        batch_size: int = 8,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> Mapping[str, PaperAIAnalysis]:
        """Analyze in bounded batches and persist every completed batch immediately.

        A later API/validation failure therefore does not discard already completed
        work.  The exception still propagates so the UI can report that the overall
        request was only partially completed.
        """

        if batch_size < 1:
            raise ValueError("batch_size must be > 0")
        total = len(papers)
        merged: dict[str, PaperAIAnalysis] = {}
        for start in range(0, total, batch_size):
            batch = papers[start : start + batch_size]
            analyses = await self.radar.analyze_async(
                analyzer,
                batch,
                domain_key=domain_key,
            )
            await asyncio.to_thread(
                self.library.save_analyses_with_topics,
                model=model,
                domain_key=domain_key,
                analyses=analyses,
                classification_source="deepseek-domain-v1",
            )
            merged.update(analyses)
            if progress_callback is not None:
                progress_callback(min(start + len(batch), total), total)
        return merged
