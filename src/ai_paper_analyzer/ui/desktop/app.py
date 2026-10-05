from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import flet as ft
import flet_secure_storage as fss

from ai_paper_analyzer.application.analysis_service import AnalysisService
from ai_paper_analyzer.application.browse_controller import BrowseController, BrowseQuery
from ai_paper_analyzer.application.export_service import ExportContext
from ai_paper_analyzer.application.library_service import LibraryService
from ai_paper_analyzer.application.radar_service import RadarResult, RadarService
from ai_paper_analyzer.application.settings_service import (
    SettingsService,
    ViewPreferences,
)
from ai_paper_analyzer.application.sync_service import (
    DailySyncGate,
    SyncCoordinator,
    SyncJobRegistry,
)
from ai_paper_analyzer.application.task_registry import TaskRegistry
from ai_paper_analyzer.domain.library import (
    ArxivHarvestProgress,
    PaperUserState,
)
from ai_paper_analyzer.domain.llm import LLMSettings, PaperAIAnalysis, SUPPORTED_DEEPSEEK_MODELS
from ai_paper_analyzer.domain.models import RankedPaper
from ai_paper_analyzer.domain.research_domains import (
    RESEARCH_DOMAINS,
    TOPIC_ALL,
    get_research_domain,
    normalize_topic_key,
    research_domain_keys,
)
from ai_paper_analyzer.infrastructure.arxiv_client import ArxivClient, ArxivError, ArxivSyncCancelled
from ai_paper_analyzer.infrastructure.deepseek_client import DeepSeekClient, DeepSeekError
from ai_paper_analyzer.infrastructure.flet_settings_store import (
    FletPreferenceStore,
    FletSecureSecretStore,
)
from ai_paper_analyzer.infrastructure.sqlite_library import SQLitePaperRepository
from ai_paper_analyzer.ui.control_updates import ControlUpdateBatcher
from ai_paper_analyzer.ui.desktop.export import ExportRequest, run_export
from ai_paper_analyzer.ui.text_format import format_paper_title
from ai_paper_analyzer.ui.paper_view import (
    DomainBrowseState,
    SORT_INTEREST,
    SORT_LATEST,
    SORT_RELEVANCE,
)
from ai_paper_analyzer.ui.components import (
    INTERACTIVE_FONT_WEIGHT,
    _badge,
    _button_style,
    _domain_segment,
    _empty_state,
    _metric,
    _paper_card,
    _set_domain_segment,
    _set_sidebar_item,
    _settings_card,
    _settings_row,
    _sidebar_item,
    _style_dropdown,
    _style_text_field,
    _toolbar_segment_style,
)
from ai_paper_analyzer.ui.theme import (
    APP_BG,
    CARD_RADIUS,
    CONTROL_RADIUS,
    DANGER,
    DANGER_SOFT,
    DIVIDER,
    DIVIDER_STRONG,
    FONT_BODY,
    FONT_CAPTION,
    FONT_CONTROL,
    FONT_DETAIL_TITLE,
    FONT_META,
    FONT_PAGE_TITLE,
    FONT_SECTION,
    INSPECTOR_WIDTH,
    PRIMARY,
    PRIMARY_BORDER,
    PRIMARY_DARK,
    PRIMARY_SOFT,
    SIDEBAR_BG,
    SIDEBAR_WIDTH,
    SURFACE,
    SURFACE_SUBTLE,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    TEXT_TERTIARY,
)


PRODUCT_NAME_ZH = "论文自动检索分析"
DOMAIN_LABELS = {domain.key: domain.label for domain in RESEARCH_DOMAINS}
RESPONSIVE_INSPECTOR_BREAKPOINT = 1420
SORT_LABELS = {
    SORT_INTEREST: "方向相关度优先",
    SORT_RELEVANCE: "领域相关度优先",
    SORT_LATEST: "最新更新优先",
}






def _default_storage_data_dir() -> Path:
    storage_data = os.environ.get("FLET_APP_STORAGE_DATA")
    if storage_data:
        return Path(storage_data)
    return Path.cwd() / ".flet" / "storage" / "data"


def _default_database_path() -> Path:
    return _default_storage_data_dir() / "embodied_ai_radar.db"


def _default_arxiv_diagnostic_path() -> Path:
    return _default_storage_data_dir() / "arxiv_http_diagnostic.log"
































