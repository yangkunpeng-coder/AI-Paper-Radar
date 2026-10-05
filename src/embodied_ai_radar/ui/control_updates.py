"""Coalesce progress patches while preserving immediate task lifecycle feedback."""

from __future__ import annotations

import asyncio
from typing import Protocol

import flet as ft

from embodied_ai_radar.application.task_registry import TaskRegistry


class UpdateTarget(Protocol):
    def update(self, *controls: ft.Control) -> None: ...


class ControlUpdateBatcher:
    def __init__(
        self, page: UpdateTarget, registry: TaskRegistry, *, interval: float = 0.1
    ) -> None:
        self.page, self.registry, self.interval = page, registry, interval
        self.loop = asyncio.get_running_loop()
        self.pending: dict[int, ft.Control] = {}
        self.worker: asyncio.Task[None] | None = None
        self.closed = False

    def request(self, *controls: ft.Control, immediate: bool = False) -> None:
        if self.closed:
            return
        self.loop.call_soon_threadsafe(self._enqueue, controls, immediate)

    def _enqueue(self, controls: tuple[ft.Control, ...], immediate: bool) -> None:
        if self.closed:
            return
        self.pending.update((id(control), control) for control in controls)
        if immediate:
            if self.worker is not None:
                self.worker.cancel()
                self.worker = None
            self._flush()
        elif self.worker is None:
            self.worker = self.registry.create(self._delayed_flush(), name="ui-progress-update")

    async def _delayed_flush(self) -> None:
        current = asyncio.current_task()
        try:
            await asyncio.sleep(self.interval)
            self._flush()
        finally:
            if self.worker is current:
                self.worker = None

    def _flush(self) -> None:
        controls = tuple(self.pending.values())
        self.pending.clear()
        if controls and not self.closed:
            self.page.update(*controls)

    def close(self) -> None:
        self.closed = True
        self.pending.clear()
        if self.worker is not None:
            self.worker.cancel()
            self.worker = None
