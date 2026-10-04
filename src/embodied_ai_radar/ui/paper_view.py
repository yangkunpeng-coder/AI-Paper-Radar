from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from embodied_ai_radar.domain.library import PaperUserState
from embodied_ai_radar.domain.llm import PaperAIAnalysis
from embodied_ai_radar.domain.models import RankedPaper

SORT_INTEREST = "interest"
SORT_RELEVANCE = "relevance"
SORT_LATEST = "latest"


@dataclass(slots=True)
class DomainBrowseState:
    """Runtime browsing position for one research domain.

    This is deliberately separate from filter preferences. A domain can keep its
    own materialized page count, scroll offset, and selected paper while another
    domain is being viewed.
    """

    loaded_count: int = 0
    scroll_offset: float = 0.0
    selected_arxiv_id: str | None = None
    viewport_anchor_id: str | None = None
    viewport_anchor_offset: float = 0.0
    viewport_locked: bool = False

    def load_limit(self, page_size: int) -> int:
        if page_size <= 0:
            raise ValueError("page_size must be > 0")
        return max(page_size, self.loaded_count)

    def remember(
        self,
        *,
        loaded_count: int,
        scroll_offset: float | None = None,
        selected_arxiv_id: str | None = None,
    ) -> None:
        self.loaded_count = max(0, int(loaded_count))
        if scroll_offset is not None:
            self.scroll_offset = max(0.0, float(scroll_offset))
        self.selected_arxiv_id = selected_arxiv_id

    def reset(self) -> None:
        self.loaded_count = 0
        self.scroll_offset = 0.0
        self.selected_arxiv_id = None
        self.viewport_anchor_id = None
        self.viewport_anchor_offset = 0.0
        self.viewport_locked = False


@dataclass(frozen=True, slots=True)
class PaperViewStats:
    total: int
    filtered: int
    favorites: int
    analyzed: int


def _state_for(
    item: RankedPaper,
    states: Mapping[str, PaperUserState],
) -> PaperUserState:
    return states.get(
        item.paper.arxiv_id,
        PaperUserState(arxiv_id=item.paper.arxiv_id),
    )


def _matches_focus(item: RankedPaper, focus_tag: str) -> bool:
    normalized = focus_tag.strip().casefold()
    if not normalized or normalized == "all":
        return True
    tags = (*item.tags, *item.interest_tags)
    return any(normalized == tag.casefold() for tag in tags)


def _sort_key(item: RankedPaper, sort_mode: str) -> tuple[object, ...]:
    updated: datetime = item.paper.updated_at
    if sort_mode == SORT_RELEVANCE:
        return (item.score, item.interest_score, updated)
    if sort_mode == SORT_LATEST:
        return (updated, item.interest_score, item.score)
    return (item.interest_score, item.score, updated)


def filter_and_sort_papers(
    items: tuple[RankedPaper, ...] | list[RankedPaper],
    *,
    states: Mapping[str, PaperUserState],
    favorites_only: bool = False,
    focus_tag: str = "all",
    sort_mode: str = SORT_INTEREST,
) -> list[RankedPaper]:
    filtered: list[RankedPaper] = []
    for item in items:
        state = _state_for(item, states)
        if favorites_only and not state.is_favorite:
            continue
        if not _matches_focus(item, focus_tag):
            continue
        filtered.append(item)

    filtered.sort(key=lambda item: _sort_key(item, sort_mode), reverse=True)
    return filtered


def view_stats(
    all_items: tuple[RankedPaper, ...] | list[RankedPaper],
    filtered_items: tuple[RankedPaper, ...] | list[RankedPaper],
    *,
    states: Mapping[str, PaperUserState],
    analyses: Mapping[str, PaperAIAnalysis],
) -> PaperViewStats:
    favorites = 0
    for item in all_items:
        state = _state_for(item, states)
        favorites += int(state.is_favorite)
    analyzed = sum(1 for item in all_items if item.paper.arxiv_id in analyses)
    return PaperViewStats(
        total=len(all_items),
        filtered=len(filtered_items),
        favorites=favorites,
        analyzed=analyzed,
    )
