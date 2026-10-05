"""Touch-first controls for the Android Tablet presentation."""

from __future__ import annotations

from datetime import timezone

import flet as ft

from embodied_ai_radar.domain.library import LibraryPaper
from embodied_ai_radar.domain.models import RankedPaper
from embodied_ai_radar.domain.research_domains import get_research_domain
from embodied_ai_radar.ui.text_format import format_paper_title
from embodied_ai_radar.ui.theme import (
    APP_BG,
    DANGER,
    DIVIDER,
    PRIMARY,
    PRIMARY_BORDER,
    PRIMARY_DARK,
    PRIMARY_SOFT,
    SURFACE,
    SURFACE_SUBTLE,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    TEXT_TERTIARY,
)

TABLET_TOUCH_TARGET = 48
TABLET_APP_BAR_HEIGHT = 60
TABLET_CARD_RADIUS = 14
TABLET_BODY_SIZE = 15
TABLET_META_SIZE = 13
TABLET_TITLE_SIZE = 17
TABLET_DETAIL_MAX_WIDTH = 760
TABLET_PAPER_CARD_HEIGHT = 184


def touch_icon_button(
    icon,
    *,
    on_click=None,
    selected: bool = False,
    disabled: bool = False,
) -> ft.IconButton:
    return ft.IconButton(
        icon=icon,
        width=TABLET_TOUCH_TARGET,
        height=TABLET_TOUCH_TARGET,
        icon_size=22,
        icon_color=PRIMARY_DARK if selected else TEXT_SECONDARY,
        bgcolor=PRIMARY_SOFT if selected else None,
        disabled=disabled,
        on_click=on_click,
        style=ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=12),
            overlay_color="#12315F8C",
        ),
    )


def touch_text_button(
    text: str,
    *,
    on_click=None,
    primary: bool = False,
    danger: bool = False,
    disabled: bool = False,
) -> ft.Button:
    foreground = DANGER if danger else ("#FFFFFF" if primary else TEXT_PRIMARY)
    background = PRIMARY if primary else SURFACE
    border = PRIMARY if primary else ("#F1C7C2" if danger else DIVIDER)
    return ft.Button(
        content=text,
        height=TABLET_TOUCH_TARGET,
        disabled=disabled,
        on_click=on_click,
        style=ft.ButtonStyle(
            bgcolor=background,
            color=foreground,
            elevation=0,
            padding=ft.Padding.symmetric(horizontal=16, vertical=0),
            side=ft.BorderSide(1, border),
            shape=ft.RoundedRectangleBorder(radius=12),
            text_style=ft.TextStyle(size=14, weight=ft.FontWeight.NORMAL),
        ),
    )


def tablet_badge(text: str) -> ft.Control:
    return ft.Container(
        padding=ft.Padding.symmetric(horizontal=9, vertical=4),
        border_radius=999,
        bgcolor=SURFACE_SUBTLE,
        border=ft.Border.all(1, DIVIDER),
        content=ft.Text(text, size=12, color=TEXT_SECONDARY, no_wrap=True),
    )


def _authors_text(item: RankedPaper) -> str:
    authors = item.paper.authors
    if not authors:
        return "作者未提供"
    if len(authors) <= 2:
        return " · ".join(authors)
    return f"{' · '.join(authors[:2])} · 等 {len(authors)} 位作者"


def _updated_text(item: RankedPaper) -> str:
    return item.paper.updated_at.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _affiliations_text(item: RankedPaper) -> str:
    rows: list[str] = []
    for author, affiliations in zip(
        item.paper.authors,
        item.paper.author_affiliations,
        strict=False,
    ):
        values = [value.strip() for value in affiliations if value.strip()]
        if values:
            rows.append(f"{author}: {', '.join(values)}")
    return "；".join(rows)


def _preview_text(entry: LibraryPaper) -> tuple[str, str]:
    analysis = entry.analysis
    if analysis is not None and analysis.is_quick_read and analysis.summary_cn.strip():
        return f"一句话看懂 · {analysis.summary_cn.strip()}", TEXT_PRIMARY
    return entry.ranked.paper.summary.strip(), TEXT_SECONDARY


