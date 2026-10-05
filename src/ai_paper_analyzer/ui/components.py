from __future__ import annotations

from datetime import timezone

import flet as ft

from ai_paper_analyzer.domain.library import PaperUserState
from ai_paper_analyzer.domain.llm import PaperAIAnalysis
from ai_paper_analyzer.domain.models import RankedPaper
from ai_paper_analyzer.ui.text_format import format_paper_title
from ai_paper_analyzer.ui.theme import (
    CARD_RADIUS,
    CONTROL_RADIUS,
    DANGER,
    DANGER_SOFT,
    DISABLED_BG,
    DISABLED_TEXT,
    DIVIDER,
    DIVIDER_STRONG,
    FONT_BODY,
    FONT_CAPTION,
    FONT_CARD_TITLE,
    FONT_CONTROL,
    FONT_META,
    PRIMARY,
    PRIMARY_BORDER,
    PRIMARY_DARK,
    PRIMARY_SOFT,
    PURPLE,
    PURPLE_SOFT,
    SURFACE,
    SURFACE_HOVER,
    SURFACE_SUBTLE,
    TEAL,
    TEAL_SOFT,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    TEXT_TERTIARY,
)

INTERACTIVE_FONT_WEIGHT = ft.FontWeight.NORMAL

def _authors_text(paper: RankedPaper) -> str:
    authors = paper.paper.authors
    if len(authors) <= 3:
        return " · ".join(authors)
    return f"{' · '.join(authors[:3])} · +{len(authors) - 3}"


def _updated_text(paper: RankedPaper) -> str:
    updated = paper.paper.updated_at.astimezone(timezone.utc)
    return updated.strftime("%Y-%m-%d")


def _affiliations_text(item: RankedPaper) -> str:
    seen: list[str] = []
    for affiliations in item.paper.author_affiliations:
        for affiliation in affiliations:
            normalized = affiliation.strip()
            if normalized and normalized not in seen:
                seen.append(normalized)
    return " · ".join(seen)


def _badge(
    text: str,
    *,
    tone: str = "neutral",
    emphasized: bool = False,
) -> ft.Control:
    palette = {
        "primary": (PRIMARY_SOFT, PRIMARY),
        "teal": (TEAL_SOFT, TEAL),
        "purple": (PURPLE_SOFT, PURPLE),
        "neutral": (SURFACE, TEXT_SECONDARY),
    }
    background, foreground = palette.get(tone, palette["neutral"])
    return ft.Container(
        padding=ft.Padding.symmetric(horizontal=10, vertical=5),
        border_radius=999,
        bgcolor=background,
        border=ft.Border.all(1, DIVIDER if tone == "neutral" else background),
        content=ft.Text(
            text,
            size=FONT_CAPTION,
            color=foreground,
            weight=ft.FontWeight.BOLD if emphasized else None,
        ),
    )


def _metric(label: str, value: int) -> ft.Control:
    return ft.Row(
        spacing=6,
        controls=[
            ft.Text(str(value), size=16, weight=ft.FontWeight.BOLD, color=TEXT_PRIMARY),
            ft.Text(label, size=FONT_CAPTION, color=TEXT_TERTIARY),
        ],
    )


