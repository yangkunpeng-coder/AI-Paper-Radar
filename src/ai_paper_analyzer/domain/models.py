from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Paper:
    arxiv_id: str
    title: str
    summary: str
    authors: tuple[str, ...]
    categories: tuple[str, ...]
    published_at: datetime
    updated_at: datetime
    abstract_url: str
    pdf_url: str | None = None
    author_affiliations: tuple[tuple[str, ...], ...] = ()
    affiliation_source: str = ""


@dataclass(frozen=True, slots=True)
class RankedPaper:
    paper: Paper
    score: int
    tags: tuple[str, ...]
    matched_terms: tuple[str, ...]
    interest_score: int = 0
    interest_tags: tuple[str, ...] = ()
    interest_matched_terms: tuple[str, ...] = ()
