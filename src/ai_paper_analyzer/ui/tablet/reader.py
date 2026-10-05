"""Safe URL preparation for the Tablet in-app paper reader."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

_TRUSTED_ARXIV_HOSTS = frozenset({"arxiv.org", "www.arxiv.org", "export.arxiv.org"})


@dataclass(frozen=True, slots=True)
class ReaderTarget:
    kind: str
    title: str
    source_url: str
    view_url: str


def _trusted_https_url(url: str | None) -> str | None:
    value = (url or "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme.casefold() != "https":
        return None
    host = (parsed.hostname or "").casefold()
    if host not in _TRUSTED_ARXIV_HOSTS:
        return None
    return value


def build_reader_target(kind: str, url: str | None) -> ReaderTarget | None:
    """Return a trusted in-app reader target or ``None`` for unsafe URLs."""

    source = _trusted_https_url(url)
    if source is None:
        return None

    normalized_kind = kind.strip().casefold()
    if normalized_kind == "pdf":
        return ReaderTarget(
            kind="pdf",
            title="论文阅读",
            source_url=source,
            view_url=source,
        )
    if normalized_kind == "arxiv":
        return ReaderTarget(
            kind="arxiv",
            title="arXiv",
            source_url=source,
            view_url=source,
        )
    raise ValueError(f"Unsupported reader kind: {kind}")
