"""Phone composition: retained list, separate reading routes and bottom navigation."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from threading import Event

import flet as ft

from embodied_ai_radar.application.export_service import (
    ExportContext,
    LiteratureExportItem,
    build_excel_export,
    build_markdown_export,
)
from embodied_ai_radar.application.sync_execution import SyncExecutionService
from embodied_ai_radar.application.sync_service import DailySyncGate
from embodied_ai_radar.domain.research_domains import (
    RESEARCH_DOMAINS,
    TOPIC_ALL,
    get_research_domain,
)
from embodied_ai_radar.ui.mobile_runtime import create_mobile_services
from embodied_ai_radar.ui.phone.browse import PhoneBrowseState
from embodied_ai_radar.ui.phone.components import (
    CARD_EXTENT,
    action,
    action_style,
    icon,
    navigation,
    paper_card,
    paper_detail,
)
from embodied_ai_radar.ui.phone.reader import PhonePdfSession
from embodied_ai_radar.ui.phone.settings import PhoneSettingsForm
from embodied_ai_radar.ui.phone.tasks import PhoneTaskPanel
from embodied_ai_radar.ui.tablet.reader import build_reader_target
from embodied_ai_radar.ui.theme import APP_BG, SURFACE, TEXT_SECONDARY

LOGGER = logging.getLogger(__name__)
RANGES = {7: "7 天", 30: "1 个月", 90: "3 个月", 180: "6 个月", 365: "1 年"}
SORTS = {"interest": "方向相关度", "relevance": "领域相关度", "latest": "最新更新"}


def bind(func, *args):
    async def handler(_event=None):
        result = func(*args)
        if inspect.isawaitable(result):
            await result

    return handler


class PhoneApp:
    def __init__(self, page, services):
        self.page, self.services = page, services
        self.states = {}
        self.state = None
        self.settings = None
        self.closed = False
        self.sync_events = {}
        self.jobs = {}
        self.status = {}
        self.write_lock = asyncio.Lock()
        self.preference_lock = asyncio.Lock()
        self.favorites_busy = set()
        self.cards = {}
        self.domain_buttons = {}
        self.header_domain = None
        self.home = None
        self.detail = None
        self.tasks_view = None
        self.settings_view = None
        self.pdf = PhonePdfSession()
        self.reader_view = None
        self.reader_generation = 0
        self.reader_busy = False
        self.pdf_image = ft.Image(src=b"", fit=ft.BoxFit.CONTAIN, gapless_playback=True)
        self.pdf_label = ft.Text("正在连接……", size=14)
        self.pdf_body = ft.Container(expand=True)
        self.reader_transition = asyncio.Lock()
        self.header = ft.Container(padding=ft.Padding.symmetric(horizontal=12, vertical=8))
        self.list = ft.ListView(
            expand=True,
            spacing=10,
            padding=12,
            item_extent=CARD_EXTENT,
            build_controls_on_demand=True,
            cache_extent=CARD_EXTENT * 2,
            on_scroll=self.on_scroll,
            scroll_interval=120,
        )
        self.tasks_body = ft.ListView(expand=True, padding=16, spacing=14)
        self.task_panel = PhoneTaskPanel(self.tasks_body, self.start_sync, self.stop_sync)
        self.launcher = ft.UrlLauncher()

    def notify(self, text):
        if not self.closed:
            self.page.show_dialog(ft.SnackBar(content=ft.Text(text), duration=4000))

    def spawn(self, key, coro):
        if key in self.jobs:
            coro.close()
            return

        started = False

        async def guarded():
            nonlocal started
            started = True
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Phone operation failed: %s", key)
                self.notify("操作未完成，请检查网络或设置后重试。")
            finally:
                self.jobs.pop(key, None)

        task = self.services.task_registry.create(guarded(), name=f"phone:{key}")
        self.jobs[key] = task

        def finished(completed):
            if not started:
                coro.close()
            if self.jobs.get(key) is completed:
                self.jobs.pop(key, None)

        task.add_done_callback(finished)

    async def start(self):
        await asyncio.to_thread(self.services.library_service.initialize)
        self.settings = await self.services.settings_service.load()
        initial = await self.services.settings_service.load_view_preferences()
        prefs = await asyncio.gather(
            *(
                self.services.settings_service.load_domain_view_preferences(d.key)
                for d in RESEARCH_DOMAINS
            )
        )
        self.states = {p.active_domain: PhoneBrowseState(p) for p in prefs}
        self.state = self.states[initial.active_domain]
        self.state.loading = True
        self.page.clean()
        self.home = ft.View(
            route="/",
            padding=0,
            bgcolor=APP_BG,
            appbar=self.appbar(
                "论文自动检索分析",
                actions=[
                    icon(ft.Icons.SYNC, bind(self.start_sync), label="同步当前领域"),
                ],
            ),
            navigation_bar=navigation(int(initial.favorites_only), self.on_navigation),
            controls=[
                ft.SafeArea(
                    expand=True,
                    avoid_intrusions_top=False,
                    avoid_intrusions_bottom=False,
                    content=ft.Column(
                        expand=True,
                        spacing=0,
                        controls=[self.header, self.list],
                    ),
                )
            ],
        )
        self.page.on_route_change = self.route_changed
        self.page.on_view_pop = self.view_pop
        self.page.on_resize = self.resized
        self.page.on_close = self.shutdown
        self.page.views[:] = [self.home]
        self.page.route = "/"
        self.render_list()
        self.page.update()
        await self.reload()
        self.spawn("startup", self.startup())
        self.spawn("nightly", self.nightly())

    def appbar(self, title, *, back=False, actions=None):
        return ft.AppBar(
            title=ft.Text(title, size=18, max_lines=1),
            bgcolor=SURFACE,
            automatically_imply_leading=False,
            leading=icon(ft.Icons.ARROW_BACK, self.back, label="返回") if back else None,
            actions=actions or [],
        )

    def render_header(self):
        state, prefs = self.state, self.state.preferences
        domain = get_research_domain(prefs.active_domain)
        topic = domain.topic(prefs.focus_tag)
        if not self.domain_buttons:
            self.domain_buttons = {
                d.key: action(d.label, bind(self.switch_domain, d.key)) for d in RESEARCH_DOMAINS
            }
            self.header_summary = ft.Text(
                expand=True,
                size=12,
                color=TEXT_SECONDARY,
                max_lines=2,
            )
            self.fresh_button = action("有更新，点击刷新列表", bind(self.reload))
            self.header.content = ft.Column(
                spacing=6,
                controls=[
                    ft.Row(
                        spacing=4,
                        controls=[
                            ft.Container(expand=True, content=button)
                            for button in self.domain_buttons.values()
                        ],
                    ),
                    ft.Row(
                        controls=[
                            self.header_summary,
                            icon(ft.Icons.TUNE, bind(self.filters), label="筛选与排序"),
                            icon(ft.Icons.MORE_HORIZ, bind(self.more_actions), label="分析与导出"),
                        ]
                    ),
                    self.fresh_button,
                ],
            )
        if self.header_domain != prefs.active_domain:
            for key, button in self.domain_buttons.items():
                button.style = action_style(key == prefs.active_domain)
            self.header_domain = prefs.active_domain
        suffix = "正在加载…" if state.loading else f"{state.counts.filtered} 篇"
        self.header_summary.value = (
            f"{RANGES[prefs.days]} · {topic.label if topic else '全部方向'} · {suffix}"
        )
        self.fresh_button.visible = state.fresh_available
        if self.home:
            self.home.navigation_bar.selected_index = int(prefs.favorites_only)

    def render_list(self):
        self.render_header()
        controls, next_cards = [], {}
        for entry in self.state.entries:
            identity = entry.ranked.paper.arxiv_id
            cached = self.cards.get(identity)
            control = (
                cached[1]
                if cached and cached[0] == entry
                else paper_card(
                    entry,
                    bind(self.select, identity),
                    bind(self.favorite, identity),
                )
            )
            next_cards[identity] = (entry, control)
            controls.append(control)
        self.cards = next_cards
        if controls:
            self.list.controls = controls
        elif self.state.loading:
            self.list.controls = [
                ft.Container(
                    padding=24,
                    content=ft.Column(
                        controls=[
                            ft.ProgressRing(),
                            ft.Text("正在加载论文…", size=16),
                        ]
                    ),
                )
            ]
        else:
            failed = self.state.load_error
            self.list.controls = [
                ft.Container(
                    padding=24,
                    content=ft.Column(
                        controls=[
                            ft.Text(
                                "加载未完成" if failed else "暂无论文",
                                size=20,
                                weight=ft.FontWeight.BOLD,
                            ),
                            ft.Text(
                                "请重试加载本地论文。"
                                if failed
                                else "尝试调整筛选，或同步当前领域。",
                                size=14,
                            ),
                            action(
                                "重试加载" if failed else "同步论文",
                                bind(self.reload if failed else self.start_sync),
                                primary=True,
                            ),
                        ]
                    ),
                )
            ]

    def append_list(self, start: int) -> None:
        # Existing cards were already sent to Flet; only allocate the new SQL page.
        if not start or len(self.cards) != start:
            self.render_list()
            return
        for entry in self.state.entries[start:]:
            identity = entry.ranked.paper.arxiv_id
            control = paper_card(entry, bind(self.select, identity), bind(self.favorite, identity))
            self.cards[identity] = (entry, control)
            self.list.controls.append(control)
        self.render_header()

    async def reload(self, state=None):
        state = state or self.state
        expected_revision = state.revision + 1
        state.loading = True
        state.load_error = False
        if state is self.state and not self.closed:
            self.render_list()
            self.page.update(self.header, self.list)
        try:
            changed = await state.reload(self.services.browse_controller)
            if changed and state is self.state and not self.closed:
                self.render_list()
                self.page.update(self.header, self.list)
                await self.list.scroll_to(offset=0, duration=0)
        except Exception:
            if state.revision == expected_revision:
                state.load_error = True
                if state is self.state and not self.closed:
                    self.render_list()
                    self.page.update(self.header, self.list)
                LOGGER.exception("Phone browse failed")
                self.notify("本地论文加载失败，请重试。")

    async def on_scroll(self, event):
        state = self.state
        state.scroll = event.pixels
        if event.pixels < CARD_EXTENT and len(state.entries) > 288 and not state.loading:
            state.revision += 1
            state.entries = state.entries[:144]
            self.render_list()
            self.page.update(self.list)
        if event.extent_after > CARD_EXTENT * 2:
            return
        try:
            start = len(state.entries)
            if await state.more(self.services.browse_controller) and state is self.state:
                self.append_list(start)
                self.page.update(self.list, self.header)
        except Exception:
            LOGGER.exception("Phone pagination failed")
            self.notify("加载更多失败，请稍后重试。")

    async def persist(self, prefs):
        async with self.preference_lock:
            await self.services.settings_service.save_view_preferences(**asdict(prefs))

    def queue_preferences(self, prefs):
        # Enqueue before navigation can yield; the lock preserves interaction order.
        async def save():
            try:
                await self.persist(prefs)
            except Exception:
                LOGGER.exception("Phone view preferences failed")
                self.notify("筛选已应用，偏好保存失败，请重试。")

        return self.services.task_registry.create(save(), name="phone-view-preferences")

    async def refresh_and_persist(self, state, refresh, *, saved=None):
        saved = saved if saved is not None else self.queue_preferences(state.preferences)
        await asyncio.gather(saved, refresh())

    async def switch_domain(self, key):
        if key == self.state.preferences.active_domain:
            return
        self.state = self.states[key]
        state = self.state
        self.cards = {}
        state.loading = not state.entries
        self.render_list()
        self.page.update(self.home)

        async def refresh():
            if state is not self.state:
                return
            if not state.entries:
                await self.reload(state)
            else:
                await self.refresh_counts(state)
                if state is self.state and not self.closed:
                    self.render_header()
                    self.page.update(self.header)
                    await self.list.scroll_to(offset=state.scroll, duration=0)

        await self.refresh_and_persist(state, refresh)

    async def on_navigation(self, event):
        index = event.control.selected_index
        if index < 2:
            value = index == 1
            if self.state.preferences.favorites_only != value:
                state = self.state
                state.configure(replace(state.preferences, favorites_only=value))
                saved = self.queue_preferences(state.preferences)
                await self.page.push_route("/")
                await self.refresh_and_persist(state, lambda: self.reload(state), saved=saved)
            else:
                await self.page.push_route("/")
        else:
            await self.page.push_route("/tasks" if index == 2 else "/settings")

    async def select(self, identity):
        entry = next((e for e in self.state.entries if e.ranked.paper.arxiv_id == identity), None)
        if entry:
            self.state.selected = entry
            self.detail = self.detail_view()
            await self.page.push_route("/paper")

    def detail_view(self):
        entry = self.state.selected
        if entry is None:
            return self.home
        paper = entry.ranked.paper
        return ft.View(
            route="/paper",
            padding=0,
            bgcolor=APP_BG,
            appbar=self.appbar("论文详情", back=True),
            controls=[
                ft.SafeArea(
                    expand=True,
                    avoid_intrusions_top=False,
                    content=paper_detail(
                        entry,
                        bind(self.favorite, paper.arxiv_id),
                        bind(self.start_analysis, [entry.ranked]),
                        bind(self.open_url, "arxiv", paper.abstract_url),
                        bind(self.open_pdf, paper.pdf_url),
                    ),
                )
            ],
        )

    def refresh_detail(self):
        if self.detail is not None and self.state.selected is not None:
            # Keep the same View and update its content only when its data changed.
            self.detail.controls = self.detail_view().controls
            if any(v is self.detail for v in self.page.views):
                self.page.update(self.detail)

    async def favorite(self, identity):
        if identity in self.favorites_busy:
            return
        state = self.state
        entry = next(
            (e for e in state.entries if e.ranked.paper.arxiv_id == identity), state.selected
        )
        if entry is None or entry.ranked.paper.arxiv_id != identity:
            return
        self.favorites_busy.add(identity)
        try:
            saved = await asyncio.to_thread(
                self.services.library_service.set_favorite,
                identity,
                not entry.state.is_favorite,
            )
            # Favorite state is shared across domains; patch all loaded copies.
            for stored in self.states.values():
                stored.patch_favorite(identity, saved)
            if state is self.state and not self.closed:
                self.render_list()
                self.page.update(self.list)
                self.refresh_detail()
            try:
                await self.refresh_counts(state)
                if state is self.state and not self.closed:
                    if not state.entries and state.counts.filtered:
                        await self.reload()
                    self.render_header()
                    self.page.update(self.header)
            except Exception:
                LOGGER.exception("Phone favorite counts failed")
                self.notify("收藏已保存，数量刷新失败，请稍后重试。")
        except Exception:
            LOGGER.exception("Phone favorite failed")
            self.notify("收藏保存失败，请重试。")
        finally:
            self.favorites_busy.discard(identity)

    async def refresh_counts(self, state):
        revision = state.revision
        counts = await asyncio.to_thread(self.services.browse_controller.counts, state.query())
        if revision == state.revision:
            state.counts = counts

    def filters(self):
        state, prefs = self.state, self.state.preferences
        domain = get_research_domain(prefs.active_domain)
        days = ft.Dropdown(
            label="时间范围",
            value=str(prefs.days),
            options=[ft.DropdownOption(str(k), v) for k, v in RANGES.items()],
        )
        topic = ft.Dropdown(
            label="研究方向",
            value=prefs.focus_tag,
            options=[
                ft.DropdownOption(TOPIC_ALL, "全部方向"),
                *[ft.DropdownOption(t.key, t.label) for t in domain.topics],
            ],
        )
        sort = ft.Dropdown(
            label="排序",
            value=prefs.sort_mode,
            options=[ft.DropdownOption(k, v) for k, v in SORTS.items()],
        )

        async def apply(_event=None):
            self.page.pop_dialog()
            if state is not self.state:
                return
            state.configure(
                replace(prefs, days=int(days.value), focus_tag=topic.value, sort_mode=sort.value)
            )
            await self.refresh_and_persist(state, lambda: self.reload(state))
            if state is self.state and state.preferences.days > prefs.days:
                await self.offer_history(state)

        self.sheet(
            [
                ft.Text("筛选论文", size=20, weight=ft.FontWeight.BOLD),
                days,
                topic,
                sort,
                action("应用筛选", apply, primary=True),
            ]
        )

    def sheet(self, controls):
        self.page.show_dialog(
            ft.BottomSheet(
                use_safe_area=True,
                show_drag_handle=True,
                scrollable=True,
                content=ft.Container(
                    padding=16, content=ft.Column(tight=True, spacing=16, controls=controls)
                ),
            )
        )

    async def offer_history(self, state):
        prefs = state.preferences
        today = datetime.now(UTC).date()
        gap = await asyncio.to_thread(
            self.services.sync_coordinator.historical_gap,
            domain_key=prefs.active_domain,
            desired_start=today - timedelta(days=prefs.days),
            today=today,
        )
        if not gap or state is not self.state:
            return

        async def fetch(_event=None):
            self.page.pop_dialog()
            self.start_sync(prefs.active_domain, prefs.days)

        self.sheet(
            [
                ft.Text("补充更早的论文？", size=20),
                ft.Text("本地尚未覆盖所选时间范围，可在后台补充获取。"),
                action("暂不获取", bind(self.page.pop_dialog)),
                action("补充获取", fetch, primary=True),
            ]
        )

    def more_actions(self):
        def choose(func, *args):
            async def selected(_event=None):
                self.page.pop_dialog()
                result = func(*args)
                if inspect.isawaitable(result):
                    await result

            return selected

        self.sheet(
            [
                ft.Text("论文操作", size=20),
                action("分析当前列表前 20 篇", choose(self.start_analysis)),
                action("导出 Excel", choose(self.start_export, "xlsx")),
                action("导出 Markdown", choose(self.start_export, "md")),
            ]
        )

    def start_sync(self, domain=None, days=None):
        domain = domain or self.state.preferences.active_domain
        if domain in self.sync_events:
            self.notify("该领域正在同步，可在任务页停止。")
            return
        event = Event()
        self.sync_events[domain] = event
        self.spawn(f"sync:{domain}", self.run_sync(domain, days, event))

    def stop_sync(self, domain):
        event = self.sync_events.get(domain)
        if event:
            event.set()
            self.status[f"sync:{domain}"] = "正在停止，已保存论文将保留"
            self.render_tasks()

    async def run_sync(self, domain, days, event):
        key = f"sync:{domain}"
        self.status[key] = "正在同步"
        self.render_tasks()
        last = 0.0

        def progress(value):
            nonlocal last
            if event.is_set() or self.closed:
                return
            self.status[key] = f"已同步 {value.saved_count} 篇"
            if time.monotonic() - last > 0.3:
                last = time.monotonic()
                self.render_tasks()

        try:
            result = await SyncExecutionService(
                self.services.radar_service,
                self.services.library_service,
                self.services.sync_coordinator,
            ).run(
                domain_key=domain,
                cancel_event=event,
                backfill_days=days,
                progress_callback=progress,
                write_lock=self.write_lock,
            )
            self.status[key] = (
                "已停止" if result.cancelled else "同步完成"
            ) + f" · {result.saved_count} 篇"
            state = self.states[domain]
            await self.refresh_counts(state)
            state.fresh_available = bool(result.saved_count)
            if state is self.state:
                if not state.entries:
                    await self.reload()
                self.render_header()
                self.page.update(self.header)
            if self.settings.task_notifications:
                self.notify(f"{get_research_domain(domain).label}：{self.status[key]}")
            if self.settings.auto_analyze and result.candidates and not result.cancelled:
                self.start_analysis(list(result.candidates[:20]), domain)
        except asyncio.CancelledError:
            event.set()
            raise
        except Exception:
            self.status[key] = "同步未完成，可重试"
            raise
        finally:
            self.sync_events.pop(domain, None)
            self.render_tasks()

    def render_tasks(self, *, force: bool = False) -> None:
        visible = self.tasks_view is not None and any(
            view is self.tasks_view for view in self.page.views
        )
        if self.closed or (not visible and not force):
            return
        changed = self.task_panel.refresh(self.status, self.sync_events)
        if visible and changed:
            self.page.update(self.tasks_body)

    def start_analysis(self, papers=None, domain=None):
        domain = domain or self.state.preferences.active_domain
        targets = (
            list(papers) if papers is not None else [e.ranked for e in self.state.entries[:20]]
        )
        self.spawn(f"analysis:{domain}", self.analyze(targets[:20], domain))

    async def analyze(self, papers, domain):
        if not papers:
            self.notify("当前没有可分析的论文。")
            return
        key = await self.services.settings_service.get_api_key()
        if not key:
            self.notify("请先在设置中保存 DeepSeek API Key。")
            return
        task = f"analysis:{domain}"
        self.status[task] = "DeepSeek 正在分析……"
        self.render_tasks()
        try:
            analyses = await self.services.analysis_service.analyze_and_persist(
                self.services.deepseek_client(api_key=key, model=self.settings.model),
                papers,
                model=self.settings.model,
                domain_key=domain,
            )
            state = self.states[domain]

            def patch(entry):
                analysis = analyses.get(entry.ranked.paper.arxiv_id)
                return replace(entry, analysis=analysis) if analysis else entry

            state.entries = [patch(e) for e in state.entries]
            if state.selected:
                state.selected = patch(state.selected)
            await self.refresh_counts(state)
            state.fresh_available = state.preferences.focus_tag != TOPIC_ALL
            if state is self.state:
                self.render_list()
                self.page.update(self.list, self.header)
                self.refresh_detail()
            self.status[task] = f"DeepSeek 完成 · {len(analyses)} 篇"
            self.notify(self.status[task])
        except Exception:
            self.status[task] = "DeepSeek 未完成，已完成批次已保存"
            raise
        finally:
            self.render_tasks()

    def start_export(self, extension):
        self.spawn("export", self.export(extension, self.state.query()))

    async def export(self, extension, query):
        self.status["export"] = "正在准备导出……"
        self.render_tasks()
        try:
            entries = await asyncio.to_thread(self.services.browse_controller.load_all, query)
            if not entries:
                self.status["export"] = "没有可导出的论文"
                self.notify("当前筛选结果为空。")
                return
            domain = get_research_domain(query.domain_key)
            context = ExportContext(
                range_label="当前时间范围",
                exported_at=datetime.now(UTC),
                domain_key=domain.key,
                domain_label=domain.label,
            )
            items = [LiteratureExportItem(e.ranked, e.state, e.analysis) for e in entries]
            builder = build_excel_export if extension == "xlsx" else build_markdown_export
            payload = await asyncio.to_thread(builder, items, context=context)
            path = await ft.FilePicker().save_file(
                file_name=f"{domain.key}_{datetime.now(UTC):%Y%m%d}.{extension}",
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=[extension],
                src_bytes=payload,
            )
            self.status["export"] = "导出完成" if path or self.page.web else "已取消导出"
            self.notify(self.status["export"])
        except Exception:
            self.status["export"] = "导出未完成，请重试"
            raise
        finally:
            self.render_tasks()

    def build_settings(self) -> ft.View:
        def on_saved(settings):
            self.settings = settings

        form = PhoneSettingsForm(
            self.page,
            self.services.settings_service,
            self.settings,
            on_saved=on_saved,
            notify=self.notify,
        )
        return ft.View(
            route="/settings",
            padding=0,
            bgcolor=APP_BG,
            appbar=self.appbar("设置", back=True),
            navigation_bar=navigation(3, self.on_navigation),
            controls=[
                ft.SafeArea(
                    expand=True,
                    avoid_intrusions_top=False,
                    avoid_intrusions_bottom=False,
                    content=form.body,
                )
            ],
        )

    async def open_url(self, kind, url):
        target = build_reader_target(kind, url)
        if target:
            await self.launcher.launch_url(target.source_url)

    async def open_pdf(self, url):
        target = build_reader_target("pdf", url)
        if target is None:
            self.notify("这篇论文没有可用的 PDF 链接。")
            return
        self.reader_generation += 1
        generation = self.reader_generation
        self.pdf.cancel.set()
        self.pdf_label.value = "正在连接……"
        self.pdf_body.content = ft.ProgressRing()
        self.reader_view = ft.View(
            route="/reader",
            padding=0,
            bgcolor=APP_BG,
            appbar=self.appbar(
                "PDF 阅读",
                back=True,
                actions=[
                    icon(
                        ft.Icons.OPEN_IN_BROWSER,
                        bind(self.open_url, "pdf", url),
                        label="浏览器查看",
                    ),
                ],
            ),
            controls=[
                ft.SafeArea(
                    expand=True,
                    avoid_intrusions_top=False,
                    content=ft.Column(
                        expand=True,
                        controls=[
                            self.pdf_body,
                            ft.Row(
                                alignment=ft.MainAxisAlignment.CENTER,
                                controls=[
                                    action("上一页", bind(self.turn_pdf, -1)),
                                    ft.Container(expand=True, content=self.pdf_label),
                                    action("下一页", bind(self.turn_pdf, 1)),
                                ],
                            ),
                        ],
                    ),
                )
            ],
        )
        await self.page.push_route("/reader")

        async def download():
            loop = asyncio.get_running_loop()
            last = 0.0

            def apply_progress(progress):
                if generation == self.reader_generation and self.page.route == "/reader":
                    self.pdf_label.value = (
                        f"已下载 {progress.downloaded_bytes / 1048576:.1f} MB"
                        if progress.downloaded_bytes
                        else "正在连接……"
                    )
                    self.page.update(self.pdf_label)

            def progress(value):
                nonlocal last
                if time.monotonic() - last > 0.3:
                    last = time.monotonic()
                    loop.call_soon_threadsafe(apply_progress, value)

            try:
                if await self.pdf.open(url, progress) and generation == self.reader_generation:
                    await self.turn_pdf(0)
            except Exception:
                if generation == self.reader_generation and self.page.route == "/reader":
                    self.pdf_label.value = "下载失败"
                    self.pdf_body.content = action("重试下载", bind(self.retry_pdf, url))
                    self.page.update(self.pdf_body, self.pdf_label)
                    raise

        async def prepare():
            async with self.reader_transition:
                if generation == self.reader_generation and not self.closed:
                    await download()

        self.spawn(f"pdf-open:{generation}", prepare())

    async def close_pdf(self):
        async with self.reader_transition:
            await self.pdf.close()
            self.pdf_image.src = b""
            self.pdf_body.content = None

    async def retry_pdf(self, url):
        await self.close_pdf()
        await self.open_pdf(url)

    async def turn_pdf(self, delta):
        if self.reader_busy or self.pdf.document is None:
            return
        self.reader_busy = True
        generation = self.reader_generation
        try:
            png = await self.pdf.render(self.pdf.index + delta)
            if png is not None and generation == self.reader_generation:
                self.pdf_image.src = png
                self.pdf_body.content = ft.InteractiveViewer(
                    content=self.pdf_image, expand=True, min_scale=1, max_scale=4
                )
                self.pdf_label.value = f"{self.pdf.index + 1} / {len(self.pdf.document.pages)}"
                self.page.update(self.pdf_body, self.pdf_label)
        except Exception:
            LOGGER.exception("Phone PDF rendering failed")
            if generation == self.reader_generation:
                self.pdf_label.value = "页面渲染失败"
                self.pdf_body.content = action("重试本页", bind(self.turn_pdf, 0))
                self.page.update(self.pdf_body, self.pdf_label)
        finally:
            self.reader_busy = False

    async def route_changed(self, _event=None):
        route = self.page.route or "/"
        if route != "/reader" and self.reader_view:
            self.reader_generation += 1
            self.reader_view = None
            # Signal now, clean up cooperatively without blocking navigation.
            self.pdf.cancel.set()
            self.spawn(f"pdf-close:{self.reader_generation}", self.close_pdf())
        views = [self.home]
        if route == "/paper" and self.detail:
            views.append(self.detail)
        elif route == "/reader" and self.reader_view:
            if self.detail:
                views.append(self.detail)
            views.append(self.reader_view)
        elif route == "/tasks":
            if self.tasks_view is None:
                self.tasks_view = ft.View(
                    route="/tasks",
                    padding=0,
                    bgcolor=APP_BG,
                    appbar=self.appbar("任务", back=True),
                    navigation_bar=navigation(2, self.on_navigation),
                    controls=[
                        ft.SafeArea(
                            expand=True,
                            avoid_intrusions_top=False,
                            avoid_intrusions_bottom=False,
                            content=self.tasks_body,
                        )
                    ],
                )
            self.render_tasks(force=True)
            views.append(self.tasks_view)
        elif route == "/settings":
            self.settings_view = self.build_settings()
            views.append(self.settings_view)
        self.page.views[:] = views
        self.page.update()

    async def back(self, _event=None):
        route = "/paper" if self.page.route == "/reader" and self.detail else "/"
        await self.page.push_route(route)

    async def view_pop(self, _event=None):
        await self.back()

    async def resized(self, _event=None):
        # Phone remains single-pane even when rotated; mounted list is retained.
        self.page.update()

    async def startup(self):
        await asyncio.to_thread(self.services.library_service.rerank_if_needed)
        if self.settings.auto_sync_on_start:
            for domain in RESEARCH_DOMAINS:
                self.start_sync(domain.key)

    async def nightly(self):
        gate = DailySyncGate()
        gate.prime(datetime.now().astimezone())
        while True:
            await asyncio.sleep(30)
            if self.settings.auto_sync_on_start and gate.should_trigger(
                datetime.now().astimezone()
            ):
                for domain in RESEARCH_DOMAINS:
                    if domain.key not in self.sync_events:
                        self.start_sync(domain.key)

    async def shutdown(self, _event=None):
        self.closed = True
        for event in self.sync_events.values():
            event.set()
        self.reader_generation += 1
        self.pdf.cancel.set()
        await self.close_pdf()
        await self.services.task_registry.cancel_all()


async def main(page: ft.Page) -> None:
    page.title = "论文自动检索分析"
    page.padding = 0
    page.bgcolor = APP_BG
    page.theme_mode = ft.ThemeMode.LIGHT
    page.add(
        ft.SafeArea(
            expand=True,
            content=ft.Column(
                alignment=ft.MainAxisAlignment.CENTER,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[ft.ProgressRing(), ft.Text("正在打开本地论文库……", size=15)],
            ),
        )
    )
    services = await create_mobile_services(page)
    app = PhoneApp(page, services)
    await app.start()