def tablet_paper_card(
    entry: LibraryPaper,
    *,
    selected: bool,
    on_select,
    on_toggle_favorite,
) -> ft.Control:
    item = entry.ranked
    preview, preview_color = _preview_text(entry)
    unique_tags: list[str] = []
    for tag in (*item.interest_tags, *item.tags):
        if tag and tag not in unique_tags:
            unique_tags.append(tag)
    visible_tags = unique_tags[:2]
    tag_controls = [tablet_badge(tag) for tag in visible_tags]

    favorite_button = touch_icon_button(
        ft.Icons.STAR if entry.state.is_favorite else ft.Icons.STAR_BORDER,
        selected=entry.state.is_favorite,
        on_click=on_toggle_favorite,
    )

    return ft.Container(
        height=TABLET_PAPER_CARD_HEIGHT,
        bgcolor=PRIMARY_SOFT if selected else SURFACE,
        border=ft.Border.all(1, PRIMARY_BORDER if selected else DIVIDER),
        border_radius=TABLET_CARD_RADIUS,
        padding=ft.Padding(left=15, top=12, right=8, bottom=12),
        ink=True,
        ink_color="#12315F8C",
        on_click=on_select,
        content=ft.Column(
            spacing=7,
            controls=[
                ft.Row(
                    spacing=6,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                    controls=[
                        ft.Text(
                            format_paper_title(item.paper.title),
                            expand=True,
                            size=TABLET_TITLE_SIZE,
                            weight=ft.FontWeight.W_600,
                            color=TEXT_PRIMARY,
                            max_lines=2,
                            overflow=ft.TextOverflow.ELLIPSIS,
                        ),
                        favorite_button,
                    ],
                ),
                ft.Text(
                    f"{_authors_text(item)}  ·  {_updated_text(item)}",
                    size=TABLET_META_SIZE,
                    color=TEXT_TERTIARY,
                    max_lines=1,
                    overflow=ft.TextOverflow.ELLIPSIS,
                ),
                ft.Text(
                    preview,
                    size=TABLET_BODY_SIZE,
                    color=preview_color,
                    max_lines=2,
                    overflow=ft.TextOverflow.ELLIPSIS,
                ),
                ft.Row(spacing=6, wrap=False, controls=tag_controls),
            ],
        ),
    )


def empty_detail() -> ft.Control:
    return ft.Container(
        expand=True,
        alignment=ft.Alignment.CENTER,
        bgcolor=APP_BG,
        content=ft.Column(
            tight=True,
            spacing=10,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Icon(ft.Icons.ARTICLE_OUTLINED, size=44, color=TEXT_TERTIARY),
                ft.Text("请选择一篇论文", size=18, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY),
                ft.Text(
                    "从左侧论文列表选择论文，这里将显示摘要和 AI 快速解读。",
                    size=TABLET_BODY_SIZE,
                    color=TEXT_SECONDARY,
                    text_align=ft.TextAlign.CENTER,
                ),
            ],
        ),
    )


def _detail_section(title: str, text: str) -> ft.Control:
    return ft.Column(
        spacing=7,
        controls=[
            ft.Text(title, size=15, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY),
            ft.Text(
                text.strip() or "未提供",
                size=TABLET_BODY_SIZE,
                color=TEXT_SECONDARY,
                selectable=True,
            ),
        ],
    )


