from __future__ import annotations

import re
import time
from typing import Iterable

import requests

from embodied_ai_radar.domain.affiliations import AffiliationLookup
from embodied_ai_radar.domain.models import Paper


class AffiliationLookupError(RuntimeError):
    pass


class SemanticScholarClient:
    """Best-effort affiliation enrichment for papers missing arXiv affiliations.

    Semantic Scholar accepts ``ARXIV:<id>`` identifiers. We first resolve paper
    authors in batches, then resolve author profiles (including ``affiliations``)
    in batches. These affiliations are an enrichment signal and may reflect an
    author's profile/current institution rather than a paper-time affiliation.
    Provenance is persisted internally for merge/audit behavior but is not part of
    the default user export.
    """

    def __init__(
        self,
        *,
        base_url: str = "https://api.semanticscholar.org/graph/v1",
        user_agent: str = "EmbodiedAIRadar/0.5.0 (personal research discovery app)",
        timeout_seconds: float = 20.0,
        minimum_interval_seconds: float = 1.0,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.minimum_interval_seconds = minimum_interval_seconds
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
        self._last_request_monotonic: float | None = None

    def enrich(self, papers: Iterable[Paper]) -> dict[str, AffiliationLookup]:
        targets = [paper for paper in papers if _has_missing_affiliations(paper)]
        if not targets:
            return {}

        result: dict[str, AffiliationLookup] = {}
        for batch in _chunks(targets, 100):
            paper_author_rows = self._paper_authors(batch)
            author_ids = {
                author_id
                for rows in paper_author_rows.values()
                for author_id, _name in rows
                if author_id
            }
            author_affiliations = self._author_affiliations(author_ids)

            for paper in batch:
                stable_id = _base_arxiv_id(paper.arxiv_id)
                s2_rows = paper_author_rows.get(stable_id, ())
                by_name = {
                    _normalize_name(name): author_affiliations.get(author_id, ())
                    for author_id, name in s2_rows
                    if author_id and name
                }
                merged: list[tuple[str, ...]] = []
                found_any = False
                for index, name in enumerate(paper.authors):
                    existing = (
                        paper.author_affiliations[index]
                        if index < len(paper.author_affiliations)
                        else ()
                    )
                    if existing:
                        merged.append(tuple(existing))
                        found_any = True
                        continue
                    enriched = tuple(by_name.get(_normalize_name(name), ()))
                    merged.append(enriched)
                    found_any = found_any or bool(enriched)

                if found_any and tuple(merged) != paper.author_affiliations:
                    result[paper.arxiv_id] = AffiliationLookup(
                        arxiv_id=paper.arxiv_id,
                        affiliations=tuple(merged),
                        source="Semantic Scholar",
                    )
        return result

    def _paper_authors(
        self, papers: list[Paper]
    ) -> dict[str, tuple[tuple[str, str], ...]]:
        ids = [f"ARXIV:{_base_arxiv_id(paper.arxiv_id)}" for paper in papers]
        payload = self._request_json(
            "POST",
            f"{self.base_url}/paper/batch",
            params={"fields": "authors"},
            json={"ids": ids},
        )
        if not isinstance(payload, list):
            raise AffiliationLookupError("Semantic Scholar paper batch returned invalid JSON")

        output: dict[str, tuple[tuple[str, str], ...]] = {}
        for paper, item in zip(papers, payload, strict=False):
            if not isinstance(item, dict):
                continue
            rows: list[tuple[str, str]] = []
            for author in item.get("authors") or []:
                if not isinstance(author, dict):
                    continue
                author_id = str(author.get("authorId") or "").strip()
                name = str(author.get("name") or "").strip()
                if name:
                    rows.append((author_id, name))
            output[_base_arxiv_id(paper.arxiv_id)] = tuple(rows)
        return output

    def _author_affiliations(self, author_ids: set[str]) -> dict[str, tuple[str, ...]]:
        output: dict[str, tuple[str, ...]] = {}
        ordered = sorted(author_ids)
        for batch in _chunks(ordered, 500):
            payload = self._request_json(
                "POST",
                f"{self.base_url}/author/batch",
                params={"fields": "name,affiliations"},
                json={"ids": batch},
            )
            if not isinstance(payload, list):
                raise AffiliationLookupError("Semantic Scholar author batch returned invalid JSON")
            for item in payload:
                if not isinstance(item, dict):
                    continue
                author_id = str(item.get("authorId") or "").strip()
                if not author_id:
                    continue
                values = tuple(
                    value.strip()
                    for value in (item.get("affiliations") or [])
                    if isinstance(value, str) and value.strip()
                )
                output[author_id] = values
        return output

    def _request_json(self, method: str, url: str, **kwargs):
        self._respect_interval()
        try:
            response = self._session.request(
                method,
                url,
                timeout=self.timeout_seconds,
                **kwargs,
            )
            self._last_request_monotonic = time.monotonic()
        except requests.RequestException as exc:
            raise AffiliationLookupError(f"Semantic Scholar network error: {exc}") from exc

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            detail = f"，Retry-After={retry_after}s" if retry_after else ""
            raise AffiliationLookupError(f"Semantic Scholar rate limited (HTTP 429){detail}")
        if response.status_code >= 400:
            preview = response.text[:300].replace("\n", " ")
            raise AffiliationLookupError(
                f"Semantic Scholar HTTP {response.status_code}: {preview}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise AffiliationLookupError("Semantic Scholar returned invalid JSON") from exc

    def _respect_interval(self) -> None:
        if self._last_request_monotonic is None:
            return
        remaining = self.minimum_interval_seconds - (
            time.monotonic() - self._last_request_monotonic
        )
        if remaining > 0:
            time.sleep(remaining)


def _base_arxiv_id(arxiv_id: str) -> str:
    return re.sub(r"v\d+$", "", arxiv_id.strip(), flags=re.IGNORECASE)


def _normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _has_missing_affiliations(paper: Paper) -> bool:
    if not paper.authors:
        return False
    if len(paper.author_affiliations) < len(paper.authors):
        return True
    return any(not group for group in paper.author_affiliations[: len(paper.authors)])


def _chunks(values, size: int):
    for index in range(0, len(values), size):
        yield values[index : index + size]