def _button_style(
    kind: str = "secondary",
    *,
    compact: bool = False,
    left: bool = False,
) -> ft.ButtonStyle:
    # Explicit state colours prevent Flet/Windows defaults from turning enabled
    # controls into grey, disabled-looking buttons. Neutral and selected actions
    # stay white; hierarchy is carried by text, outline and hover states instead.
    palette = {
        "primary": (PRIMARY, PRIMARY_DARK, "#FFFFFF", PRIMARY),
        "secondary": (SURFACE, SURFACE_HOVER, TEXT_PRIMARY, DIVIDER_STRONG),
        "nav": ("#00000000", SURFACE_HOVER, TEXT_SECONDARY, "#00000000"),
        "sidebar_selected": (PRIMARY_SOFT, PRIMARY_SOFT, PRIMARY_DARK, PRIMARY_BORDER),
        "soft": (SURFACE, PRIMARY_SOFT, PRIMARY_DARK, PRIMARY_BORDER),
        "link": ("#00000000", SURFACE_HOVER, PRIMARY_DARK, "#00000000"),
        "ghost": ("#00000000", SURFACE_HOVER, TEXT_SECONDARY, "#00000000"),
        "selected": (SURFACE, PRIMARY_SOFT, PRIMARY_DARK, PRIMARY),
        "purple": (SURFACE, PURPLE_SOFT, PURPLE, "#D9CFF0"),
        "danger": (DANGER_SOFT, "#FDE5E2", DANGER, "#F1C7C2"),
    }
    background, hover, foreground, outline = palette.get(kind, palette["secondary"])
    return ft.ButtonStyle(
        bgcolor={
            ft.ControlState.DEFAULT: background,
            ft.ControlState.HOVERED: hover,
            ft.ControlState.PRESSED: hover,
            ft.ControlState.DISABLED: DISABLED_BG,
        },
        color={
            ft.ControlState.DEFAULT: foreground,
            ft.ControlState.HOVERED: foreground,
            ft.ControlState.PRESSED: foreground,
            ft.ControlState.DISABLED: DISABLED_TEXT,
        },
        elevation=0,
        overlay_color="#00000000",
        padding=ft.Padding.symmetric(
            horizontal=12 if compact else 16,
            vertical=7 if compact else 9,
        ),
        shape=ft.RoundedRectangleBorder(radius=CONTROL_RADIUS),
        side=ft.BorderSide(width=1, color=outline),
        alignment=ft.Alignment.CENTER_LEFT if left else ft.Alignment.CENTER,
        text_style=ft.TextStyle(
            size=FONT_CONTROL,
            weight=INTERACTIVE_FONT_WEIGHT,
        ),
    )


def _toolbar_segment_style(selected: bool) -> ft.ButtonStyle:
    return ft.ButtonStyle(
        bgcolor={
            ft.ControlState.DEFAULT: PRIMARY_SOFT if selected else "#00000000",
            ft.ControlState.HOVERED: PRIMARY_SOFT if selected else SURFACE_HOVER,
            ft.ControlState.PRESSED: PRIMARY_SOFT,
        },
        color={
            ft.ControlState.DEFAULT: PRIMARY_DARK if selected else TEXT_SECONDARY,
            ft.ControlState.HOVERED: PRIMARY_DARK if selected else TEXT_PRIMARY,
            ft.ControlState.PRESSED: PRIMARY_DARK,
        },
        elevation=0,
        overlay_color="#00000000",
        padding=ft.Padding.symmetric(horizontal=10, vertical=0),
        shape=ft.RoundedRectangleBorder(radius=6),
        side=ft.BorderSide(width=0, color="#00000000"),
        text_style=ft.TextStyle(size=FONT_CONTROL, weight=INTERACTIVE_FONT_WEIGHT),
    )


def _sidebar_item(text: str, *, width: float, selected: bool = False) -> ft.Container:
    control = ft.Container(
        width=width,
        padding=ft.Padding.symmetric(horizontal=12, vertical=9),
        border_radius=CONTROL_RADIUS,
        ink=True,
        ink_color="#16315F8C",
    )
    _set_sidebar_item(control, text=text, selected=selected)
    return control


def _set_sidebar_item(control: ft.Container, *, text: str, selected: bool) -> None:
    # Keep navigation selection lightweight: a subtle row background plus a slim
    # ink-blue leading marker. Avoid the old bordered rounded block that made the
    # Sidebar feel like a stack of oversized buttons.
    control.bgcolor = SURFACE_HOVER if selected else "#00000000"
    control.border = None
    control.content = ft.Row(
        spacing=8,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            ft.Container(
                width=3,
                height=18,
                border_radius=999,
                bgcolor=PRIMARY if selected else "#00000000",
            ),
            ft.Text(
                text,
                expand=True,
                size=FONT_CONTROL,
                color=PRIMARY_DARK if selected else TEXT_SECONDARY,
                weight=INTERACTIVE_FONT_WEIGHT,
                no_wrap=True,
                overflow=ft.TextOverflow.ELLIPSIS,
            ),
        ],
    )


