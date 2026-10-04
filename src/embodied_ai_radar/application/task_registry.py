from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

_T = TypeVar("_T")


class TaskRegistry:
    """Own long-lived asyncio tasks so a page/session can shut them down cleanly.

    Short-lived ``asyncio.gather`` helper tasks do not need to be registered. This
    registry is intended for fire-and-forget work whose lifetime may otherwise
    outlive the Flet page that created it.
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closing = False

    def create(
        self,
        coro: Coroutine[Any, Any, _T],
        *,
        name: str | None = None,
    ) -> asyncio.Task[_T]:
        if self._closing:
            coro.close()
            raise RuntimeError("task registry is closing")
        task = asyncio.create_task(coro, name=name)
        self.track(task)
        return task

    def track(self, task: asyncio.Task[_T]) -> asyncio.Task[_T]:
        if self._closing:
            task.cancel()
            return task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @property
    def active_count(self) -> int:
        return sum(not task.done() for task in self._tasks)

    async def cancel_all(self) -> None:
        self._closing = True
        tasks = tuple(task for task in self._tasks if not task.done())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
