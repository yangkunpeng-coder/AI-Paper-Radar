"""One-page PDF session with cancellable download and deterministic cleanup."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from threading import Event

from embodied_ai_radar.ui.tablet.pdf_reader import download_temporary_pdf, render_pdf_page


class PhonePdfSession:
    def __init__(self, *, cache_bytes: int = 8 * 1024 * 1024, cache_pages: int = 3):
        if cache_bytes < 0 or cache_pages < 0:
            raise ValueError("PDF cache limits must be non-negative")
        self.document = None
        self.cancel = Event()
        self.generation = 0
        self.worker = None
        self.lock = asyncio.Lock()
        self.index = 0
        self.cache_bytes, self.cache_pages = cache_bytes, cache_pages
        self.cached_bytes = 0
        self.cache: OrderedDict[int, bytes] = OrderedDict()

    def _cache_page(self, index: int, png: bytes) -> None:
        if not self.cache_pages or not self.cache_bytes or len(png) > self.cache_bytes:
            return
        self.cache[index] = png
        self.cached_bytes += len(png)
        while len(self.cache) > self.cache_pages or self.cached_bytes > self.cache_bytes:
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

    async def render(self, index):
        generation = self.generation
        async with self.lock:
            document = self.document
            if document is None or generation != self.generation:
                return None
            index = max(0, min(index, len(document.pages) - 1))
            if index in self.cache:
                self.cache.move_to_end(index)
                self.index = index
                return self.cache[index]
            # Shield the native worker so close cannot delete a file in use.
            worker = asyncio.create_task(
                asyncio.to_thread(
                    render_pdf_page,
                    document,
                    index,
                    target_width=1400,
                )
            )
            try:
                png = await asyncio.shield(worker)
            except asyncio.CancelledError:
                await worker
                raise
            if generation != self.generation:
                return None
            self._cache_page(index, png)
            self.index = index
            return png

    async def close(self):
        self.generation += 1
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
