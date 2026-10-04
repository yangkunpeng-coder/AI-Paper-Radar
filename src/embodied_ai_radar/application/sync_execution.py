"""Reusable sync execution loop for mobile presentations.

Ordinary synchronization is latest-only and independent from the active browse
window. Explicit historical backfill is a separate request. The application
layer therefore owns both planning modes while the presentation only chooses
which user action was requested.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Event
from typing import Callable

from embodied_ai_radar.application.library_service import LibraryService
from embodied_ai_radar.application.radar_service import RadarService
from embodied_ai_radar.application.sync_service import SyncCoordinator
from embodied_ai_radar.domain.library import ArxivHarvestProgress, base_arxiv_id
from embodied_ai_radar.domain.models import RankedPaper


@dataclass(frozen=True, slots=True)
class SyncExecutionProgress:
    phase: str
    completed_chunks: int
    total_chunks: int
    category: str = ""
    category_index: int = 0
    category_count: int = 0
    completed_categories: int = 0
    page_number: int = 0
    fetched_count: int = 0
    saved_count: int = 0

    @property
    def fraction(self) -> float | None:
        if self.total_chunks <= 0:
            return None
        if self.phase in {"harvesting", "partial_saved"} and self.category_count > 0:
            category_fraction = self.completed_categories / self.category_count
            return min(
                1.0,
                (self.completed_chunks + category_fraction) / self.total_chunks,
            )
        return min(1.0, self.completed_chunks / self.total_chunks)


@dataclass(frozen=True, slots=True)
class SyncExecutionResult:
    completed: bool
    cancelled: bool
    fetched_count: int
    saved_count: int
    candidates: tuple[RankedPaper, ...]
    completed_chunks: int
    total_chunks: int


class SyncExecutionService:
    """Run one domain sync using the existing application services."""

    def __init__(
        self,
        radar: RadarService,
        library: LibraryService,
        coordinator: SyncCoordinator,
    ) -> None:
        self.radar = radar
        self.library = library
        self.coordinator = coordinator

    async def run(
        self,
        *,
        domain_key: str,
        cancel_event: Event,
        backfill_days: int | None = None,
        progress_callback: Callable[[SyncExecutionProgress], None] | None = None,
        write_lock: asyncio.Lock | None = None,
    ) -> SyncExecutionResult:
        today = datetime.now(UTC).date()
        if backfill_days is None:
            plan = await asyncio.to_thread(
                self.coordinator.plan_incremental,
                domain_key=domain_key,
                today=today,
            )
        else:
            if backfill_days < 1:
                raise ValueError("backfill_days must be positive")
            desired_start = (datetime.now(UTC) - timedelta(days=backfill_days)).date()
            plan = await asyncio.to_thread(
                self.coordinator.plan_backfill,
                domain_key=domain_key,
                desired_start=desired_start,
                today=today,
            )
        total_chunks = max(1, len(plan.intervals))
        completed_chunks = 0
        total_fetched = 0
        candidate_by_id: dict[str, RankedPaper] = {}
        saved_candidate_ids: set[str] = set()
        visible_saved_count = 0
        # Snapshot the current domain membership once. OAI overlap and category
        # fan-out can surface the same stable arXiv ID many times; a dict gives
        # O(1) membership checks and prevents redundant rank/upsert work.
        existing_by_id = await asyncio.to_thread(
            self.library.domain_paper_versions,
            domain_key=domain_key,
        )
        scheduled_new_ids: set[str] = set()

        await self._write(
            write_lock,
            self.coordinator.begin,
            domain_key=domain_key,
        )
        self._publish(
            progress_callback,
            SyncExecutionProgress("preparing", 0, total_chunks),
        )

        try:
            for interval in plan.intervals:
                if cancel_event.is_set():
                    break

                loop = asyncio.get_running_loop()
                partial_writes: list = []

                async def persist_partial_page(
                    ranked_result,
                    event: ArxivHarvestProgress,
                ) -> None:
                    nonlocal visible_saved_count
                    if not ranked_result.papers:
                        return
                    await self._write(
                        write_lock,
                        self.library.remember,
                        ranked_result.papers,
                        domain_key=domain_key,
                        topic_keys_by_arxiv_id=ranked_result.topic_keys_by_arxiv_id,
                        classification_source="domain-rule-v1",
                    )
                    for item in ranked_result.papers:
                        stable_id = base_arxiv_id(item.paper.arxiv_id)
                        existing_by_id[stable_id] = item.paper.arxiv_id
                        previous = candidate_by_id.get(stable_id)
                        if (
                            previous is None
                            or item.paper.updated_at > previous.paper.updated_at
                        ):
                            candidate_by_id[stable_id] = item
                        saved_candidate_ids.add(stable_id)

                    # OAI publishes raw metadata in batches, but the product
                    # promise is to reveal *saved relevant papers* in roughly
                    # 20-paper steps. Do not rebuild the Tablet list for every
                    # raw page batch or expose the much larger scan count.
                    saved_count = len(saved_candidate_ids)
                    if saved_count // 20 <= visible_saved_count // 20:
                        return
                    visible_saved_count = saved_count
                    self._publish(
                        progress_callback,
                        SyncExecutionProgress(
                            phase="partial_saved",
                            completed_chunks=completed_chunks,
                            total_chunks=total_chunks,
                            category=event.category,
                            category_index=event.category_index,
                            category_count=event.category_count,
                            completed_categories=event.completed_categories,
                            page_number=event.page_number,
                            fetched_count=total_fetched + event.fetched_count,
                            saved_count=saved_count,
                        ),
                    )

                def live_progress(event: ArxivHarvestProgress) -> None:
                    progress = SyncExecutionProgress(
                        phase="harvesting",
                        completed_chunks=completed_chunks,
                        total_chunks=total_chunks,
                        category=event.category,
                        category_index=event.category_index,
                        category_count=event.category_count,
                        completed_categories=event.completed_categories,
                        page_number=event.page_number,
                        fetched_count=total_fetched + event.fetched_count,
                        saved_count=len(saved_candidate_ids),
                    )
                    loop.call_soon_threadsafe(self._publish, progress_callback, progress)
                    if not event.papers:
                        return
                    # OAI-PMH pages can repeat the same paper because of overlap
                    # and multi-category harvesting. Do not re-rank or rewrite an
                    # ID already present in this domain, and reserve new IDs as
                    # soon as they are scheduled so another category page cannot
                    # race the pending persistence coroutine.
                    fresh_papers = []
                    for paper in event.papers:
                        stable_id = base_arxiv_id(paper.arxiv_id)
                        if stable_id in existing_by_id or stable_id in scheduled_new_ids:
                            continue
                        scheduled_new_ids.add(stable_id)
                        fresh_papers.append(paper)
                    if not fresh_papers:
                        return
                    # Rank only genuinely new metadata, then persist back on the
                    # application loop so Tablet UI remains responsive.
                    ranked_result = self.radar.rank_papers(
                        tuple(fresh_papers),
                        domain_key=domain_key,
                    )
                    if not ranked_result.papers:
                        return
                    partial_writes.append(
                        asyncio.run_coroutine_threadsafe(
                            persist_partial_page(ranked_result, event),
                            loop,
                        )
                    )

                try:
                    result = await self.radar.sync_range_async(
                        from_date=interval.from_date,
                        until_date=interval.until_date,
                        domain_key=domain_key,
                        cancel_event=cancel_event,
                        progress_callback=live_progress,
                    )
                except Exception:
                    if partial_writes:
                        await asyncio.gather(
                            *(asyncio.wrap_future(item) for item in partial_writes)
                        )
                    if cancel_event.is_set():
                        break
                    raise

                if partial_writes:
                    await asyncio.gather(
                        *(asyncio.wrap_future(item) for item in partial_writes)
                    )

                total_fetched += result.fetched_count
                new_result_papers = tuple(
                    item
                    for item in result.papers
                    if base_arxiv_id(item.paper.arxiv_id) not in existing_by_id
                )
                for item in new_result_papers:
                    stable_id = base_arxiv_id(item.paper.arxiv_id)
                    previous = candidate_by_id.get(stable_id)
                    if previous is None or item.paper.updated_at > previous.paper.updated_at:
                        candidate_by_id[stable_id] = item

                # Sources without page payloads still use the end-of-interval
                # persistence path. For OAI, persist only genuinely new relevant
                # items not already written by page callbacks. Existing domain IDs
                # are intentionally left untouched during ordinary pulls.
                missing_final = tuple(
                    item
                    for item in new_result_papers
                    if base_arxiv_id(item.paper.arxiv_id) not in saved_candidate_ids
                )
                if not partial_writes:
                    to_remember = new_result_papers
                else:
                    to_remember = missing_final
                if to_remember:
                    topic_keys = {
                        item.paper.arxiv_id: result.topic_keys_by_arxiv_id.get(
                            item.paper.arxiv_id, ()
                        )
                        for item in to_remember
                    }
                    await self._write(
                        write_lock,
                        self.library.remember,
                        to_remember,
                        domain_key=domain_key,
                        topic_keys_by_arxiv_id=topic_keys,
                        classification_source="domain-rule-v1",
                    )
                    for item in to_remember:
                        stable_id = base_arxiv_id(item.paper.arxiv_id)
                        saved_candidate_ids.add(stable_id)
                        existing_by_id[stable_id] = item.paper.arxiv_id
                await self._write(
                    write_lock,
                    self.coordinator.checkpoint,
                    domain_key=domain_key,
                    from_date=interval.from_date,
                    until_date=interval.until_date,
                )
                completed_chunks += 1
                self._publish(
                    progress_callback,
                    SyncExecutionProgress(
                        "saved",
                        completed_chunks,
                        total_chunks,
                        fetched_count=total_fetched,
                        saved_count=len(saved_candidate_ids),
                    ),
                )

            if cancel_event.is_set():
                await self._write(
                    write_lock,
                    self.coordinator.cancel,
                    domain_key=domain_key,
                )
                return SyncExecutionResult(
                    completed=False,
                    cancelled=True,
                    fetched_count=total_fetched,
                    saved_count=len(saved_candidate_ids),
                    candidates=self._ordered_candidates(candidate_by_id),
                    completed_chunks=completed_chunks,
                    total_chunks=total_chunks,
                )

            await self._write(
                write_lock,
                self.coordinator.finish,
                domain_key=domain_key,
            )
            return SyncExecutionResult(
                completed=True,
                cancelled=False,
                fetched_count=total_fetched,
                saved_count=len(saved_candidate_ids),
                candidates=self._ordered_candidates(candidate_by_id),
                completed_chunks=completed_chunks,
                total_chunks=total_chunks,
            )
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                cancel_event.set()
                await self._write(
                    write_lock,
                    self.coordinator.cancel,
                    domain_key=domain_key,
                )
                raise
            await self._write(
                write_lock,
                self.coordinator.fail,
                f"{type(exc).__name__}: {exc}",
                domain_key=domain_key,
            )
            raise

    @staticmethod
    def _publish(
        callback: Callable[[SyncExecutionProgress], None] | None,
        progress: SyncExecutionProgress,
    ) -> None:
        if callback is not None:
            callback(progress)

    @staticmethod
    async def _write(lock: asyncio.Lock | None, func, *args, **kwargs):
        if lock is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        async with lock:
            return await asyncio.to_thread(func, *args, **kwargs)

    @staticmethod
    def _ordered_candidates(items: dict[str, RankedPaper]) -> tuple[RankedPaper, ...]:
        return tuple(
            sorted(
                items.values(),
                key=lambda item: (
                    item.interest_score,
                    item.score,
                    item.paper.updated_at,
                ),
                reverse=True,
            )
        )
