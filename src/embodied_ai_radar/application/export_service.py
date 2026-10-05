from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from tempfile import TemporaryDirectory

from embodied_ai_radar.domain.library import PaperUserState
from embodied_ai_radar.domain.llm import PaperAIAnalysis
from embodied_ai_radar.domain.models import RankedPaper
from embodied_ai_radar.domain.research_domains import DOMAIN_EMBODIED


@dataclass(frozen=True, slots=True)
class LiteratureExportItem:
    ranked: RankedPaper
    state: PaperUserState
    analysis: PaperAIAnalysis | None = None


@dataclass(frozen=True, slots=True)
class ExportContext:
    range_label: str
    exported_at: datetime
    domain_key: str = DOMAIN_EMBODIED
    domain_label: str = "具身智能"
    scope_label: str = "全部论文"
    topic_label: str = "全部方向"
    sort_label: str = "方向相关度优先"


@dataclass(frozen=True, slots=True)
class ExportColumn:
    header: str
    width: int
    link_label: str = ""


# One user-facing field policy powers both Markdown and Excel. Internal ranking,
# filtering, favorite/read state, affiliation provenance, and DeepSeek cache
# fields intentionally stay out of the default export.
EXPORT_COLUMNS: tuple[ExportColumn, ...] = (
    ExportColumn("序号", 6),
    ExportColumn("arXiv ID", 18),
    ExportColumn("论文标题", 48),
    ExportColumn("作者", 36),
    ExportColumn("作者单位", 48),
    ExportColumn("摘要", 72),
    ExportColumn("arXiv 分类", 22),
    ExportColumn("发布时间", 18),
    ExportColumn("更新时间", 18),
    ExportColumn("arXiv 链接", 14, "arXiv"),
    ExportColumn("PDF 链接", 14, "PDF"),
)


def export_headers() -> tuple[str, ...]:
    return tuple(column.header for column in EXPORT_COLUMNS)


def build_markdown_export(
    items: list[LiteratureExportItem],
    *,
    context: ExportContext,
) -> bytes:
    headers = export_headers()
    lines = [
        f"# {context.domain_label}论文导出",
        "",
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    buffer = BytesIO()
    buffer.write(("\n".join(lines) + "\n").encode("utf-8"))
    for row in _export_rows(items):
        line = "| " + " | ".join(_markdown_cell(value) for value in row) + " |\n"
        buffer.write(line.encode("utf-8"))
    return buffer.getvalue()


def build_excel_export(
    items: list[LiteratureExportItem],
    *,
    context: ExportContext,
) -> bytes:
    try:
        import xlsxwriter
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Excel 导出需要 XlsxWriter，请在当前 Conda 环境执行：python -m pip install XlsxWriter"
        ) from exc

    headers = export_headers()
    buffer = BytesIO()
    # External metadata must remain literal, including merged heading text.
    # Only the explicit link columns below should create hyperlinks.
    with (
        TemporaryDirectory(prefix="paper-export-") as temporary_directory,
        xlsxwriter.Workbook(
            buffer,
            {
                "constant_memory": True,
                "tmpdir": temporary_directory,
                "strings_to_formulas": False,
                "strings_to_urls": False,
            },
        ) as workbook,
    ):
        worksheet = workbook.add_worksheet("论文清单")

        title_format = workbook.add_format({"bold": True, "font_size": 16, "valign": "vcenter"})
        header_format = workbook.add_format(
            {
                "bold": True,
                "text_wrap": True,
                "valign": "vcenter",
                "align": "center",
                "border": 1,
                "bg_color": "#EAF2F8",
            }
        )
        cell_format = workbook.add_format({"text_wrap": True, "valign": "top", "border": 1})
        center_format = workbook.add_format(
            {"text_wrap": True, "valign": "top", "align": "center", "border": 1}
        )
        link_format = workbook.add_format(
            {"font_color": "blue", "underline": True, "valign": "top", "border": 1}
        )

        worksheet.set_row(0, 26)
        worksheet.merge_range(
            0,
            0,
            0,
            len(headers) - 1,
            f"{context.domain_label}论文导出",
            title_format,
        )
        header_row = 2
        worksheet.set_row(header_row, 34)
        for column, header in enumerate(headers):
            worksheet.write(header_row, column, header, header_format)

        for row_offset, values in enumerate(_export_rows(items), start=1):
            row_index = header_row + row_offset
            for column, value in enumerate(values):
                policy = EXPORT_COLUMNS[column]
                if policy.link_label and value:
                    worksheet.write_url(
                        row_index,
                        column,
                        str(value),
                        link_format,
                        string=policy.link_label,
                    )
                elif column == 0:
                    worksheet.write(row_index, column, value, center_format)
                else:
                    worksheet.write_string(row_index, column, str(value), cell_format)

        worksheet.freeze_panes(header_row + 1, 0)
        worksheet.autofilter(header_row, 0, header_row + len(items), len(headers) - 1)
        for column, policy in enumerate(EXPORT_COLUMNS):
            worksheet.set_column(column, column, policy.width)

    return buffer.getvalue()


def _export_rows(items: list[LiteratureExportItem]) -> Iterator[tuple[object, ...]]:
    for index, item in enumerate(items, start=1):
        yield _row_values(index, item)


def _row_values(index: int, item: LiteratureExportItem) -> tuple[object, ...]:
    paper = item.ranked.paper
    return (
        index,
        paper.arxiv_id,
        paper.title,
        "; ".join(paper.authors),
        _format_affiliations(item.ranked),
        paper.summary,
        "; ".join(paper.categories),
        _format_datetime(paper.published_at),
        _format_datetime(paper.updated_at),
        paper.abstract_url,
        paper.pdf_url or "",
    )


def _format_affiliations(ranked: RankedPaper) -> str:
    paper = ranked.paper
    parts: list[str] = []
    for index, author in enumerate(paper.authors):
        affiliations = (
            paper.author_affiliations[index] if index < len(paper.author_affiliations) else ()
        )
        normalized = tuple(value.strip() for value in affiliations if value.strip())
        if normalized:
            parts.append(f"{author}: {' / '.join(normalized)}")
    return "; ".join(parts)


def _format_datetime(value: datetime) -> str:
    normalized = value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.strftime("%Y-%m-%d %H:%M UTC")


def _markdown_cell(value: object) -> str:
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("|", "\\|").replace("\n", "<br>")
    return text.strip()
