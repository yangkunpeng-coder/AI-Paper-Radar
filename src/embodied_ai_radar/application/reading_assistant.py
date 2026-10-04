from __future__ import annotations

import asyncio
from typing import Protocol


MAX_SELECTED_TEXT_CHARS = 8_000
MAX_PAGE_TEXT_CHARS = 16_000
MAX_QUESTION_CHARS = 800


class ReadingAssistantClient(Protocol):
    def complete_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
    ) -> str: ...


class ReadingAssistantService:
    """Prompt and execution boundary for PDF reading-assistant actions."""

    async def translate_selection(
        self,
        client: ReadingAssistantClient,
        *,
        text: str,
        paper_title: str = "",
        page_number: int | None = None,
    ) -> str:
        selection = self._require_text(text, MAX_SELECTED_TEXT_CHARS)
        context = self._context_line(paper_title, page_number)
        return await asyncio.to_thread(
            client.complete_text,
            system_prompt=(
                "你是严谨的学术论文翻译助手。把用户提供的论文片段翻译成简体中文。"
                "保留公式、变量、模型名、数据集名和引用编号；术语优先采用学术界常用译法。"
                "不要补充原文没有的信息，不要解释，只输出译文。"
            ),
            user_prompt=f"{context}\n\n需要翻译的论文片段：\n{selection}",
            max_tokens=2200,
        )

    async def answer_selection_question(
        self,
        client: ReadingAssistantClient,
        *,
        text: str,
        question: str,
        paper_title: str = "",
        page_number: int | None = None,
    ) -> str:
        selection = self._require_text(text, MAX_SELECTED_TEXT_CHARS)
        normalized_question = (question or "").strip()[:MAX_QUESTION_CHARS]
        if not normalized_question:
            normalized_question = "请解释这段内容在说什么、关键概念是什么，以及为什么重要。"
        context = self._context_line(paper_title, page_number)
        return await asyncio.to_thread(
            client.complete_text,
            system_prompt=(
                "你是论文阅读助手。只根据用户给出的论文片段和明确问题作答。"
                "使用简体中文，优先解释技术含义、公式/变量、方法逻辑和与论文论证的关系。"
                "如果片段不足以支持结论，要明确说明，不要猜测论文其他部分。"
            ),
            user_prompt=(
                f"{context}\n\n选中的论文片段：\n{selection}\n\n"
                f"用户问题：\n{normalized_question}"
            ),
            max_tokens=2400,
        )

    async def explain_page(
        self,
        client: ReadingAssistantClient,
        *,
        page_text: str,
        paper_title: str = "",
        page_number: int | None = None,
    ) -> str:
        text = self._require_text(page_text, MAX_PAGE_TEXT_CHARS)
        context = self._context_line(paper_title, page_number)
        return await asyncio.to_thread(
            client.complete_text,
            system_prompt=(
                "你是学术论文逐页阅读助手。只依据当前页文字进行解读，使用简体中文。"
                "按以下结构回答：1）本页主旨；2）关键方法/论点/结果；"
                "3）重要术语、公式或变量；4）阅读这一页时最值得注意的点。"
                "若当前页缺少足够上下文，明确指出，不要推断未提供的实验或结论。"
            ),
            user_prompt=f"{context}\n\n当前页提取文字：\n{text}",
            max_tokens=2600,
        )

    @staticmethod
    def _require_text(value: str, limit: int) -> str:
        normalized = (value or "").strip()
        if not normalized:
            raise ValueError("没有可发送给阅读助手的文字。")
        if len(normalized) <= limit:
            return normalized
        return normalized[:limit].rstrip() + "\n\n[内容过长，已截取当前操作允许的前半部分]"

    @staticmethod
    def _context_line(paper_title: str, page_number: int | None) -> str:
        details: list[str] = []
        title = (paper_title or "").strip()
        if title:
            details.append(f"论文标题：{title}")
        if page_number is not None and page_number > 0:
            details.append(f"PDF 页码：第 {page_number} 页")
        return "\n".join(details) if details else "论文 PDF 阅读上下文"
