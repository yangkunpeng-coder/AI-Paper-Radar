"""Android Tablet presentation.

Material 3 adaptive principles:
- 600-839dp: single-pane list/detail navigation.
- >=840dp: list-detail master/detail layout.
- primary navigation lives in a drawer; filters use an end drawer or bottom sheet.
- all core data/sync/analysis/export behavior comes from application services.
"""

from __future__ import annotations

import asyncio
from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import dataclass, replace
import logging
from datetime import UTC, datetime, timedelta
import time
from threading import Event

import flet as ft

from embodied_ai_radar.application.export_service import (
    ExportContext,
    LiteratureExportItem,
    build_excel_export,
    build_markdown_export,
)
from embodied_ai_radar.application.sync_execution import (
    SyncExecutionProgress,
    SyncExecutionService,
)
from embodied_ai_radar.application.sync_service import DailySyncGate
from embodied_ai_radar.domain.library import LibraryPaper, LibraryViewCounts, base_arxiv_id
from embodied_ai_radar.domain.llm import PaperAIAnalysis, SUPPORTED_DEEPSEEK_MODELS
from embodied_ai_radar.domain.research_domains import (
    RESEARCH_DOMAINS,
    TOPIC_ALL,
    get_research_domain,
    normalize_topic_key,
    research_domain_keys,
)
from embodied_ai_radar.ui.mobile_runtime import create_mobile_services
from embodied_ai_radar.ui.paper_view import SORT_INTEREST, SORT_LATEST, SORT_RELEVANCE
from embodied_ai_radar.ui.tablet.components import (
    TABLET_APP_BAR_HEIGHT,
    TABLET_PAPER_CARD_HEIGHT,
    stat_metric,
    tablet_paper_card,
    tablet_paper_detail,
    touch_icon_button,
    touch_text_button,
)
from embodied_ai_radar.ui.tablet.layout import (
    TabletLayoutMode,
    tablet_layout_mode,
)
from embodied_ai_radar.ui.tablet.pdf_reader import (
    PdfDownloadCancelled,
    PdfDownloadProgress,
    PdfReaderError,
    TemporaryPdfDocument,
    download_temporary_pdf,
    pdf_render_target_width,
    render_pdf_page,
)
from embodied_ai_radar.ui.tablet.reader import ReaderTarget, build_reader_target
from embodied_ai_radar.ui.tablet.state import TabletState
from embodied_ai_radar.ui.theme import (
    APP_BG,
    DIVIDER,
    PRIMARY_BORDER,
    PRIMARY_DARK,
    PRIMARY_SOFT,
    SURFACE,
    SURFACE_SUBTLE,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    TEXT_TERTIARY,
)

LOGGER = logging.getLogger(__name__)

PRODUCT_NAME_ZH = "论文自动检索分析"
PAGE_SIZE = 24
LIST_SPACING = 10
LIST_ITEM_EXTENT = TABLET_PAPER_CARD_HEIGHT + LIST_SPACING
LIST_CACHE_EXTENT = LIST_ITEM_EXTENT * 2
# Keep a bounded working set when the user returns near the top of a deep list.
# Flet still lazily builds ListView children, but Python-side Control objects are
# real objects; compacting an idle near-top list avoids retaining thousands of
# rich Card subtrees indefinitely.
LIST_COMPACT_KEEP = PAGE_SIZE * 6
LIST_COMPACT_THRESHOLD = PAGE_SIZE * 12
LIST_COMPACT_MAX_OFFSET = LIST_ITEM_EXTENT * 6
LIST_COMPACT_IDLE_SECONDS = 0.65
SYNC_HEAD_REFRESH_LIMIT = PAGE_SIZE * 3
SYNC_LIVE_MUTATION_MAX_OFFSET = LIST_ITEM_EXTENT * 6
PDF_RENDER_RADIUS = 1
TASK_SHEET_REFRESH_INTERVAL = 0.25
SORT_LABELS = {
    SORT_INTEREST: "方向相关度优先",
    SORT_RELEVANCE: "领域相关度优先",
    SORT_LATEST: "最新更新优先",
}
RANGE_LABELS = {
    7: "最近 7 天",
    30: "最近 1 个月",
    90: "最近 3 个月",
    180: "最近 6 个月",
    365: "最近 1 年",
}


@dataclass(slots=True)
class _TaskStatus:
    key: str
    title: str
    detail: str = ""
    progress: float | None = None
    pause_callback: object | None = None
    resume_callback: object | None = None
    close_callback: object | None = None
    pausing: bool = False
    paused: bool = False


def _cutoff(days: int) -> datetime:
    start = (datetime.now(UTC) - timedelta(days=days)).date()
    return datetime(start.year, start.month, start.day, tzinfo=UTC)


def _export_range_stamp(days: int) -> str:
    return {7: "7d", 30: "1m", 90: "3m", 180: "6m", 365: "1y"}[days]


