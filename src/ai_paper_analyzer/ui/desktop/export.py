"""Desktop export orchestration, independent of mutable browsing controls."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import flet as ft

from ai_paper_analyzer.application.browse_controller import BrowseController, BrowseQuery
from ai_paper_analyzer.application.export_service import (
    ExportContext,
    LiteratureExportItem,
    build_excel_export,
    build_markdown_export,
)


@dataclass(frozen=True, slots=True)
class ExportRequest:
    query: BrowseQuery
    context: ExportContext
    range_stamp: str


class StatusCallback(Protocol):
    def __call__(self, message: str, *, failed: bool = False) -> None: ...


class NotificationCallback(Protocol):
    def __call__(self, title: str, message: str, *, failed: bool = False) -> None: ...


async def run_export(
    file_format: str,
    request: ExportRequest,
    *,
    browse_controller: BrowseController,
    page: ft.Page,
    show_busy: Callable[[str, str, str], None],
    hide_busy: Callable[[str], None],
    show_status: StatusCallback,
    notify_task: NotificationCallback,
) -> None:
    date_stamp = request.context.exported_at.strftime("%Y%m%d")
    export_task_id = f"export:{file_format}"
    try:
        entries = await asyncio.to_thread(browse_controller.load_all, request.query, batch_size=500)
        items = [LiteratureExportItem(e.ranked, e.state, e.analysis) for e in entries]
        if not items:
            show_status("当前筛选结果为空，没有可导出的论文。")
            return
        format_label = "Excel" if file_format == "xlsx" else "Markdown"
        show_busy(
            export_task_id,
            f"正在生成 {format_label}",
            f"正在整理当前筛选的 {len(items)} 篇论文……",
        )
        if file_format == "xlsx":
            payload = await asyncio.to_thread(
                build_excel_export,
                items,
                context=request.context,
            )
            file_name = (
                f"{request.context.domain_key}_papers_{request.range_stamp}_{date_stamp}.xlsx"
            )
            allowed_extensions = ["xlsx"]
        elif file_format == "md":
            payload = await asyncio.to_thread(
                build_markdown_export,
                items,
                context=request.context,
            )
            file_name = f"{request.context.domain_key}_papers_{request.range_stamp}_{date_stamp}.md"
            allowed_extensions = ["md"]
        else:
            raise ValueError(f"Unsupported export format: {file_format}")

        hide_busy(export_task_id)
        file_path = await ft.FilePicker().save_file(
            dialog_title=f"导出{request.context.domain_label}论文",
            file_name=file_name,
            file_type=ft.FilePickerFileType.CUSTOM,
            allowed_extensions=allowed_extensions,
            src_bytes=payload,
        )
        if page.web:
            show_status(f"已导出 {len(items)} 篇论文：{file_name}")
            notify_task(f"{format_label} 导出完成", f"已导出 {len(items)} 篇论文")
        elif file_path:
            show_status(f"已导出 {len(items)} 篇论文：{file_path}")
            notify_task(f"{format_label} 导出完成", f"已导出 {len(items)} 篇论文")
        else:
            show_status("已取消导出。")
    except Exception as exc:
        hide_busy(export_task_id)
        show_status(f"导出失败：{type(exc).__name__}: {exc}", failed=True)
        notify_task("导出失败", f"{type(exc).__name__}: {exc}", failed=True)
    page.update()
