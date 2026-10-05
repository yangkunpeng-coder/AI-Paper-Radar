"""Phone settings form; persistence remains owned by SettingsService."""

from __future__ import annotations

import logging
from collections.abc import Callable

import flet as ft

from ai_paper_analyzer import __version__
from ai_paper_analyzer.application.settings_service import SettingsService
from ai_paper_analyzer.domain.llm import SUPPORTED_DEEPSEEK_MODELS, LLMSettings
from ai_paper_analyzer.ui.phone.components import action
from ai_paper_analyzer.ui.theme import TEXT_SECONDARY

LOGGER = logging.getLogger(__name__)


class PhoneSettingsForm:
    def __init__(
        self,
        page: ft.Page,
        service: SettingsService,
        settings: LLMSettings,
        *,
        on_saved: Callable[[LLMSettings], None],
        notify: Callable[[str], None],
    ) -> None:
        self.page, self.service = page, service
        self.on_saved, self.notify = on_saved, notify
        self.key = ft.TextField(
            label="DeepSeek API Key",
            password=True,
            can_reveal_password=True,
            hint_text="已保存，留空保留" if settings.has_api_key else "输入 API Key",
        )
        self.model = ft.Dropdown(
            label="模型",
            value=settings.model,
            options=[ft.DropdownOption(m) for m in SUPPORTED_DEEPSEEK_MODELS],
        )
        self.auto_sync = ft.Switch(label="打开应用时自动同步", value=settings.auto_sync_on_start)
        self.auto_analyze = ft.Switch(label="同步后自动分析", value=settings.auto_analyze)
        self.notices = ft.Switch(label="任务完成提示", value=settings.task_notifications)

        self.body = ft.ListView(
            padding=16,
            spacing=18,
            expand=True,
            controls=[
                ft.Text("DeepSeek", size=22, weight=ft.FontWeight.BOLD),
                ft.Text("未配置 Key 也可检索、收藏和导出论文。", size=14),
                self.key,
                self.model,
                self.auto_sync,
                self.auto_analyze,
                self.notices,
                action("保存设置", self.save, primary=True),
                action("删除 API Key", self.remove),
                ft.Text(f"论文自动检索分析 · v{__version__}", size=12, color=TEXT_SECONDARY),
            ],
        )

    async def save(self, _event=None) -> None:
        try:
            saved = await self.service.save(
                model=self.model.value,
                auto_analyze=self.auto_analyze.value,
                auto_sync_on_start=self.auto_sync.value,
                task_notifications=self.notices.value,
                api_key=self.key.value,
            )
            self.key.value = ""
            self.key.hint_text = "已保存，留空保留" if saved.has_api_key else "输入 API Key"
            self.on_saved(saved)
            self.page.update(self.key)
            self.notify("设置已保存。")
        except Exception:
            LOGGER.exception("Phone settings failed")
            self.notify("设置保存失败，请重试。")

    async def remove(self, _event=None) -> None:
        try:
            saved = await self.service.delete_api_key()
            self.key.value, self.key.hint_text = "", "输入 API Key"
            self.on_saved(saved)
            self.page.update(self.key)
            self.notify("API Key 已删除。")
        except Exception:
            LOGGER.exception("Phone API key removal failed")
            self.notify("API Key 删除失败，请重试。")
