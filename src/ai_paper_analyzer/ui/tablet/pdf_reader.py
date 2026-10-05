"""Temporary local PDF download/render helpers for the Tablet reader.

Only the paper the user explicitly opens is downloaded. The file lives below
Flet's app-temporary directory (when available) and is removed when the reader
is closed. Pages are rasterized with PyMuPDF so Android does not depend on
WebView's inconsistent PDF support.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import requests


class PdfReaderError(RuntimeError):
    """Raised when a paper cannot be downloaded or decoded as a PDF."""


class PdfDownloadCancelled(PdfReaderError):
    """Raised when the current reader session cancels an in-flight download."""


@dataclass(frozen=True, slots=True)
class PdfDownloadProgress:
    phase: str
    downloaded_bytes: int = 0
    total_bytes: int | None = None

    @property
    def fraction(self) -> float | None:
        if not self.total_bytes or self.total_bytes <= 0:
            return None
        return max(0.0, min(1.0, self.downloaded_bytes / self.total_bytes))


@dataclass(frozen=True, slots=True)
class PdfPageInfo:
    width: float
    height: float


@dataclass(slots=True)
class TemporaryPdfDocument:
    directory: Path
    path: Path
    pages: tuple[PdfPageInfo, ...]

    def cleanup(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)


PDF_CONNECT_TIMEOUT = 8.0
PDF_READ_TIMEOUT = 20.0
PDF_TOTAL_TIMEOUT = 120.0
PDF_MAX_BYTES = 128 * 1024 * 1024
PDF_DOWNLOAD_CHUNK_SIZE = 256 * 1024


def _temporary_root() -> Path:
    configured = os.getenv("FLET_APP_STORAGE_TEMP")
    if configured:
        root = Path(configured)
        root.mkdir(parents=True, exist_ok=True)
        return root
    return Path(tempfile.gettempdir())


def _inspect_pdf(path: Path) -> tuple[PdfPageInfo, ...]:
    try:
        import pymupdf

        with pymupdf.open(path) as document:
            if document.page_count < 1:
                raise PdfReaderError("PDF 没有可阅读页面。")
            return tuple(
                PdfPageInfo(float(page.rect.width), float(page.rect.height))
                for page in document
            )
    except PdfReaderError:
        raise
    except Exception as exc:  # pragma: no cover - exact PyMuPDF error is version-specific.
        raise PdfReaderError("下载的文件不是可阅读的 PDF。") from exc


def _content_length(headers: object) -> int | None:
    try:
        raw = headers.get("Content-Length")  # type: ignore[union-attr]
    except AttributeError:
        return None
    if raw in (None, ""):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _content_type(headers: object) -> str:
    try:
        raw = headers.get("Content-Type", "")  # type: ignore[union-attr]
    except AttributeError:
        return ""
    return str(raw).split(";", 1)[0].strip().lower()


def download_temporary_pdf(
    url: str,
    *,
    timeout: tuple[float, float] = (PDF_CONNECT_TIMEOUT, PDF_READ_TIMEOUT),
    total_timeout: float = PDF_TOTAL_TIMEOUT,
    max_bytes: int = PDF_MAX_BYTES,
    chunk_size: int = PDF_DOWNLOAD_CHUNK_SIZE,
    session: requests.Session | None = None,
    cancel_event: Event | None = None,
    progress_callback: Callable[[PdfDownloadProgress], None] | None = None,
) -> TemporaryPdfDocument:
    """Stream one PDF to app-temp storage and validate it with PyMuPDF.

    The function is intentionally synchronous so callers can move it to a worker
    thread. Network reads are bounded by connect/read timeouts plus a total
    deadline; cancellation is cooperative between chunks/read timeouts. Partial
    files are always removed on failure or cancellation.
    """

    directory = Path(
        tempfile.mkdtemp(prefix="ai-paper-radar-pdf-", dir=str(_temporary_root()))
    )
    partial = directory / "paper.pdf.part"
    target = directory / "paper.pdf"
    client = session or requests.Session()
    owns_session = session is None
    started_at = time.monotonic()

    def notify(phase: str, downloaded: int = 0, total: int | None = None) -> None:
        if progress_callback is not None:
            progress_callback(
                PdfDownloadProgress(
                    phase=phase,
                    downloaded_bytes=max(0, int(downloaded)),
                    total_bytes=total,
                )
            )

    def ensure_active() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise PdfDownloadCancelled("PDF 下载已取消。")
        if total_timeout > 0 and time.monotonic() - started_at > total_timeout:
            raise PdfReaderError("PDF 下载超时，请稍后重试。")

    try:
        ensure_active()
        notify("connecting")
        with client.get(
            url,
            stream=True,
            timeout=timeout,
            headers={"User-Agent": "AI-Paper-Radar/1.0 (+temporary-pdf-reader)"},
        ) as response:
            response.raise_for_status()
            ensure_active()

            content_type = _content_type(getattr(response, "headers", {}))
            if content_type.startswith("text/html") or content_type.startswith("text/plain"):
                raise PdfReaderError("下载结果不是 PDF 文件。")

            total = _content_length(getattr(response, "headers", {}))
            if total is not None and total > max_bytes:
                raise PdfReaderError("PDF 文件过大，建议使用右上角浏览器查看。")

            downloaded = 0
            notify("downloading", downloaded, total)
            with partial.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    ensure_active()
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    if downloaded > max_bytes:
                        raise PdfReaderError("PDF 文件过大，建议使用右上角浏览器查看。")
                    handle.write(chunk)
                    notify("downloading", downloaded, total)

            ensure_active()
            if downloaded == 0:
                raise PdfReaderError("PDF 下载失败，请稍后重试。")

        notify("verifying", downloaded, total)
        with partial.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                raise PdfReaderError("下载结果不是 PDF 文件。")
        partial.replace(target)
        pages = _inspect_pdf(target)
        ensure_active()
        return TemporaryPdfDocument(directory=directory, path=target, pages=pages)
    except PdfDownloadCancelled:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    except PdfReaderError:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    except requests.Timeout as exc:
        shutil.rmtree(directory, ignore_errors=True)
        raise PdfReaderError("PDF 下载超时，请稍后重试。") from exc
    except requests.RequestException as exc:
        shutil.rmtree(directory, ignore_errors=True)
        raise PdfReaderError("PDF 下载失败，请稍后重试。") from exc
    except Exception as exc:
        shutil.rmtree(directory, ignore_errors=True)
        raise PdfReaderError("PDF 下载失败，请稍后重试。") from exc
    finally:
        if owns_session:
            client.close()


PDF_RENDER_MIN_WIDTH = 1800
PDF_RENDER_MAX_WIDTH = 2800
PDF_RENDER_DENSITY = 2.35
PDF_RENDER_MAX_SCALE = 5.0


def pdf_render_target_width(display_width: float) -> int:
    """Choose a crisp raster width without rendering every page at device-max size."""

    requested = int(round(max(1.0, float(display_width)) * PDF_RENDER_DENSITY))
    return max(PDF_RENDER_MIN_WIDTH, min(PDF_RENDER_MAX_WIDTH, requested))


def render_pdf_page(
    document: TemporaryPdfDocument,
    page_index: int,
    *,
    target_width: int = 2200,
) -> bytes:
    """Render one page to PNG bytes at a high-resolution tablet reading width."""

    if page_index < 0 or page_index >= len(document.pages):
        raise IndexError(page_index)
    try:
        import pymupdf

        with pymupdf.open(document.path) as pdf:
            page = pdf.load_page(page_index)
            page_width = max(float(page.rect.width), 1.0)
            scale = max(1.0, min(PDF_RENDER_MAX_SCALE, target_width / page_width))
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            return pixmap.tobytes("png")
    except Exception as exc:
        raise PdfReaderError(f"PDF 第 {page_index + 1} 页渲染失败。") from exc
