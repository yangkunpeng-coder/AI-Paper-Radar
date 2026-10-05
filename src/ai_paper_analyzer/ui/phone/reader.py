"""One-page PDF session with cancellable download and deterministic cleanup."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from threading import Event

from ai_paper_analyzer.ui.tablet.pdf_reader import download_temporary_pdf, render_pdf_page

LOGGER = logging.getLogger(__name__)


class PhonePdfSession:
    def __init__(
        self,
        *,
        cache_bytes: int = 8 * 1024 * 1024,
        cache_pages: int = 3,
        prefetch_delay: float = 0.15,
    ):
        if cache_bytes < 0 or cache_pages < 0:
            raise ValueError("PDF cache limits must be non-negative")
        self.document = None
        self.cancel = Event()
        self.generation = 0
        self.worker = None
        self.raster_worker = None
        self.raster_index = None
        self.lock = asyncio.Lock()
        self.index = 0
        self.cache_bytes, self.cache_pages = cache_bytes, cache_pages
        self.cached_bytes = 0
        self.cache: OrderedDict[int, bytes] = OrderedDict()
        self.prefetch_delay = prefetch_delay
        self.render_revision = 0
        self.prefetch_request = None
        self.prefetch_paused = False

    def _cache_page(self, index: int, png: bytes, *, protected_index=None) -> None:
        if not self.cache_pages or not self.cache_bytes or len(png) > self.cache_bytes:
            return
        self.cache[index] = png
        self.cached_bytes += len(png)
        while len(self.cache) > self.cache_pages or self.cached_bytes > self.cache_bytes:
            if next(iter(self.cache)) == protected_index and len(self.cache) > 1:
                self.cache.move_to_end(protected_index)
            _, evicted = self.cache.popitem(last=False)
            self.cached_bytes -= len(evicted)

    async def open(self, url, progress=None):
        await self.close()
        generation = self.generation
        self.cancel = Event()
        worker = asyncio.create_task(
            asyncio.to_thread(
                download_temporary_pdf,
                url,
                cancel_event=self.cancel,
                progress_callback=progress,
            )
        )
        self.worker = worker
        try:
            document = await asyncio.shield(worker)
        except asyncio.CancelledError:
            self.cancel.set()
            try:
                document = await worker
                await asyncio.to_thread(document.cleanup)
            except Exception:
                pass
            raise
        if generation != self.generation:
            await asyncio.to_thread(document.cleanup)
            return False
        self.document = document
        self.index = 0
        return True

    async def render(self, index, *, activate=True, expected_revision=None):
        if activate:
            self.render_revision += 1
        generation = self.generation
        if self.document is not None:
            index = max(0, min(index, len(self.document.pages) - 1))
        # If the user turns to the neighbor being prefetched, reuse that result
        # rather than rasterizing the same page again after waiting for the lock.
        shared = self.raster_worker if activate and self.raster_index == index else None
        async with self.lock:
            document = self.document
            if document is None or generation != self.generation:
                return None
            if not activate and (self.prefetch_paused or expected_revision != self.render_revision):
                return None
            index = max(0, min(index, len(document.pages) - 1))
            if index in self.cache:
                self.cache.move_to_end(index)
                if activate:
                    self.index = index
                return self.cache[index]
            # Shield the native worker so close cannot delete a file in use.
            worker = shared
            if worker is None:
                worker = asyncio.create_task(
                    asyncio.to_thread(render_pdf_page, document, index, target_width=1400)
                )
                self.raster_worker, self.raster_index = worker, index
            try:
                png = await asyncio.shield(worker)
            except asyncio.CancelledError:
                await worker
                raise
            finally:
                if self.raster_worker is worker:
                    self.raster_worker = None
                    self.raster_index = None
            if generation != self.generation:
                return None
            if activate:
                self._cache_page(index, png)
                self.index = index
            elif expected_revision == self.render_revision and not self.prefetch_paused:
                # Prefetch must not evict the page being viewed from a small budget.
                current_bytes = len(self.cache.get(self.index, b""))
                if current_bytes + len(png) <= self.cache_bytes:
                    self._cache_page(index, png, protected_index=self.index)
            return png

    def queue_prefetch(self, direction=1):
        if self.document is None or not self.cache_bytes or self.cache_pages < 2:
            self.prefetch_request = None
            return
        self.prefetch_request = (self.index, 1 if direction >= 0 else -1, self.render_revision)

    async def prefetch_latest(self):
        while self.prefetch_request is not None and not self.prefetch_paused:
            request = self.prefetch_request
            await asyncio.sleep(self.prefetch_delay)
            if request != self.prefetch_request:
                continue
            index, direction, revision = request
            document = self.document
            if document is None or revision != self.render_revision:
                return
            for neighbor in (index + direction, index - direction):
                if request != self.prefetch_request or self.prefetch_paused:
                    break
                if revision != self.render_revision:
                    return
                if not 0 <= neighbor < len(document.pages):
                    continue
                try:
                    await self.render(neighbor, activate=False, expected_revision=revision)
                except Exception:
                    # An optional neighbor failure must not interrupt the current page.
                    LOGGER.debug("PDF neighbor prefetch failed", exc_info=True)
            if request == self.prefetch_request:
                return

    async def close(self):
        self.generation += 1
        self.render_revision += 1
        self.prefetch_request = None
        self.prefetch_paused = False
        self.cancel.set()
        self.cache.clear()
        self.cached_bytes = 0
        worker, self.worker = self.worker, None
        document, self.document = self.document, None
        if worker is not None:
            try:
                downloaded = await asyncio.shield(worker)
                document = document or downloaded
            except Exception:
                pass
        async with self.lock:
            if document is not None:
                await asyncio.to_thread(document.cleanup)
