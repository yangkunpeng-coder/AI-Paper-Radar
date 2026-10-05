"""Retained task controls; callers decide when the panel is visible."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from threading import Event

import flet as ft

from ai_paper_analyzer.domain.research_domains import RESEARCH_DOMAINS
from ai_paper_analyzer.ui.phone.components import action
from ai_paper_analyzer.ui.theme import SURFACE


class PhoneTaskPanel:
    def __init__(
        self,
        body: ft.ListView,
        start_sync: Callable[[str], None],
        stop_sync: Callable[[str], None],
    ) -> None:
        self.body = body
        self.start_sync, self.stop_sync = start_sync, stop_sync
        self.labels: dict[str, ft.Text] = {}
        self.buttons: dict[str, ft.Button] = {}
        self.messages: dict[str, ft.Text] = {}
        self.running: dict[str, bool] = {}

    def _handler(self, domain: str, running: bool):
        async def handle(_event=None) -> None:
            (self.stop_sync if running else self.start_sync)(domain)

        return handle

    def refresh(self, status: Mapping[str, str], events: Mapping[str, Event]) -> bool:
        """Apply current state and report whether a UI update is needed."""
        changed = not self.labels
        if not self.labels:
            self.body.controls = [ft.Text("后台任务", size=22, weight=ft.FontWeight.BOLD)]
            for domain in RESEARCH_DOMAINS:
                label = self.labels[domain.key] = ft.Text(size=14)
                button = self.buttons[domain.key] = action("同步", self._handler(domain.key, False))
                self.body.controls.append(
                    ft.Container(
                        bgcolor=SURFACE,
                        padding=16,
                        border_radius=14,
                        content=ft.Column(
                            controls=[
                                ft.Text(domain.label, size=17, weight=ft.FontWeight.BOLD),
                                label,
                                button,
                            ]
                        ),
                    )
                )
        for domain in RESEARCH_DOMAINS:
            key = domain.key
            label, button = self.labels[key], self.buttons[key]
            text = status.get(f"sync:{key}", "尚未开始同步")
            running = key in events
            disabled = running and events[key].is_set()
            if label.value != text:
                label.value = text
                changed = True
            if self.running.get(key) != running:
                button.content = "停止" if running else "同步"
                button.on_click = self._handler(key, running)
                self.running[key] = running
                changed = True
            if button.disabled != disabled:
                button.disabled = disabled
                changed = True

        messages = {key: value for key, value in status.items() if not key.startswith("sync:")}
        if list(messages) != list(self.messages):
            self.messages = {
                key: self.messages[key] if key in self.messages else ft.Text(size=14)
                for key in messages
            }
            self.body.controls = self.body.controls[: 1 + len(RESEARCH_DOMAINS)] + list(
                self.messages.values()
            )
            changed = True
        for key, value in messages.items():
            if self.messages[key].value != value:
                self.messages[key].value = value
                changed = True
        return changed