def tablet_paper_detail(
    entry: LibraryPaper | None,
    *,
    on_toggle_favorite=None,
    on_analyze=None,
    on_open_arxiv=None,
    on_open_pdf=None,
    compact_header: bool = False,
) -> ft.Control:
    if entry is None:
        return empty_detail()

    item = entry.ranked
    paper = item.paper
    analysis = entry.analysis
    affiliation_text = _affiliations_text(item)
    tags: list[str] = []
    for tag in (*item.interest_tags, *item.tags):
        if tag and tag not in tags:
            tags.append(tag)

    actions: list[ft.Control] = [
        touch_text_button(
            "★ 收藏" if entry.state.is_favorite else "☆ 收藏",
            on_click=on_toggle_favorite,
        ),
        touch_text_button("arXiv", on_click=on_open_arxiv),
    ]
    if paper.pdf_url:
        actions.append(touch_text_button("PDF", on_click=on_open_pdf))
    if on_analyze is not None:
        actions.append(touch_text_button("AI 解读", on_click=on_analyze, primary=True))

    analysis_controls: list[ft.Control]
    if analysis is None:
        analysis_controls = [
            ft.Container(
                padding=16,
                bgcolor=SURFACE_SUBTLE,
                border_radius=12,
                content=ft.Text(
                    "尚未生成 DeepSeek 快速解读。配置 API Key 后可按需分析。",
                    size=TABLET_BODY_SIZE,
                    color=TEXT_SECONDARY,
                ),
            )
        ]
    elif analysis.is_quick_read:
        domain = get_research_domain(analysis.domain_key)
        topic_labels = [
            topic.label
            for key in analysis.topic_keys
            if (topic := domain.topic(key)) is not None
        ]
        analysis_controls = [
            ft.Row(
                wrap=True,
                spacing=6,
                controls=[
                    tablet_badge(f"DeepSeek · {domain.label}"),
                    *[tablet_badge(label) for label in topic_labels],
                ],
            ),
            _detail_section("一句话看懂", analysis.summary_cn),
            _detail_section("解决什么问题", analysis.problem_cn),
            _detail_section("提出什么方法", analysis.method_cn),
            _detail_section("为什么值得关注", analysis.recommendation_cn),
        ]
    else:
        analysis_controls = [
            _detail_section("旧版 DeepSeek 解读", analysis.summary_cn),
            ft.Text(
                "重新分析可获得新版结构化快速解读。",
                size=TABLET_META_SIZE,
                color=TEXT_TERTIARY,
            ),
        ]

    return ft.Container(
        expand=True,
        bgcolor=APP_BG,
        padding=ft.Padding.symmetric(horizontal=18 if compact_header else 24, vertical=18),
        content=ft.Column(
            scroll=ft.ScrollMode.AUTO,
            spacing=18,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Container(
                    width=TABLET_DETAIL_MAX_WIDTH,
                    content=ft.Column(
                        spacing=16,
                        controls=[
                            ft.Text(
                                format_paper_title(paper.title),
                                size=22,
                                weight=ft.FontWeight.W_600,
                                color=TEXT_PRIMARY,
                                selectable=True,
                            ),
                            ft.Text(
                                f"{_authors_text(item)}  ·  {_updated_text(item)}  ·  {paper.arxiv_id}",
                                size=TABLET_META_SIZE,
                                color=TEXT_TERTIARY,
                                selectable=True,
                            ),
                            *(
                                [_detail_section("作者单位", affiliation_text)]
                                if affiliation_text
                                else []
                            ),
                            ft.Row(
                                spacing=6,
                                wrap=True,
                                controls=[tablet_badge(tag) for tag in tags[:8]],
                            ),
                            ft.Row(spacing=8, wrap=True, controls=actions),
                            ft.Divider(height=1, color=DIVIDER),
                            ft.Text(
                                "论文快速解读",
                                size=18,
                                weight=ft.FontWeight.W_600,
                                color=TEXT_PRIMARY,
                            ),
                            *analysis_controls,
                            ft.Divider(height=1, color=DIVIDER),
                            _detail_section("Abstract", paper.summary),
                        ],
                    ),
                )
            ],
        ),
    )


def stat_metric(label: str, value: int) -> ft.Control:
    """Static count display; intentionally not button/chip shaped."""

    return ft.Row(
        tight=True,
        spacing=5,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            ft.Text(str(value), size=16, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY),
            ft.Text(label, size=12, color=TEXT_TERTIARY),
        ],
    )
