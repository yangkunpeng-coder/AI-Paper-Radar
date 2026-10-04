from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from embodied_ai_radar.domain.llm import PaperAIAnalysis
from embodied_ai_radar.domain.models import Paper, RankedPaper

_VERSION_SUFFIX = re.compile(r"v\d+$", re.IGNORECASE)


def base_arxiv_id(arxiv_id: str) -> str:
    """Return a stable arXiv id without the version suffix.

    User state such as favorite/read should survive v1 -> v2 updates, while the
    repository still keeps the latest full version id for freshness checks.
    """

    normalized = arxiv_id.strip()
    if not normalized:
        raise ValueError("arxiv_id is required")
    return _VERSION_SUFFIX.sub("", normalized)


@dataclass(frozen=True, slots=True)
class PaperUserState:
    arxiv_id: str
    is_favorite: bool = False
    is_read: bool = False


@dataclass(frozen=True, slots=True)
class LibraryPaper:
    ranked: RankedPaper
    state: PaperUserState
    first_seen_at: datetime
    last_seen_at: datetime
    analysis: PaperAIAnalysis | None = None


@dataclass(frozen=True, slots=True)
class LibraryViewCounts:
    """Cheap aggregate counts for a paged library view.

    ``total`` / ``favorites`` / ``analyzed`` describe the active domain and time
    range. ``filtered`` additionally applies the current favorite/topic filters.
    The UI can therefore show accurate metrics without materializing thousands
    of paper rows just to count them.
    """

    total: int = 0
    filtered: int = 0
    favorites: int = 0
    analyzed: int = 0


@dataclass(frozen=True, slots=True)
class ArxivHarvestProgress:
    """Observable progress for a date-range arXiv harvest.

    OAI-PMH does not expose a reliable total page count up front, so UI progress
    is reported honestly by completed category stages while page_number and
    fetched_count show live activity inside the current category.
    """

    category: str
    category_index: int
    category_count: int
    page_number: int
    fetched_count: int
    completed_categories: int
    phase: str
    papers: tuple[Paper, ...] = ()


@dataclass(frozen=True, slots=True)
class SyncState:
    """Durable coverage and lifecycle state for arXiv synchronization."""

    last_successful_sync: datetime | None = None
    earliest_covered_date: date | None = None
    latest_covered_date: date | None = None
    status: str = "idle"
    last_attempt_at: datetime | None = None
    last_cancelled_at: datetime | None = None
    last_error: str = ""