def _domain_segment(text: str, *, selected: bool = False) -> ft.Container:
    control = ft.Container(
        width=112,
        height=34,
        alignment=ft.Alignment.CENTER,
        border_radius=8,
        ink=True,
        ink_color="#16315F8C",
    )
    _set_domain_segment(control, text=text, selected=selected)
    return control


def _set_domain_segment(control: ft.Container, *, text: str, selected: bool) -> None:
    # Keep geometry and typography identical in both states. Changing font weight
    # or replacing the Text control makes the label appear to jump by a pixel on
    # Windows/Skia when the selected segment changes. Selection is expressed only
    # through background, border and text colour.
    control.bgcolor = PRIMARY_SOFT if selected else SURFACE
    control.border = ft.Border.all(1, PRIMARY_BORDER if selected else "#00000000")
    if isinstance(control.content, ft.Text):
        label = control.content
        label.value = text
    else:
        label = ft.Text(text)
        control.content = label
    label.size = FONT_CONTROL
    label.color = PRIMARY_DARK if selected else TEXT_SECONDARY
    label.weight = INTERACTIVE_FONT_WEIGHT
    label.text_align = ft.TextAlign.CENTER
    label.no_wrap = True


def _style_dropdown(control: ft.Dropdown) -> None:
    control.dense = True
    control.filled = True
    control.fill_color = SURFACE
    control.bgcolor = SURFACE
    control.color = TEXT_PRIMARY
    control.text_size = FONT_CONTROL
    control.content_padding = ft.Padding.symmetric(horizontal=14, vertical=10)
    control.border = {
        ft.ControlState.DEFAULT: ft.OutlineInputBorder(
            border_radius=CONTROL_RADIUS,
            side=ft.BorderSide(width=1, color=DIVIDER_STRONG),
        ),
        ft.ControlState.FOCUSED: ft.OutlineInputBorder(
            border_radius=CONTROL_RADIUS,
            side=ft.BorderSide(width=1.5, color=PRIMARY),
        ),
    }


def _style_text_field(control: ft.TextField) -> None:
    control.filled = True
    control.fill_color = SURFACE
    control.color = TEXT_PRIMARY
    control.text_size = FONT_BODY
    control.content_padding = ft.Padding.symmetric(horizontal=14, vertical=12)
    control.border = {
        ft.ControlState.DEFAULT: ft.OutlineInputBorder(
            border_radius=CONTROL_RADIUS,
            side=ft.BorderSide(width=1, color=DIVIDER_STRONG),
        ),
        ft.ControlState.FOCUSED: ft.OutlineInputBorder(
            border_radius=CONTROL_RADIUS,
            side=ft.BorderSide(width=1.5, color=PRIMARY),
        ),
    }


def _settings_row(title: str, detail: str, trailing: ft.Control) -> ft.Control:
    return ft.Container(
        padding=ft.Padding.symmetric(vertical=6),
        content=ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Column(
                    expand=True,
                    spacing=3,
                    controls=[
                        ft.Text(
                            title,
                            size=FONT_BODY,
                            weight=INTERACTIVE_FONT_WEIGHT,
                            color=TEXT_PRIMARY,
                        ),
                        ft.Text(detail, size=FONT_CAPTION, color=TEXT_TERTIARY),
                    ],
                ),
                trailing,
            ],
        ),
    )


def _settings_card(title: str, subtitle: str, controls: list[ft.Control]) -> ft.Control:
    return ft.Container(
        padding=22,
        bgcolor=SURFACE,
        border=ft.Border.all(1, DIVIDER),
        border_radius=16,
        content=ft.Column(
            spacing=16,
            controls=[
                ft.Column(
                    spacing=4,
                    controls=[
                        ft.Text(title, size=18, weight=ft.FontWeight.BOLD, color=TEXT_PRIMARY),
                        ft.Text(subtitle, size=FONT_CAPTION, color=TEXT_TERTIARY),
                    ],
                ),
                ft.Container(height=1, bgcolor=DIVIDER),
                *controls,
            ],
        ),
    )


