"""Compact phone controls: a single column, wrapping actions and 48dp targets."""

from __future__ import annotations

import flet as ft

from embodied_ai_radar.domain.library import LibraryPaper
from embodied_ai_radar.ui.text_format import format_paper_title
from embodied_ai_radar.ui.theme import (
    DIVIDER,
    PRIMARY_DARK,
    PRIMARY_SOFT,
    SURFACE,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)

CARD_HEIGHT = 216
CARD_EXTENT = CARD_HEIGHT + 10


def action(text, handler, *, primary=False, disabled=False):
    return ft.Button(
        content=text,
        height=48,
        on_click=handler,
        disabled=disabled,
        style=action_style(primary),
    )


def action_style(primary: bool = False) -> ft.ButtonStyle:
    return ft.ButtonStyle(
        bgcolor=PRIMARY_DARK if primary else SURFACE,
        color="#ffffff" if primary else TEXT_PRIMARY,
        padding=ft.Padding.symmetric(horizontal=12, vertical=0),
        shape=ft.RoundedRectangleBorder(radius=12),
    )


def icon(icon_data, handler, *, label):
    return ft.Semantics(
        label=label,
        button=True,
        content=ft.IconButton(
            icon=icon_data,
            on_click=handler,
            width=48,
            height=48,
            icon_color=PRIMARY_DARK,
        ),
    )


def navigation(index, handler):
    return ft.NavigationBar(
        selected_index=index,
        on_change=handler,
        bgcolor=SURFACE,
        indicator_color=PRIMARY_SOFT,
        destinations=[
            ft.NavigationBarDestination(icon=ft.Icons.ARTICLE_OUTLINED, label="论文"),
            ft.NavigationBarDestination(icon=ft.Icons.STAR_BORDER, label="收藏"),
            ft.NavigationBarDestination(icon=ft.Icons.SYNC, label="任务"),
            ft.NavigationBarDestination(icon=ft.Icons.SETTINGS_OUTLINED, label="设置"),
        ],
    )


def paper_card(entry: LibraryPaper, select, favorite):
    paper = entry.ranked.paper
    preview = (
        entry.analysis.summary_cn
        if entry.analysis and entry.analysis.is_quick_read
        else paper.summary
    )
    return ft.Container(
        height=CARD_HEIGHT,
        padding=12,
        border_radius=14,
        bgcolor=SURFACE,
        border=ft.Border.all(1, DIVIDER),
        ink=True,
        on_click=select,
        content=ft.Column(
            spacing=8,
            controls=[
                ft.Row(
                    vertical_alignment=ft.CrossAxisAlignment.START,
                    controls=[
                        ft.Text(
                            format_paper_title(paper.title),
                            size=16,
                            weight=ft.FontWeight.BOLD,
                            max_lines=3,
                            overflow=ft.TextOverflow.ELLIPSIS,
                            expand=True,
                        ),
                        icon(
                            ft.Icons.STAR if entry.state.is_favorite else ft.Icons.STAR_BORDER,
                            favorite,
                            label="取消收藏" if entry.state.is_favorite else "收藏论文",
                        ),
                    ],
                ),
                ft.Text(
                    " · ".join(paper.authors[:2]) or "作者未提供",
                    size=12,
                    color=TEXT_SECONDARY,
                    max_lines=1,
                    overflow=ft.TextOverflow.ELLIPSIS,
                ),
                ft.Text(
                    preview,
                    size=14,
                    max_lines=2,
                    overflow=ft.TextOverflow.ELLIPSIS,
                    color=TEXT_SECONDARY,
                ),
                ft.Text(
                    paper.updated_at.strftime("%Y-%m-%d")
                    + ("  ·  已有 AI 解读" if entry.analysis else ""),
                    size=12,
                    color=PRIMARY_DARK,
                ),
            ],
        ),
    )


def section(title, text):
    return ft.Column(
        spacing=8,
        controls=[
            ft.Text(title, size=16, weight=ft.FontWeight.BOLD, color=TEXT_PRIMARY),
            ft.Text(text or "未提供", size=15, selectable=True, color=TEXT_SECONDARY),
        ],
    )


def paper_detail(entry, favorite, analyze, arxiv, pdf):
    paper, analysis = entry.ranked.paper, entry.analysis
    content = [
        ft.Text(
            format_paper_title(paper.title), size=22, weight=ft.FontWeight.BOLD, selectable=True
        ),
        ft.Text(" · ".join(paper.authors), size=13, selectable=True, color=TEXT_SECONDARY),
        ft.Text(f"{paper.updated_at:%Y-%m-%d} · {paper.arxiv_id}", size=12),
        ft.Row(
            wrap=True,
            spacing=8,
            run_spacing=8,
            controls=[
                action("已收藏" if entry.state.is_favorite else "收藏", favorite),
                action("arXiv", arxiv),
                action("PDF", pdf, disabled=not paper.pdf_url),
                action("AI 解读", analyze, primary=True),
            ],
        ),
    ]
    affiliations = [
        f"{author}: {', '.join(values)}"
        for author, values in zip(paper.authors, paper.author_affiliations, strict=False)
        if values
    ]
    if affiliations:
        content.append(section("作者单位", "；".join(affiliations)))
    if analysis:
        content.extend(
            [
                section("一句话看懂", analysis.summary_cn),
                section("解决什么问题", analysis.problem_cn),
                section("提出什么方法", analysis.method_cn),
                section("为什么值得关注", analysis.recommendation_cn),
            ]
        )
    else:
        content.append(section("论文快速解读", "配置 DeepSeek API Key 后，点击 AI 解读。"))
    content.append(section("Abstract", paper.summary))
    return ft.ListView(controls=content, spacing=20, padding=16, expand=True)