async def main(page: ft.Page) -> None:
    page.title = PRODUCT_NAME_ZH
    page.padding = 0
    page.bgcolor = APP_BG
    page.theme_mode = ft.ThemeMode.LIGHT
    page.horizontal_alignment = ft.CrossAxisAlignment.STRETCH

    boot_message = ft.Text("正在准备本地论文库……", size=14, color=TEXT_SECONDARY)
    boot = ft.SafeArea(
        expand=True,
        content=ft.Column(
            expand=True,
            spacing=0,
            controls=[
                ft.Container(
                    height=TABLET_APP_BAR_HEIGHT,
                    bgcolor=SURFACE,
                    border=ft.Border.only(bottom=ft.BorderSide(1, DIVIDER)),
                    padding=ft.Padding.symmetric(horizontal=18, vertical=0),
                    content=ft.Row(
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Image(src="icon.png", width=32, height=32),
                            ft.Text(
                                PRODUCT_NAME_ZH,
                                size=18,
                                weight=ft.FontWeight.W_600,
                                color=TEXT_PRIMARY,
                            ),
                            ft.Container(expand=True),
                            ft.ProgressRing(width=18, height=18, stroke_width=2),
                            boot_message,
                        ],
                    ),
                ),
                ft.Container(expand=True, bgcolor=APP_BG),
            ],
        ),
    )
    page.add(boot)
    await asyncio.sleep(0)

    services = await create_mobile_services(page)
    boot_message.value = "正在打开本地论文库……"
    page.update()
    await asyncio.to_thread(services.library_service.initialize)

    current_settings = await services.settings_service.load()
    view_preferences = await services.settings_service.load_view_preferences()
    auto_sync_domain_keys = await services.settings_service.load_auto_sync_domain_keys(
        fallback_domain=view_preferences.active_domain
    )

    state = TabletState(active_domain=view_preferences.active_domain)
    active_domain = view_preferences.active_domain
    selected_days = view_preferences.days
    selected_topic_key = normalize_topic_key(active_domain, view_preferences.focus_tag)
    favorites_only = view_preferences.favorites_only
    sort_mode = view_preferences.sort_mode
    current_entries: list[LibraryPaper] = []
    current_entry_by_id: dict[str, LibraryPaper] = {}
    current_entry_index: dict[str, int] = {}
    current_entry_id_by_stable: dict[str, str] = {}
    current_counts = LibraryViewCounts()
    loading_more = False
    restoring_scroll = False
    papers_scroll: ft.ListView | None = None
    paper_card_controls: dict[str, ft.Container] = {}
    paper_card_entry_cache: dict[str, LibraryPaper] = {}
    paper_control_order: list[str] = []
    library_view_lock = asyncio.Lock()
    sync_head_stale: set[str] = set()
    selected_entry_cache: dict[str, LibraryPaper] = {}
    detail_host: ft.Container | None = None
    list_header_host: ft.Container | None = None
    paper_list_host: ft.Container | None = None
    tasks: dict[str, _TaskStatus] = {}
    active_task_sheet: ft.BottomSheet | None = None
    task_sheet_refresh_task: asyncio.Task[None] | None = None
    last_task_sheet_refresh_at = 0.0
    sync_cancel_events: dict[str, Event] = {}
    sync_pause_requested: set[str] = set()
    sync_close_requested: set[str] = set()
    sync_paused_scopes: dict[str, int | None] = {}
    sync_view_refresh_tasks: dict[str, asyncio.Task[None]] = {}
    sync_view_refresh_pending: set[str] = set()
    last_sync_view_refresh_at: dict[str, float] = {}
    sync_status_by_domain: dict[str, str] = {}
    list_sync_status_text: ft.Text | None = None
    last_list_scroll_at = 0.0
    list_compaction_task: asyncio.Task[None] | None = None
    home_view_layout_mode: TabletLayoutMode | None = None
    reader_target: ReaderTarget | None = None
    reader_return_route = "/"
    reader_pdf_document: TemporaryPdfDocument | None = None
    reader_pdf_generation = 0
    reader_pdf_error = ""
    reader_pdf_download_task: asyncio.Task[None] | None = None
    reader_pdf_download_cancel: Event | None = None
    reader_pdf_download_progress = PdfDownloadProgress("idle")
    reader_pdf_status_text: ft.Text | None = None
    reader_pdf_progress_bar: ft.ProgressBar | None = None
    reader_pdf_page_hosts: dict[int, ft.Container] = {}
    reader_pdf_page_offsets: list[tuple[float, float]] = []
    reader_pdf_page_starts: list[float] = []
    reader_pdf_rendered_pages: set[int] = set()
    reader_pdf_rendering_pages: set[int] = set()
    reader_pdf_page_pngs: dict[int, bytes] = {}
    reader_pdf_focus_index = 0
    reader_pdf_scroll_direction = 1
    reader_pdf_requested_focus = 0
    reader_pdf_render_worker: asyncio.Task[None] | None = None
    reader_pdf_render_width = 2200
    reader_pdf_list: ft.ListView | None = None
    reader_pdf_scroll_pixels = 0.0
    sync_write_lock = asyncio.Lock()
    sync_execution = SyncExecutionService(
        services.radar_service,
        services.library_service,
        services.sync_coordinator,
    )
    launcher = ft.UrlLauncher()

    def layout_mode() -> TabletLayoutMode:
        return tablet_layout_mode(page.width)

    def make_query():
        from embodied_ai_radar.application.browse_controller import BrowseQuery

        return BrowseQuery(
            updated_after=_cutoff(selected_days),
            domain_key=active_domain,
            topic_key=selected_topic_key,
            favorites_only=favorites_only,
            sort_mode=sort_mode,
        )

    async def show_message(message: str, *, failed: bool = False, seconds: float = 4) -> None:
        """Show a lightweight system-style status instead of a large danger banner."""

        accent = "#B54708" if failed else PRIMARY_DARK
        icon = ft.Icons.ERROR_OUTLINE if failed else ft.Icons.INFO_OUTLINE
        page.show_dialog(
            ft.SnackBar(
                bgcolor=SURFACE,
                duration=ft.Duration(seconds=seconds),
                content=ft.Container(
                    padding=ft.Padding.symmetric(horizontal=4, vertical=2),
                    content=ft.Row(
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Icon(icon, size=18, color=accent),
                            ft.Text(
                                message,
                                expand=True,
                                size=13,
                                color=TEXT_PRIMARY,
                            ),
                        ],
                    ),
                ),
            )
        )

    def rebuild_current_entry_indexes() -> None:
        current_entry_by_id.clear()
        current_entry_index.clear()
        current_entry_id_by_stable.clear()
        for index, entry in enumerate(current_entries):
            arxiv_id = entry.ranked.paper.arxiv_id
            current_entry_by_id[arxiv_id] = entry
            current_entry_index[arxiv_id] = index
            current_entry_id_by_stable[base_arxiv_id(arxiv_id)] = arxiv_id

    def append_current_entry_indexes(entries: list[LibraryPaper], *, start: int) -> None:
        for index, entry in enumerate(entries, start=start):
            arxiv_id = entry.ranked.paper.arxiv_id
            current_entry_by_id[arxiv_id] = entry
            current_entry_index[arxiv_id] = index
            current_entry_id_by_stable[base_arxiv_id(arxiv_id)] = arxiv_id

    def reindex_current_entries_from(start: int) -> None:
        for index in range(max(0, start), len(current_entries)):
            arxiv_id = current_entries[index].ranked.paper.arxiv_id
            current_entry_index[arxiv_id] = index

    def selected_entry() -> LibraryPaper | None:
        target = state.selected_arxiv_id
        if not target:
            return None
        return current_entry_by_id.get(target) or selected_entry_cache.get(target)

    async def persist_view_preferences() -> None:
        await services.settings_service.save_view_preferences(
            active_domain=active_domain,
            days=selected_days,
            favorites_only=favorites_only,
            focus_tag=selected_topic_key,
            sort_mode=sort_mode,
        )

    async def reload_library(*, preserve_loaded: bool = True) -> None:
        nonlocal current_entries, current_counts
        browse_state = state.browse_state()
        selected = selected_entry()
        if selected is not None:
            selected_entry_cache[selected.ranked.paper.arxiv_id] = selected
        if preserve_loaded:
            limit = browse_state.load_limit(PAGE_SIZE)
            if browse_state.scroll_offset <= LIST_COMPACT_MAX_OFFSET:
                limit = min(limit, LIST_COMPACT_KEEP)
        else:
            limit = PAGE_SIZE
        query = make_query()
        entries_task = asyncio.to_thread(
            services.browse_controller.load_page,
            query,
            limit=limit,
        )
        counts_task = asyncio.to_thread(services.browse_controller.counts, query)
        entries, counts = await asyncio.gather(entries_task, counts_task)
        # A filter/domain change can finish while this SQLite read is in flight.
        # Never let an older query overwrite the newer interaction state.
        if query != make_query():
            return
        async with library_view_lock:
            current_entries = entries
            current_counts = counts
            rebuild_current_entry_indexes()
            browse_state.loaded_count = len(current_entries)
            selected_now = current_entry_by_id.get(state.selected_arxiv_id or "")
            if selected_now is not None:
                selected_entry_cache[selected_now.ranked.paper.arxiv_id] = selected_now

    async def load_more() -> list[LibraryPaper]:
        nonlocal current_entries
        if len(current_entries) >= current_counts.filtered:
            return []
        query = make_query()
        offset = len(current_entries)
        new_entries = await asyncio.to_thread(
            services.browse_controller.load_page,
            query,
            limit=PAGE_SIZE,
            offset=offset,
        )
        if query != make_query():
            return []
        async with library_view_lock:
            # A near-top compaction can happen while the SQLite page is in flight.
            # In that case this result belongs to the old deep offset and must be
            # discarded; the next extent-after event will query the new offset.
            if offset != len(current_entries):
                return []
            # Background sync can replace rows while keeping the same materialized
            # depth. De-duplicate by ID instead of blindly appending stale rows.
            unique = [
                entry
                for entry in new_entries
                if entry.ranked.paper.arxiv_id not in current_entry_by_id
            ]
            start = len(current_entries)
            current_entries.extend(unique)
            append_current_entry_indexes(unique, start=start)
            state.browse_state().loaded_count = len(current_entries)
            return unique

    def remember_viewport_anchor(pixels: float, *, lock: bool = True) -> None:
        browse_state = state.browse_state()
        browse_state.scroll_offset = max(0.0, pixels)
        if not current_entries:
            return
        index = min(int(browse_state.scroll_offset // LIST_ITEM_EXTENT), len(current_entries) - 1)
        browse_state.viewport_anchor_id = current_entries[index].ranked.paper.arxiv_id
        browse_state.viewport_anchor_offset = max(
            0.0,
            browse_state.scroll_offset - (index * LIST_ITEM_EXTENT),
        )
        if lock:
            browse_state.viewport_locked = True

    async def restore_scroll() -> None:
        nonlocal restoring_scroll
        if papers_scroll is None:
            return
        browse_state = state.browse_state()
        target_offset = browse_state.scroll_offset
        if browse_state.viewport_locked and browse_state.viewport_anchor_id:
            anchor_index = current_entry_index.get(browse_state.viewport_anchor_id)
            if anchor_index is not None:
                target_offset = (
                    anchor_index * LIST_ITEM_EXTENT + browse_state.viewport_anchor_offset
                )
                browse_state.scroll_offset = target_offset
        restoring_scroll = True
        try:
            await papers_scroll.scroll_to(
                offset=target_offset,
                duration=0,
            )
        except (AttributeError, RuntimeError):
            pass
        finally:
            restoring_scroll = False

    async def compact_materialized_list(*, known_scroll: float | None = None) -> bool:
        """Bound the Python/Flet working set while the viewport is safely near the top.

        Flutter lazily lays out ListView children, but every rich paper card in
        ``controls`` is still a Python control subtree. After a user has browsed
        thousands of rows and returns to the top, retaining all those controls
        only makes future diffs/index maintenance more expensive. Keep a modest
        head and let normal SQL pagination materialize the tail again on demand.
        """

        nonlocal current_entries
        if papers_scroll is None or len(current_entries) <= LIST_COMPACT_THRESHOLD:
            return False
        if known_scroll is None:
            try:
                known_scroll = float(papers_scroll.scroll_offset or 0)
            except (AttributeError, TypeError, ValueError):
                return False
        if known_scroll > LIST_COMPACT_MAX_OFFSET:
            return False

        selected = selected_entry()
        if selected is not None:
            selected_entry_cache[selected.ranked.paper.arxiv_id] = selected

        async with library_view_lock:
            if len(current_entries) <= LIST_COMPACT_THRESHOLD:
                return False
            dropped = current_entries[LIST_COMPACT_KEEP:]
            current_entries = current_entries[:LIST_COMPACT_KEEP]
            rebuild_current_entry_indexes()
            state.browse_state().loaded_count = len(current_entries)

        dropped_ids = {entry.ranked.paper.arxiv_id for entry in dropped}
        del papers_scroll.controls[LIST_COMPACT_KEEP:]
        del paper_control_order[LIST_COMPACT_KEEP:]
        for arxiv_id in dropped_ids:
            paper_card_controls.pop(arxiv_id, None)
            paper_card_entry_cache.pop(arxiv_id, None)
        try:
            papers_scroll.update()
        except RuntimeError:
            pass
        return True

    def schedule_list_compaction() -> None:
        nonlocal list_compaction_task
        if papers_scroll is None or len(current_entries) <= LIST_COMPACT_THRESHOLD:
            return
        if list_compaction_task is not None and not list_compaction_task.done():
            return

        async def worker() -> None:
            nonlocal list_compaction_task
            current = asyncio.current_task()
            try:
                await asyncio.sleep(LIST_COMPACT_IDLE_SECONDS)
                idle_for = time.monotonic() - last_list_scroll_at
                if idle_for < LIST_COMPACT_IDLE_SECONDS:
                    await asyncio.sleep(LIST_COMPACT_IDLE_SECONDS - idle_for)
                await compact_materialized_list()
            finally:
                if list_compaction_task is current:
                    list_compaction_task = None

        list_compaction_task = services.task_registry.create(
            worker(),
            name="tablet-list-working-set-compact",
        )

    async def handle_papers_scroll(event: ft.OnScrollEvent) -> None:
        nonlocal loading_more, last_list_scroll_at
        last_list_scroll_at = time.monotonic()
        pixels = float(event.pixels)
        remember_viewport_anchor(pixels, lock=not restoring_scroll)
        if pixels <= LIST_COMPACT_MAX_OFFSET:
            schedule_list_compaction()
        if (
            active_domain in sync_head_stale
            and pixels <= SYNC_LIVE_MUTATION_MAX_OFFSET
        ):
            schedule_sync_view_refresh(active_domain)
        if restoring_scroll or loading_more:
            return
        if len(current_entries) >= current_counts.filtered or event.extent_after > 650:
            return
        loading_more = True
        try:
            new_entries = await load_more()
            if new_entries and papers_scroll is not None:
                append_paper_cards(new_entries)
                papers_scroll.update()
        finally:
            loading_more = False

    async def maybe_offer_historical_fetch(
        *,
        domain_key: str,
        previous_days: int,
        new_days: int,
    ) -> None:
        if new_days <= previous_days:
            return
        today = datetime.now(UTC).date()
        desired_start = _cutoff(new_days).date()
        gap = await asyncio.to_thread(
            services.sync_coordinator.historical_gap,
            domain_key=domain_key,
            desired_start=desired_start,
            today=today,
        )
        if gap is None:
            return

        async def dismiss(_event=None) -> None:
            page.pop_dialog()

        async def fetch_missing(_event=None) -> None:
            page.pop_dialog()
            services.task_registry.create(
                run_sync(domain_key, new_days),
                name=f"tablet-range-fetch:{domain_key}",
            )

        page.show_dialog(
            ft.AlertDialog(
                modal=True,
                bgcolor=SURFACE,
                shape=ft.RoundedRectangleBorder(radius=20),
                title=ft.Text(
                    "补充更早的论文？",
                    size=18,
                    weight=ft.FontWeight.W_600,
                    color=TEXT_PRIMARY,
                ),
                content=ft.Text(
                    f"你选择了{RANGE_LABELS[new_days]}。本地尚未覆盖 "
                    f"{gap.from_date.isoformat()} 至 {gap.until_date.isoformat()} 的较早时间段，"
                    "其中可能有尚未下载的论文。是否现在补充获取？",
                    size=14,
                    color=TEXT_SECONDARY,
                ),
                actions=[
                    touch_text_button("暂不获取", on_click=dismiss),
                    touch_text_button("补充获取", on_click=fetch_missing, primary=True),
                ],
                actions_alignment=ft.MainAxisAlignment.END,
            )
        )

    async def apply_filter_values(
        *,
        days: int | None = None,
        topic_key: str | None = None,
        favorites: bool | None = None,
        sort: str | None = None,
        render: bool = True,
    ) -> None:
        nonlocal selected_days, selected_topic_key, favorites_only, sort_mode
        previous_days = selected_days
        filter_domain = active_domain
        if days is not None:
            selected_days = days
        if topic_key is not None:
            selected_topic_key = normalize_topic_key(active_domain, topic_key)
        if favorites is not None:
            favorites_only = bool(favorites)
        if sort is not None:
            sort_mode = sort
        state.reset_current_browse()
        await persist_view_preferences()
        await reload_library(preserve_loaded=False)
        if render:
            await render_current_route(restore_position=False, rebuild_home=True)
        if days is not None:
            await maybe_offer_historical_fetch(
                domain_key=filter_domain,
                previous_days=previous_days,
                new_days=selected_days,
            )

    async def switch_domain(domain_key: str) -> None:
        nonlocal active_domain, selected_days, selected_topic_key, favorites_only, sort_mode
        if domain_key == active_domain:
            return
        if papers_scroll is not None:
            try:
                remember_viewport_anchor(float(papers_scroll.scroll_offset or 0), lock=True)
            except (AttributeError, TypeError, ValueError):
                pass
        state.switch_domain(domain_key)
        active_domain = domain_key
        preferences = await services.settings_service.load_domain_view_preferences(domain_key)
        selected_days = preferences.days
        selected_topic_key = preferences.focus_tag
        favorites_only = preferences.favorites_only
        sort_mode = preferences.sort_mode
        await services.settings_service.save_active_domain(domain_key)
        await reload_library(preserve_loaded=True)
        # Keep the navigation drawer open while the user adjusts several choices.
        # Rebuild only the already-mounted home hosts behind the drawer; rebuilding
        # the route here causes Flet to dismiss the drawer immediately.
        await refresh_home_hosts(refresh_detail=True, update_appbar=True, restore_position=True)
        refresh_navigation_drawer()

    async def toggle_favorite(arxiv_id: str) -> None:
        nonlocal current_counts
        entry = current_entry_by_id.get(arxiv_id) or selected_entry_cache.get(arxiv_id)
        if entry is None:
            return
        next_favorite = not entry.state.is_favorite
        await asyncio.to_thread(
            services.library_service.set_favorite,
            arxiv_id,
            next_favorite,
        )

        removed_from_favorites = False
        updated: LibraryPaper | None = None
        async with library_view_lock:
            # Sync refresh can change the list while the SQLite write is running.
            # Resolve the row again by stable UI ID; never use list.index(old_obj),
            # which races with background list replacement and caused the reported
            # `... is not in list` runtime crash.
            index = current_entry_index.get(arxiv_id)
            current = current_entry_by_id.get(arxiv_id)
            if current is None or index is None:
                cached = selected_entry_cache.get(arxiv_id, entry)
                updated = replace(
                    cached,
                    state=replace(cached.state, is_favorite=next_favorite),
                )
                selected_entry_cache[arxiv_id] = updated
            else:
                updated = replace(
                    current,
                    state=replace(current.state, is_favorite=next_favorite),
                )
                favorite_delta = 1 if next_favorite else -1
                selected_entry_cache[arxiv_id] = updated
                if favorites_only and not next_favorite:
                    removed_from_favorites = True
                    current_entries.pop(index)
                    current_counts = replace(
                        current_counts,
                        filtered=max(0, current_counts.filtered - 1),
                        favorites=max(0, current_counts.favorites - 1),
                    )
                    if papers_scroll is not None and index < len(papers_scroll.controls):
                        papers_scroll.controls.pop(index)
                    if index < len(paper_control_order):
                        paper_control_order.pop(index)
                    paper_card_controls.pop(arxiv_id, None)
                    paper_card_entry_cache.pop(arxiv_id, None)
                    current_entry_by_id.pop(arxiv_id, None)
                    current_entry_index.pop(arxiv_id, None)
                    stable_id = base_arxiv_id(arxiv_id)
                    if current_entry_id_by_stable.get(stable_id) == arxiv_id:
                        current_entry_id_by_stable.pop(stable_id, None)
                    reindex_current_entries_from(index)
                else:
                    current_entries[index] = updated
                    current_counts = replace(
                        current_counts,
                        favorites=max(0, current_counts.favorites + favorite_delta),
                    )
                    current_entry_by_id[arxiv_id] = updated
                    current_entry_id_by_stable[base_arxiv_id(arxiv_id)] = arxiv_id
                    if papers_scroll is not None and index < len(papers_scroll.controls):
                        papers_scroll.controls[index] = make_paper_card(updated)

        if removed_from_favorites:
            more = await load_more()
            append_paper_cards(more)

        # Update only the controls that actually changed. A page-wide update with
        # hundreds/thousands of list controls serializes the whole tree and makes
        # taps look unresponsive while sync is running.
        if papers_scroll is not None:
            try:
                papers_scroll.update()
            except RuntimeError:
                pass
        if list_header_host is not None:
            list_header_host.content = build_list_header()
            try:
                list_header_host.update()
            except RuntimeError:
                pass
        if state.selected_arxiv_id == arxiv_id and updated is not None:
            if detail_host is not None:
                detail_host.content = build_paper_detail(updated)
                try:
                    detail_host.update()
                except RuntimeError:
                    pass
            route = page.route or "/"
            if route.startswith("/paper/") and len(page.views) > 1:
                page.views[-1].controls[0] = build_paper_detail(updated, compact_header=True)
                try:
                    page.views[-1].update()
                except RuntimeError:
                    pass

    def _set_paper_card_selected(card: ft.Container, selected: bool) -> None:
        card.bgcolor = PRIMARY_SOFT if selected else SURFACE
        card.border = ft.Border.all(1, PRIMARY_BORDER if selected else DIVIDER)

    async def select_paper(arxiv_id: str) -> None:
        nonlocal detail_host
        previous = state.selected_arxiv_id
        if papers_scroll is not None:
            try:
                remember_viewport_anchor(float(papers_scroll.scroll_offset or 0), lock=True)
            except (AttributeError, TypeError, ValueError):
                state.browse_state().viewport_locked = True
        state.remember_selection(arxiv_id)
        entry = current_entry_by_id.get(arxiv_id)
        if entry is not None:
            selected_entry_cache[arxiv_id] = entry

        # Mutate both selection cards before navigation. On single-pane tablets the
        # mounted home view stays underneath the detail view, so returning does not
        # need to rebuild hundreds/thousands of paper cards just to reflect selection.
        changed_cards: list[ft.Container] = []
        if previous and previous in paper_card_controls:
            card = paper_card_controls[previous]
            _set_paper_card_selected(card, False)
            changed_cards.append(card)
        if arxiv_id in paper_card_controls:
            card = paper_card_controls[arxiv_id]
            _set_paper_card_selected(card, True)
            changed_cards.append(card)

        if layout_mode() is TabletLayoutMode.SINGLE_PANE:
            await page.push_route(f"/paper/{arxiv_id}")
            return

        updates: list[ft.Control] = list(changed_cards)
        if detail_host is not None:
            detail_host.content = build_paper_detail(selected_entry())
            updates.append(detail_host)
        if updates:
            try:
                page.update(*updates)
            except RuntimeError:
                pass

    def _pdf_download_message(progress: PdfDownloadProgress) -> str:
        if progress.phase == "connecting":
            return "正在连接 arXiv…"
        if progress.phase == "downloading":
            if progress.fraction is not None:
                return f"正在下载 PDF · {int(progress.fraction * 100):d}%"
            downloaded_mb = progress.downloaded_bytes / (1024 * 1024)
            return f"正在下载 PDF · {downloaded_mb:.1f} MB"
        if progress.phase == "verifying":
            return "正在校验 PDF…"
        return "正在准备 PDF…"

    def _update_pdf_download_controls() -> None:
        progress = reader_pdf_download_progress
        updates: list[ft.Control] = []
        if reader_pdf_status_text is not None:
            reader_pdf_status_text.value = _pdf_download_message(progress)
            updates.append(reader_pdf_status_text)
        if reader_pdf_progress_bar is not None:
            reader_pdf_progress_bar.value = progress.fraction
            updates.append(reader_pdf_progress_bar)
        if updates:
            try:
                page.update(*updates)
            except RuntimeError:
                pass

    def _apply_pdf_download_progress(
        generation: int,
        progress: PdfDownloadProgress,
    ) -> None:
        nonlocal reader_pdf_download_progress
        if generation != reader_pdf_generation:
            return
        if reader_target is None or reader_target.kind != "pdf":
            return
        reader_pdf_download_progress = progress
        _update_pdf_download_controls()

    async def cleanup_pdf_reader() -> None:
        nonlocal reader_pdf_document, reader_pdf_generation, reader_pdf_error
        nonlocal reader_pdf_focus_index, reader_pdf_list, reader_pdf_scroll_pixels
        nonlocal reader_pdf_requested_focus, reader_pdf_render_worker, reader_pdf_scroll_direction
        nonlocal reader_pdf_download_task, reader_pdf_download_cancel
        nonlocal reader_pdf_download_progress, reader_pdf_status_text, reader_pdf_progress_bar
        reader_pdf_generation += 1
        if reader_pdf_download_cancel is not None:
            # Cooperative cancellation keeps navigation instant while the worker
            # exits at the next network read boundary and removes its partial file.
            reader_pdf_download_cancel.set()
        reader_pdf_download_task = None
        reader_pdf_download_cancel = None
        reader_pdf_download_progress = PdfDownloadProgress("idle")
        reader_pdf_status_text = None
        reader_pdf_progress_bar = None
        worker = reader_pdf_render_worker
        reader_pdf_render_worker = None
        document = reader_pdf_document
        reader_pdf_document = None
        reader_pdf_error = ""
        reader_pdf_focus_index = 0
        reader_pdf_requested_focus = 0
        reader_pdf_scroll_direction = 1
        reader_pdf_list = None
        reader_pdf_scroll_pixels = 0.0
        reader_pdf_page_hosts.clear()
        reader_pdf_page_offsets.clear()
        reader_pdf_page_starts.clear()
        reader_pdf_rendered_pages.clear()
        reader_pdf_rendering_pages.clear()
        reader_pdf_page_pngs.clear()
        if worker is not None and not worker.done():
            # Do not delete the temp file while PyMuPDF is still rasterizing it.
            # Generation invalidation makes the worker discard its result and exit.
            await asyncio.gather(worker, return_exceptions=True)
        if document is not None:
            await asyncio.to_thread(document.cleanup)

    def pdf_focus_index_for_offset(offset: float) -> int:
        if not reader_pdf_page_starts:
            return 0
        target = max(0.0, float(offset))
        # Page offsets are monotonic. Binary search avoids walking every page on
        # each scroll callback for long papers.
        index = bisect_right(reader_pdf_page_starts, target) - 1
        return max(0, min(index, len(reader_pdf_page_starts) - 1))

    def _pdf_page_image(page_index: int, png: bytes) -> ft.Image:
        host = reader_pdf_page_hosts[page_index]
        return ft.Image(
            src=png,
            width=host.width,
            height=host.height,
            fit=ft.BoxFit.CONTAIN,
            filter_quality=ft.FilterQuality.HIGH,
        )

    async def _render_pdf_neighborhood(generation: int, focus_index: int) -> None:
        """Render one latest-focus neighborhood; older scroll requests are coalesced."""

        document = reader_pdf_document
        if document is None or generation != reader_pdf_generation:
            return
        focus_index = max(0, min(focus_index, len(document.pages) - 1))
        # Render the page the user can actually see first, then prefetch in the
        # direction of travel. The previous implementation rendered numeric order
        # (often the previous page first), making fast swipes wait on invisible work.
        targets = [focus_index]
        for distance in range(1, PDF_RENDER_RADIUS + 1):
            primary = focus_index + (distance * reader_pdf_scroll_direction)
            secondary = focus_index - (distance * reader_pdf_scroll_direction)
            if 0 <= primary < len(document.pages):
                targets.append(primary)
            if 0 <= secondary < len(document.pages):
                targets.append(secondary)
        for page_index in targets:
            if generation != reader_pdf_generation or document is not reader_pdf_document:
                return
            # If the user has already flung to another page, finish at most the
            # current raster then let the worker switch to the newest focus.
            if focus_index != reader_pdf_requested_focus and page_index != focus_index:
                return
            if page_index in reader_pdf_rendered_pages or page_index in reader_pdf_rendering_pages:
                continue
            host = reader_pdf_page_hosts.get(page_index)
            if host is None:
                continue
            reader_pdf_rendering_pages.add(page_index)
            try:
                png = await asyncio.to_thread(
                    render_pdf_page,
                    document,
                    page_index,
                    target_width=reader_pdf_render_width,
                )
                if generation != reader_pdf_generation or document is not reader_pdf_document:
                    return
                reader_pdf_page_pngs[page_index] = png
                reader_pdf_rendered_pages.add(page_index)
                host.content = _pdf_page_image(page_index, png)
                try:
                    host.update()
                except RuntimeError:
                    pass
            finally:
                reader_pdf_rendering_pages.discard(page_index)

        keep = set(
            range(
                max(0, focus_index - PDF_RENDER_RADIUS),
                min(len(document.pages), focus_index + PDF_RENDER_RADIUS + 1),
            )
        )
        for page_index in tuple(reader_pdf_rendered_pages - keep):
            host = reader_pdf_page_hosts.get(page_index)
            reader_pdf_rendered_pages.discard(page_index)
            reader_pdf_page_pngs.pop(page_index, None)
            if host is not None:
                host.content = ft.Container(expand=True, bgcolor=SURFACE)
                try:
                    host.update()
                except RuntimeError:
                    pass

    async def render_pdf_pages(generation: int, focus_index: int | None = None) -> None:
        """Single-worker latest-page-wins PDF renderer.

        Rapid scroll used to create one high-resolution PyMuPDF job per focus
        change. Those jobs could overlap in the thread pool and compete with UI
        event handling. One worker now consumes only the newest requested focus.
        """

        nonlocal reader_pdf_render_worker, reader_pdf_requested_focus
        document = reader_pdf_document
        if document is None or generation != reader_pdf_generation:
            return
        requested = reader_pdf_focus_index if focus_index is None else focus_index
        reader_pdf_requested_focus = max(0, min(requested, len(document.pages) - 1))

        current = asyncio.current_task()
        if (
            reader_pdf_render_worker is not None
            and reader_pdf_render_worker is not current
            and not reader_pdf_render_worker.done()
        ):
            return
        reader_pdf_render_worker = current
        try:
            while generation == reader_pdf_generation and document is reader_pdf_document:
                focus = reader_pdf_requested_focus
                await _render_pdf_neighborhood(generation, focus)
                if focus == reader_pdf_requested_focus:
                    break
        finally:
            if reader_pdf_render_worker is current:
                reader_pdf_render_worker = None

    def schedule_pdf_render(focus_index: int | None = None) -> None:
        nonlocal reader_pdf_render_worker, reader_pdf_requested_focus
        document = reader_pdf_document
        if document is None:
            return
        requested = reader_pdf_focus_index if focus_index is None else focus_index
        reader_pdf_requested_focus = max(0, min(requested, len(document.pages) - 1))
        if reader_pdf_render_worker is not None and not reader_pdf_render_worker.done():
            return
        reader_pdf_render_worker = services.task_registry.create(
            render_pdf_pages(reader_pdf_generation, reader_pdf_requested_focus),
            name=f"tablet-pdf-render-{reader_pdf_generation}",
        )

    async def handle_pdf_scroll(event: ft.OnScrollEvent) -> None:
        nonlocal reader_pdf_focus_index, reader_pdf_scroll_pixels, reader_pdf_requested_focus
        nonlocal reader_pdf_scroll_direction
        reader_pdf_scroll_pixels = float(event.pixels)
        focus_index = pdf_focus_index_for_offset(reader_pdf_scroll_pixels)
        if focus_index == reader_pdf_focus_index:
            return
        reader_pdf_scroll_direction = 1 if focus_index > reader_pdf_focus_index else -1
        reader_pdf_focus_index = focus_index
        reader_pdf_requested_focus = focus_index
        schedule_pdf_render(focus_index)

    async def prepare_pdf_reader(
        target: ReaderTarget,
        generation: int,
        cancel_event: Event,
    ) -> None:
        nonlocal reader_pdf_document, reader_pdf_error
        nonlocal reader_pdf_download_task, reader_pdf_download_cancel
        nonlocal reader_pdf_download_progress
        loop = asyncio.get_running_loop()
        last_emit_at = 0.0
        last_phase = ""
        last_percent = -1

        def report_progress(progress: PdfDownloadProgress) -> None:
            nonlocal last_emit_at, last_phase, last_percent
            now = time.monotonic()
            percent = int((progress.fraction or 0.0) * 100) if progress.fraction is not None else -1
            phase_changed = progress.phase != last_phase
            percent_changed = percent >= 0 and percent != last_percent
            if not phase_changed and now - last_emit_at < 0.25 and not (percent_changed and percent == 100):
                return
            last_emit_at = now
            last_phase = progress.phase
            last_percent = percent
            loop.call_soon_threadsafe(_apply_pdf_download_progress, generation, progress)

        try:
            document = await asyncio.to_thread(
                download_temporary_pdf,
                target.source_url,
                cancel_event=cancel_event,
                progress_callback=report_progress,
            )
        except PdfDownloadCancelled:
            return
        except PdfReaderError as exc:
            if generation != reader_pdf_generation:
                return
            reader_pdf_error = str(exc)
            if page.route == "/reader":
                await render_current_route(restore_position=False)
            return
        finally:
            current = asyncio.current_task()
            if reader_pdf_download_task is current:
                reader_pdf_download_task = None
            if reader_pdf_download_cancel is cancel_event:
                reader_pdf_download_cancel = None

        if generation != reader_pdf_generation or reader_target is not target:
            await asyncio.to_thread(document.cleanup)
            return
        reader_pdf_document = document
        reader_pdf_error = ""
        reader_pdf_download_progress = PdfDownloadProgress("ready", 1, 1)
        if page.route == "/reader":
            # Rebuild once at the state boundary from download UI to local pages;
            # page rasterization continues through the existing single worker.
            await render_current_route(restore_position=False)

    async def open_reader(kind: str, url: str | None) -> None:
        nonlocal reader_target, reader_return_route, reader_pdf_generation, reader_pdf_error
        nonlocal reader_pdf_download_task, reader_pdf_download_cancel
        nonlocal reader_pdf_download_progress
        target = build_reader_target(kind, url)
        if target is None:
            await show_message("仅支持在应用内打开可信的 arXiv HTTPS 链接。", failed=True)
            return
        if papers_scroll is not None:
            try:
                remember_viewport_anchor(float(papers_scroll.scroll_offset or 0), lock=True)
            except (AttributeError, TypeError, ValueError):
                pass
        await cleanup_pdf_reader()
        reader_target = target
        reader_return_route = page.route or "/"
        reader_pdf_error = ""
        reader_pdf_generation += 1
        generation = reader_pdf_generation
        if target.kind == "pdf":
            reader_pdf_download_progress = PdfDownloadProgress("connecting")
            reader_pdf_download_cancel = Event()
        await page.push_route("/reader")
        if target.kind == "pdf":
            cancel_event = reader_pdf_download_cancel
            if cancel_event is None:
                return
            reader_pdf_download_task = services.task_registry.create(
                prepare_pdf_reader(target, generation, cancel_event),
                name=f"tablet-pdf-download-{generation}",
            )

    def open_reader_handler(kind: str, url: str | None):
        async def handler(_event=None) -> None:
            await open_reader(kind, url)

        return handler

    def build_paper_detail(
        entry: LibraryPaper | None,
        *,
        compact_header: bool = False,
    ) -> ft.Control:
        arxiv_id = entry.ranked.paper.arxiv_id if entry is not None else None
        return tablet_paper_detail(
            entry,
            compact_header=compact_header,
            on_toggle_favorite=favorite_handler(arxiv_id) if arxiv_id else None,
            on_analyze=analyze_selected if arxiv_id else None,
            on_open_arxiv=(
                open_reader_handler("arxiv", entry.ranked.paper.abstract_url)
                if entry is not None
                else None
            ),
            on_open_pdf=(
                open_reader_handler("pdf", entry.ranked.paper.pdf_url)
                if entry is not None
                else None
            ),
        )

    def refresh_task_sheet_now() -> None:
        nonlocal last_task_sheet_refresh_at
        if active_task_sheet is None:
            return
        active_task_sheet.content = build_task_sheet_content()
        try:
            active_task_sheet.update()
        except RuntimeError:
            return
        last_task_sheet_refresh_at = time.monotonic()

    def schedule_task_sheet_refresh(*, immediate: bool = False) -> None:
        """Coalesce progress UI so worker callbacks do not rebuild the sheet at wire speed."""

        nonlocal task_sheet_refresh_task
        if active_task_sheet is None:
            return
        if immediate:
            if task_sheet_refresh_task is not None and not task_sheet_refresh_task.done():
                task_sheet_refresh_task.cancel()
            else:
                task_sheet_refresh_task = None
            refresh_task_sheet_now()
            return
        if task_sheet_refresh_task is not None and not task_sheet_refresh_task.done():
            return

        async def worker() -> None:
            nonlocal task_sheet_refresh_task
            current = asyncio.current_task()
            try:
                delay = TASK_SHEET_REFRESH_INTERVAL - (
                    time.monotonic() - last_task_sheet_refresh_at
                )
                if delay > 0:
                    await asyncio.sleep(delay)
                refresh_task_sheet_now()
            finally:
                if task_sheet_refresh_task is current:
                    task_sheet_refresh_task = None

        task_sheet_refresh_task = services.task_registry.create(
            worker(),
            name="tablet-task-sheet-refresh",
        )

    def update_task(
        key: str,
        *,
        title: str,
        detail: str = "",
        progress: float | None = None,
        pause_callback=None,
        resume_callback=None,
        close_callback=None,
        pausing: bool = False,
        paused: bool = False,
    ) -> None:
        existing = tasks.get(key)
        # Once the user asks to pause, late worker progress can still arrive while
        # the current request unwinds. Keep the visible task in its user-selected
        # state until the backend confirms pause, rather than flashing "running".
        if existing is not None and (existing.pausing or existing.paused):
            if not pausing and not paused:
                return
        tasks[key] = _TaskStatus(
            key=key,
            title=title,
            detail=detail,
            progress=progress,
            pause_callback=pause_callback,
            resume_callback=resume_callback,
            close_callback=close_callback,
            pausing=pausing,
            paused=paused,
        )
        schedule_task_sheet_refresh(immediate=pausing or paused)

    def finish_task(key: str) -> None:
        tasks.pop(key, None)
        schedule_task_sheet_refresh(immediate=True)

    def build_task_sheet_content() -> ft.Control:
        rows: list[ft.Control] = []
        if not tasks:
            rows.append(ft.Text("当前没有运行中的任务。", size=14, color=TEXT_SECONDARY))
        for task in tasks.values():
            is_sync = task.key.startswith("sync:")
            heading = ft.Row(
                spacing=6,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Text(task.title, expand=True, size=15, weight=ft.FontWeight.W_600),
                    *(
                        [
                            touch_icon_button(
                                ft.Icons.CLOSE,
                                on_click=task.close_callback
                            )
                        ]
                        if task.close_callback is not None
                        else []
                    ),
                ],
            )
            body_controls: list[ft.Control] = [heading]
            # Sync internals such as category/page/checkpoint stay out of product
            # UI, but the user still gets a stable task-level progress indicator.
            if not is_sync and task.detail:
                body_controls.append(ft.Text(task.detail, size=13, color=TEXT_SECONDARY))
            if task.progress is not None or is_sync:
                body_controls.append(ft.ProgressBar(value=task.progress, height=4))

            if task.pausing:
                action = touch_text_button("正在暂停", disabled=True)
            elif task.paused and task.resume_callback is not None:
                action = touch_text_button("已暂停", on_click=task.resume_callback, primary=True)
            elif task.pause_callback is not None:
                action = touch_text_button("停止", on_click=task.pause_callback, danger=True)
            else:
                action = None
            if action is not None:
                body_controls.append(
                    ft.Row(
                        alignment=ft.MainAxisAlignment.END,
                        controls=[action],
                    )
                )

            rows.append(
                ft.Container(
                    padding=ft.Padding(left=14, top=8, right=8, bottom=12),
                    bgcolor=SURFACE,
                    border=ft.Border.all(1, DIVIDER),
                    border_radius=16,
                    content=ft.Column(
                        tight=True,
                        spacing=8,
                        controls=body_controls,
                    ),
                )
            )

        return ft.SafeArea(
            content=ft.Container(
                padding=ft.Padding(left=20, top=6, right=20, bottom=12),
                content=ft.Column(
                    tight=True,
                    spacing=12,
                    controls=[
                        ft.Text(
                            "正在运行",
                            size=19,
                            weight=ft.FontWeight.W_600,
                            color=TEXT_PRIMARY,
                        ),
                        *rows,
                    ],
                ),
            )
        )

    async def show_task_sheet(_event=None) -> None:
        nonlocal active_task_sheet

        def dismissed(_event=None) -> None:
            nonlocal active_task_sheet
            active_task_sheet = None

        sheet = ft.BottomSheet(
            show_drag_handle=True,
            scrollable=True,
            bgcolor=SURFACE,
            content=build_task_sheet_content(),
            on_dismiss=dismissed,
        )
        active_task_sheet = sheet
        page.show_dialog(sheet)

    def set_sync_status(domain_key: str, message: str) -> None:
        if message:
            sync_status_by_domain[domain_key] = message
        else:
            sync_status_by_domain.pop(domain_key, None)
        if domain_key == active_domain and list_sync_status_text is not None:
            list_sync_status_text.value = message
            list_sync_status_text.visible = bool(message)
            try:
                list_sync_status_text.update()
            except RuntimeError:
                pass

    async def request_sync_pause(domain_key: str) -> None:
        cancel_event = sync_cancel_events.get(domain_key)
        if cancel_event is None or cancel_event.is_set():
            return
        sync_pause_requested.add(domain_key)
        task = tasks.get(f"sync:{domain_key}")
        update_task(
            f"sync:{domain_key}",
            title=f"{get_research_domain(domain_key).label}同步",
            progress=task.progress if task is not None else None,
            close_callback=sync_close_handler(domain_key),
            pausing=True,
        )
        set_sync_status(domain_key, "正在暂停同步…")
        # Event is a cooperative interrupt: the visible state changes first, then
        # the worker exits at its next safe request/checkpoint boundary.
        cancel_event.set()

    async def resume_sync(domain_key: str) -> None:
        if domain_key not in sync_paused_scopes or domain_key in sync_cancel_events:
            return
        backfill_days = sync_paused_scopes.pop(domain_key)
        sync_pause_requested.discard(domain_key)
        sync_close_requested.discard(domain_key)
        tasks.pop(f"sync:{domain_key}", None)
        update_task(
            f"sync:{domain_key}",
            title=f"{get_research_domain(domain_key).label}同步",
            progress=None,
            pause_callback=sync_pause_handler(domain_key),
            close_callback=sync_close_handler(domain_key),
        )
        services.task_registry.create(
            run_sync(domain_key, backfill_days, resumed=True),
            name=f"tablet-resume-sync:{domain_key}",
        )

    async def request_sync_close(domain_key: str) -> None:
        key = f"sync:{domain_key}"
        # Closing is optimistic in the UI. The backend still receives a cancel
        # signal and performs checkpoint/resource cleanup after the card vanishes.
        tasks.pop(key, None)
        sync_paused_scopes.pop(domain_key, None)
        sync_pause_requested.discard(domain_key)
        sync_close_requested.add(domain_key)
        cancel_event = sync_cancel_events.get(domain_key)
        if cancel_event is not None:
            cancel_event.set()
        set_sync_status(domain_key, "")
        schedule_task_sheet_refresh(immediate=True)

    def sync_pause_handler(domain_key: str):
        async def handler(_event=None) -> None:
            await request_sync_pause(domain_key)

        return handler

    def sync_resume_handler(domain_key: str):
        async def handler(_event=None) -> None:
            await resume_sync(domain_key)

        return handler

    def sync_close_handler(domain_key: str):
        async def handler(_event=None) -> None:
            await request_sync_close(domain_key)

        return handler

    async def refresh_home_hosts(
        *,
        refresh_detail: bool = False,
        update_appbar: bool = False,
        restore_position: bool = False,
    ) -> None:
        """Refresh mounted home controls without replacing the route/ListView.

        Keeping the same ListView instance is important on Android: replacing the
        whole list during background sync interrupts touch scrolling and produces
        visible jumps. The selected detail is intentionally left alone unless the
        caller explicitly changes research domains.
        """

        nonlocal papers_scroll
        if list_header_host is not None:
            list_header_host.content = build_list_header()
            list_header_host.update()

        if paper_list_host is not None:
            if current_entries and papers_scroll is not None:
                # Reuse mounted card controls whenever their backing LibraryPaper
                # is unchanged. Background sync can prepend/reorder results, but
                # rebuilding every card allocates a large Flutter subtree and is
                # the main source of touch-scroll hitching on Android tablets.
                previous_controls = dict(paper_card_controls)
                previous_entries = dict(paper_card_entry_cache)
                previous_order = list(paper_control_order)
                paper_card_controls.clear()
                paper_card_entry_cache.clear()
                next_controls: list[ft.Control] = []
                next_order: list[str] = []
                rebuilt = False
                for entry in current_entries:
                    arxiv_id = entry.ranked.paper.arxiv_id
                    card = previous_controls.get(arxiv_id)
                    if card is None or previous_entries.get(arxiv_id) != entry:
                        card = make_paper_card(entry)
                        rebuilt = True
                    else:
                        paper_card_controls[arxiv_id] = card
                        paper_card_entry_cache[arxiv_id] = entry
                    next_order.append(arxiv_id)
                    next_controls.append(card)
                paper_control_order[:] = next_order
                if rebuilt or previous_order != next_order:
                    papers_scroll.controls[:] = next_controls
                    papers_scroll.update()
            else:
                # Covers the empty -> first results transition after the first
                # safe sync batch, as well as a filter that becomes empty.
                paper_list_host.content = build_paper_list()
                paper_list_host.update()

        if refresh_detail and detail_host is not None:
            detail_host.content = build_paper_detail(selected_entry())
            detail_host.update()

        if update_appbar:
            home_view = next((view for view in page.views if view.route == "/"), None)
            appbar = getattr(home_view, "appbar", None) if home_view is not None else None
            title = getattr(appbar, "title", None) if appbar is not None else None
            if isinstance(title, ft.Text):
                title.value = get_research_domain(active_domain).label
                appbar.update()

        if restore_position and papers_scroll is not None:
            await restore_scroll()

    async def refresh_synced_view(domain_key: str) -> None:
        """Patch only the live-sync head instead of rebuilding a deep ListView.

        Counts always refresh. Deep in the list, the visible model is left untouched.
        Near the top, first compact an old deep working set, then load/merge at most
        three pages. The merge snapshot and assignment share the same lock so a
        concurrent load-more cannot make the UI patch operate on a stale list.
        """

        nonlocal current_entries, current_counts
        if domain_key != active_domain:
            return

        query = make_query()
        scroll_pixels = 0.0
        if papers_scroll is not None:
            try:
                scroll_pixels = float(papers_scroll.scroll_offset or 0)
                remember_viewport_anchor(scroll_pixels, lock=True)
            except (AttributeError, TypeError, ValueError):
                scroll_pixels = 0.0

        # Mature recycler/windowed-list designs keep the materialized UI bounded.
        # When the user has returned to the head, retaining thousands of old Flet
        # card subtrees buys nothing: normal SQL pagination can materialize them
        # again if the user scrolls down. Compact before doing any live-head merge.
        if scroll_pixels <= LIST_COMPACT_MAX_OFFSET:
            await compact_materialized_list(known_scroll=scroll_pixels)

        counts = await asyncio.to_thread(services.browse_controller.counts, query)
        if domain_key != active_domain or query != make_query():
            return

        if scroll_pixels > SYNC_LIVE_MUTATION_MAX_OFFSET:
            async with library_view_lock:
                current_counts = counts
                sync_head_stale.add(domain_key)
            if list_header_host is not None:
                list_header_host.content = build_list_header()
                try:
                    list_header_host.update()
                except RuntimeError:
                    pass
            return

        requested_head_limit = min(
            max(PAGE_SIZE, len(current_entries)),
            SYNC_HEAD_REFRESH_LIMIT,
        )
        fresh_head = await asyncio.to_thread(
            services.browse_controller.load_page,
            query,
            limit=requested_head_limit,
        )
        if domain_key != active_domain or query != make_query():
            return

        async with library_view_lock:
            old_entries = list(current_entries)
            old_loaded = max(PAGE_SIZE, len(old_entries))
            old_ids = [entry.ranked.paper.arxiv_id for entry in old_entries]
            old_id_set = set(old_ids)
            old_by_id = {entry.ranked.paper.arxiv_id: entry for entry in old_entries}
            fresh_ids = {entry.ranked.paper.arxiv_id for entry in fresh_head}
            # Sync skips already-known stable IDs before ranking/upsert, so existing
            # rows keep their relative order. Merge the fresh bounded head with the
            # materialized tail, then patch only actual new/changed head controls.
            tail = [
                entry
                for entry in old_entries
                if entry.ranked.paper.arxiv_id not in fresh_ids
            ]
            desired_entries = [*fresh_head, *tail][:old_loaded]
            desired_ids = [entry.ranked.paper.arxiv_id for entry in desired_entries]
            desired_id_set = set(desired_ids)
            insertions = [
                (index, entry)
                for index, entry in enumerate(desired_entries)
                if entry.ranked.paper.arxiv_id not in old_id_set
            ]
            changed = [
                (index, entry)
                for index, entry in enumerate(desired_entries[:requested_head_limit])
                if (
                    entry.ranked.paper.arxiv_id in old_by_id
                    and old_by_id[entry.ranked.paper.arxiv_id] != entry
                )
            ]
            dropped_ids = old_id_set - desired_id_set

            current_entries = desired_entries
            current_counts = counts
            rebuild_current_entry_indexes()
            state.browse_state().loaded_count = len(current_entries)
            sync_head_stale.discard(domain_key)

        if papers_scroll is None and desired_entries and paper_list_host is not None:
            paper_list_host.content = build_paper_list()
            try:
                paper_list_host.update()
            except RuntimeError:
                pass
        elif papers_scroll is not None:
            controls = papers_scroll.controls
            for index, entry in insertions:
                controls.insert(index, make_paper_card(entry))
                paper_control_order.insert(index, entry.ranked.paper.arxiv_id)
            while len(controls) > len(desired_entries):
                controls.pop()
            while len(paper_control_order) > len(desired_entries):
                paper_control_order.pop()
            for arxiv_id in dropped_ids:
                paper_card_controls.pop(arxiv_id, None)
                paper_card_entry_cache.pop(arxiv_id, None)
            for index, entry in changed:
                if index < len(controls):
                    controls[index] = make_paper_card(entry)
                    if index < len(paper_control_order):
                        paper_control_order[index] = entry.ranked.paper.arxiv_id
            if insertions or changed:
                try:
                    papers_scroll.update()
                except RuntimeError:
                    pass

        if list_header_host is not None:
            list_header_host.content = build_list_header()
            try:
                list_header_host.update()
            except RuntimeError:
                pass

        if (insertions or changed) and papers_scroll is not None:
            await restore_scroll()

    def schedule_sync_view_refresh(domain_key: str) -> None:
        # Coalesce bursts without dropping the final saved-chunk notification.
        # If another chunk lands while one refresh is running, the worker loops
        # once more instead of silently discarding that update.
        sync_view_refresh_pending.add(domain_key)
        existing = sync_view_refresh_tasks.get(domain_key)
        if existing is not None and not existing.done():
            return

        async def refresh_loop() -> None:
            try:
                while domain_key in sync_view_refresh_pending:
                    sync_view_refresh_pending.discard(domain_key)
                    # Coalesce rapid OAI batches. While a finger is moving the
                    # list, do no list mutation at all; resume shortly after the
                    # gesture ends. This keeps background sync invisible to touch
                    # scrolling while still surfacing new papers continuously.
                    await asyncio.sleep(0.35)
                    while True:
                        idle_for = time.monotonic() - last_list_scroll_at
                        if idle_for >= 0.75:
                            break
                        await asyncio.sleep(0.75 - idle_for)
                    since_refresh = time.monotonic() - last_sync_view_refresh_at.get(
                        domain_key, 0.0
                    )
                    if since_refresh < 0.75:
                        await asyncio.sleep(0.75 - since_refresh)
                    await refresh_synced_view(domain_key)
                    last_sync_view_refresh_at[domain_key] = time.monotonic()
            finally:
                sync_view_refresh_tasks.pop(domain_key, None)

        sync_view_refresh_tasks[domain_key] = services.task_registry.create(
            refresh_loop(),
            name=f"tablet-sync-view-refresh:{domain_key}",
        )

    async def run_sync(
        domain_key: str,
        backfill_days: int | None = None,
        *,
        resumed: bool = False,
    ) -> None:
        key = f"sync:{domain_key}"
        label = get_research_domain(domain_key).label
        sync_scope = (
            f"补充 {RANGE_LABELS[backfill_days]}"
            if backfill_days is not None
            else "最新增量"
        )
        if domain_key in sync_cancel_events:
            await show_message(f"{label}已经在同步。")
            return
        if domain_key in sync_paused_scopes and not resumed:
            await show_task_sheet()
            return

        initial_state = await asyncio.to_thread(
            services.library_service.sync_state,
            domain_key=domain_key,
        )
        initial_sync = (
            initial_state.earliest_covered_date is None
            and initial_state.latest_covered_date is None
            and initial_state.last_successful_sync is None
        )
        cancel_event = Event()
        sync_cancel_events[domain_key] = cancel_event
        sync_pause_requested.discard(domain_key)
        sync_close_requested.discard(domain_key)
        set_sync_status(
            domain_key,
            "正在同步最近 7 天论文…"
            if initial_sync and backfill_days is None
            else f"{sync_scope} · 正在同步…",
        )
        leave_paused = False
        visible_saved_count = 0

        def on_progress(progress: SyncExecutionProgress) -> None:
            nonlocal visible_saved_count
            if cancel_event.is_set():
                return
            if progress.phase == "harvesting":
                detail = "正在同步论文"
            elif progress.phase == "partial_saved":
                detail = "正在同步论文"
                visible_saved_count = progress.saved_count
                set_sync_status(
                    domain_key,
                    f"正在同步论文 · 已同步 {progress.saved_count} 篇",
                )
                # Only visible saved-paper thresholds request a list refresh.
                # Raw OAI scan counts stay internal so the header count and the
                # status line describe the same user-visible dataset.
                schedule_sync_view_refresh(domain_key)
            elif progress.phase == "saved":
                detail = "正在同步论文"
                # A bootstrap uses one-day durable chunks. Do not refresh the
                # list after every short day chunk; surface intermediate data in
                # 20-paper steps and flush the final remainder once at completion.
                if progress.completed_chunks >= progress.total_chunks:
                    visible_saved_count = progress.saved_count
                    set_sync_status(
                        domain_key,
                        f"已同步 {progress.saved_count} 篇论文",
                    )
                    schedule_sync_view_refresh(domain_key)
                elif progress.saved_count >= visible_saved_count + 20:
                    visible_saved_count = progress.saved_count
                    set_sync_status(
                        domain_key,
                        f"正在同步论文 · 已同步 {progress.saved_count} 篇",
                    )
                    schedule_sync_view_refresh(domain_key)
            else:
                detail = f"{sync_scope} · 正在连接 arXiv"
            update_task(
                key,
                title=f"{label}同步",
                detail=detail,
                progress=progress.fraction,
                pause_callback=sync_pause_handler(domain_key),
                close_callback=sync_close_handler(domain_key),
            )

        update_task(
            key,
            title=f"{label}同步",
            detail=f"{sync_scope} · 准备中",
            progress=None,
            pause_callback=sync_pause_handler(domain_key),
            close_callback=sync_close_handler(domain_key),
        )
        try:
            result = await sync_execution.run(
                domain_key=domain_key,
                backfill_days=backfill_days,
                cancel_event=cancel_event,
                progress_callback=on_progress,
                write_lock=sync_write_lock,
            )
            if domain_key == active_domain:
                schedule_sync_view_refresh(domain_key)
            if result.cancelled:
                if domain_key in sync_pause_requested and domain_key not in sync_close_requested:
                    leave_paused = True
                    sync_paused_scopes[domain_key] = backfill_days
                    task = tasks.get(key)
                    update_task(
                        key,
                        title=f"{label}同步",
                        progress=task.progress if task is not None else None,
                        resume_callback=sync_resume_handler(domain_key),
                        close_callback=sync_close_handler(domain_key),
                        paused=True,
                    )
                    set_sync_status(domain_key, "同步已暂停")
            else:
                await show_message(
                    f"{label}{sync_scope}同步完成：已同步 {result.saved_count} 篇相关论文。"
                )
                settings = await services.settings_service.load()
                if result.candidates and settings.has_api_key and settings.auto_analyze:
                    services.task_registry.create(
                        run_analysis(
                            list(result.candidates[:20]),
                            domain_key=domain_key,
                            source_label=f"{label}同步候选",
                        ),
                        name=f"tablet-auto-analysis:{domain_key}",
                    )
        except Exception:
            # Transport errors are retried by ArxivClient. If all retries are
            # exhausted, keep already persisted papers and fail silently. A pause
            # request still becomes a stable paused task even if cancellation
            # races with the current network call.
            if domain_key in sync_pause_requested and domain_key not in sync_close_requested:
                leave_paused = True
                sync_paused_scopes[domain_key] = backfill_days
                task = tasks.get(key)
                update_task(
                    key,
                    title=f"{label}同步",
                    progress=task.progress if task is not None else None,
                    resume_callback=sync_resume_handler(domain_key),
                    close_callback=sync_close_handler(domain_key),
                    paused=True,
                )
                set_sync_status(domain_key, "同步已暂停")
            elif domain_key == active_domain:
                schedule_sync_view_refresh(domain_key)
        finally:
            sync_cancel_events.pop(domain_key, None)
            sync_pause_requested.discard(domain_key)
            sync_close_requested.discard(domain_key)
            if not leave_paused:
                set_sync_status(domain_key, "")
                finish_task(key)

    async def sync_current(_event=None) -> None:
        services.task_registry.create(
            run_sync(active_domain),
            name=f"tablet-sync:{active_domain}",
        )

    async def apply_analysis_results(
        analyses: Mapping[str, PaperAIAnalysis],
        *,
        domain_key: str,
    ) -> None:
        """Refresh only papers whose persisted analysis changed.

        DeepSeek completion used to reload every materialized row and rebuild the
        entire route. With a long list that can mean 1000+ cards for a 1-paper
        analysis. The analysis service already returns exactly the changed IDs, so
        patch those loaded cards/detail and refresh only aggregate counts.
        """

        nonlocal current_counts
        if domain_key != active_domain or not analyses:
            return
        analysis_map = analyses
        query = make_query()
        counts = await asyncio.to_thread(services.browse_controller.counts, query)
        if domain_key != active_domain or query != make_query():
            return

        changed_indices: list[int] = []
        selected_updated: LibraryPaper | None = None
        async with library_view_lock:
            for analysis_id, analysis in analysis_map.items():
                stable_id = base_arxiv_id(analysis_id)
                loaded_id = (
                    analysis_id
                    if analysis_id in current_entry_by_id
                    else current_entry_id_by_stable.get(stable_id)
                )
                if loaded_id is None:
                    selected_id = state.selected_arxiv_id
                    cached = selected_entry_cache.get(selected_id or "")
                    if (
                        selected_id
                        and cached is not None
                        and base_arxiv_id(selected_id) == stable_id
                    ):
                        selected_updated = replace(cached, analysis=analysis)
                        selected_entry_cache[selected_id] = selected_updated
                    continue
                index = current_entry_index.get(loaded_id)
                current = current_entry_by_id.get(loaded_id)
                if index is None or current is None:
                    continue
                updated = replace(current, analysis=analysis)
                current_entries[index] = updated
                current_entry_by_id[loaded_id] = updated
                selected_entry_cache[loaded_id] = updated
                paper_card_entry_cache[loaded_id] = updated
                changed_indices.append(index)
                if state.selected_arxiv_id == loaded_id:
                    selected_updated = updated
            current_counts = counts

        if papers_scroll is not None:
            controls = papers_scroll.controls
            for index in changed_indices:
                if index >= len(current_entries) or index >= len(controls):
                    continue
                entry = current_entries[index]
                controls[index] = make_paper_card(entry)
                if index < len(paper_control_order):
                    paper_control_order[index] = entry.ranked.paper.arxiv_id
            if changed_indices:
                try:
                    papers_scroll.update()
                except RuntimeError:
                    pass

        if list_header_host is not None:
            list_header_host.content = build_list_header()
            try:
                list_header_host.update()
            except RuntimeError:
                pass

        if selected_updated is not None and detail_host is not None:
            detail_host.content = build_paper_detail(selected_updated)
            try:
                detail_host.update()
            except RuntimeError:
                pass
        route = page.route or "/"
        if selected_updated is not None and route.startswith("/paper/") and len(page.views) > 1:
            page.views[-1].controls[0] = build_paper_detail(
                selected_updated,
                compact_header=True,
            )
            try:
                page.views[-1].update()
            except RuntimeError:
                pass

    async def run_analysis(
        papers,
        *,
        domain_key: str,
        source_label: str,
    ) -> None:
        nonlocal current_settings
        key = f"deepseek:{domain_key}"
        current_settings = await services.settings_service.load()
        api_key = await services.settings_service.get_api_key()
        if not api_key:
            await show_message("请先在设置中保存 DeepSeek API Key。", failed=True)
            return
        targets = list(papers)[:20]
        if not targets:
            await show_message("当前没有可分析的论文。")
            return
        update_task(
            key,
            title="DeepSeek 快速解读",
            detail=f"{source_label} · 0/{len(targets)} 篇",
        )

        def progress(done: int, total: int) -> None:
            update_task(
                key,
                title="DeepSeek 快速解读",
                detail=f"{source_label} · {done}/{total} 篇 · 分批完成即保存",
                progress=(done / total) if total else None,
            )

        try:
            client = services.deepseek_client(api_key=api_key, model=current_settings.model)
            analyses = await services.analysis_service.analyze_and_persist(
                client,
                targets,
                model=current_settings.model,
                domain_key=domain_key,
                batch_size=8,
                progress_callback=progress,
            )
            if domain_key == active_domain:
                await apply_analysis_results(analyses, domain_key=domain_key)
            await show_message(f"DeepSeek 已完成 {len(targets)} 篇论文快速解读。")
        except Exception as exc:
            await show_message(
                f"DeepSeek 分析失败：{type(exc).__name__}: {exc}",
                failed=True,
                seconds=7,
            )
        finally:
            finish_task(key)

    async def analyze_selected(_event=None) -> None:
        entry = selected_entry()
        if entry is None:
            await show_message("请先选择一篇论文。")
            return
        services.task_registry.create(
            run_analysis(
                [entry.ranked],
                domain_key=active_domain,
                source_label="当前论文",
            ),
            name=f"tablet-analysis:{entry.ranked.paper.arxiv_id}",
        )

    async def analyze_batch(_event=None) -> None:
        services.task_registry.create(
            run_analysis(
                [entry.ranked for entry in current_entries[:20]],
                domain_key=active_domain,
                source_label="当前列表",
            ),
            name=f"tablet-analysis-batch:{active_domain}",
        )

    async def load_all_filtered_entries() -> list[LibraryPaper]:
        return await asyncio.to_thread(
            services.browse_controller.load_all,
            make_query(),
            batch_size=500,
        )

    async def export_literature(file_format: str) -> None:
        key = f"export:{file_format}"
        entries = await load_all_filtered_entries()
        if not entries:
            await show_message("当前筛选结果为空，没有可导出的论文。")
            return
        domain = get_research_domain(active_domain)
        topic = domain.topic(selected_topic_key)
        context = ExportContext(
            range_label=RANGE_LABELS[selected_days],
            exported_at=datetime.now(UTC),
            domain_key=domain.key,
            domain_label=domain.label,
            scope_label="收藏" if favorites_only else "全部论文",
            topic_label=topic.label if topic else "全部方向",
            sort_label=SORT_LABELS[sort_mode],
        )
        items = [
            LiteratureExportItem(
                ranked=entry.ranked,
                state=entry.state,
                analysis=entry.analysis,
            )
            for entry in entries
        ]
        update_task(key, title="准备导出", detail=f"正在整理 {len(items)} 篇论文")
        try:
            if file_format == "xlsx":
                payload = await asyncio.to_thread(build_excel_export, items, context=context)
                extension = "xlsx"
                format_label = "Excel"
            else:
                payload = await asyncio.to_thread(build_markdown_export, items, context=context)
                extension = "md"
                format_label = "Markdown"
            file_name = (
                f"{domain.key}_papers_{_export_range_stamp(selected_days)}_"
                f"{datetime.now(UTC).strftime('%Y%m%d')}.{extension}"
            )
            file_path = await ft.FilePicker().save_file(
                dialog_title=f"导出{domain.label}论文",
                file_name=file_name,
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=[extension],
                src_bytes=payload,
            )
            if page.web or file_path:
                await show_message(f"{format_label} 导出完成：{file_name}")
            else:
                await show_message("已取消导出。")
        except Exception as exc:
            await show_message(
                f"导出失败：{type(exc).__name__}: {exc}",
                failed=True,
                seconds=7,
            )
        finally:
            finish_task(key)

    async def export_excel(_event=None) -> None:
        await export_literature("xlsx")

    async def export_markdown(_event=None) -> None:
        await export_literature("md")

    def refresh_navigation_drawer() -> None:
        if page.drawer is None:
            return
        refreshed = build_navigation_drawer()
        page.drawer.controls = refreshed.controls
        page.drawer.update()

    def domain_handler(domain_key: str):
        async def handler(_event=None) -> None:
            await switch_domain(domain_key)
        return handler

    def topic_handler(topic_key: str):
        async def handler(_event=None) -> None:
            await apply_filter_values(topic_key=topic_key, render=False)
            await refresh_home_hosts(refresh_detail=False, restore_position=True)
            refresh_navigation_drawer()
        return handler

    async def choose_all(_event=None) -> None:
        # Drawer choices are non-modal configuration. Keep it open so the user
        # can change several items and dismiss it only by tapping outside/right.
        await apply_filter_values(topic_key=TOPIC_ALL, favorites=False, render=False)
        await refresh_home_hosts(refresh_detail=False, restore_position=True)
        refresh_navigation_drawer()

    async def choose_favorites(_event=None) -> None:
        await apply_filter_values(favorites=True, render=False)
        await refresh_home_hosts(refresh_detail=False, restore_position=True)
        refresh_navigation_drawer()

    def drawer_row(
        label: str,
        *,
        selected: bool = False,
        trailing: str = "",
        on_click=None,
    ) -> ft.Control:
        return ft.Container(
            height=48,
            padding=ft.Padding.symmetric(horizontal=16, vertical=0),
            border_radius=12,
            bgcolor=PRIMARY_SOFT if selected else None,
            ink=True,
            on_click=on_click,
            content=ft.Row(
                controls=[
                    ft.Text(
                        label,
                        expand=True,
                        size=14,
                        color=PRIMARY_DARK if selected else TEXT_PRIMARY,
                    ),
                    ft.Text(trailing, size=13, color=TEXT_TERTIARY),
                ]
            ),
        )

    def build_navigation_drawer() -> ft.NavigationDrawer:
        domain = get_research_domain(active_domain)

        async def select_drawer_topic(topic_key: str) -> None:
            await apply_filter_values(topic_key=topic_key, render=False)
            await refresh_home_hosts(refresh_detail=False, restore_position=True)
            refresh_navigation_drawer()

        controls: list[ft.Control] = [
            ft.Container(
                padding=ft.Padding(left=18, top=20, right=18, bottom=12),
                content=ft.Column(
                    spacing=4,
                    controls=[
                        ft.Text(PRODUCT_NAME_ZH, size=18, weight=ft.FontWeight.W_600),
                        ft.Text("论文浏览", size=12, color=TEXT_TERTIARY),
                    ],
                ),
            ),
            ft.Container(
                padding=ft.Padding.symmetric(horizontal=18, vertical=4),
                content=ft.Text("研究领域", size=12, color=TEXT_TERTIARY),
            ),
        ]
        for item in RESEARCH_DOMAINS:
            controls.append(
                drawer_row(
                    item.label,
                    selected=item.key == active_domain,
                    on_click=domain_handler(item.key),
                )
            )
        controls.extend(
            [
                ft.Divider(height=18, color=DIVIDER),
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=18, vertical=4),
                    content=ft.Text("研究方向", size=12, color=TEXT_TERTIARY),
                ),
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=16, vertical=4),
                    content=build_selection_picker(
                        selected_key=selected_topic_key,
                        options=[
                            (TOPIC_ALL, "全部方向"),
                            *[(topic.key, topic.label) for topic in domain.topics],
                        ],
                        on_select=select_drawer_topic,
                        width=266,
                    ),
                ),
            ]
        )
        controls.extend(
            [
                ft.Divider(height=18, color=DIVIDER),
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=18, vertical=4),
                    content=ft.Text("论文", size=12, color=TEXT_TERTIARY),
                ),
                drawer_row(
                    "全部论文",
                    selected=not favorites_only and selected_topic_key == TOPIC_ALL,
                    trailing=str(current_counts.total),
                    on_click=choose_all,
                ),
                drawer_row(
                    "收藏",
                    selected=favorites_only,
                    trailing=str(current_counts.favorites),
                    on_click=choose_favorites,
                ),
                ft.Divider(height=18, color=DIVIDER),
                drawer_row(
                    "任务中心",
                    trailing=str(len(tasks)) if tasks else "",
                    on_click=open_tasks_from_drawer,
                ),
                drawer_row("设置", on_click=open_settings_from_drawer),
                ft.Container(height=18),
            ]
        )
        return ft.NavigationDrawer(
            width=304,
            bgcolor=SURFACE,
            selected_index=-1,
            controls=controls,
        )

    async def open_navigation(_event=None) -> None:
        page.drawer = build_navigation_drawer()
        await page.show_drawer()

    async def open_tasks_from_drawer(_event=None) -> None:
        await page.close_drawer()
        await show_task_sheet()

    async def open_settings_from_drawer(_event=None) -> None:
        await page.close_drawer()
        page.navigate("/settings")

    def build_selection_picker(
        *,
        selected_key: str,
        options: list[tuple[str, str]],
        on_select,
        width: float = 320,
    ) -> ft.PopupMenuButton:
        """Rounded native-form style picker used by Tablet filters/settings."""

        labels = dict(options)
        current = {"key": selected_key}
        value_text = ft.Text(
            labels.get(selected_key, selected_key),
            size=14,
            color=TEXT_PRIMARY,
            no_wrap=True,
        )
        row_by_key: dict[str, ft.Container] = {}
        check_by_key: dict[str, ft.Icon] = {}

        def sync_picker_ui(key: str) -> None:
            current["key"] = key
            value_text.value = labels.get(key, key)
            for option_key, row in row_by_key.items():
                selected = option_key == key
                row.bgcolor = PRIMARY_SOFT if selected else SURFACE
                row.content.controls[0].color = PRIMARY_DARK if selected else TEXT_PRIMARY
                check_by_key[option_key].visible = selected

        def handler(key: str):
            async def select(_event=None) -> None:
                if key == current["key"]:
                    return
                sync_picker_ui(key)
                try:
                    value_text.update()
                except RuntimeError:
                    pass
                await on_select(key)

            return select

        items: list[ft.PopupMenuItem] = []
        for key, label in options:
            selected = key == selected_key
            check = ft.Icon(ft.Icons.CHECK, size=16, color=PRIMARY_DARK, visible=selected)
            row = ft.Container(
                height=42,
                alignment=ft.Alignment.CENTER,
                padding=ft.Padding.symmetric(horizontal=12, vertical=0),
                border_radius=10,
                bgcolor=PRIMARY_SOFT if selected else SURFACE,
                content=ft.Row(
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Text(
                            label,
                            expand=True,
                            size=14,
                            color=PRIMARY_DARK if selected else TEXT_PRIMARY,
                        ),
                        ft.Container(width=18, alignment=ft.Alignment.CENTER, content=check),
                    ],
                ),
            )
            row_by_key[key] = row
            check_by_key[key] = check
            items.append(
                ft.PopupMenuItem(
                    content=row,
                    height=44,
                    padding=0,
                    on_click=handler(key),
                )
            )

        return ft.PopupMenuButton(
            width=width,
            height=50,
            padding=0,
            menu_position=ft.PopupMenuPosition.UNDER,
            bgcolor=SURFACE,
            elevation=3,
            shadow_color="#1F0F172A",
            menu_padding=ft.Padding.all(6),
            size_constraints=ft.BoxConstraints(min_width=width, max_width=width),
            shape=ft.RoundedRectangleBorder(
                radius=14,
                side=ft.BorderSide(width=1, color=DIVIDER),
            ),
            content=ft.Container(
                width=width,
                height=50,
                alignment=ft.Alignment.CENTER,
                padding=ft.Padding.symmetric(horizontal=14, vertical=0),
                bgcolor=SURFACE,
                border=ft.Border.all(1, DIVIDER),
                border_radius=14,
                content=ft.Row(
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Container(
                            expand=True,
                            alignment=ft.Alignment.CENTER_LEFT,
                            content=value_text,
                        ),
                        ft.Icon(ft.Icons.EXPAND_MORE, size=19, color=TEXT_TERTIARY),
                    ],
                ),
            ),
            items=items,
        )

    def build_filter_content(*, bottom_sheet: bool) -> tuple[ft.Control, dict[str, object]]:
        async def apply_filter_in_place(**values) -> None:
            # Keep the drawer/dialog mounted while several settings are adjusted.
            # Rebuilding the route dismisses Flet overlays after every choice.
            await apply_filter_values(render=False, **values)
            await refresh_home_hosts(refresh_detail=True, restore_position=False)

        async def change_days(key: str) -> None:
            await apply_filter_in_place(days=int(key))

        async def change_sort(key: str) -> None:
            await apply_filter_in_place(sort=key)

        async def change_topic(key: str) -> None:
            await apply_filter_in_place(topic_key=key)

        async def change_favorites(event) -> None:
            await apply_filter_in_place(favorites=bool(event.control.value))

        domain = get_research_domain(active_domain)
        picker_width = 320.0 if bottom_sheet else 332.0
        day_picker = build_selection_picker(
            selected_key=str(selected_days),
            options=[(str(days), label) for days, label in RANGE_LABELS.items()],
            on_select=change_days,
            width=picker_width,
        )
        sort_picker = build_selection_picker(
            selected_key=sort_mode,
            options=list(SORT_LABELS.items()),
            on_select=change_sort,
            width=picker_width,
        )
        topic_picker = build_selection_picker(
            selected_key=selected_topic_key,
            options=[
                (TOPIC_ALL, "全部方向"),
                *[(topic.key, topic.label) for topic in domain.topics],
            ],
            on_select=change_topic,
            width=picker_width,
        )
        favorite_switch = ft.Switch(
            value=favorites_only,
            on_change=change_favorites,
        )
        values: dict[str, object] = {
            "days": day_picker,
            "sort": sort_picker,
            "topic": topic_picker,
            "favorites": favorite_switch,
        }

        def field(title: str, control: ft.Control) -> ft.Control:
            return ft.Column(
                spacing=7,
                controls=[
                    ft.Text(title, size=13, color=TEXT_TERTIARY),
                    control,
                ],
            )

        content = ft.Container(
            width=372 if not bottom_sheet else None,
            padding=ft.Padding(left=20, top=16, right=20, bottom=24),
            content=ft.Column(
                spacing=18,
                controls=[
                    ft.Column(
                        spacing=4,
                        controls=[
                            ft.Text("筛选论文", size=20, weight=ft.FontWeight.W_600),
                            ft.Text(
                                "修改后立即保存并生效，无需再次确认。",
                                size=12,
                                color=TEXT_TERTIARY,
                            ),
                        ],
                    ),
                    field("时间范围", day_picker),
                    field("排序", sort_picker),
                    field("研究方向", topic_picker),
                    ft.Container(
                        height=58,
                        padding=ft.Padding.symmetric(horizontal=16, vertical=0),
                        bgcolor=SURFACE,
                        border=ft.Border.all(1, DIVIDER),
                        border_radius=14,
                        content=ft.Row(
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            controls=[
                                ft.Column(
                                    expand=True,
                                    tight=True,
                                    spacing=2,
                                    controls=[
                                        ft.Text("仅显示收藏", size=14, color=TEXT_PRIMARY),
                                        ft.Text(
                                            "只改变本地列表，不会触发联网同步",
                                            size=11,
                                            color=TEXT_TERTIARY,
                                        ),
                                    ],
                                ),
                                favorite_switch,
                            ],
                        ),
                    ),
                ],
            ),
        )
        return content, values

    async def open_filters(_event=None) -> None:
        if layout_mode() is TabletLayoutMode.LIST_DETAIL:
            content, _values = build_filter_content(bottom_sheet=False)
            page.end_drawer = ft.NavigationDrawer(
                width=380,
                bgcolor=SURFACE,
                selected_index=-1,
                controls=[content],
            )
            await page.show_end_drawer()
        else:
            content, _values = build_filter_content(bottom_sheet=True)
            page.show_dialog(
                ft.AlertDialog(
                    modal=False,
                    scrollable=True,
                    bgcolor=SURFACE,
                    content=content,
                    inset_padding=ft.Padding.symmetric(horizontal=32, vertical=28),
                    shape=ft.RoundedRectangleBorder(radius=18),
                )
            )

    def card_select_handler(arxiv_id: str):
        async def handler(_event=None) -> None:
            await select_paper(arxiv_id)
        return handler

    def favorite_handler(arxiv_id: str):
        async def handler(_event=None) -> None:
            await toggle_favorite(arxiv_id)
        return handler

    def make_paper_card(entry: LibraryPaper) -> ft.Container:
        arxiv_id = entry.ranked.paper.arxiv_id
        card = tablet_paper_card(
            entry,
            selected=arxiv_id == state.selected_arxiv_id,
            on_select=card_select_handler(arxiv_id),
            on_toggle_favorite=favorite_handler(arxiv_id),
        )
        paper_card_controls[arxiv_id] = card
        paper_card_entry_cache[arxiv_id] = entry
        return card

    def append_paper_cards(entries: list[LibraryPaper]) -> None:
        if papers_scroll is None:
            return
        papers_scroll.controls.extend(make_paper_card(entry) for entry in entries)
        paper_control_order.extend(entry.ranked.paper.arxiv_id for entry in entries)

    def build_paper_list() -> ft.Control:
        nonlocal papers_scroll
        paper_card_controls.clear()
        paper_card_entry_cache.clear()
        paper_control_order.clear()
        if not current_entries:
            papers_scroll = None
            return ft.Container(
                expand=True,
                padding=36,
                alignment=ft.Alignment.CENTER,
                content=ft.Text(
                    "当前筛选范围暂无论文。\n可以同步最新论文；扩大时间范围时，如本地缺少较早内容会主动提示补充获取。",
                    size=15,
                    color=TEXT_SECONDARY,
                    text_align=ft.TextAlign.CENTER,
                ),
            )

        controls = [make_paper_card(entry) for entry in current_entries]
        paper_control_order[:] = [entry.ranked.paper.arxiv_id for entry in current_entries]
        papers_scroll = ft.ListView(
            expand=True,
            spacing=LIST_SPACING,
            item_extent=TABLET_PAPER_CARD_HEIGHT,
            build_controls_on_demand=True,
            cache_extent=LIST_CACHE_EXTENT,
            scroll_interval=180,
            on_scroll=handle_papers_scroll,
            controls=controls,
        )
        return papers_scroll

    def build_list_header() -> ft.Control:
        nonlocal list_sync_status_text
        domain = get_research_domain(active_domain)
        topic = domain.topic(selected_topic_key)
        chips = [
            stat_metric("本地", current_counts.total),
            stat_metric("当前", current_counts.filtered),
            stat_metric("收藏", current_counts.favorites),
            stat_metric("已分析", current_counts.analyzed),
        ]
        status = sync_status_by_domain.get(active_domain, "")
        list_sync_status_text = ft.Text(
            status,
            size=12,
            color=PRIMARY_DARK,
            visible=bool(status),
        )
        return ft.Column(
            spacing=7,
            controls=[
                ft.Row(spacing=10, wrap=True, controls=chips),
                ft.Text(
                    f"{RANGE_LABELS[selected_days]} · "
                    f"{topic.label if topic else '全部方向'} · {SORT_LABELS[sort_mode]}",
                    size=12,
                    color=TEXT_TERTIARY,
                ),
                list_sync_status_text,
            ],
        )

    def build_app_bar(*, title: str | None = None, back: bool = False) -> ft.AppBar:
        domain_label = get_research_domain(active_domain).label
        if back:
            leading = touch_icon_button(
                ft.Icons.ARROW_BACK,
                on_click=lambda _e=None: page.navigate("/"),
            )
        else:
            leading = touch_icon_button(ft.Icons.MENU, on_click=open_navigation)
        actions: list[ft.Control] = []
        if not back:
            actions.extend(
                [
                    touch_icon_button(ft.Icons.FILTER_LIST, on_click=open_filters),
                    touch_icon_button(ft.Icons.SYNC, on_click=sync_current),
                    touch_icon_button(ft.Icons.TASK_ALT, on_click=show_task_sheet),
                    ft.PopupMenuButton(
                        icon=ft.Icons.MORE_VERT,
                        width=48,
                        height=48,
                        bgcolor=SURFACE,
                        elevation=3,
                        menu_padding=ft.Padding.all(6),
                        shape=ft.RoundedRectangleBorder(radius=14),
                        items=[
                            ft.PopupMenuItem(content=ft.Text("分析当前论文"), on_click=analyze_selected),
                            ft.PopupMenuItem(
                                content=ft.Text("批量 DeepSeek（最多 20 篇）"),
                                on_click=analyze_batch,
                            ),
                            ft.PopupMenuItem(content=ft.Text("导出 Excel"), on_click=export_excel),
                            ft.PopupMenuItem(content=ft.Text("导出 Markdown"), on_click=export_markdown),
                            ft.PopupMenuItem(
                                content=ft.Text("设置"),
                                on_click=lambda _e=None: page.navigate("/settings"),
                            ),
                        ],
                    ),
                ]
            )
        return ft.AppBar(
            toolbar_height=TABLET_APP_BAR_HEIGHT,
            bgcolor=SURFACE,
            color=TEXT_PRIMARY,
            elevation=0,
            elevation_on_scroll=0,
            shadow_color=ft.Colors.TRANSPARENT,
            leading=leading,
            leading_width=56,
            title=ft.Text(
                title or domain_label,
                size=18,
                weight=ft.FontWeight.W_600,
                color=TEXT_PRIMARY,
            ),
            actions=actions,
            actions_padding=ft.Padding.only(right=6),
            automatically_imply_leading=False,
        )

    def build_home_view() -> ft.View:
        nonlocal detail_host, list_header_host, paper_list_host, home_view_layout_mode
        mode = layout_mode()
        home_view_layout_mode = mode
        detail_host = None
        list_header_host = ft.Container(content=build_list_header())
        paper_list_host = ft.Container(expand=True, content=build_paper_list())
        list_pane = ft.Container(
            expand=40 if mode is TabletLayoutMode.LIST_DETAIL else True,
            bgcolor=APP_BG,
            padding=ft.Padding(left=14, top=14, right=14, bottom=8),
            content=ft.Column(
                expand=True,
                spacing=12,
                controls=[list_header_host, paper_list_host],
            ),
        )
        if mode is TabletLayoutMode.LIST_DETAIL:
            entry = selected_entry()
            detail_host = ft.Container(
                expand=60,
                bgcolor=APP_BG,
                content=build_paper_detail(entry),
            )
            content = ft.Row(
                expand=True,
                spacing=0,
                controls=[
                    list_pane,
                    ft.VerticalDivider(width=1, thickness=1, color=DIVIDER),
                    detail_host,
                ],
            )
        else:
            content = list_pane
        return ft.View(
            route="/",
            can_pop=False,
            padding=0,
            spacing=0,
            bgcolor=APP_BG,
            appbar=build_app_bar(),
            controls=[content],
        )

    def build_detail_view(arxiv_id: str) -> ft.View:
        if state.selected_arxiv_id != arxiv_id:
            state.remember_selection(arxiv_id)
        entry = selected_entry()
        return ft.View(
            route=f"/paper/{arxiv_id}",
            padding=0,
            spacing=0,
            bgcolor=APP_BG,
            appbar=build_app_bar(title="论文详情", back=True),
            controls=[build_paper_detail(entry, compact_header=True)],
        )

    def build_reader_view() -> ft.View:
        nonlocal reader_pdf_focus_index, reader_pdf_render_width, reader_pdf_list
        nonlocal reader_pdf_scroll_pixels, reader_pdf_status_text, reader_pdf_progress_bar
        nonlocal reader_pdf_scroll_direction
        target = reader_target
        if target is None:
            return ft.View(
                route="/reader",
                padding=0,
                bgcolor=SURFACE,
                appbar=build_app_bar(title="论文阅读", back=True),
                controls=[
                    ft.Container(
                        expand=True,
                        alignment=ft.Alignment.CENTER,
                        bgcolor=SURFACE,
                        content=ft.Text("没有可打开的论文链接。", color=TEXT_SECONDARY),
                    )
                ],
            )

        async def go_back(_event=None) -> None:
            if target.kind == "pdf":
                await cleanup_pdf_reader()
            await page.push_route(reader_return_route)

        async def open_external(_event=None) -> None:
            await launcher.launch_url(target.source_url)

        if target.kind == "pdf":
            document = reader_pdf_document
            reader_appbar = ft.AppBar(
                toolbar_height=TABLET_APP_BAR_HEIGHT,
                bgcolor=SURFACE,
                color=TEXT_PRIMARY,
                elevation=0,
                elevation_on_scroll=0,
                shadow_color=ft.Colors.TRANSPARENT,
                leading=touch_icon_button(ft.Icons.ARROW_BACK, on_click=go_back),
                leading_width=56,
                title=ft.Text(target.title, size=18, weight=ft.FontWeight.W_600),
                # Keep PDF chrome deliberately minimal: the only right-side
                # action is opening the trusted source URL in the browser.
                actions=[
                    touch_icon_button(ft.Icons.OPEN_IN_BROWSER, on_click=open_external),
                ],
                actions_padding=ft.Padding.only(right=6),
                automatically_imply_leading=False,
            )
            if document is None:
                controls: list[ft.Control] = []
                if reader_pdf_error:
                    reader_pdf_status_text = None
                    reader_pdf_progress_bar = None
                    controls.append(
                        ft.Text(
                            reader_pdf_error,
                            size=14,
                            color="#B54708",
                            text_align=ft.TextAlign.CENTER,
                        )
                    )
                else:
                    reader_pdf_status_text = ft.Text(
                        _pdf_download_message(reader_pdf_download_progress),
                        size=14,
                        color=TEXT_SECONDARY,
                        text_align=ft.TextAlign.CENTER,
                    )
                    reader_pdf_progress_bar = ft.ProgressBar(
                        width=320,
                        value=reader_pdf_download_progress.fraction,
                    )
                    controls.extend([reader_pdf_progress_bar, reader_pdf_status_text])
                if reader_pdf_error:
                    controls.append(
                        ft.Text(
                            "可使用右上角浏览器按钮查看原始 PDF。",
                            size=12,
                            color=TEXT_TERTIARY,
                            text_align=ft.TextAlign.CENTER,
                        )
                    )
                content: ft.Control = ft.Container(
                    expand=True,
                    alignment=ft.Alignment.CENTER,
                    padding=28,
                    content=ft.Column(
                        tight=True,
                        spacing=14,
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=controls,
                    ),
                )
            else:
                reader_pdf_status_text = None
                reader_pdf_progress_bar = None
                reader_pdf_page_hosts.clear()
                reader_pdf_page_offsets.clear()
                reader_pdf_page_starts.clear()
                reader_pdf_rendered_pages.clear()
                reader_pdf_rendering_pages.clear()
                reader_pdf_page_pngs.clear()
                reader_pdf_focus_index = 0
                reader_pdf_scroll_direction = 1
                reader_pdf_scroll_pixels = 0.0
                display_width = max(560.0, min(1060.0, float(page.width or 1280) - 56.0))
                reader_pdf_render_width = pdf_render_target_width(display_width)
                page_controls: list[ft.Control] = []
                cursor = 16.0
                for page_index, info in enumerate(document.pages):
                    page_height = display_width * info.height / max(info.width, 1.0)
                    host = ft.Container(
                        width=display_width,
                        height=page_height,
                        bgcolor=SURFACE,
                        border=ft.Border.all(1, DIVIDER),
                        alignment=ft.Alignment.CENTER,
                        content=ft.ProgressRing(width=22, height=22, stroke_width=2),
                    )
                    reader_pdf_page_hosts[page_index] = host
                    reader_pdf_page_starts.append(cursor)
                    reader_pdf_page_offsets.append((cursor, cursor + page_height))
                    cursor += page_height + 12.0
                    page_controls.append(
                        ft.Container(
                            alignment=ft.Alignment.CENTER,
                            content=host,
                        )
                    )

                reader_pdf_list = ft.ListView(
                    expand=True,
                    spacing=12,
                    padding=ft.Padding.symmetric(horizontal=18, vertical=16),
                    build_controls_on_demand=True,
                    cache_extent=360,
                    scroll_interval=120,
                    on_scroll=handle_pdf_scroll,
                    controls=page_controls,
                )
                content = reader_pdf_list
            return ft.View(
                route="/reader",
                padding=0,
                spacing=0,
                bgcolor=APP_BG,
                appbar=reader_appbar,
                controls=[content],
            )

        # arXiv abstract pages remain WebView-backed; only PDF uses local bytes.
        import flet_webview as fwv

        first_paint_ready = {"value": False}
        status = ft.Text("正在加载…", size=13, color=TEXT_TERTIARY)
        loading = ft.ProgressRing(width=24, height=24, stroke_width=2)
        loading_overlay = ft.Container(
            expand=True,
            bgcolor=SURFACE,
            alignment=ft.Alignment.CENTER,
            content=ft.Column(
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=12,
                controls=[loading, status],
            ),
        )

        def update_reader_chrome(
            *,
            loading_now: bool,
            message: str = "",
            failed: bool = False,
        ) -> None:
            status.value = message
            status.color = "#B54708" if failed else TEXT_TERTIARY
            loading.visible = loading_now
            loading_overlay.visible = loading_now or bool(message)
            try:
                loading_overlay.update()
            except RuntimeError:
                pass

        def page_started(_event=None) -> None:
            if not first_paint_ready["value"]:
                update_reader_chrome(loading_now=True, message="正在加载…")

        def page_ended(_event=None) -> None:
            if not first_paint_ready["value"]:
                first_paint_ready["value"] = True
                webview.opacity = 1
                try:
                    webview.update()
                except RuntimeError:
                    pass
            update_reader_chrome(loading_now=False)

        def page_error(event=None) -> None:
            if first_paint_ready["value"]:
                return
            detail = getattr(event, "data", "") or "页面加载失败"
            update_reader_chrome(
                loading_now=False,
                message=f"加载失败：{detail}",
                failed=True,
            )

        webview = fwv.WebView(
            url=target.view_url,
            expand=True,
            opacity=0,
            bgcolor=SURFACE,
            on_page_started=page_started,
            on_page_ended=page_ended,
            on_web_resource_error=page_error,
        )

        async def retry_web(_event=None) -> None:
            update_reader_chrome(loading_now=True, message="正在重新加载…")
            await webview.reload()

        reader_appbar = ft.AppBar(
            toolbar_height=TABLET_APP_BAR_HEIGHT,
            bgcolor=SURFACE,
            color=TEXT_PRIMARY,
            elevation=0,
            elevation_on_scroll=0,
            shadow_color=ft.Colors.TRANSPARENT,
            leading=touch_icon_button(ft.Icons.ARROW_BACK, on_click=go_back),
            leading_width=56,
            title=ft.Text(target.title, size=18, weight=ft.FontWeight.W_600),
            actions=[
                touch_icon_button(ft.Icons.REFRESH, on_click=retry_web),
                touch_icon_button(ft.Icons.OPEN_IN_BROWSER, on_click=open_external),
            ],
            actions_padding=ft.Padding.only(right=6),
            automatically_imply_leading=False,
        )
        return ft.View(
            route="/reader",
            padding=0,
            spacing=0,
            bgcolor=SURFACE,
            appbar=reader_appbar,
            controls=[
                ft.Stack(
                    expand=True,
                    controls=[webview, loading_overlay],
                )
            ],
        )

    def build_settings_view() -> ft.View:
        settings_save_lock = asyncio.Lock()
        selected_model = {"value": current_settings.model}
        api_helper = ft.Text(
            (
                "已安全保存 Key；输入新 Key 后按回车或离开输入框会自动更新。"
                if current_settings.has_api_key
                else "输入 Key 后按回车或离开输入框自动保存；保存后不会回显。"
            ),
            size=12,
            color=TEXT_TERTIARY,
        )
        api_key_field = ft.TextField(
            password=True,
            can_reveal_password=True,
            label="DeepSeek API Key",
            helper=api_helper,
            border_radius=14,
            border_color=DIVIDER,
            focused_border_color=PRIMARY_BORDER,
            bgcolor=SURFACE,
            text_size=14,
        )
        auto_analyze = ft.Switch(value=current_settings.auto_analyze)
        auto_sync = ft.Switch(value=current_settings.auto_sync_on_start)
        notifications = ft.Switch(value=current_settings.task_notifications)

        async def persist_settings(*, api_key: str | None = None) -> bool:
            nonlocal current_settings, auto_sync_domain_keys
            selected_domains = research_domain_keys()
            try:
                async with settings_save_lock:
                    current_settings = await services.settings_service.save(
                        model=selected_model["value"],
                        auto_analyze=bool(auto_analyze.value),
                        auto_sync_on_start=bool(auto_sync.value),
                        auto_sync_domain_keys=selected_domains,
                        task_notifications=bool(notifications.value),
                        api_key=api_key,
                    )
                auto_sync_domain_keys = selected_domains
                return True
            except Exception as exc:
                await show_message(
                    f"自动保存失败：{type(exc).__name__}: {exc}",
                    failed=True,
                    seconds=6,
                )
                return False

        async def save_api_key(_event=None) -> None:
            value = (api_key_field.value or "").strip()
            if not value:
                return
            if await persist_settings(api_key=value):
                api_key_field.value = ""
                api_helper.value = "已安全保存 Key；输入新 Key 可继续更新。"
                delete_key_button.disabled = False
                page.update()
                await show_message("DeepSeek API Key 已安全保存。")

        async def change_model(key: str) -> None:
            selected_model["value"] = key
            await persist_settings()

        async def change_auto_analyze(_event=None) -> None:
            await persist_settings()

        async def change_notifications(_event=None) -> None:
            await persist_settings()

        async def change_auto_sync(_event=None) -> None:
            await persist_settings()

        auto_analyze.on_change = change_auto_analyze
        auto_sync.on_change = change_auto_sync
        notifications.on_change = change_notifications
        api_key_field.on_submit = save_api_key
        api_key_field.on_blur = save_api_key

        model_picker = build_selection_picker(
            selected_key=current_settings.model,
            options=[(model, model) for model in SUPPORTED_DEEPSEEK_MODELS],
            on_select=change_model,
            width=250,
        )

        async def test_connection(_event=None) -> None:
            key = (api_key_field.value or "").strip() or await services.settings_service.get_api_key()
            if not key:
                await show_message("请先输入或保存 DeepSeek API Key。", failed=True)
                return
            update_task("deepseek:test", title="测试 DeepSeek 连接", detail="正在连接……")
            try:
                client = services.deepseek_client(
                    api_key=key,
                    model=selected_model["value"],
                )
                await asyncio.to_thread(client.test_connection)
                await show_message("DeepSeek 连接成功。")
            except Exception as exc:
                await show_message(f"DeepSeek 连接失败：{exc}", failed=True)
            finally:
                finish_task("deepseek:test")

        async def delete_key(_event=None) -> None:
            nonlocal current_settings
            if await services.settings_service.get_api_key() is None:
                delete_key_button.disabled = True
                page.update()
                await show_message("当前没有已保存的 DeepSeek API Key。")
                return
            current_settings = await services.settings_service.delete_api_key()
            api_key_field.value = ""
            api_helper.value = "当前未保存 Key；输入后按回车或离开输入框自动保存。"
            delete_key_button.disabled = True
            page.update()
            await show_message("已删除本机保存的 DeepSeek API Key。")

        def settings_row(title: str, detail: str, trailing: ft.Control) -> ft.Control:
            return ft.Container(
                padding=ft.Padding.symmetric(horizontal=16, vertical=12),
                content=ft.Row(
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Column(
                            expand=True,
                            spacing=3,
                            controls=[
                                ft.Text(title, size=15, weight=ft.FontWeight.W_500),
                                ft.Text(detail, size=12, color=TEXT_TERTIARY),
                            ],
                        ),
                        trailing,
                    ]
                ),
            )

        def settings_group(controls: list[ft.Control], *, padding: float = 0) -> ft.Control:
            if padding:
                content: ft.Control = ft.Container(
                    padding=padding,
                    content=ft.Column(spacing=12, controls=controls),
                )
            else:
                separated: list[ft.Control] = []
                for index, control in enumerate(controls):
                    if index:
                        separated.append(ft.Divider(height=1, color=DIVIDER))
                    separated.append(control)
                content = ft.Column(spacing=0, controls=separated)
            return ft.Container(
                bgcolor=SURFACE,
                border=ft.Border.all(1, DIVIDER),
                border_radius=16,
                content=content,
            )

        def section_title(text: str) -> ft.Control:
            return ft.Container(
                padding=ft.Padding.only(left=4, top=6, bottom=1),
                content=ft.Text(
                    text,
                    size=13,
                    weight=ft.FontWeight.W_600,
                    color=TEXT_TERTIARY,
                ),
            )

        sync_rows = [
            settings_row(
                "自动同步 arXiv",
                "开启后自动同步全部预设研究领域；启动时检查，打开期间每天 22:00 再检查一次。",
                auto_sync,
            ),
        ]
        delete_key_button = touch_text_button(
            "删除 API Key",
            on_click=delete_key,
            danger=True,
            disabled=not current_settings.has_api_key,
        )
        deepseek_controls = [
            api_key_field,
            settings_row("模型", "用于论文快速解读", model_picker),
            settings_row(
                "同步后自动分析",
                "仅在已配置 API Key 时对本轮候选执行。",
                auto_analyze,
            ),
            ft.Row(
                spacing=8,
                wrap=True,
                controls=[
                    touch_text_button("测试连接", on_click=test_connection),
                    delete_key_button,
                ],
            ),
        ]

        sync_group = settings_group(sync_rows)
        deepseek_group = settings_group(deepseek_controls, padding=16)
        notification_group = settings_group(
            [
                settings_row(
                    "任务完成提示",
                    "保留应用内任务完成与失败反馈。",
                    notifications,
                )
            ]
        )
        wide_landscape = bool(
            (page.width or 0) >= 1000
            and (page.width or 0) > (page.height or 0)
        )
        if wide_landscape:
            settings_content: list[ft.Control] = [
                ft.Row(
                    spacing=20,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                    controls=[
                        ft.Column(
                            expand=1,
                            spacing=10,
                            controls=[
                                section_title("同步"),
                                sync_group,
                                section_title("通知与反馈"),
                                notification_group,
                            ],
                        ),
                        ft.Column(
                            expand=1,
                            spacing=10,
                            controls=[
                                section_title("DeepSeek"),
                                deepseek_group,
                            ],
                        ),
                    ],
                )
            ]
        else:
            settings_content = [
                section_title("同步"),
                sync_group,
                section_title("DeepSeek"),
                deepseek_group,
                section_title("通知与反馈"),
                notification_group,
            ]

        body = ft.Column(
            scroll=ft.ScrollMode.AUTO,
            spacing=10,
            controls=[
                ft.Text("设置", size=22, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY),
                *settings_content,
                ft.Container(height=24),
            ],
        )
        available_width = max(560.0, float(page.width or 800.0) - 48.0)
        content_width = min(1180.0 if wide_landscape else 820.0, available_width)
        return ft.View(
            route="/settings",
            padding=0,
            spacing=0,
            bgcolor=APP_BG,
            appbar=build_app_bar(title="设置", back=True),
            controls=[
                ft.Container(
                    expand=True,
                    alignment=ft.Alignment.TOP_CENTER,
                    padding=ft.Padding.symmetric(horizontal=20, vertical=18),
                    content=ft.Container(
                        width=content_width,
                        content=body,
                    ),
                )
            ],
        )

    async def render_current_route(
        *,
        restore_position: bool = True,
        rebuild_home: bool = False,
    ) -> None:
        """Render the route stack while preserving the mounted Home/ListView.

        On single-pane tablets, opening a paper used to rebuild Home before pushing
        Detail. A deep list therefore recreated every materialized card for every
        tap. Keep Home mounted underneath Detail/Settings/Reader and rebuild it only
        when the layout class or query/filter model actually changes.
        """

        route = page.route or "/"
        mode = layout_mode()
        mounted_home = next((view for view in page.views if view.route == "/"), None)
        needs_home_rebuild = (
            rebuild_home
            or mounted_home is None
            or home_view_layout_mode is not mode
        )
        home_view = build_home_view() if needs_home_rebuild else mounted_home

        if route == "/reader":
            # Preserve the already-mounted Home/Detail stack and add only the reader
            # surface. This keeps the long paper list out of the navigation hot path.
            base_views = [view for view in page.views if view.route != "/reader"]
            if needs_home_rebuild:
                base_views = [view for view in base_views if view.route != "/"]
                base_views.insert(0, home_view)
            elif not any(view.route == "/" for view in base_views):
                base_views.insert(0, home_view)
            page.views[:] = base_views
            page.views.append(build_reader_view())
            page.update()
            if (
                reader_target is not None
                and reader_target.kind == "pdf"
                and reader_pdf_document is not None
            ):
                schedule_pdf_render(reader_pdf_focus_index)
            return

        if route.startswith("/paper/") and mode is TabletLayoutMode.SINGLE_PANE:
            arxiv_id = route.removeprefix("/paper/")
            page.views[:] = [home_view, build_detail_view(arxiv_id)]
        elif route == "/settings":
            page.views[:] = [home_view, build_settings_view()]
        else:
            page.views[:] = [home_view]
        page.update()
        if restore_position and (mode is TabletLayoutMode.LIST_DETAIL or route == "/"):
            await restore_scroll()

    async def handle_route_change(_event=None) -> None:
        route = page.route or "/"
        if route != "/reader" and reader_target is not None and reader_target.kind == "pdf":
            # Covers system Back/navigation as well as our own AppBar button.
            # Incrementing the generation also invalidates an in-flight download.
            await cleanup_pdf_reader()
        if route.startswith("/paper/"):
            state.remember_selection(route.removeprefix("/paper/"))
        state.settings_open = route == "/settings"
        await render_current_route()

    async def handle_view_pop(event: ft.ViewPopEvent) -> None:
        # Dialogs/drawers are dismissed by Flet before the view stack reaches here.
        # A popped detail/settings/reader view returns to the stable previous route.
        if event.view is not None and event.view.route == "/reader":
            await cleanup_pdf_reader()
        if event.view is not None and len(page.views) > 1:
            page.views.remove(event.view)
            target = page.views[-1].route
            await page.push_route(target)
        else:
            await page.push_route("/")

    async def handle_resize(_event=None) -> None:
        # Window size classes can change at runtime through rotation or split-screen.
        if papers_scroll is not None:
            try:
                remember_viewport_anchor(float(papers_scroll.scroll_offset or 0), lock=True)
            except (AttributeError, TypeError, ValueError):
                pass
        # Preserve selection/filter/loaded depth and keep the same paper visible.
        route = page.route or "/"
        next_mode = layout_mode()
        layout_changed = home_view_layout_mode is not next_mode
        if next_mode is TabletLayoutMode.LIST_DETAIL and route.startswith("/paper/"):
            page.route = "/"
            await render_current_route(rebuild_home=True)
            return
        if (
            next_mode is TabletLayoutMode.SINGLE_PANE
            and route == "/"
            and state.selected_arxiv_id
        ):
            await page.push_route(f"/paper/{state.selected_arxiv_id}")
            return
        await render_current_route(rebuild_home=layout_changed)

    async def background_startup() -> None:
        nonlocal current_settings, auto_sync_domain_keys
        try:
            await asyncio.to_thread(services.library_service.rerank_if_needed)
        except Exception:
            pass
        current_settings = await services.settings_service.load()
        auto_sync_domain_keys = await services.settings_service.load_auto_sync_domain_keys(
            fallback_domain=active_domain
        )
        if not current_settings.auto_sync_on_start:
            return
        for domain_key in auto_sync_domain_keys:
            services.task_registry.create(
                run_sync(domain_key),
                name=f"tablet-startup-sync:{domain_key}",
            )

    async def nightly_auto_sync_loop() -> None:
        nonlocal current_settings, auto_sync_domain_keys
        gate = DailySyncGate(hour=22, minute=0)
        gate.prime(datetime.now().astimezone())
        while True:
            await asyncio.sleep(30)
            if not gate.should_trigger(datetime.now().astimezone()):
                continue
            current_settings = await services.settings_service.load()
            if not current_settings.auto_sync_on_start:
                continue
            auto_sync_domain_keys = await services.settings_service.load_auto_sync_domain_keys(
                fallback_domain=active_domain
            )
            for domain_key in auto_sync_domain_keys:
                if domain_key in sync_cancel_events or domain_key in sync_paused_scopes:
                    continue
                services.task_registry.create(
                    run_sync(domain_key),
                    name=f"tablet-nightly-sync:{domain_key}",
                )

    async def shutdown(_event=None) -> None:
        for event in sync_cancel_events.values():
            event.set()
        await cleanup_pdf_reader()
        await services.task_registry.cancel_all()

    boot_message.value = "正在加载论文……"
    page.update()
    await reload_library(preserve_loaded=False)

    page.clean()
    page.on_route_change = handle_route_change
    page.on_view_pop = handle_view_pop
    page.on_resize = handle_resize
    page.on_close = shutdown
    page.route = "/"
    await render_current_route(restore_position=False)
    services.task_registry.create(background_startup(), name="tablet-startup")
    services.task_registry.create(nightly_auto_sync_loop(), name="tablet-nightly-sync")