async def mount(page: ft.Page, *, configure_desktop_window: bool) -> None:
    page.title = PRODUCT_NAME_ZH
    page.padding = 0
    page.theme_mode = ft.ThemeMode.LIGHT
    page.bgcolor = APP_BG
    page.horizontal_alignment = ft.CrossAxisAlignment.STRETCH
    # Desktop window chrome belongs to the Desktop presentation. Tablet/Phone
    # compatibility shells reuse this workspace temporarily in v1.1.0 but must
    # not impose desktop-only window constraints on Android.
    if configure_desktop_window:
        page.window.maximized = True
        page.window.min_width = 1180
        page.window.min_height = 720
        if os.name == "nt":
            icon_path = Path(__file__).resolve().parents[3] / "assets" / "icon.ico"
            if icon_path.exists():
                page.window.icon = str(icon_path)

    # Render a lightweight application shell immediately. The previous startup
    # placeholder used a centered bordered card, which looked like a dialog flashing
    # briefly on fast Windows launches. Keep the anti-black-screen behavior, but make
    # startup visually continuous with the real header instead of showing a modal-like
    # surface.
    boot_message = ft.Text(
        "正在准备本地论文库……",
        size=FONT_META,
        color=TEXT_TERTIARY,
    )
    boot_header = ft.Container(
        height=76,
        bgcolor=SURFACE,
        padding=ft.Padding.symmetric(horizontal=24, vertical=10),
        border=ft.Border.all(1, DIVIDER),
        content=ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Row(
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Image(src="icon.png", width=34, height=34, fit=ft.BoxFit.COVER),
                        ft.Text(
                            PRODUCT_NAME_ZH,
                            size=20,
                            weight=ft.FontWeight.BOLD,
                            color=TEXT_PRIMARY,
                        ),
                    ],
                ),
                ft.Row(
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.ProgressRing(
                            width=18,
                            height=18,
                            stroke_width=2,
                            semantics_label="正在启动",
                        ),
                        boot_message,
                    ],
                ),
            ],
        ),
    )
    boot_screen = ft.SafeArea(
        expand=True,
        content=ft.Column(
            expand=True,
            spacing=0,
            controls=[
                boot_header,
                ft.Container(expand=True, bgcolor=APP_BG),
            ],
        ),
    )
    page.add(boot_screen)
    await asyncio.sleep(0)

    arxiv_client = ArxivClient(diagnostic_log_path=_default_arxiv_diagnostic_path())
    radar_service = RadarService(arxiv_client)
    repository = SQLitePaperRepository(_default_database_path())
    library_service = LibraryService(repository)
    browse_controller = BrowseController(library_service)
    sync_coordinator = SyncCoordinator(library_service)
    analysis_service = AnalysisService(radar_service, library_service)
    boot_message.value = "正在打开本地论文库……"
    page.update()
    await asyncio.to_thread(library_service.initialize)

    secure_storage = fss.SecureStorage(
        android_options=fss.AndroidOptions(
            reset_on_error=True,
            migrate_on_algorithm_change=True,
        )
    )
    settings_service = SettingsService(
        FletSecureSecretStore(secure_storage),
        FletPreferenceStore(ft.SharedPreferences()),
    )

    boot_message.value = "正在加载设置和本地论文……"
    page.update()
    current_settings = await settings_service.load()
    view_preferences = await settings_service.load_view_preferences()
    domain_view_preferences_cache: dict[str, ViewPreferences] = {
        view_preferences.active_domain: view_preferences
    }
    domain_view_preference_tasks: dict[str, asyncio.Task[ViewPreferences]] = {}
    active_domain = view_preferences.active_domain
    # Tracks the segment the user most recently requested. It can lead
    # ``active_domain`` briefly while the target domain is loaded in the
    # background, which gives immediate visual feedback without blocking input.
    requested_domain = active_domain
    domain_switch_generation = 0
    selected_topic_key = normalize_topic_key(active_domain, view_preferences.focus_tag)
    selected_days = view_preferences.days
    auto_sync_domain_keys = await settings_service.load_auto_sync_domain_keys(
        fallback_domain=active_domain
    )

    def cutoff_for_days(days: int) -> datetime:
        start = (datetime.now(UTC) - timedelta(days=days)).date()
        return datetime(start.year, start.month, start.day, tzinfo=UTC)

    def selected_cutoff() -> datetime:
        return cutoff_for_days(selected_days)

    def make_browse_query(
        *,
        domain_key: str,
        days: int,
        topic_key: str,
        favorites_only: bool,
        sort: str,
    ) -> BrowseQuery:
        return BrowseQuery(
            updated_after=cutoff_for_days(days),
            domain_key=domain_key,
            topic_key=topic_key,
            favorites_only=favorites_only,
            sort_mode=sort,
        )

    initial_range_label = {
        7: "最近 7 天",
        30: "最近 1 个月",
        90: "最近 3 个月",
        180: "最近 6 个月",
        365: "最近 1 年",
    }[selected_days]
    # Only materialize one screen-sized page for startup/domain changes. Counts
    # are fetched separately with cheap aggregate SQL, so the UI can stay
    # accurate without constructing thousands of Python/Flet objects up front.
    library_page_size = 24
    initial_query = make_browse_query(
        domain_key=active_domain,
        days=selected_days,
        topic_key=selected_topic_key,
        favorites_only=view_preferences.favorites_only,
        sort=view_preferences.sort_mode,
    )
    initial_entries_task = asyncio.create_task(
        asyncio.to_thread(
            browse_controller.load_page,
            initial_query,
            limit=library_page_size,
        )
    )
    initial_counts_task = asyncio.create_task(
        asyncio.to_thread(browse_controller.counts, initial_query)
    )
    initial_sync_state_task = asyncio.create_task(
        asyncio.to_thread(library_service.sync_state, domain_key=active_domain)
    )
    cached_library, current_view_counts, sync_state = await asyncio.gather(
        initial_entries_task, initial_counts_task, initial_sync_state_task
    )
    current_result = RadarResult(
        fetched_count=0,
        papers=tuple(entry.ranked for entry in cached_library),
    )
    paper_states: dict[str, PaperUserState] = {
        entry.ranked.paper.arxiv_id: entry.state for entry in cached_library
    }
    expanded_abstract_ids: set[str] = set()

    analyses: dict[str, PaperAIAnalysis] = {
        entry.ranked.paper.arxiv_id: entry.analysis
        for entry in cached_library
        if entry.analysis is not None
    }
    # Pagination/scroll/selection are runtime browsing state, not filter
    # preferences. Keep one independent snapshot per top-level research domain
    # so switching domains never inherits another domain's list position.
    domain_browse_states = {
        domain.key: DomainBrowseState() for domain in RESEARCH_DOMAINS
    }
    domain_browse_states[active_domain].loaded_count = len(current_result.papers)

    title = ft.Text(
        PRODUCT_NAME_ZH,
        size=20,
        weight=ft.FontWeight.BOLD,
        color=TEXT_PRIMARY,
        no_wrap=True,
    )
    initial_status_text = (
        f"已加载{initial_range_label}首批 {len(current_result.papers)} 篇论文"
        f"（当前筛选共 {current_view_counts.filtered} 篇）。"
        if current_result.papers
        else f"本地{initial_range_label}暂无符合当前筛选的论文；需要时点击同步。"
    )
    # Status and long-running tasks live at the bottom of the left Sidebar. Keep
    # them narrow enough to stay inside the navigation column rather than
    # floating over the paper workspace.
    task_center_width = SIDEBAR_WIDTH - 32
    task_center_inner_width = task_center_width - 24
    status = ft.Text(
        "",
        size=FONT_META,
        color=TEXT_PRIMARY,
        max_lines=2,
        overflow=ft.TextOverflow.ELLIPSIS,
    )
    status_panel = ft.Container(
        visible=False,
        width=task_center_width,
        padding=ft.Padding.symmetric(horizontal=10, vertical=9),
        bgcolor=SURFACE,
        border=ft.Border.all(1, PRIMARY_BORDER),
        border_radius=CONTROL_RADIUS,
        content=status,
    )
    status_hide_task: asyncio.Task[None] | None = None
    activity_task_controls: dict[
        str, tuple[ft.Container, ft.Text, ft.Text, ft.ProgressBar, ft.Text]
    ] = {}
    activity_tasks_column = ft.Column(spacing=6)

    paper_list = ft.Column(
        spacing=14,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )
    stats_row = ft.Row(spacing=28, wrap=False)
    refresh_button = ft.FilledButton(
        content="同步",
        width=72,
        height=36,
        style=_button_style("primary", compact=True),
    )
    analyze_button = ft.OutlinedButton(
        content="批量 DeepSeek",
        disabled=True,
        style=_button_style("secondary", compact=True),
    )
    export_excel_button = ft.OutlinedButton(
        content="Excel", style=_button_style("secondary", compact=True)
    )
    export_markdown_button = ft.OutlinedButton(
        content="Markdown", style=_button_style("secondary", compact=True)
    )
    more_button = ft.OutlinedButton(
        content="更多",
        width=64,
        height=36,
        style=_button_style("secondary", compact=True),
    )
    settings_nav_button = ft.TextButton(content="设置", style=_button_style("ghost", compact=True))
    back_to_library_button = ft.TextButton(
        content="← 论文",
        visible=False,
        style=_button_style("ghost", compact=True),
    )
    domain_buttons = {
        key: _domain_segment(label, selected=key == active_domain)
        for key, label in DOMAIN_LABELS.items()
    }
    domain_selector = ft.Row(
        width=348,
        height=36,
        spacing=6,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[domain_buttons[key] for key in DOMAIN_LABELS],
    )
    selected_arxiv_id: str | None = None
    restoring_domain_scroll = False
    inspector_collapsed = False
    inspector_auto_collapsed = False
    inspector_collapse_button = ft.TextButton(
        content="收起",
        style=_button_style("ghost", compact=True),
    )
    inspector_expand_button = ft.TextButton(
        content="展开",
        style=_button_style("ghost", compact=True),
    )
    inspector_body = ft.Column(
        expand=True,
        spacing=14,
        scroll=ft.ScrollMode.HIDDEN,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )
    inspector_collapsed_body = ft.Column(
        expand=True,
        spacing=12,
        alignment=ft.MainAxisAlignment.START,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            inspector_expand_button,
            ft.Text(
                "分析",
                size=FONT_CAPTION,
                color=TEXT_TERTIARY,
                weight=INTERACTIVE_FONT_WEIGHT,
            ),
        ],
    )

    range_7d_button = ft.TextButton(
        content="7天", width=44, height=32, style=_toolbar_segment_style(False)
    )
    range_1m_button = ft.TextButton(
        content="1月", width=44, height=32, style=_toolbar_segment_style(True)
    )
    range_3m_button = ft.TextButton(
        content="3月", width=44, height=32, style=_toolbar_segment_style(False)
    )
    range_6m_button = ft.TextButton(
        content="6月", width=44, height=32, style=_toolbar_segment_style(False)
    )
    range_1y_button = ft.TextButton(
        content="1年", width=44, height=32, style=_toolbar_segment_style(False)
    )
    range_segment = ft.Container(
        height=36,
        padding=2,
        bgcolor=SURFACE,
        border=ft.Border.all(1, DIVIDER_STRONG),
        border_radius=CONTROL_RADIUS,
        content=ft.Row(
            spacing=0,
            wrap=False,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                range_7d_button,
                range_1m_button,
                range_3m_button,
                range_6m_button,
                range_1y_button,
            ],
        ),
    )
    range_buttons = {
        7: range_7d_button,
        30: range_1m_button,
        90: range_3m_button,
        180: range_6m_button,
        365: range_1y_button,
    }

    auto_loading_more = False
    sync_jobs = SyncJobRegistry()
    sync_tasks: dict[str, asyncio.Task[bool]] = {}
    task_registry = TaskRegistry()
    task_ui_updates = ControlUpdateBatcher(page, task_registry)
    sync_write_lock = asyncio.Lock()

    sync_job_details: dict[str, ft.Text] = {}
    sync_job_progress: dict[str, ft.ProgressBar] = {}
    sync_job_progress_texts: dict[str, ft.Text] = {}
    sync_job_cancel_buttons: dict[str, ft.TextButton] = {}
    sync_job_cards: dict[str, ft.Container] = {}
    for domain in RESEARCH_DOMAINS:
        detail = ft.Text(
            "等待开始……",
            size=FONT_CAPTION,
            color=TEXT_SECONDARY,
            max_lines=1,
            overflow=ft.TextOverflow.ELLIPSIS,
        )
        progress = ft.ProgressBar(
            width=max(task_center_inner_width - 20, 120),
            value=None,
            border_radius=999,
            semantics_label=f"{domain.label}同步进度",
        )
        progress_text = ft.Text("", size=FONT_CAPTION, color=TEXT_TERTIARY)
        cancel_button = ft.TextButton(
            content="停止",
            style=_button_style("danger", compact=True),
        )
        card = ft.Container(
            visible=False,
            width=task_center_inner_width,
            padding=ft.Padding.symmetric(horizontal=8, vertical=8),
            bgcolor=SURFACE,
            border=ft.Border.all(1, DIVIDER_STRONG),
            border_radius=CONTROL_RADIUS,
            content=ft.Column(
                spacing=7,
                controls=[
                    ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Text(
                                domain.label,
                                size=FONT_BODY,
                                weight=ft.FontWeight.BOLD,
                                color=TEXT_PRIMARY,
                            ),
                            cancel_button,
                        ],
                    ),
                    detail,
                    progress,
                    progress_text,
                ],
            ),
        )
        sync_job_details[domain.key] = detail
        sync_job_progress[domain.key] = progress
        sync_job_progress_texts[domain.key] = progress_text
        sync_job_cancel_buttons[domain.key] = cancel_button
        sync_job_cards[domain.key] = card

    task_center_expanded = False
    task_center_summary = ft.Text(
        "",
        size=FONT_CAPTION,
        color=TEXT_SECONDARY,
        max_lines=1,
        overflow=ft.TextOverflow.ELLIPSIS,
    )
    task_center_expand_button = ft.TextButton(
        content="展开",
        style=_button_style("ghost", compact=True),
    )
    sync_jobs_detail_column = ft.Column(
        spacing=8,
        controls=[sync_job_cards[domain.key] for domain in RESEARCH_DOMAINS],
    )
    task_center_detail_column = ft.Column(
        visible=False,
        spacing=8,
        controls=[activity_tasks_column, sync_jobs_detail_column],
    )
    task_center_panel = ft.Container(
        visible=False,
        width=task_center_width,
        padding=ft.Padding.symmetric(horizontal=10, vertical=9),
        bgcolor=SURFACE,
        border=ft.Border.all(1, DIVIDER_STRONG),
        border_radius=CONTROL_RADIUS,
        content=ft.Column(
            spacing=8,
            controls=[
                ft.Row(
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.ProgressRing(
                            width=18,
                            height=18,
                            stroke_width=2,
                            semantics_label="后台任务进行中",
                        ),
                        ft.Column(
                            expand=True,
                            spacing=1,
                            controls=[
                                ft.Text(
                                    "任务中心",
                                    size=FONT_BODY,
                                    weight=ft.FontWeight.BOLD,
                                    color=TEXT_PRIMARY,
                                ),
                                task_center_summary,
                            ],
                        ),
                        task_center_expand_button,
                    ],
                ),
                task_center_detail_column,
            ],
        ),
    )
    sort_mode = view_preferences.sort_mode
    sort_label = ft.Text(
        SORT_LABELS.get(sort_mode, SORT_LABELS[SORT_INTEREST]),
        size=FONT_CONTROL,
        color=TEXT_PRIMARY,
        no_wrap=True,
    )
    sort_menu_rows: dict[str, ft.Container] = {}
    sort_menu_checks: dict[str, ft.Icon] = {}

    def make_sort_menu_item(key: str) -> ft.PopupMenuItem:
        check = ft.Icon(ft.Icons.CHECK, size=15, color=PRIMARY, visible=key == sort_mode)
        row = ft.Container(
            height=34,
            alignment=ft.Alignment.CENTER,
            padding=ft.Padding.symmetric(horizontal=10, vertical=0),
            border_radius=7,
            bgcolor=PRIMARY_SOFT if key == sort_mode else SURFACE,
            content=ft.Row(
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Text(
                        SORT_LABELS[key],
                        expand=True,
                        size=FONT_CONTROL,
                        color=PRIMARY_DARK if key == sort_mode else TEXT_PRIMARY,
                        weight=INTERACTIVE_FONT_WEIGHT,
                        no_wrap=True,
                    ),
                    ft.Container(width=16, alignment=ft.Alignment.CENTER, content=check),
                ],
            ),
        )
        sort_menu_rows[key] = row
        sort_menu_checks[key] = check
        return ft.PopupMenuItem(content=row, height=36, padding=0)

    sort_menu_items = [
        make_sort_menu_item(SORT_INTEREST),
        make_sort_menu_item(SORT_RELEVANCE),
        make_sort_menu_item(SORT_LATEST),
    ]

    def sync_sort_selector_ui() -> None:
        sort_label.value = SORT_LABELS.get(sort_mode, SORT_LABELS[SORT_INTEREST])
        for key, row in sort_menu_rows.items():
            selected = key == sort_mode
            row.bgcolor = PRIMARY_SOFT if selected else SURFACE
            text_control = row.content.controls[0]
            text_control.color = PRIMARY_DARK if selected else TEXT_PRIMARY
            sort_menu_checks[key].visible = selected

    def sort_handler(key: str):
        async def handler(_event=None) -> None:
            nonlocal sort_mode
            if key == sort_mode:
                return
            sort_mode = key
            sync_sort_selector_ui()
            await apply_filter()

        return handler

    for key, item in zip(SORT_LABELS, sort_menu_items, strict=True):
        item.on_click = sort_handler(key)

    sort_selector = ft.PopupMenuButton(
        width=164,
        height=36,
        padding=0,
        menu_position=ft.PopupMenuPosition.UNDER,
        bgcolor=SURFACE,
        elevation=3,
        shadow_color="#240F172A",
        menu_padding=ft.Padding.all(4),
        size_constraints=ft.BoxConstraints(min_width=164, max_width=164),
        shape=ft.RoundedRectangleBorder(
            radius=CONTROL_RADIUS,
            side=ft.BorderSide(width=1, color=DIVIDER_STRONG),
        ),
        content=ft.Container(
            width=164,
            height=36,
            alignment=ft.Alignment.CENTER,
            padding=ft.Padding.symmetric(horizontal=12, vertical=0),
            bgcolor=SURFACE,
            border=ft.Border.all(1, DIVIDER_STRONG),
            border_radius=CONTROL_RADIUS,
            content=ft.Row(
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Container(
                        expand=True,
                        alignment=ft.Alignment.CENTER_LEFT,
                        content=sort_label,
                    ),
                    ft.Icon(ft.Icons.ARROW_DROP_DOWN, size=18, color=TEXT_SECONDARY),
                ],
            ),
        ),
        items=sort_menu_items,
    )
    favorites_only_checkbox = ft.Checkbox(
        label="仅收藏",
        value=view_preferences.favorites_only,
    )
    more_actions_panel = ft.Container(
        visible=False,
        padding=ft.Padding.symmetric(horizontal=0, vertical=4),
        content=ft.Row(
            spacing=7,
            wrap=True,
            alignment=ft.MainAxisAlignment.END,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                analyze_button,
                export_excel_button,
                export_markdown_button,
            ],
        ),
    )
    sidebar_item_width = SIDEBAR_WIDTH - 32
    library_all_button = _sidebar_item(
        "全部论文", width=sidebar_item_width, selected=True
    )
    library_favorites_button = _sidebar_item("收藏", width=sidebar_item_width)
    direction_all_button = _sidebar_item(
        "全部方向", width=sidebar_item_width, selected=True
    )
    library_scope_section = ft.Column(
        spacing=3,
        controls=[
            ft.Text(
                "论文范围",
                size=FONT_CAPTION,
                color=TEXT_TERTIARY,
                weight=ft.FontWeight.BOLD,
            ),
            library_all_button,
            library_favorites_button,
        ],
    )
    sidebar_scope_divider = ft.Container(height=1, bgcolor=DIVIDER)
    direction_title = ft.Text(
        "研究方向",
        size=FONT_CAPTION,
        color=TEXT_TERTIARY,
        weight=ft.FontWeight.BOLD,
    )
    direction_buttons: dict[str, ft.Container] = {}
    direction_items_column = ft.Column(spacing=5)
    direction_section = ft.Column(
        spacing=5,
        controls=[direction_title, direction_items_column],
    )

    api_key_field = ft.TextField(
        label=None,
        password=True,
        can_reveal_password=True,
        helper=ft.Text(
            "留空保存会保留现有 Key；已保存的 Key 不会回显。",
            size=FONT_CAPTION,
            color=TEXT_TERTIARY,
        ),
    )
    model_dropdown = ft.Dropdown(
        label=None,
        value=current_settings.model,
        options=[ft.DropdownOption(key=model, text=model) for model in SUPPORTED_DEEPSEEK_MODELS],
    )
    auto_analyze_switch = ft.Switch(value=current_settings.auto_analyze)
    auto_sync_on_start_switch = ft.Switch(value=current_settings.auto_sync_on_start)
    task_notifications_switch = ft.Switch(value=current_settings.task_notifications)
    save_settings_button = ft.FilledButton(content="保存设置", style=_button_style("primary"))
    test_connection_button = ft.OutlinedButton(content="测试连接", style=_button_style("secondary"))
    delete_key_button = ft.OutlinedButton(content="删除 API Key", style=_button_style("danger"))
    api_key_field.width = 560
    model_dropdown.width = 320
    _style_text_field(api_key_field)
    _style_dropdown(model_dropdown)

    papers_view = ft.Column(
        expand=True,
        spacing=14,
        scroll=ft.ScrollMode.HIDDEN,
        scroll_interval=120,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )
    settings_view = ft.Column(
        expand=True,
        spacing=18,
        visible=False,
        scroll=ft.ScrollMode.HIDDEN,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )

    def show_busy(
        task_id: str,
        message: str,
        detail: str = "请稍候，不需要重复点击。",
        *,
        progress: float | None = None,
        progress_text: str = "",
    ) -> None:
        controls = activity_task_controls.get(task_id)
        if controls is None:
            message_control = ft.Text(
                message,
                size=FONT_BODY,
                weight=ft.FontWeight.BOLD,
                color=TEXT_PRIMARY,
            )
            detail_control = ft.Text(
                detail,
                size=FONT_CAPTION,
                color=TEXT_SECONDARY,
                max_lines=2,
                overflow=ft.TextOverflow.ELLIPSIS,
            )
            progress_control = ft.ProgressBar(
                width=task_center_inner_width,
                value=progress,
                border_radius=999,
                semantics_label="任务进度",
            )
            progress_text_control = ft.Text(
                progress_text,
                size=FONT_CAPTION,
                color=TEXT_TERTIARY,
            )
            card = ft.Container(
                width=task_center_inner_width,
                padding=ft.Padding.symmetric(horizontal=8, vertical=8),
                border_radius=CONTROL_RADIUS,
                bgcolor=SURFACE,
                border=ft.Border.all(1, DIVIDER_STRONG),
                content=ft.Column(
                    spacing=8,
                    controls=[
                        ft.Row(
                            spacing=10,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            controls=[
                                ft.ProgressRing(
                                    width=24,
                                    height=24,
                                    stroke_width=2.2,
                                    semantics_label="正在执行任务",
                                ),
                                ft.Column(
                                    expand=True,
                                    spacing=2,
                                    controls=[message_control, detail_control],
                                ),
                            ],
                        ),
                        progress_control,
                        progress_text_control,
                    ],
                ),
            )
            controls = (
                card,
                message_control,
                detail_control,
                progress_control,
                progress_text_control,
            )
            activity_task_controls[task_id] = controls
            activity_tasks_column.controls.append(card)
        _, message_control, detail_control, progress_control, progress_text_control = controls
        message_control.value = message
        detail_control.value = detail
        progress_control.value = progress
        progress_text_control.value = progress_text
        update_task_center()
        task_ui_updates.request(
            task_center_panel, range_segment, refresh_button, immediate=True
        )

    def update_busy(
        task_id: str,
        message: str,
        detail: str | None = None,
        *,
        progress: float | None = None,
        progress_text: str | None = None,
    ) -> None:
        controls = activity_task_controls.get(task_id)
        if controls is None:
            show_busy(
                task_id,
                message,
                detail or "请稍候，不需要重复点击。",
                progress=progress,
                progress_text=progress_text or "",
            )
            return
        _, message_control, detail_control, progress_control, progress_text_control = controls
        message_control.value = message
        if detail is not None:
            detail_control.value = detail
        progress_control.value = progress
        if progress_text is not None:
            progress_text_control.value = progress_text
        update_task_center()
        task_ui_updates.request(
            task_center_panel, range_segment, refresh_button, immediate=False
        )

    def hide_busy(task_id: str) -> None:
        controls = activity_task_controls.pop(task_id, None)
        if controls is None:
            return
        card = controls[0]
        if card in activity_tasks_column.controls:
            activity_tasks_column.controls.remove(card)
        update_task_center()
        task_ui_updates.request(
            task_center_panel, range_segment, refresh_button, immediate=True
        )


    def show_status(
        message: str,
        *,
        duration_seconds: float = 4.0,
        failed: bool = False,
    ) -> None:
        nonlocal status_hide_task

        is_error = failed or "失败" in message or "错误" in message
        status.value = message
        status.color = DANGER if is_error else TEXT_PRIMARY
        status_panel.bgcolor = DANGER_SOFT if is_error else SURFACE
        status_panel.border = ft.Border.all(1, DANGER if is_error else PRIMARY_BORDER)
        status_panel.visible = True

        if status_hide_task is not None and not status_hide_task.done():
            status_hide_task.cancel()

        async def hide_later() -> None:
            try:
                await asyncio.sleep(7.0 if is_error else duration_seconds)
            except asyncio.CancelledError:
                return
            status_panel.visible = False
            try:
                page.update()
            except Exception:
                return

        status_hide_task = task_registry.create(hide_later(), name="status-hide")
        page.update()

    def notify_task(title_text: str, detail_text: str = "", *, failed: bool = False) -> None:
        if not current_settings.task_notifications:
            return
        message = title_text if not detail_text else f"{title_text} · {detail_text}"
        show_status(message, duration_seconds=5.0, failed=failed)

    def range_label(days: int) -> str:
        return {
            7: "最近 7 天",
            30: "最近 1 个月",
            90: "最近 3 个月",
            180: "最近 6 个月",
            365: "最近 1 年",
        }[days]

    def update_range_buttons() -> None:
        short_labels = {7: "7天", 30: "1月", 90: "3月", 180: "6月", 365: "1年"}
        for days, button in range_buttons.items():
            button.content = short_labels[days]
            button.style = _toolbar_segment_style(days == selected_days)
        if sync_jobs.is_active(active_domain):
            refresh_button.content = "同步中"
            refresh_button.disabled = True
        else:
            refresh_button.content = "同步"
            refresh_button.disabled = False

    def update_key_hint(settings: LLMSettings) -> None:
        api_key_field.hint_text = "已安全保存（不回显）" if settings.has_api_key else "输入 DeepSeek API Key"
        delete_key_button.disabled = not settings.has_api_key

    def state_for(item: RankedPaper) -> PaperUserState:
        return paper_states.get(
            item.paper.arxiv_id,
            PaperUserState(arxiv_id=item.paper.arxiv_id),
        )

    def browse_state_for(domain_key: str) -> DomainBrowseState:
        return domain_browse_states.setdefault(domain_key, DomainBrowseState())

    def remember_active_browse_state(*, scroll_offset: float | None = None) -> None:
        browse_state_for(active_domain).remember(
            loaded_count=len(current_result.papers),
            scroll_offset=scroll_offset,
            selected_arxiv_id=selected_arxiv_id,
        )

    def reset_active_browse_state() -> None:
        browse_state_for(active_domain).reset()

    async def restore_active_scroll_position() -> None:
        nonlocal restoring_domain_scroll
        target_offset = browse_state_for(active_domain).scroll_offset
        restoring_domain_scroll = True
        try:
            await papers_view.scroll_to(offset=target_offset, duration=0)
        finally:
            restoring_domain_scroll = False

    inspector_launcher = ft.UrlLauncher()

    def selected_item() -> RankedPaper | None:
        if selected_arxiv_id is None:
            return None
        for item in current_result.papers:
            if item.paper.arxiv_id == selected_arxiv_id:
                return item
        return None

    def inspector_header() -> ft.Row:
        return ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Text(
                    "论文快速解读",
                    size=18,
                    weight=ft.FontWeight.BOLD,
                    color=TEXT_PRIMARY,
                ),
                inspector_collapse_button,
            ],
        )

    def render_inspector() -> None:
        inspector_body.controls.clear()
        item = selected_item()
        if item is None:
            inspector_body.controls.extend(
                [
                    inspector_header(),
                    ft.Container(
                        padding=24,
                        bgcolor=SURFACE_SUBTLE,
                        border_radius=CARD_RADIUS,
                        content=ft.Column(
                            spacing=7,
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                            controls=[
                                ft.Text("选择一篇论文", weight=ft.FontWeight.BOLD, color=TEXT_PRIMARY),
                                ft.Text(
                                    "点击中间任意论文卡片，这里会显示快速解读。",
                                    size=FONT_META,
                                    color=TEXT_SECONDARY,
                                    text_align=ft.TextAlign.CENTER,
                                ),
                            ],
                        ),
                    ),
                ]
            )
            return

        state = state_for(item)
        analysis = analyses.get(item.paper.arxiv_id)
        tags = []
        for tag in (*item.interest_tags, *item.tags):
            if tag not in tags:
                tags.append(tag)

        async def open_abstract(_event=None) -> None:
            await inspector_launcher.launch_url(item.paper.abstract_url)

        async def open_pdf(_event=None) -> None:
            if item.paper.pdf_url:
                await inspector_launcher.launch_url(item.paper.pdf_url)

        async def toggle_current_favorite(_event=None) -> None:
            await toggle_favorite(item.paper.arxiv_id)

        async def analyze_current(_event=None) -> None:
            await run_deepseek_analysis([item], source_label="当前论文")
            render_inspector()
            page.update()

        def toggle_abstract(_event=None) -> None:
            arxiv_id = item.paper.arxiv_id
            if arxiv_id in expanded_abstract_ids:
                expanded_abstract_ids.remove(arxiv_id)
            else:
                expanded_abstract_ids.add(arxiv_id)
            render_inspector()
            page.update()

        def analysis_section(title: str, body: str) -> ft.Container:
            return ft.Container(
                padding=ft.Padding.only(top=4, bottom=5),
                content=ft.Column(
                    spacing=5,
                    controls=[
                        ft.Text(
                            title,
                            size=FONT_META,
                            weight=ft.FontWeight.BOLD,
                            color=TEXT_PRIMARY,
                        ),
                        ft.Text(body, size=FONT_BODY, color=TEXT_SECONDARY),
                    ],
                ),
            )

        inspector_body.controls.extend(
            [
                inspector_header(),
                ft.Text(
                    format_paper_title(item.paper.title),
                    size=FONT_DETAIL_TITLE,
                    weight=ft.FontWeight.BOLD,
                    color=TEXT_PRIMARY,
                ),
                ft.Row(spacing=7, wrap=True, controls=[_badge(tag) for tag in tags[:4]]),
                ft.Row(
                    spacing=8,
                    wrap=True,
                    controls=[
                        ft.OutlinedButton(
                            content="arXiv",
                            on_click=open_abstract,
                            style=_button_style("secondary", compact=True),
                        ),
                        ft.OutlinedButton(
                            content="PDF",
                            on_click=open_pdf,
                            disabled=not bool(item.paper.pdf_url),
                            style=_button_style("secondary", compact=True),
                        ),
                        ft.OutlinedButton(
                            content="★ 收藏" if state.is_favorite else "☆ 收藏",
                            on_click=toggle_current_favorite,
                            style=_button_style(
                                "soft" if state.is_favorite else "secondary", compact=True
                            ),
                        ),
                    ],
                ),
                ft.Divider(color=DIVIDER),
            ]
        )

        analysis_domain = (
            get_research_domain(analysis.domain_key)
            if analysis is not None
            else get_research_domain(active_domain)
        )
        ai_topic_labels = []
        if analysis is not None:
            ai_topic_labels = [
                topic.label
                for topic_key in analysis.topic_keys
                if (topic := analysis_domain.topic(topic_key)) is not None
            ]

        if analysis is None:
            inspector_body.controls.extend(
                [
                    ft.Text(
                        f"AI 快速解读 · {analysis_domain.label}",
                        size=FONT_SECTION,
                        weight=ft.FontWeight.BOLD,
                        color=TEXT_PRIMARY,
                    ),
                    ft.OutlinedButton(
                        content="DeepSeek 快速分析",
                        on_click=analyze_current,
                        style=_button_style("secondary"),
                    ),
                ]
            )
        elif analysis.is_quick_read:
            inspector_body.controls.extend(
                [
                    ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        controls=[
                            ft.Text(
                                f"AI 快速解读 · {analysis_domain.label}",
                                size=FONT_SECTION,
                                weight=ft.FontWeight.BOLD,
                                color=TEXT_PRIMARY,
                            ),
                            ft.OutlinedButton(
                                content="重新分析",
                                on_click=analyze_current,
                                style=_button_style("secondary", compact=True),
                            ),
                        ],
                    ),
                ]
            )
            if ai_topic_labels:
                inspector_body.controls.append(
                    ft.Text(
                        f"研究方向：{' · '.join(ai_topic_labels)}",
                        size=FONT_META,
                        color=PRIMARY_DARK,
                    )
                )
            inspector_body.controls.extend(
                [
                    analysis_section("一句话看懂", analysis.summary_cn),
                    analysis_section("解决什么问题", analysis.problem_cn),
                    analysis_section("提出什么方法", analysis.method_cn),
                    analysis_section("为什么值得关注", analysis.recommendation_cn),
                ]
            )
        else:
            inspector_body.controls.extend(
                [
                    ft.Text(
                        f"DeepSeek · {analysis_domain.label}",
                        size=FONT_SECTION,
                        weight=ft.FontWeight.BOLD,
                        color=TEXT_PRIMARY,
                    ),
                    ft.Text(
                        "这是旧版分析缓存。重新分析后会切换为新的 Title + Abstract 结构化快速解读。",
                        size=FONT_META,
                        color=TEXT_TERTIARY,
                    ),
                    analysis_section("旧版中文摘要", analysis.summary_cn),
                    analysis_section("旧版推荐理由", analysis.recommendation_cn),
                    ft.OutlinedButton(
                        content="重新快速分析",
                        on_click=analyze_current,
                        style=_button_style("secondary"),
                    ),
                ]
            )

        inspector_body.controls.append(ft.Divider(color=DIVIDER))
        abstract_expanded = item.paper.arxiv_id in expanded_abstract_ids
        inspector_body.controls.append(
            ft.Container(
                padding=ft.Padding.symmetric(horizontal=2, vertical=7),
                ink=True,
                ink_color="#12315F8C",
                on_click=toggle_abstract,
                content=ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.Text(
                            "原始摘要",
                            size=FONT_BODY,
                            weight=INTERACTIVE_FONT_WEIGHT,
                            color=TEXT_PRIMARY,
                        ),
                        ft.Text(
                            "收起" if abstract_expanded else "展开",
                            size=FONT_CONTROL,
                            color=PRIMARY_DARK,
                        ),
                    ],
                ),
            )
        )
        if abstract_expanded:
            inspector_body.controls.append(
                ft.Text(
                    item.paper.summary,
                    size=FONT_BODY,
                    color=TEXT_SECONDARY,
                    selectable=True,
                )
            )

    paper_card_controls: dict[str, ft.Container] = {}
    paper_card_values: dict[str, tuple] = {}
    paper_card_indexes: dict[str, int] = {}
    favorite_updates: set[str] = set()

    def active_browse_query() -> BrowseQuery:
        return make_browse_query(
            domain_key=active_domain,
            days=selected_days,
            topic_key=selected_topic_key,
            favorites_only=bool(favorites_only_checkbox.value),
            sort=sort_mode,
        )

    def _set_paper_card_selected(card: ft.Container, selected: bool) -> None:
        card.bgcolor = SURFACE_SUBTLE if selected else SURFACE
        card.border = ft.Border.all(
            1,
            PRIMARY_BORDER if selected else DIVIDER,
        )

    def select_paper(arxiv_id: str):
        def handler(_event=None) -> None:
            nonlocal selected_arxiv_id
            previous = selected_arxiv_id
            selected_arxiv_id = arxiv_id
            remember_active_browse_state()
            updates = []
            if previous and previous in paper_card_controls:
                card = paper_card_controls[previous]
                _set_paper_card_selected(card, False)
                updates.append(card)
            if arxiv_id in paper_card_controls:
                card = paper_card_controls[arxiv_id]
                _set_paper_card_selected(card, True)
                if all(control is not card for control in updates):
                    updates.append(card)
            render_inspector()
            if not inspector_collapsed:
                updates.append(inspector_body)
            if updates:
                page.update(*updates)

        return handler

    async def toggle_favorite(arxiv_id: str) -> None:
        nonlocal current_result, current_view_counts
        if arxiv_id in favorite_updates:
            return
        favorite_updates.add(arxiv_id)
        try:
            current = paper_states.get(arxiv_id, PaperUserState(arxiv_id=arxiv_id))
            saved = await asyncio.to_thread(
                library_service.set_favorite, arxiv_id, not current.is_favorite,
            )
            # Capture the CURRENT query after the write: the user may have
            # switched domains while persistence was in flight.
            query = active_browse_query()
            if requested_domain != active_domain:
                return
            index = paper_card_indexes.get(arxiv_id)
            if index is not None and index < len(current_result.papers):
                item = current_result.papers[index]
                if item.paper.arxiv_id == arxiv_id:
                    paper_states[arxiv_id] = saved
                    if query.favorites_only and not saved.is_favorite:
                        current_result = replace(
                            current_result,
                            papers=tuple(
                                paper for paper in current_result.papers
                                if paper.paper.arxiv_id != arxiv_id
                            ),
                        )
                        paper_states.pop(arxiv_id, None)
                        analyses.pop(arxiv_id, None)
                        render_papers()
                    else:
                        paper_list.controls[index] = make_paper_card(item)
            render_inspector()
            remember_active_browse_state()
            updates = [paper_list]
            if not inspector_collapsed:
                updates.append(inspector_body)
            page.update(*updates)

            # The persisted favorite is visible before the slower count query.
            try:
                counts = await asyncio.to_thread(browse_controller.counts, query)
            except Exception:
                show_status("收藏已保存，数量刷新失败，请稍后重试。", failed=True)
                return
            if query != active_browse_query() or requested_domain != active_domain:
                return
            current_view_counts = counts
            if query.favorites_only and not current_result.papers and counts.filtered:
                # Removing the last loaded favorite must still expose the next page.
                if not await reload_library_view(preserve_loaded=False):
                    return
                render_papers()
                render_inspector()
                refill_updates = [paper_list]
                if not inspector_collapsed:
                    refill_updates.append(inspector_body)
                page.update(*refill_updates)
            update_library_counts_ui()
            page.update(stats_row)
        finally:
            favorite_updates.discard(arxiv_id)

    def favorite_handler(arxiv_id: str):
        async def handler(_event) -> None:
            await toggle_favorite(arxiv_id)

        return handler

    def visible_papers() -> list[RankedPaper]:
        # ``current_result`` is already the SQL-filtered/sorted page set. Keeping
        # this helper preserves the call sites used by analysis/export actions
        # without re-sorting every loaded row on each render.
        return list(current_result.papers)

    def update_library_counts_ui() -> None:
        stats_row.controls = [
            _metric("本地", current_view_counts.total),
            _metric("当前", current_view_counts.filtered),
            _metric("收藏", current_view_counts.favorites),
            _metric("已分析", current_view_counts.analyzed),
        ]

    def make_paper_card(item: RankedPaper) -> ft.Container:
        arxiv_id = item.paper.arxiv_id
        values = (item, state_for(item), analyses.get(arxiv_id))
        cached = paper_card_controls.get(arxiv_id)
        if cached is not None and paper_card_values.get(arxiv_id) == values:
            _set_paper_card_selected(cached, arxiv_id == selected_arxiv_id)
            return cached
        card = _paper_card(
            item,
            state=state_for(item),
            analysis=analyses.get(item.paper.arxiv_id),
            on_toggle_favorite=favorite_handler(item.paper.arxiv_id),
            on_select=select_paper(item.paper.arxiv_id),
            selected=item.paper.arxiv_id == selected_arxiv_id,
        )
        assert isinstance(card, ft.Container)
        paper_card_controls[arxiv_id] = card
        paper_card_values[arxiv_id] = values
        return card

    def append_paper_cards(items: list[RankedPaper]) -> None:
        for item in items:
            paper_card_indexes[item.paper.arxiv_id] = len(paper_list.controls)
            paper_list.controls.append(make_paper_card(item))

    def render_papers() -> None:
        nonlocal selected_arxiv_id
        paper_list.controls.clear()
        paper_card_indexes.clear()
        visible_items = visible_papers()
        visible_ids = {item.paper.arxiv_id for item in visible_items}
        for stale_id in paper_card_controls.keys() - visible_ids:
            paper_card_controls.pop(stale_id, None)
            paper_card_values.pop(stale_id, None)
        if visible_items and selected_arxiv_id not in visible_ids:
            selected_arxiv_id = visible_items[0].paper.arxiv_id
        elif not visible_items:
            selected_arxiv_id = None

        remember_active_browse_state()
        update_library_counts_ui()

        if current_view_counts.total == 0:
            paper_list.controls.append(
                _empty_state(
                    "本地还没有论文",
                    f"当前查看{range_label(selected_days)}。同步只检查最新论文；扩大浏览范围时，如本地缺少更早内容会询问是否补充获取。",
                )
            )
            return
        if current_view_counts.filtered == 0:
            paper_list.controls.append(
                _empty_state(
                    "没有符合条件的论文",
                    "可以切换论文范围或研究方向后再看。",
                )
            )
            return

        append_paper_cards(visible_items)

    async def reload_library_view(
        *,
        fetched_count: int = 0,
        expected_domain: str | None = None,
        preserve_loaded: bool = True,
    ) -> bool:
        nonlocal current_result, paper_states, analyses, current_view_counts
        target_domain = active_domain
        target_days = selected_days
        target_topic = selected_topic_key
        target_favorites = bool(favorites_only_checkbox.value)
        target_sort = sort_mode
        target_limit = (
            max(library_page_size, len(current_result.papers))
            if preserve_loaded
            else library_page_size
        )
        if expected_domain is not None and target_domain != expected_domain:
            return False
        if requested_domain != target_domain:
            return False

        query = make_browse_query(
            domain_key=target_domain,
            days=target_days,
            topic_key=target_topic,
            favorites_only=target_favorites,
            sort=target_sort,
        )
        entries_task = asyncio.create_task(
            asyncio.to_thread(
                browse_controller.load_page,
                query,
                limit=target_limit,
            )
        )
        counts_task = asyncio.create_task(
            asyncio.to_thread(browse_controller.counts, query)
        )
        entries, counts = await asyncio.gather(entries_task, counts_task)
        # A user may switch domains/filters while the SQLite read is running.
        # Never let stale background work overwrite the newly selected view.
        if (
            active_domain != target_domain
            or selected_days != target_days
            or selected_topic_key != target_topic
            or bool(favorites_only_checkbox.value) != target_favorites
            or (sort_mode) != target_sort
            or requested_domain != target_domain
        ):
            return False
        current_view_counts = counts
        current_result = RadarResult(
            fetched_count=fetched_count,
            papers=tuple(entry.ranked for entry in entries),
        )
        paper_states = {entry.ranked.paper.arxiv_id: entry.state for entry in entries}
        analyses = {
            entry.ranked.paper.arxiv_id: entry.analysis
            for entry in entries
            if entry.analysis is not None
        }
        return True

    async def load_more_library_view() -> list[RankedPaper]:
        nonlocal current_result, paper_states, analyses
        target_domain = active_domain
        target_days = selected_days
        target_topic = selected_topic_key
        target_favorites = bool(favorites_only_checkbox.value)
        target_sort = sort_mode
        offset = len(current_result.papers)
        if offset >= current_view_counts.filtered:
            return []
        query = make_browse_query(
            domain_key=target_domain,
            days=target_days,
            topic_key=target_topic,
            favorites_only=target_favorites,
            sort=target_sort,
        )
        entries = await asyncio.to_thread(
            browse_controller.load_page,
            query,
            limit=library_page_size,
            offset=offset,
        )
        if (
            active_domain != target_domain
            or selected_days != target_days
            or selected_topic_key != target_topic
            or bool(favorites_only_checkbox.value) != target_favorites
            or (sort_mode) != target_sort
            or requested_domain != target_domain
        ):
            return []
        existing_ids = {item.paper.arxiv_id for item in current_result.papers}
        fresh_entries = [
            entry for entry in entries if entry.ranked.paper.arxiv_id not in existing_ids
        ]
        if not fresh_entries:
            return []
        new_items = [entry.ranked for entry in fresh_entries]
        current_result = RadarResult(
            fetched_count=current_result.fetched_count,
            papers=(*current_result.papers, *new_items),
        )
        browse_state_for(target_domain).remember(
            loaded_count=len(current_result.papers),
            selected_arxiv_id=selected_arxiv_id,
        )
        for entry in fresh_entries:
            arxiv_id = entry.ranked.paper.arxiv_id
            paper_states[arxiv_id] = entry.state
            if entry.analysis is not None:
                analyses[arxiv_id] = entry.analysis
        return new_items

    async def run_deepseek_analysis(
        targets: tuple[RankedPaper, ...] | list[RankedPaper] | None = None,
        *,
        source_label: str = "当前筛选结果",
        domain_key: str | None = None,
        background: bool = False,
    ) -> None:
        nonlocal analyses, current_settings

        analysis_domain = domain_key or active_domain
        analysis_profile = get_research_domain(analysis_domain)
        analysis_targets = list(targets) if targets is not None else visible_papers()
        if not analysis_targets:
            if not background or active_domain == analysis_domain:
                show_status(f"当前没有可供 DeepSeek 分析的{analysis_profile.label}论文。")
            return

        current_settings = await settings_service.load()
        api_key = await settings_service.get_api_key()
        if not api_key:
            if not background or active_domain == analysis_domain:
                show_status("尚未设置 DeepSeek API Key，请先进入设置。")
                if not background:
                    analyze_button.disabled = True
            return

        selected_targets = analysis_targets[:20]
        analysis_task_id = (
            f"deepseek:{analysis_domain}:{'background' if background else 'manual'}"
        )
        if not background:
            analyze_button.disabled = True
            show_status(
                f"正在用 {current_settings.model} 按{analysis_profile.label}视角分析 "
                f"{source_label}的前 {len(selected_targets)} 篇论文……"
            )
            show_busy(
                analysis_task_id,
                f"正在分析 · {analysis_profile.label}",
                f"{source_label} · 模型：{current_settings.model} · "
                f"本次 {len(selected_targets)} 篇",
            )
        else:
            show_busy(
                analysis_task_id,
                f"后台分析 · {analysis_profile.label}",
                f"DeepSeek · {len(selected_targets)} 篇候选论文",
            )
            if active_domain == analysis_domain:
                show_status(
                    f"{analysis_profile.label}同步已完成；DeepSeek 正在后台分析 "
                    f"{len(selected_targets)} 篇候选论文。"
                )

        analysis_completed_count = 0
        try:
            client = DeepSeekClient(api_key=api_key, model=current_settings.model)

            def publish_analysis_progress(done: int, total: int) -> None:
                nonlocal analysis_completed_count
                analysis_completed_count = done
                update_busy(
                    analysis_task_id,
                    (
                        f"后台分析 · {analysis_profile.label}"
                        if background
                        else f"正在分析 · {analysis_profile.label}"
                    ),
                    f"{source_label} · 模型：{current_settings.model}",
                    progress=(done / total) if total else 1.0,
                    progress_text=f"{done}/{total} 篇 · 分批完成即保存",
                )

            new_analyses = await analysis_service.analyze_and_persist(
                client,
                selected_targets,
                model=current_settings.model,
                domain_key=analysis_domain,
                batch_size=8,
                progress_callback=publish_analysis_progress,
            )
            if active_domain == analysis_domain:
                await reload_library_view(expected_domain=analysis_domain)
                render_papers()
                render_inspector()
                show_status(
                    f"DeepSeek 已按{analysis_profile.label}视角分析并缓存 "
                    f"{len(new_analyses)} 篇论文。"
                )
            notify_task(
                f"{analysis_profile.label} DeepSeek 分析完成",
                f"已分析并缓存 {len(new_analyses)} 篇论文",
            )
        except DeepSeekError as exc:
            if not background or active_domain == analysis_domain:
                show_status(
                    f"DeepSeek 分析失败：{exc}。"
                    + (
                        f"已完成的 {analysis_completed_count} 篇已保存。"
                        if analysis_completed_count
                        else ""
                    ),
                    failed=True,
                )
            notify_task(
                "DeepSeek 分析失败",
                str(exc)
                + (
                    f" · 已完成 {analysis_completed_count} 篇已保存"
                    if analysis_completed_count
                    else ""
                ),
                failed=True,
            )
        except Exception as exc:
            if not background or active_domain == analysis_domain:
                show_status(
                    f"DeepSeek 分析发生未预期错误：{type(exc).__name__}: {exc}",
                    failed=True,
                )
            notify_task("DeepSeek 分析失败", f"{type(exc).__name__}: {exc}", failed=True)
        finally:
            hide_busy(analysis_task_id)
            if not background:
                analyze_button.disabled = (
                    not current_settings.has_api_key or not current_result.papers
                )

    def update_task_center() -> None:
        nonlocal task_center_expanded
        active_domains = tuple(sync_jobs.active_domains)
        active_set = set(active_domains)
        activity_count = len(activity_task_controls)
        total_tasks = activity_count + len(active_domains)
        if total_tasks == 0:
            task_center_expanded = False

        for domain_key, card in sync_job_cards.items():
            card.visible = domain_key in active_set

        task_center_panel.visible = total_tasks > 0
        task_center_detail_column.visible = total_tasks > 0 and task_center_expanded
        task_center_expand_button.content = "收起" if task_center_expanded else "展开"

        if total_tasks == 1 and activity_count == 1:
            only_controls = next(iter(activity_task_controls.values()))
            task_center_summary.value = only_controls[1].value or "正在执行任务"
        elif total_tasks == 1 and active_domains:
            domain_key = active_domains[0]
            detail = sync_job_details[domain_key].value or "正在同步……"
            task_center_summary.value = f"{DOMAIN_LABELS[domain_key]}同步 · {detail}"
        elif total_tasks > 1:
            sync_count = len(active_domains)
            detail_parts = []
            if sync_count:
                detail_parts.append(f"同步 {sync_count}")
            if activity_count:
                detail_parts.append(f"其他 {activity_count}")
            task_center_summary.value = f"{total_tasks} 个任务进行中 · {' / '.join(detail_parts)}"
        else:
            task_center_summary.value = ""
        update_range_buttons()

    def update_sync_jobs_panel() -> None:
        # Compatibility name used by the existing sync orchestration. All running
        # work now feeds one compact Sidebar task center.
        update_task_center()

    def toggle_task_center(_event=None) -> None:
        nonlocal task_center_expanded
        task_center_expanded = not task_center_expanded
        update_task_center()
        task_ui_updates.request(
            task_center_panel, range_segment, refresh_button, immediate=True
        )

    task_center_expand_button.on_click = toggle_task_center

    def update_sync_job_ui(
        domain_key: str,
        *,
        detail: str,
        progress: float | None,
        progress_text: str,
    ) -> None:
        cancel_event = sync_jobs.cancel_event(domain_key)
        if cancel_event is None or cancel_event.is_set():
            return
        sync_job_details[domain_key].value = detail
        sync_job_progress[domain_key].value = progress
        sync_job_progress_texts[domain_key].value = progress_text
        sync_job_cancel_buttons[domain_key].disabled = False
        sync_job_cancel_buttons[domain_key].content = "停止"
        update_sync_jobs_panel()
        task_ui_updates.request(
            task_center_panel, range_segment, refresh_button, immediate=False
        )

    def request_sync_cancel(domain_key: str) -> None:
        if not sync_jobs.cancel(domain_key):
            return
        sync_job_details[domain_key].value = "正在安全停止；当前网络请求结束后保存检查点……"
        sync_job_cancel_buttons[domain_key].disabled = True
        sync_job_cancel_buttons[domain_key].content = "正在停止…"
        if domain_key == active_domain:
            show_status(
                f"正在停止{DOMAIN_LABELS[domain_key]}同步；已完成的数据会保留，"
                "未完成日期不会标记为已覆盖。"
            )
        update_sync_jobs_panel()
        task_ui_updates.request(
            task_center_panel, range_segment, refresh_button, immediate=True
        )

    async def perform_sync(
        *,
        source_label: str,
        domain_key: str,
        backfill_days: int | None = None,
    ) -> bool:
        nonlocal current_settings, sync_state

        sync_domain = domain_key
        sync_profile = get_research_domain(sync_domain)
        sync_label = sync_profile.label
        cancel_event = sync_jobs.cancel_event(sync_domain)
        if cancel_event is None:
            return False

        total_fetched = 0
        candidate_by_id: dict[str, RankedPaper] = {}
        completed_steps = 0
        sync_steps = 1

        def publish_sync_state(state) -> None:
            nonlocal sync_state
            if sync_domain == active_domain:
                sync_state = state

        async def refresh_target_if_visible(*, fetched_count: int = 0) -> None:
            if sync_domain != active_domain:
                return
            refreshed = await reload_library_view(
                fetched_count=fetched_count,
                expected_domain=sync_domain,
            )
            if not refreshed:
                return
            render_papers()
            render_inspector()
            page.update()

        try:
            today = datetime.now(UTC).date()
            if backfill_days is None:
                sync_plan = await asyncio.to_thread(
                    sync_coordinator.plan_incremental,
                    domain_key=sync_domain,
                    today=today,
                )
                target_range_text = "最新增量 · 检查至今天"
            else:
                desired_start = (datetime.now(UTC) - timedelta(days=backfill_days)).date()
                sync_plan = await asyncio.to_thread(
                    sync_coordinator.plan_backfill,
                    domain_key=sync_domain,
                    desired_start=desired_start,
                    today=today,
                )
                target_range_text = (
                    f"补充获取 {range_label(backfill_days)} · "
                    f"{desired_start.isoformat()} ～ {today.isoformat()}"
                )
            target_sync_state = sync_plan.state
            intervals = sync_plan.intervals
            sync_steps = max(len(intervals), 1)

            async with sync_write_lock:
                target_sync_state = await asyncio.to_thread(
                    sync_coordinator.begin, domain_key=sync_domain
                )
            publish_sync_state(target_sync_state)
            update_sync_job_ui(
                sync_domain,
                detail=f"{source_label} · {target_range_text} · 准备连接 arXiv",
                progress=None,
                progress_text=f"0/{sync_steps} 个同步块",
            )
            if sync_domain == active_domain:
                show_status(
                    f"{sync_label}同步已在后台开始；你可以继续浏览、筛选、收藏、导出或分析论文。"
                )

            async def sync_interval_with_live_progress(index: int, interval) -> RadarResult:
                loop = asyncio.get_running_loop()
                progress_queue: asyncio.Queue[ArxivHarvestProgress] = asyncio.Queue()

                def publish_progress(event: ArxivHarvestProgress) -> None:
                    loop.call_soon_threadsafe(progress_queue.put_nowait, event)

                task = asyncio.create_task(
                    radar_service.sync_range_async(
                        from_date=interval.from_date,
                        until_date=interval.until_date,
                        domain_key=sync_domain,
                        cancel_event=cancel_event,
                        progress_callback=publish_progress,
                    )
                )

                while not task.done() or not progress_queue.empty():
                    try:
                        event = await asyncio.wait_for(progress_queue.get(), timeout=0.2)
                    except TimeoutError:
                        continue

                    category_total = max(event.category_count, 1)
                    category_fraction = event.completed_categories / category_total
                    overall = min(
                        1.0,
                        max(0.0, (completed_steps + category_fraction) / sync_steps),
                    )
                    progress_value = overall if overall > 0 else None
                    page_label = f"第 {event.page_number} 页" if event.page_number else "准备中"
                    activity = (
                        f"{event.category} · {page_label} · 已读取 {event.fetched_count} 篇"
                    )
                    update_sync_job_ui(
                        sync_domain,
                        detail=activity,
                        progress=progress_value,
                        progress_text=(
                            f"同步块 {index}/{sync_steps} · "
                            f"分类 {event.category_index}/{category_total}"
                        ),
                    )
                    # Live sync progress belongs in the non-modal task card.
                    # Do not turn every OAI page update into a transient toast.

                return await task

            cancelled = False
            for index, interval in enumerate(intervals, start=1):
                if cancel_event.is_set():
                    cancelled = True
                    break

                completed_before = completed_steps / sync_steps
                update_sync_job_ui(
                    sync_domain,
                    detail=(
                        f"{interval.from_date.isoformat()} ～ {interval.until_date.isoformat()} "
                        "· 正在连接 arXiv"
                    ),
                    progress=completed_before if completed_before > 0 else None,
                    progress_text=f"同步块 {index}/{sync_steps}",
                )

                try:
                    result = await sync_interval_with_live_progress(index, interval)
                except ArxivSyncCancelled:
                    cancelled = True
                    break

                total_fetched += result.fetched_count
                for item in result.papers:
                    previous = candidate_by_id.get(item.paper.arxiv_id)
                    if previous is None or item.paper.updated_at > previous.paper.updated_at:
                        candidate_by_id[item.paper.arxiv_id] = item

                # Network harvesting runs concurrently across domains. SQLite writes
                # are intentionally short and serialized so cross-domain jobs never
                # compete for the single writer slot.
                async with sync_write_lock:
                    await asyncio.to_thread(
                        library_service.remember,
                        result.papers,
                        domain_key=sync_domain,
                        topic_keys_by_arxiv_id=result.topic_keys_by_arxiv_id,
                        classification_source="domain-rule-v1",
                    )
                    target_sync_state = await asyncio.to_thread(
                        sync_coordinator.checkpoint,
                        from_date=interval.from_date,
                        until_date=interval.until_date,
                        domain_key=sync_domain,
                    )
                publish_sync_state(target_sync_state)
                completed_steps += 1
                completed = completed_steps / sync_steps
                await refresh_target_if_visible(fetched_count=total_fetched)
                update_sync_job_ui(
                    sync_domain,
                    detail=(
                        f"已保存 {interval.from_date.isoformat()} ～ "
                        f"{interval.until_date.isoformat()}"
                    ),
                    progress=completed,
                    progress_text=f"已完成 {completed_steps}/{sync_steps} 个同步块",
                )

            if cancelled or cancel_event.is_set():
                async with sync_write_lock:
                    target_sync_state = await asyncio.to_thread(
                        sync_coordinator.cancel, domain_key=sync_domain
                    )
                publish_sync_state(target_sync_state)
                await refresh_target_if_visible(fetched_count=total_fetched)
                if sync_domain == active_domain:
                    show_status(
                        f"{sync_label}同步已停止：已安全保存 {completed_steps}/{sync_steps} 个同步块；"
                        "下次会从缺口继续。"
                    )
                notify_task(
                    f"{sync_label} arXiv 同步已停止",
                    f"已安全保存 {completed_steps}/{sync_steps} 个同步块，下次可继续",
                )
                return False

            async with sync_write_lock:
                target_sync_state = await asyncio.to_thread(
                    sync_coordinator.finish, domain_key=sync_domain
                )
            publish_sync_state(target_sync_state)
            latest_refresh_candidates = tuple(
                sorted(
                    candidate_by_id.values(),
                    key=lambda item: (item.interest_score, item.score, item.paper.updated_at),
                    reverse=True,
                )
            )
            await refresh_target_if_visible(fetched_count=total_fetched)
            completion_text = (
                f"{sync_label}同步完成：扫描 {total_fetched} 篇元数据，本轮筛出 "
                f"{len(latest_refresh_candidates)} 篇候选。"
            )
            if sync_domain == active_domain:
                show_status(completion_text)
            notify_task(
                f"{sync_label} arXiv 同步完成",
                f"扫描 {total_fetched} 篇，本轮筛出 {len(latest_refresh_candidates)} 篇候选",
            )
            current_settings = await settings_service.load()
            if (
                latest_refresh_candidates
                and current_settings.has_api_key
                and current_settings.auto_analyze
            ):
                # The sync job is already complete at this point. Start automatic
                # analysis as its own background task so the domain sync card can
                # close immediately and a new sync is not held hostage by DeepSeek.
                task_registry.create(
                    run_deepseek_analysis(
                        latest_refresh_candidates,
                        source_label=f"{sync_label}本轮同步候选",
                        domain_key=sync_domain,
                        background=True,
                    ),
                    name=f"deepseek-auto:{sync_domain}",
                )
            return True
        except ArxivError as exc:
            async with sync_write_lock:
                target_sync_state = await asyncio.to_thread(
                    sync_coordinator.fail, str(exc), domain_key=sync_domain
                )
            publish_sync_state(target_sync_state)
            diagnostic_suffix = (
                f" 诊断日志：{arxiv_client.last_diagnostic_log_path}"
                if arxiv_client.last_diagnostic_log_path
                else ""
            )
            if sync_domain == active_domain:
                show_status(
                    f"{sync_label}同步失败：{exc}。已完成的同步块仍保留。{diagnostic_suffix}",
                    failed=True,
                )
            notify_task(f"{sync_label} arXiv 同步失败", str(exc), failed=True)
            return False
        except Exception as exc:
            async with sync_write_lock:
                target_sync_state = await asyncio.to_thread(
                    sync_coordinator.fail,
                    f"{type(exc).__name__}: {exc}",
                    domain_key=sync_domain,
                )
            publish_sync_state(target_sync_state)
            if sync_domain == active_domain:
                show_status(
                    f"{sync_label}同步发生未预期错误：{type(exc).__name__}: {exc}",
                    failed=True,
                )
            notify_task(
                f"{sync_label} arXiv 同步失败",
                f"{type(exc).__name__}: {exc}",
                failed=True,
            )
            return False
        finally:
            sync_jobs.finish(sync_domain)
            sync_tasks.pop(sync_domain, None)
            sync_job_cards[sync_domain].visible = False
            sync_job_progress[sync_domain].value = None
            sync_job_progress_texts[sync_domain].value = ""
            sync_job_cancel_buttons[sync_domain].disabled = False
            sync_job_cancel_buttons[sync_domain].content = "停止"
            update_sync_jobs_panel()
            task_ui_updates.request(
                task_center_panel, range_segment, refresh_button, immediate=True
            )
            if sync_domain == active_domain:
                try:
                    sync_state = await asyncio.to_thread(
                        library_service.sync_state, domain_key=sync_domain
                    )
                except Exception:
                    pass

    def start_sync_job(
        *,
        domain_key: str,
        source_label: str,
        backfill_days: int | None = None,
    ) -> bool:
        cancel_event = sync_jobs.start(domain_key)
        if cancel_event is None:
            if domain_key == active_domain:
                show_status(f"{DOMAIN_LABELS[domain_key]}已经在后台同步，无需重复启动。")
            return False

        sync_job_cards[domain_key].visible = True
        sync_job_details[domain_key].value = (
            f"补充获取 {range_label(backfill_days)} · 等待后台任务启动……"
            if backfill_days is not None
            else "最新增量 · 等待后台任务启动……"
        )
        sync_job_progress[domain_key].value = None
        sync_job_progress_texts[domain_key].value = "准备中"
        sync_job_cancel_buttons[domain_key].disabled = False
        sync_job_cancel_buttons[domain_key].content = "停止"
        update_sync_jobs_panel()
        task = task_registry.create(
            perform_sync(
                source_label=source_label,
                domain_key=domain_key,
                backfill_days=backfill_days,
            ),
            name=f"sync:{domain_key}",
        )
        sync_tasks[domain_key] = task
        task_ui_updates.request(
            task_center_panel, range_segment, refresh_button, immediate=True
        )
        return True

    async def sync_arxiv(_event=None) -> None:
        start_sync_job(
            domain_key=active_domain,
            source_label="手动同步",
        )

    async def maybe_offer_historical_fetch(
        *,
        domain_key: str,
        previous_days: int,
        new_days: int,
    ) -> None:
        if new_days <= previous_days:
            return
        today = datetime.now(UTC).date()
        desired_start = cutoff_for_days(new_days).date()
        gap = await asyncio.to_thread(
            sync_coordinator.historical_gap,
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
            start_sync_job(
                domain_key=domain_key,
                source_label="时间范围补充获取",
                backfill_days=new_days,
            )

        page.show_dialog(
            ft.AlertDialog(
                modal=True,
                bgcolor=SURFACE,
                shape=ft.RoundedRectangleBorder(radius=16),
                title=ft.Text(
                    "补充更早的论文？",
                    size=18,
                    weight=ft.FontWeight.BOLD,
                    color=TEXT_PRIMARY,
                ),
                content=ft.Text(
                    f"你选择了{range_label(new_days)}。本地尚未覆盖 "
                    f"{gap.from_date.isoformat()} 至 {gap.until_date.isoformat()} 的较早时间段，"
                    "其中可能有尚未下载的论文。是否现在补充获取？",
                    size=FONT_BODY,
                    color=TEXT_SECONDARY,
                ),
                actions=[
                    ft.TextButton(
                        content="暂不获取",
                        on_click=dismiss,
                        style=_button_style("ghost", compact=True),
                    ),
                    ft.FilledButton(
                        content="补充获取",
                        on_click=fetch_missing,
                        style=_button_style("primary", compact=True),
                    ),
                ],
                actions_alignment=ft.MainAxisAlignment.END,
            )
        )

    async def set_time_range(days: int) -> None:
        nonlocal selected_days, sync_state, selected_arxiv_id
        previous_days = selected_days
        range_domain = active_domain
        selected_days = days
        reset_active_browse_state()
        selected_arxiv_id = None
        update_range_buttons()
        sync_state = await asyncio.to_thread(
            library_service.sync_state, domain_key=active_domain
        )
        await reload_library_view(preserve_loaded=False)
        render_papers()
        render_inspector()
        show_status(
            f"已切换到{range_label(selected_days)}；这是本地浏览范围。"
            "普通同步只检查最新论文；如果较早时间段尚未获取，会询问是否补充。"
        )
        analyze_button.disabled = (
            not current_settings.has_api_key or not current_result.papers
        )
        await persist_view_preferences()
        page.update()
        await restore_active_scroll_position()
        await maybe_offer_historical_fetch(
            domain_key=range_domain,
            previous_days=previous_days,
            new_days=days,
        )

    def range_handler(days: int):
        async def handler(_event) -> None:
            await set_time_range(days)

        return handler

    async def export_literature(file_format: str) -> None:
        # Capture query and metadata before the first await; browsing remains free
        # to change while the worker reads and builds this export.
        export_days = selected_days
        query = make_browse_query(
            domain_key=active_domain,
            days=export_days,
            topic_key=selected_topic_key,
            favorites_only=bool(favorites_only_checkbox.value),
            sort=sort_mode,
        )
        export_domain = get_research_domain(query.domain_key)
        export_topic = export_domain.topic(query.topic_key)
        export_context = ExportContext(
            range_label=range_label(export_days),
            exported_at=datetime.now(UTC),
            domain_key=export_domain.key,
            domain_label=export_domain.label,
            scope_label="收藏" if query.favorites_only else "全部论文",
            topic_label=export_topic.label if export_topic is not None else "全部方向",
            sort_label=SORT_LABELS.get(query.sort_mode, SORT_LABELS[SORT_INTEREST]),
        )
        range_stamp = {
            7: "7d",
            30: "1m",
            90: "3m",
            180: "6m",
            365: "1y",
        }[export_days]
        await run_export(
            file_format,
            ExportRequest(query, export_context, range_stamp),
            browse_controller=browse_controller,
            page=page,
            show_busy=show_busy,
            hide_busy=hide_busy,
            show_status=show_status,
            notify_task=notify_task,
        )

    async def export_excel(_event=None) -> None:
        await export_literature("xlsx")

    async def export_markdown(_event=None) -> None:
        await export_literature("md")

    async def analyze(_event=None) -> None:
        await run_deepseek_analysis()

    async def save_settings(_event=None) -> None:
        nonlocal current_settings, auto_sync_domain_keys

        model = model_dropdown.value or current_settings.model
        api_key = api_key_field.value.strip() if api_key_field.value else None
        selected_auto_sync_domains = research_domain_keys()
        try:
            current_settings = await settings_service.save(
                model=model,
                auto_analyze=bool(auto_analyze_switch.value),
                auto_sync_on_start=bool(auto_sync_on_start_switch.value),
                auto_sync_domain_keys=selected_auto_sync_domains,
                task_notifications=bool(task_notifications_switch.value),
                api_key=api_key,
            )
            auto_sync_domain_keys = selected_auto_sync_domains
            api_key_field.value = ""
            update_key_hint(current_settings)
            analyze_button.disabled = (
                not current_settings.has_api_key or not current_result.papers
            )
            selected_labels = "、".join(DOMAIN_LABELS[key] for key in auto_sync_domain_keys)
            sync_summary = (
                f"自动同步：{selected_labels}（启动时 + 每晚 22:00）"
                if current_settings.auto_sync_on_start
                else "自动同步：已关闭"
            )
            show_status(f"设置已保存。{sync_summary}。API Key 存在系统安全存储中。")
        except Exception as exc:
            show_status(f"保存失败：{type(exc).__name__}: {exc}", failed=True)
        page.update()

    async def test_connection(_event=None) -> None:
        model = model_dropdown.value or current_settings.model
        typed_key = api_key_field.value.strip() if api_key_field.value else ""
        api_key = typed_key or await settings_service.get_api_key()
        if not api_key:
            show_status("请先输入或保存 DeepSeek API Key。", failed=True)
            return

        test_connection_button.disabled = True
        connection_task_id = "deepseek:test-connection"
        show_busy(connection_task_id, "正在测试 DeepSeek 连接", f"模型：{model}")
        try:
            client = DeepSeekClient(api_key=api_key, model=model)
            await asyncio.to_thread(client.test_connection)
            show_status("DeepSeek 连接成功。")
        except DeepSeekError as exc:
            show_status(f"DeepSeek 连接失败：{exc}", failed=True)
        except Exception as exc:
            show_status(
                f"连接测试发生未预期错误：{type(exc).__name__}: {exc}",
                failed=True,
            )
        finally:
            test_connection_button.disabled = False
            hide_busy(connection_task_id)

    async def delete_key(_event=None) -> None:
        nonlocal current_settings

        try:
            if await settings_service.get_api_key() is None:
                delete_key_button.disabled = True
                show_status("当前没有已保存的 DeepSeek API Key。")
                page.update()
                return
            current_settings = await settings_service.delete_api_key()
            api_key_field.value = ""
            update_key_hint(current_settings)
            analyze_button.disabled = True
            show_status("DeepSeek API Key 已从本机安全存储删除。")
        except Exception as exc:
            show_status(f"删除失败：{type(exc).__name__}: {exc}", failed=True)
        page.update()

    def update_sidebar_selection() -> None:
        # “论文范围”和“研究方向”是两个独立筛选维度。
        # 研究方向来自当前 ResearchDomain 配置，不再由 UI 硬编码。
        _set_sidebar_item(
            library_all_button,
            text=f"全部论文  {current_view_counts.total:,}",
            selected=not bool(favorites_only_checkbox.value),
        )
        _set_sidebar_item(
            library_favorites_button,
            text=f"收藏  {current_view_counts.favorites:,}",
            selected=bool(favorites_only_checkbox.value),
        )

        _set_sidebar_item(
            direction_all_button,
            text="全部方向",
            selected=selected_topic_key == TOPIC_ALL,
        )
        profile = get_research_domain(active_domain)
        topics_by_key = {topic.key: topic for topic in profile.topics}
        for topic_key, button in direction_buttons.items():
            topic = topics_by_key[topic_key]
            _set_sidebar_item(
                button,
                text=topic.label,
                selected=selected_topic_key == topic_key,
            )

    async def load_domain_view_preferences_cached(domain_key: str) -> ViewPreferences:
        cached = domain_view_preferences_cache.get(domain_key)
        if cached is not None:
            return cached

        task = domain_view_preference_tasks.get(domain_key)
        if task is None:
            task = asyncio.create_task(
                settings_service.load_domain_view_preferences(domain_key)
            )
            domain_view_preference_tasks[domain_key] = task
        try:
            loaded = await task
            domain_view_preferences_cache[domain_key] = loaded
            return loaded
        finally:
            if domain_view_preference_tasks.get(domain_key) is task:
                domain_view_preference_tasks.pop(domain_key, None)

    async def persist_view_preferences() -> None:
        try:
            saved = await settings_service.save_view_preferences(
                active_domain=active_domain,
                days=selected_days,
                favorites_only=bool(favorites_only_checkbox.value),
                focus_tag=selected_topic_key,
                sort_mode=sort_mode,
            )
            domain_view_preferences_cache[active_domain] = saved
        except Exception:
            # Preference persistence must never block normal browsing.
            pass

    async def apply_filter(_event=None) -> None:
        nonlocal selected_arxiv_id
        reset_active_browse_state()
        selected_arxiv_id = None
        update_sidebar_selection()
        await reload_library_view(preserve_loaded=False)
        render_papers()
        render_inspector()
        await persist_view_preferences()
        page.update()
        await restore_active_scroll_position()

    async def select_library_all(_event=None) -> None:
        favorites_only_checkbox.value = False
        await apply_filter()

    async def select_library_favorites(_event=None) -> None:
        favorites_only_checkbox.value = True
        await apply_filter()

    def select_focus(topic_key: str):
        async def handler(_event=None) -> None:
            nonlocal selected_topic_key
            selected_topic_key = normalize_topic_key(active_domain, topic_key)
            await apply_filter()

        return handler

    def rebuild_direction_sidebar() -> None:
        profile = get_research_domain(active_domain)
        direction_title.value = "研究方向"
        library_scope_section.visible = profile.data_enabled
        sidebar_scope_divider.visible = profile.data_enabled

        direction_buttons.clear()
        controls: list[ft.Control] = [direction_all_button]
        direction_all_button.on_click = select_focus(TOPIC_ALL)
        for topic in profile.topics:
            button = _sidebar_item(topic.label, width=sidebar_item_width)
            button.on_click = select_focus(topic.key)
            direction_buttons[topic.key] = button
            controls.append(button)
        direction_items_column.controls = controls
        update_sidebar_selection()

    def update_domain_selector(selected_domain: str) -> None:
        for domain in RESEARCH_DOMAINS:
            button = domain_buttons[domain.key]
            _set_domain_segment(
                button,
                text=domain.label,
                selected=domain.key == selected_domain,
            )

    def update_domain_ui() -> None:
        update_domain_selector(active_domain)
        rebuild_direction_sidebar()
        update_library_counts_ui()
        update_range_buttons()

    def update_library_surface_visibility() -> None:
        if settings_shell.visible:
            workspace_view.visible = False
            return

        workspace_view.visible = True
        center_panel.content = papers_surface
        inspector_panel.visible = True

    def select_domain(domain_key: str):
        async def handler(_event=None) -> None:
            nonlocal active_domain, requested_domain, domain_switch_generation
            nonlocal selected_topic_key, selected_days, sync_state, selected_arxiv_id
            nonlocal current_result, paper_states, analyses, current_view_counts, sort_mode
            if domain_key not in DOMAIN_LABELS:
                return

            # Snapshot the domain we are leaving before the shared scrollable is
            # repopulated. Its loaded-page count, scroll offset and selected paper
            # must not leak into the target domain.
            remember_active_browse_state()

            # Give the click immediate visual feedback. The expensive part below
            # only restores the target domain's own materialized page count plus
            # aggregate counts.
            domain_switch_generation += 1
            switch_generation = domain_switch_generation
            requested_domain = domain_key
            update_domain_selector(domain_key)
            page.update()
            await asyncio.sleep(0)

            if domain_key == active_domain:
                return

            try:
                target_preferences = await load_domain_view_preferences_cached(domain_key)
                if (
                    switch_generation != domain_switch_generation
                    or requested_domain != domain_key
                ):
                    return

                target_days = target_preferences.days
                target_topic = normalize_topic_key(
                    domain_key, target_preferences.focus_tag
                )
                target_favorites = target_preferences.favorites_only
                target_sort = target_preferences.sort_mode
                target_browse_state = browse_state_for(domain_key)
                target_limit = target_browse_state.load_limit(library_page_size)
                target_query = make_browse_query(
                    domain_key=domain_key,
                    days=target_days,
                    topic_key=target_topic,
                    favorites_only=target_favorites,
                    sort=target_sort,
                )
                entries_task = asyncio.create_task(
                    asyncio.to_thread(
                        browse_controller.load_page,
                        target_query,
                        limit=target_limit,
                    )
                )
                counts_task = asyncio.create_task(
                    asyncio.to_thread(browse_controller.counts, target_query)
                )
                sync_state_task = asyncio.create_task(
                    asyncio.to_thread(library_service.sync_state, domain_key=domain_key)
                )
                entries, target_counts, target_sync_state = await asyncio.gather(
                    entries_task, counts_task, sync_state_task
                )
                if (
                    switch_generation != domain_switch_generation
                    or requested_domain != domain_key
                ):
                    return

                active_domain = domain_key
                selected_days = target_days
                selected_topic_key = target_topic
                favorites_only_checkbox.value = target_favorites
                sort_mode = target_sort
                sync_sort_selector_ui()
                sync_state = target_sync_state
                current_view_counts = target_counts
                current_result = RadarResult(
                    fetched_count=0,
                    papers=tuple(entry.ranked for entry in entries),
                )
                paper_states = {
                    entry.ranked.paper.arxiv_id: entry.state for entry in entries
                }
                analyses = {
                    entry.ranked.paper.arxiv_id: entry.analysis
                    for entry in entries
                    if entry.analysis is not None
                }
                selected_arxiv_id = target_browse_state.selected_arxiv_id

                update_domain_ui()
                update_library_surface_visibility()
                render_papers()
                render_inspector()
                analyze_button.disabled = (
                    not current_settings.has_api_key or not current_result.papers
                )
                active_job_count = len(sync_jobs.active_domains)
                background_note = (
                    f"；后台有 {active_job_count} 个领域正在同步。"
                    if active_job_count
                    else "。"
                )
                show_status(
                    f"已切换到{DOMAIN_LABELS[active_domain]} · {range_label(selected_days)}；"
                    f"先显示 {len(current_result.papers)} / {current_view_counts.filtered} 篇"
                    f"{background_note}"
                )
                page.update()
                await restore_active_scroll_position()

                try:
                    await settings_service.save_active_domain(active_domain)
                except Exception:
                    pass
            except Exception as exc:
                if (
                    switch_generation == domain_switch_generation
                    and requested_domain == domain_key
                ):
                    requested_domain = active_domain
                    update_domain_selector(active_domain)
                    show_status(
                        f"切换到{DOMAIN_LABELS[domain_key]}失败："
                        f"{type(exc).__name__}: {exc}",
                        error=True,
                    )
                    page.update()

        return handler

    async def handle_papers_scroll(event: ft.OnScrollEvent) -> None:
        nonlocal auto_loading_more

        remember_active_browse_state(scroll_offset=event.pixels)
        if restoring_domain_scroll:
            return
        if auto_loading_more or not papers_view.visible:
            return
        if len(current_result.papers) >= current_view_counts.filtered:
            return
        if event.extent_after > 700:
            return

        auto_loading_more = True
        try:
            new_items = await load_more_library_view()
            if new_items:
                append_paper_cards(new_items)
                remember_active_browse_state(scroll_offset=event.pixels)
                page.update()
        finally:
            auto_loading_more = False

    def show_papers(_event=None) -> None:
        settings_shell.visible = False
        back_to_library_button.visible = False
        settings_nav_button.visible = True
        domain_selector_slot.visible = True
        update_library_surface_visibility()
        page.update()

    def show_settings(_event=None) -> None:
        workspace_view.visible = False
        settings_shell.visible = True
        back_to_library_button.visible = True
        settings_nav_button.visible = False
        domain_selector_slot.visible = False
        page.update()

    def toggle_more_actions(_event=None) -> None:
        more_actions_panel.visible = not more_actions_panel.visible
        more_button.style = _button_style(
            "selected" if more_actions_panel.visible else "secondary", compact=True
        )
        page.update()

    refresh_button.on_click = sync_arxiv
    for domain_key, cancel_button in sync_job_cancel_buttons.items():
        cancel_button.on_click = (
            lambda _event, key=domain_key: request_sync_cancel(key)
        )
    analyze_button.on_click = analyze
    export_excel_button.on_click = export_excel
    export_markdown_button.on_click = export_markdown
    more_button.on_click = toggle_more_actions
    range_7d_button.on_click = range_handler(7)
    range_1m_button.on_click = range_handler(30)
    range_3m_button.on_click = range_handler(90)
    range_6m_button.on_click = range_handler(180)
    range_1y_button.on_click = range_handler(365)
    save_settings_button.on_click = save_settings
    test_connection_button.on_click = test_connection
    delete_key_button.on_click = delete_key
    settings_nav_button.on_click = show_settings
    back_to_library_button.on_click = show_papers
    favorites_only_checkbox.on_change = apply_filter
    library_all_button.on_click = select_library_all
    library_favorites_button.on_click = select_library_favorites
    for key, button in domain_buttons.items():
        button.on_click = select_domain(key)

    update_key_hint(current_settings)
    update_range_buttons()
    rebuild_direction_sidebar()
    papers_view.on_scroll = handle_papers_scroll

    toolbar = ft.Container(
        padding=ft.Padding.symmetric(horizontal=10, vertical=6),
        bgcolor=SURFACE,
        border=ft.Border.all(1, DIVIDER),
        border_radius=CONTROL_RADIUS,
        content=ft.Column(
            spacing=6,
            controls=[
                ft.Row(
                    spacing=8,
                    wrap=False,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        range_segment,
                        sort_selector,
                        ft.Container(expand=True),
                        refresh_button,
                        more_button,
                    ],
                ),
                more_actions_panel,
            ],
        ),
    )

    papers_view.controls = [
        toolbar,
        stats_row,
        paper_list,
    ]

    sidebar_panel = ft.Container(
        width=SIDEBAR_WIDTH,
        bgcolor=SIDEBAR_BG,
        padding=16,
        border=ft.Border.all(1, DIVIDER),
        content=ft.Column(
            spacing=16,
            scroll=ft.ScrollMode.HIDDEN,
            controls=[
                library_scope_section,
                sidebar_scope_divider,
                direction_section,
                ft.Container(expand=True),
            ],
        ),
    )

    inspector_panel = ft.Container(
        width=INSPECTOR_WIDTH,
        bgcolor=SURFACE,
        padding=24,
        border=ft.Border.all(1, DIVIDER),
        content=inspector_body,
    )

    def set_inspector_collapsed(collapsed: bool, *, update_page: bool = True) -> None:
        nonlocal inspector_collapsed
        inspector_collapsed = collapsed
        inspector_panel.width = 64 if collapsed else INSPECTOR_WIDTH
        inspector_panel.padding = 8 if collapsed else 24
        inspector_panel.content = inspector_collapsed_body if collapsed else inspector_body
        if update_page:
            page.update()

    def toggle_inspector(_event=None) -> None:
        nonlocal inspector_auto_collapsed
        # A manual choice wins until a later resize crosses the responsive
        # threshold again. This keeps the narrow-window default helpful without
        # making the Inspector impossible to reopen when the user wants it.
        inspector_auto_collapsed = False
        set_inspector_collapsed(not inspector_collapsed)

    def apply_responsive_layout(width: float | None, *, update_page: bool = True) -> None:
        nonlocal inspector_auto_collapsed
        if width is None or width <= 0:
            return
        should_auto_collapse = width < RESPONSIVE_INSPECTOR_BREAKPOINT
        if should_auto_collapse and not inspector_collapsed:
            inspector_auto_collapsed = True
            set_inspector_collapsed(True, update_page=update_page)
        elif not should_auto_collapse and inspector_auto_collapsed:
            inspector_auto_collapsed = False
            set_inspector_collapsed(False, update_page=update_page)

    inspector_collapse_button.on_click = toggle_inspector
    inspector_expand_button.on_click = toggle_inspector

    papers_surface = ft.Stack(
        expand=True,
        fit=ft.StackFit.EXPAND,
        controls=[papers_view],
    )
    center_panel = ft.Container(
        expand=True,
        bgcolor=APP_BG,
        padding=ft.Padding.symmetric(horizontal=22, vertical=16),
        content=papers_surface,
    )

    workspace_view = ft.Row(
        expand=True,
        spacing=0,
        vertical_alignment=ft.CrossAxisAlignment.STRETCH,
        controls=[sidebar_panel, center_panel, inspector_panel],
    )

    settings_view.controls = [
        ft.Column(
            spacing=10,
            horizontal_alignment=ft.CrossAxisAlignment.START,
            controls=[
                ft.Column(
                    spacing=3,
                    controls=[
                        ft.Text(
                            "设置",
                            size=FONT_PAGE_TITLE,
                            weight=ft.FontWeight.BOLD,
                            color=TEXT_PRIMARY,
                        ),
                        ft.Text(
                            "启动同步领域、通知与 DeepSeek 偏好；API Key 始终保存在系统安全存储中。",
                            size=FONT_META,
                            color=TEXT_TERTIARY,
                        ),
                    ],
                ),
            ],
        ),
        ft.Container(height=1, bgcolor=DIVIDER),
        _settings_card(
            "同步",
            "开启后自动更新全部预设研究领域；启动时检查一次，软件持续打开时每天 22:00 再检查一次。",
            [
                _settings_row(
                    "自动同步 arXiv",
                    "默认开启。具身智能、智能体、大语言模型全部自动同步；任务在后台运行，不阻塞本地操作。",
                    auto_sync_on_start_switch,
                ),
                ft.Container(height=1, bgcolor=DIVIDER),
                ft.Column(
                    spacing=4,
                    controls=[
                        ft.Text(
                            "自动同步范围",
                            size=FONT_BODY,
                            weight=INTERACTIVE_FONT_WEIGHT,
                            color=TEXT_PRIMARY,
                        ),
                        ft.Text(
                            "固定覆盖全部预设研究领域；启动与每晚 22:00 都只检查最新增量，浏览时间范围与研究方向仍只影响本地筛选。",
                            size=FONT_CAPTION,
                            color=TEXT_TERTIARY,
                        ),
                    ],
                ),
                ft.Text(
                    "运行中的同步与其他长任务统一在左侧任务中心查看。",
                    size=FONT_CAPTION,
                    color=TEXT_TERTIARY,
                ),
            ],
        ),
        _settings_card(
            "通知与反馈",
            "长任务完成、停止或失败时，在左侧导航栏底部显示提示。",
            [
                _settings_row(
                    "任务完成提示",
                    "默认开启。同步、DeepSeek 和导出完成后都会提示。",
                    task_notifications_switch,
                ),
            ],
        ),
        _settings_card(
            "DeepSeek",
            "仅基于 Title + Abstract 做快速解读，并按当前研究领域辅助归类方向；不推测正文内容。",
            [
                ft.Column(
                    spacing=6,
                    controls=[
                        ft.Text(
                            "API Key",
                            size=FONT_BODY,
                            weight=INTERACTIVE_FONT_WEIGHT,
                            color=TEXT_PRIMARY,
                        ),
                        api_key_field,
                    ],
                ),
                _settings_row(
                    "模型",
                    "选择用于三领域快速解读的 DeepSeek 模型；提示词会随当前领域切换，但始终只使用标题和摘要。",
                    model_dropdown,
                ),
                ft.Container(height=1, bgcolor=DIVIDER),
                _settings_row(
                    "同步后自动分析",
                    "开启后会对同步得到的候选论文做 Title + Abstract 快速解读，并写入该领域缓存与研究方向，因此会产生 API 用量。",
                    auto_analyze_switch,
                ),
                ft.Row(
                    alignment=ft.MainAxisAlignment.END,
                    spacing=8,
                    wrap=True,
                    controls=[delete_key_button, test_connection_button],
                ),
            ],
        ),
        ft.Row(
            alignment=ft.MainAxisAlignment.END,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[save_settings_button],
        ),
        ft.Container(height=16),
    ]

    async def background_startup_maintenance() -> None:
        try:
            changed = await asyncio.to_thread(library_service.rerank_if_needed)
            if changed:
                await reload_library_view()
                render_papers()
                render_inspector()
                page.update()
        except Exception:
            # Background re-ranking is maintenance only. It must not clutter the
            # primary browsing surface or block normal use if it cannot complete.
            return

    async def preload_domain_view_preferences() -> None:
        # Warm the two inactive domain view states after the first frame. This
        # keeps the first click on Agent/LLM from paying multiple
        # SharedPreferences round-trips while preserving fast startup rendering.
        await asyncio.gather(
            *(
                load_domain_view_preferences_cached(domain.key)
                for domain in RESEARCH_DOMAINS
                if domain.key != active_domain
            ),
            return_exceptions=True,
        )

    async def background_startup_tasks() -> None:
        # Keep all startup work off the first frame. Local maintenance finishes
        # first, then every selected research domain gets its own independent
        # background sync job. ArxivClient globally serializes HTTP request slots
        # to preserve polite access while the domain jobs progress concurrently.
        await background_startup_maintenance()
        if current_settings.auto_sync_on_start:
            for domain_key in auto_sync_domain_keys:
                start_sync_job(
                    source_label="启动自动同步",
                    domain_key=domain_key,
                )

    async def nightly_auto_sync_loop() -> None:
        """Run one best-effort automatic refresh at 22:00 local time while open.

        This intentionally complements rather than replaces startup sync. It is
        an in-app convenience only: if the application is closed there is no
        background service. The gate is re-evaluated every 30 seconds so system
        sleep/clock changes do not require a long fragile timer.
        """

        nonlocal current_settings, auto_sync_domain_keys
        gate = DailySyncGate(hour=22, minute=0)
        gate.prime(datetime.now().astimezone())
        while True:
            await asyncio.sleep(30)
            now_local = datetime.now().astimezone()
            if not gate.should_trigger(now_local):
                continue

            # Reload settings at trigger time so changes made during the day take
            # effect without restarting the app. The legacy field name
            # auto_sync_on_start now acts as the single auto-sync enable switch.
            try:
                current_settings = await settings_service.load()
                auto_sync_domain_keys = await settings_service.load_auto_sync_domain_keys(
                    fallback_domain=active_domain
                )
            except Exception as exc:
                show_status(
                    f"22:00 自动同步无法读取设置：{type(exc).__name__}: {exc}",
                    failed=True,
                )
                continue

            if not current_settings.auto_sync_on_start:
                continue

            started_labels: list[str] = []
            for domain_key in auto_sync_domain_keys:
                if sync_jobs.is_active(domain_key):
                    continue
                if start_sync_job(
                    source_label="22:00 定时同步",
                    domain_key=domain_key,
                ):
                    started_labels.append(DOMAIN_LABELS[domain_key])

            if started_labels:
                show_status(f"22:00 自动同步已开始：{'、'.join(started_labels)}。")

    # Keep the domain selector physically centered without using an overlapping
    # Stack. The previous full-width positioned center layer could stretch the
    # selector across the Header and paint over/crop the brand block on Windows.
    # Symmetric expanding side slots keep the middle slot centered while every
    # region owns its own horizontal space.
    domain_selector_slot = ft.Container(
        width=348,
        alignment=ft.Alignment.CENTER,
        content=domain_selector,
    )

    header = ft.Container(
        height=76,
        bgcolor=SURFACE,
        padding=ft.Padding.symmetric(horizontal=24, vertical=10),
        border=ft.Border.all(1, DIVIDER),
        content=ft.Row(
            spacing=0,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Container(
                    expand=True,
                    alignment=ft.Alignment.CENTER_LEFT,
                    content=ft.Row(
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Image(
                                src="icon.png",
                                width=34,
                                height=34,
                                fit=ft.BoxFit.COVER,
                            ),
                            title,
                        ],
                    ),
                ),
                domain_selector_slot,
                ft.Container(
                    expand=True,
                    alignment=ft.Alignment.CENTER_RIGHT,
                    content=ft.Row(
                        spacing=6,
                        alignment=ft.MainAxisAlignment.END,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[back_to_library_button, settings_nav_button],
                    ),
                ),
            ],
        ),
    )

    content = ft.Column(
        expand=True,
        spacing=0,
        controls=[
            header,
            workspace_view,
            ft.Container(
                visible=False,
                bgcolor=APP_BG,
            ),
            ft.Container(
                expand=True,
                visible=False,
            ),
        ],
    )

    settings_shell = ft.Container(
        expand=True,
        visible=False,
        bgcolor=APP_BG,
        padding=ft.Padding.symmetric(horizontal=28, vertical=24),
        content=ft.Row(
            alignment=ft.MainAxisAlignment.CENTER,
            vertical_alignment=ft.CrossAxisAlignment.START,
            controls=[ft.Container(width=1040, content=settings_view)],
        ),
    )

    async def navigate_selected_paper(step: int) -> None:
        if settings_shell.visible or not current_result.papers:
            return

        items = list(current_result.papers)
        ids = [item.paper.arxiv_id for item in items]
        if selected_arxiv_id in ids:
            index = ids.index(selected_arxiv_id)
        else:
            index = -1 if step > 0 else len(ids)

        target_index = index + step
        if step > 0 and target_index >= len(ids):
            if len(items) < current_view_counts.filtered:
                new_items = await load_more_library_view()
                if new_items:
                    append_paper_cards(new_items)
                    items = list(current_result.papers)
                    ids = [item.paper.arxiv_id for item in items]
            target_index = min(target_index, len(ids) - 1)
        elif step < 0:
            target_index = max(0, target_index)

        if not ids or target_index < 0 or target_index >= len(ids):
            return
        select_paper(ids[target_index])()

    async def focus_filter_toolbar() -> None:
        # Ctrl+F is a desktop Focus shortcut for the current list-filter toolbar.
        # The compact sort popup is the stable keyboard-focus target; it does not add a
        # misleading partial-text search over only the currently loaded SQL page.
        try:
            focus_result = sort_selector.focus()
            if asyncio.iscoroutine(focus_result):
                await focus_result
        except (AttributeError, RuntimeError):
            # Older Flet runtimes may not expose imperative focus on PopupMenuButton.
            # The shortcut must remain harmless rather than breaking navigation.
            return

    async def handle_keyboard_event(event: ft.KeyboardEvent) -> None:
        key = (event.key or "").strip().lower()
        command = bool(event.ctrl or event.meta)

        if command and key == ",":
            if not settings_shell.visible:
                show_settings()
            return

        if key in {"escape", "esc"}:
            if settings_shell.visible:
                show_papers()
            elif more_actions_panel.visible:
                more_actions_panel.visible = False
                more_button.style = _button_style("secondary", compact=True)
                page.update()
            return

        if settings_shell.visible:
            return

        if command and key == "f":
            await focus_filter_toolbar()
            return

        if event.alt and key in {"arrow down", "arrowdown", "down"}:
            await navigate_selected_paper(1)
            return
        if event.alt and key in {"arrow up", "arrowup", "up"}:
            await navigate_selected_paper(-1)

    def handle_page_resized(_event=None) -> None:
        apply_responsive_layout(page.width, update_page=True)

    page.on_keyboard_event = handle_keyboard_event
    page.on_resized = handle_page_resized
    apply_responsive_layout(page.width, update_page=False)

    # The sidebar and paper surface are shared by all three live research domains.
    content.controls = [header, workspace_view, settings_shell]
    settings_view.visible = True

    render_papers()
    render_inspector()
    update_domain_ui()
    update_library_surface_visibility()
    # Anchor transient status and long-running task feedback over the empty
    # bottom area of the left navigation rail (not the paper workspace). This
    # keeps the task center visible even on the settings surface while matching
    # the user's expected "left bottom" location.
    feedback_hud = ft.Container(
        left=16,
        bottom=18,
        width=task_center_width,
        content=ft.Column(
            tight=True,
            spacing=6,
            controls=[task_center_panel, status_panel],
        ),
    )
    app_stack = ft.Stack(
        expand=True,
        fit=ft.StackFit.EXPAND,
        controls=[
            ft.SafeArea(expand=True, content=content),
            feedback_hud,
        ],
    )
    boot_screen.visible = False
    page.add(app_stack)
    page.update()
    show_status(initial_status_text)

    async def shutdown_page_tasks(_event=None) -> None:
        # A closing Flet session must not leave fire-and-forget coroutines owned by
        # this page. Signal sync workers first so their transport thread can stop
        # politely, then cancel the remaining page tasks.
        active_sync_domains = sync_jobs.active_domains
        for domain_key in active_sync_domains:
            sync_jobs.cancel(domain_key)
        for domain_key in active_sync_domains:
            try:
                async with sync_write_lock:
                    await asyncio.to_thread(
                        sync_coordinator.cancel, domain_key=domain_key
                    )
            except Exception:
                pass
        task_ui_updates.close()
        await task_registry.cancel_all()

    page.on_close = shutdown_page_tasks

    # Warm inactive view preferences and start maintenance/network work only after
    # the first real UI frame is visible. Domain navigation then feels immediate
    # without adding preference I/O to startup rendering.
    task_registry.create(
        preload_domain_view_preferences(),
        name="preload-domain-preferences",
    )
    task_registry.create(background_startup_tasks(), name="startup-background")
    task_registry.create(nightly_auto_sync_loop(), name="nightly-auto-sync")


async def main(page: ft.Page) -> None:
    """Mount the stable Windows/Desktop presentation."""
    await mount(page, configure_desktop_window=True)
