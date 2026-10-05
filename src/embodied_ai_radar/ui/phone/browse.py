"""Phone browsing state, independent of Flet widgets."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from embodied_ai_radar.application.browse_controller import BrowseController, BrowseQuery
from embodied_ai_radar.application.settings_service import ViewPreferences
from embodied_ai_radar.domain.library import LibraryPaper, LibraryViewCounts

PAGE_SIZE = 24


@dataclass
class PhoneBrowseState:
    preferences: ViewPreferences
    entries: list[LibraryPaper] = field(default_factory=list)
    counts: LibraryViewCounts = field(default_factory=LibraryViewCounts)
    scroll: float = 0
    selected: LibraryPaper | None = None
    revision: int = 0
    loading: bool = False
    fresh_available: bool = False
    load_error: bool = False

    def query(self) -> BrowseQuery:
        prefs = self.preferences
        start = (datetime.now(UTC) - timedelta(days=prefs.days)).date()
        return BrowseQuery(
            updated_after=datetime.combine(start, datetime.min.time(), tzinfo=UTC),
            domain_key=prefs.active_domain,
            topic_key=prefs.focus_tag,
            favorites_only=prefs.favorites_only,
            sort_mode=prefs.sort_mode,
        )

    def configure(self, preferences: ViewPreferences) -> None:
        self.preferences = preferences
        self.revision += 1
        self.entries = []
        self.scroll = 0
        self.loading = False
        self.fresh_available = False
        self.load_error = False

    async def reload(self, browser: BrowseController) -> bool:
        self.revision += 1
        revision, query = self.revision, self.query()
        self.loading = True
        try:
            result = await asyncio.to_thread(browser.load_snapshot, query, limit=PAGE_SIZE)
            if revision != self.revision:
                return False
            self.entries = list(result.entries)
            self.counts = result.counts
            self.scroll = 0
            self.fresh_available = False
            self.load_error = False
            return True
        finally:
            if revision == self.revision:
                self.loading = False

    async def more(self, browser: BrowseController) -> bool:
        if self.loading or len(self.entries) >= self.counts.filtered:
            return False
        self.loading = True
        revision, offset, query = self.revision, len(self.entries), self.query()
        try:
            entries = await asyncio.to_thread(
                browser.load_page,
                query,
                limit=PAGE_SIZE,
                offset=offset,
            )
            if revision != self.revision or offset != len(self.entries):
                return False
            existing = {e.ranked.paper.arxiv_id for e in self.entries}
            self.entries.extend(e for e in entries if e.ranked.paper.arxiv_id not in existing)
            return bool(entries)
        finally:
            if revision == self.revision:
                self.loading = False

    def patch_favorite(self, arxiv_id, saved) -> None:
        def patch(entry):
            return replace(entry, state=saved) if entry.ranked.paper.arxiv_id == arxiv_id else entry

        self.entries = [
            patch(e)
            for e in self.entries
            if not (
                e.ranked.paper.arxiv_id == arxiv_id
                and self.preferences.favorites_only
                and not saved.is_favorite
            )
        ]
        if self.selected is not None:
            self.selected = patch(self.selected)