def _empty_state(title: str, detail: str) -> ft.Control:
    return ft.Container(
        padding=44,
        alignment=ft.Alignment.CENTER,
        bgcolor=SURFACE,
        border=ft.Border.all(1, DIVIDER),
        border_radius=CARD_RADIUS,
        content=ft.Column(
            spacing=9,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Text(title, size=18, weight=ft.FontWeight.BOLD, color=TEXT_PRIMARY),
                ft.Text(
                    detail,
                    size=FONT_BODY,
                    color=TEXT_SECONDARY,
                    text_align=ft.TextAlign.CENTER,
                ),
            ],
        ),
    )


def _paper_card(
    item: RankedPaper,
    *,
    state: PaperUserState,
    analysis: PaperAIAnalysis | None = None,
    on_toggle_favorite=None,
    on_select=None,
    selected: bool = False,
) -> ft.Control:
    launcher = ft.UrlLauncher()

    async def open_url(url: str) -> None:
        await launcher.launch_url(url)

    def open_handler(url: str):
        async def handler(_event) -> None:
            await open_url(url)

        return handler

    favorite_label = "★" if state.is_favorite else "☆"
    tags = (*item.interest_tags, *item.tags)
    unique_tags: list[str] = []
    for tag in tags:
        if tag not in unique_tags:
            unique_tags.append(tag)

    visible_tags = unique_tags[:3]
    tag_controls: list[ft.Control] = [_badge(tag) for tag in visible_tags]
    if len(unique_tags) > 3:
        tag_controls.append(_badge(f"+{len(unique_tags) - 3}"))

    if analysis is not None and analysis.is_quick_read and analysis.summary_cn.strip():
        preview_text = f"一句话看懂 · {analysis.summary_cn.strip()}"
        preview_color = TEXT_PRIMARY
    else:
        preview_text = item.paper.summary
        preview_color = TEXT_SECONDARY

    buttons: list[ft.Control] = [
        ft.OutlinedButton(
            content="arXiv",
            on_click=open_handler(item.paper.abstract_url),
            style=_button_style("link", compact=True),
        ),
    ]
    if item.paper.pdf_url:
        buttons.append(
            ft.OutlinedButton(
                content="PDF",
                on_click=open_handler(item.paper.pdf_url),
                style=_button_style("link", compact=True),
            )
        )
    affiliation = _affiliations_text(item)
    meta_parts = [_authors_text(item)]
    if affiliation:
        meta_parts.append(affiliation)
    meta_parts.append(_updated_text(item))
    meta_text = "  ·  ".join(meta_parts)

    return ft.Container(
        padding=ft.Padding.symmetric(horizontal=16, vertical=11),
        bgcolor=SURFACE_SUBTLE if selected else SURFACE,
        border=ft.Border.all(1, PRIMARY_BORDER if selected else DIVIDER),
        border_radius=CARD_RADIUS,
        on_click=on_select,
        ink=on_select is not None,
        ink_color="#15315F8C",
        content=ft.Column(
            spacing=7,
            controls=[
                ft.Row(
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                    controls=[
                        ft.Text(
                            format_paper_title(item.paper.title),
                            expand=True,
                            size=FONT_CARD_TITLE,
                            weight=ft.FontWeight.BOLD,
                            color=TEXT_PRIMARY,
                            max_lines=2,
                            overflow=ft.TextOverflow.ELLIPSIS,
                        ),
                        ft.OutlinedButton(
                            content=favorite_label,
                            on_click=on_toggle_favorite,
                            style=_button_style("soft" if state.is_favorite else "secondary", compact=True),
                        ),
                    ],
                ),
                ft.Row(spacing=6, wrap=True, controls=tag_controls),
                ft.Text(
                    meta_text,
                    size=FONT_META,
                    color=TEXT_SECONDARY,
                    max_lines=1,
                    overflow=ft.TextOverflow.ELLIPSIS,
                ),
                ft.Text(
                    preview_text,
                    size=FONT_BODY,
                    color=preview_color,
                    max_lines=2,
                    overflow=ft.TextOverflow.ELLIPSIS,
                ),
                ft.Row(
                    spacing=7,
                    wrap=True,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=buttons,
                ),
            ],
        ),
    )
