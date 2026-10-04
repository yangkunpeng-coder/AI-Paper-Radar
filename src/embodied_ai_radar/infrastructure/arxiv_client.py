from __future__ import annotations

import os
import platform
import re
import sys
import time
import urllib.parse
from threading import Event, Lock
from collections.abc import Callable
from pathlib import Path
import xml.etree.ElementTree as ET

import requests
from datetime import UTC, date, datetime, timedelta

from embodied_ai_radar.domain.library import ArxivHarvestProgress
from embodied_ai_radar.domain.models import Paper

ATOM_NS = "http://www.w3.org/2005/Atom"
ARXIV_NS = "http://arxiv.org/schemas/atom"
DC_NS = "http://purl.org/dc/elements/1.1/"
NS = {"atom": ATOM_NS, "arxiv": ARXIV_NS, "dc": DC_NS}

DEFAULT_CATEGORIES = ("cs.RO", "cs.AI", "cs.CV", "cs.LG")
_RSS_ID_PREFIX = "oai:arXiv.org:"
_OAI_ID_PREFIX = "oai:arXiv.org:"


class ArxivError(RuntimeError):
    pass


class ArxivSyncCancelled(ArxivError):
    pass


class ArxivClient:
    """Fetch arXiv daily announcements and bootstrap history via OAI-PMH.

    The daily Atom feed answers the product question "what was announced today?".
    OAI-PMH is used for history/bootstrap because arXiv explicitly documents it as
    the preferred metadata harvesting/synchronization interface. This avoids making
    product startup depend on the legacy Search API, which can reject otherwise
    valid programmatic requests with HTTP 406 in some environments.

    The client is synchronous by design; Flet callers must offload `fetch_recent()`
    from the UI event loop (the application service already does this).
    """

    def __init__(
        self,
        *,
        base_url: str = "https://rss.arxiv.org/atom",
        oai_base_url: str = "https://oaipmh.arxiv.org/oai",
        user_agent: str = "EmbodiedAIRadar/0.5.0 (personal research discovery app)",
        minimum_interval_seconds: float = 3.0,
        timeout_seconds: float = 20.0,
        history_lookback_days: int = 7,
        session: requests.Session | None = None,
        diagnostic_log_path: str | Path | None = None,
        diagnostic_max_bytes: int = 1_000_000,
        max_retries: int = 3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.oai_base_url = oai_base_url
        self.user_agent = user_agent
        self.minimum_interval_seconds = minimum_interval_seconds
        self.timeout_seconds = timeout_seconds
        self.history_lookback_days = history_lookback_days
        self._session = session or requests.Session()
        self.diagnostic_log_path = (
            Path(diagnostic_log_path) if diagnostic_log_path is not None else None
        )
        if diagnostic_max_bytes < 1:
            raise ValueError("diagnostic_max_bytes must be positive")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        self.diagnostic_max_bytes = diagnostic_max_bytes
        self.max_retries = max_retries
        self.last_diagnostic_log_path = ""
        # requests supplies browser-compatible HTTP negotiation defaults such as
        # Accept: */* and content-encoding support.  Keep the client identity
        # descriptive while avoiding urllib's sparse/default transport, which
        # produced HTTP 406 for arXiv Search/OAI on the user's Windows network.
        self._session.headers.update({"User-Agent": self.user_agent})
        self._last_request_monotonic: float | None = None
        # All research-domain jobs share one transport. A process-wide request
        # slot for this client keeps requests.Session thread-safe and preserves
        # arXiv's minimum interval even when multiple domain sync jobs run
        # concurrently in background threads.
        self._request_lock = Lock()
        self._diagnostic_lock = Lock()
        self._diagnostics_initialized = False
        self.last_fetch_note = ""

    def build_recent_url(
        self,
        *,
        categories: tuple[str, ...] = DEFAULT_CATEGORIES,
        max_results: int = 150,
    ) -> str:
        if not categories:
            raise ValueError("At least one arXiv category is required")
        if not 1 <= max_results <= 2000:
            raise ValueError("max_results must be between 1 and 2000")

        category_path = "+".join(categories)
        return f"{self.base_url}/{category_path}"

    def fetch_recent(
        self,
        *,
        categories: tuple[str, ...] = DEFAULT_CATEGORIES,
        max_results: int = 150,
    ) -> list[Paper]:
        """Return today's announcement feed, with OAI-PMH history fallback."""

        self._reset_diagnostics()
        feed_url = self.build_recent_url(categories=categories, max_results=max_results)
        papers = self._fetch_atom_url(feed_url, source_label="news feed")
        if papers:
            self.last_fetch_note = "已读取 arXiv 官方每日更新源。"
            return papers[:max_results]

        try:
            historical = self.fetch_recent_history(
                categories=categories,
                max_results=max_results,
            )
        except ArxivError as exc:
            self.last_fetch_note = (
                "arXiv 每日更新源当前没有条目；OAI-PMH 最近历史回填也暂时不可用："
                f"{exc}"
            )
            return []

        if historical:
            self.last_fetch_note = (
                "arXiv 每日更新源当前没有条目，已通过 OAI-PMH 自动回填最近历史论文。"
            )
        else:
            self.last_fetch_note = "arXiv 每日更新源和 OAI-PMH 最近历史当前都没有条目。"
        return historical[:max_results]

    def build_oai_history_url(
        self,
        *,
        category: str | None = None,
        from_date: date | None = None,
        until_date: date | None = None,
        resumption_token: str | None = None,
    ) -> str:
        if resumption_token:
            params = urllib.parse.urlencode(
                {"verb": "ListRecords", "resumptionToken": resumption_token}
            )
            return f"{self.oai_base_url}?{params}"

        if not category or "." not in category:
            raise ValueError("category must look like 'cs.RO'")
        archive, subject = category.split(".", maxsplit=1)
        if not archive or not subject:
            raise ValueError("category must look like 'cs.RO'")

        if from_date is None:
            from_date = datetime.now(UTC).date() - timedelta(days=self.history_lookback_days)
        if until_date is not None and from_date > until_date:
            raise ValueError("from_date must not be after until_date")

        values = {
            "verb": "ListRecords",
            "metadataPrefix": "arXiv",
            "set": f"{archive}:{archive}:{subject}",
            "from": from_date.isoformat(),
        }
        if until_date is not None:
            values["until"] = until_date.isoformat()
        params = urllib.parse.urlencode(values)
        return f"{self.oai_base_url}?{params}"

    def fetch_recent_history(
        self,
        *,
        categories: tuple[str, ...] = DEFAULT_CATEGORIES,
        max_results: int = 150,
    ) -> list[Paper]:
        if not categories:
            raise ValueError("At least one arXiv category is required")
        if not 1 <= max_results <= 2000:
            raise ValueError("max_results must be between 1 and 2000")

        from_date = datetime.now(UTC).date() - timedelta(days=self.history_lookback_days)
        merged: dict[str, Paper] = {}
        errors: list[str] = []

        # Keep the lightweight one-page path for the legacy daily-feed fallback.
        # Range synchronization below follows OAI-PMH resumption tokens fully.
        for category in categories:
            url = self.build_oai_history_url(category=category, from_date=from_date)
            try:
                category_papers = self._fetch_oai_url(
                    url,
                    source_label=f"OAI-PMH {category}",
                )
            except ArxivError as exc:
                errors.append(f"{category}: {exc}")
                continue

            for paper in category_papers:
                identity = _base_arxiv_id(paper.arxiv_id)
                previous = merged.get(identity)
                if previous is None or paper.updated_at > previous.updated_at:
                    merged[identity] = paper

        if not merged and errors:
            raise ArxivError("; ".join(errors))

        papers = sorted(merged.values(), key=lambda paper: paper.updated_at, reverse=True)
        return papers[:max_results]

    def fetch_date_range(
        self,
        *,
        from_date: date,
        until_date: date,
        categories: tuple[str, ...] = DEFAULT_CATEGORIES,
        max_pages_per_category: int = 250,
        cancel_event: Event | None = None,
        progress_callback: Callable[[ArxivHarvestProgress], None] | None = None,
    ) -> list[Paper]:
        """Harvest a complete OAI-PMH date range, following resumption tokens."""

        if not categories:
            raise ValueError("At least one arXiv category is required")
        if from_date > until_date:
            raise ValueError("from_date must not be after until_date")
        if max_pages_per_category < 1:
            raise ValueError("max_pages_per_category must be positive")

        self._reset_diagnostics()
        merged: dict[str, Paper] = {}

        category_count = len(categories)
        for category_index, category in enumerate(categories, start=1):
            self._raise_if_cancelled(cancel_event)
            token: str | None = None
            page_number = 0
            if progress_callback is not None:
                progress_callback(
                    ArxivHarvestProgress(
                        category=category,
                        category_index=category_index,
                        category_count=category_count,
                        page_number=0,
                        fetched_count=len(merged),
                        completed_categories=category_index - 1,
                        phase="category_start",
                    )
                )
            while True:
                self._raise_if_cancelled(cancel_event)
                page_number += 1
                if progress_callback is not None:
                    progress_callback(
                        ArxivHarvestProgress(
                            category=category,
                            category_index=category_index,
                            category_count=category_count,
                            page_number=page_number,
                            fetched_count=len(merged),
                            completed_categories=category_index - 1,
                            phase="page_start",
                        )
                    )
                if page_number > max_pages_per_category:
                    raise ArxivError(
                        f"OAI-PMH {category} exceeded {max_pages_per_category} pages"
                    )
                url = self.build_oai_history_url(
                    category=category if token is None else None,
                    from_date=from_date if token is None else None,
                    until_date=until_date if token is None else None,
                    resumption_token=token,
                )
                if cancel_event is None:
                    papers, token = self._fetch_oai_page(
                        url,
                        source_label=f"OAI-PMH {category} page {page_number}",
                    )
                else:
                    papers, token = self._fetch_oai_page(
                        url,
                        source_label=f"OAI-PMH {category} page {page_number}",
                        cancel_event=cancel_event,
                    )
                # Publish OAI records in bounded batches so the application can
                # persist and reveal papers while a large page is still being
                # processed. Twenty is the product-level visible batch size for
                # first-run and ongoing Tablet sync.
                batch_size = 20
                for offset in range(0, len(papers), batch_size):
                    batch = papers[offset : offset + batch_size]
                    for paper in batch:
                        identity = _base_arxiv_id(paper.arxiv_id)
                        previous = merged.get(identity)
                        if previous is None or paper.updated_at > previous.updated_at:
                            merged[identity] = paper
                    if progress_callback is not None and batch:
                        is_final_batch = offset + batch_size >= len(papers)
                        progress_callback(
                            ArxivHarvestProgress(
                                category=category,
                                category_index=category_index,
                                category_count=category_count,
                                page_number=page_number,
                                fetched_count=len(merged),
                                completed_categories=(
                                    category_index
                                    if is_final_batch and not token
                                    else category_index - 1
                                ),
                                phase=(
                                    "category_complete"
                                    if is_final_batch and not token
                                    else "page_batch"
                                ),
                                papers=tuple(batch),
                            )
                        )
                if progress_callback is not None and not papers:
                    progress_callback(
                        ArxivHarvestProgress(
                            category=category,
                            category_index=category_index,
                            category_count=category_count,
                            page_number=page_number,
                            fetched_count=len(merged),
                            completed_categories=(
                                category_index if not token else category_index - 1
                            ),
                            phase="category_complete" if not token else "page_complete",
                            papers=(),
                        )
                    )
                if not token:
                    break

        papers = sorted(merged.values(), key=lambda paper: paper.updated_at, reverse=True)
        self.last_fetch_note = (
            f"已通过 OAI-PMH 同步 {from_date.isoformat()} 至 {until_date.isoformat()}。"
        )
        return papers

    def _fetch_atom_url(self, url: str, *, source_label: str) -> list[Paper]:
        payload = self._fetch_bytes(url, source_label=source_label)
        try:
            return parse_atom_feed(payload)
        except (ET.ParseError, ValueError) as exc:
            raise ArxivError(f"Invalid arXiv Atom response from {source_label}: {exc}") from exc

    def _fetch_oai_url(self, url: str, *, source_label: str) -> list[Paper]:
        papers, _token = self._fetch_oai_page(url, source_label=source_label)
        return papers

    def _fetch_oai_page(
        self,
        url: str,
        *,
        source_label: str,
        cancel_event: Event | None = None,
    ) -> tuple[list[Paper], str | None]:
        payload = self._fetch_bytes(
            url,
            source_label=source_label,
            cancel_event=cancel_event,
        )
        try:
            return parse_oai_arxiv_feed(payload)
        except (ET.ParseError, ValueError) as exc:
            raise ArxivError(f"Invalid arXiv OAI-PMH response from {source_label}: {exc}") from exc

    def _fetch_bytes(
        self,
        url: str,
        *,
        source_label: str,
        cancel_event: Event | None = None,
    ) -> bytes:
        last_error: requests.RequestException | None = None
        for attempt in range(self.max_retries + 1):
            self._raise_if_cancelled(cancel_event)
            self._acquire_request_slot(cancel_event)
            response: requests.Response | None = None
            request_started = False
            try:
                # The interval is checked while holding the shared slot so two
                # concurrent domain jobs cannot both observe the same idle window.
                self._respect_request_interval(cancel_event)
                self._raise_if_cancelled(cancel_event)
                request_started = True
                response = self._session.get(url, timeout=self.timeout_seconds)
                self._raise_if_cancelled(cancel_event)
                self._write_http_diagnostic(
                    source_label=source_label,
                    url=url,
                    response=response,
                )
                response.raise_for_status()
                return response.content
            except requests.RequestException as exc:
                last_error = exc
                if response is None:
                    self._write_http_diagnostic(
                        source_label=source_label,
                        url=url,
                        error=exc,
                    )
                if attempt < self.max_retries and self._is_retryable_request_error(exc):
                    continue
                status = exc.response.status_code if exc.response is not None else None
                content_type = (
                    exc.response.headers.get("Content-Type")
                    if exc.response is not None
                    else None
                )
                detail = f"HTTP {status}" if status is not None else str(exc)
                if content_type:
                    detail += f" ({content_type})"
                raise ArxivError(f"Unable to reach arXiv {source_label}: {detail}") from exc
            finally:
                if request_started:
                    self._last_request_monotonic = time.monotonic()
                self._request_lock.release()

        # The loop always returns or raises; keep a defensive terminal branch for
        # static analyzers and future edits.
        raise ArxivError(f"Unable to reach arXiv {source_label}: {last_error}")

    @staticmethod
    def _is_retryable_request_error(exc: requests.RequestException) -> bool:
        if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
            return True
        status = exc.response.status_code if exc.response is not None else None
        return status == 429 or (status is not None and 500 <= status <= 599)

    def _acquire_request_slot(self, cancel_event: Event | None) -> None:
        while True:
            self._raise_if_cancelled(cancel_event)
            if self._request_lock.acquire(timeout=0.1):
                return

    def _reset_diagnostics(self) -> None:
        if self.diagnostic_log_path is None:
            return
        with self._diagnostic_lock:
            if self._diagnostics_initialized:
                return
            try:
                self.diagnostic_log_path.parent.mkdir(parents=True, exist_ok=True)
                self.diagnostic_log_path.write_text(
                    "AI Paper Auto Retrieval & Analysis arXiv HTTP diagnostics\n"
                    f"started_utc={datetime.now(UTC).isoformat()}\n"
                    f"python={sys.version.replace(chr(10), ' ')}\n"
                    f"platform={platform.platform()}\n"
                    f"requests={requests.__version__}\n"
                    f"session_trust_env={getattr(self._session, 'trust_env', None)}\n"
                    f"HTTP_PROXY={_sanitize_proxy(os.environ.get('HTTP_PROXY'))}\n"
                    f"HTTPS_PROXY={_sanitize_proxy(os.environ.get('HTTPS_PROXY'))}\n"
                    f"ALL_PROXY={_sanitize_proxy(os.environ.get('ALL_PROXY'))}\n"
                    f"NO_PROXY={_sanitize_no_proxy(os.environ.get('NO_PROXY'))}\n\n",
                    encoding="utf-8",
                )
                self.last_diagnostic_log_path = str(self.diagnostic_log_path)
                self._diagnostics_initialized = True
            except OSError:
                self.last_diagnostic_log_path = ""

    def _write_http_diagnostic(
        self,
        *,
        source_label: str,
        url: str,
        response: requests.Response | None = None,
        error: Exception | None = None,
    ) -> None:
        if self.diagnostic_log_path is None:
            return
        with self._diagnostic_lock:
            try:
                self.diagnostic_log_path.parent.mkdir(parents=True, exist_ok=True)
                lines = [
                    "=" * 80,
                    f"time_utc={datetime.now(UTC).isoformat()}",
                    f"source={source_label}",
                    f"url={url}",
                ]
                if response is not None:
                    prepared = response.request
                    lines.extend(
                        [
                            f"request_method={prepared.method if prepared is not None else 'GET'}",
                            f"request_url={prepared.url if prepared is not None else url}",
                            "request_headers:",
                        ]
                    )
                    if prepared is not None:
                        lines.extend(
                            f"  {name}: {_redact_header(name, value)}"
                            for name, value in prepared.headers.items()
                        )
                    lines.extend(
                        [
                            f"status={response.status_code} {response.reason}",
                            f"final_url={response.url}",
                            f"redirect_history={[item.status_code for item in response.history]}",
                            "response_headers:",
                        ]
                    )
                    lines.extend(
                        f"  {name}: {_redact_header(name, value)}"
                        for name, value in response.headers.items()
                    )
                    encoding = getattr(response, "encoding", None) or "utf-8"
                    preview = (
                        response.content[:4000]
                        .decode(encoding, errors="replace")
                        .replace("\x00", "")
                    )
                    lines.extend(["response_body_preview:", preview])
                if error is not None:
                    lines.append(f"transport_error={type(error).__name__}: {error}")
                lines.append("")
                payload = "\n".join(lines) + "\n"
                self._rotate_diagnostic_log_if_needed(len(payload.encode("utf-8")))
                with self.diagnostic_log_path.open("a", encoding="utf-8") as handle:
                    handle.write(payload)
                self.last_diagnostic_log_path = str(self.diagnostic_log_path)
            except (OSError, UnicodeError):
                return

    def _rotate_diagnostic_log_if_needed(self, incoming_bytes: int = 0) -> None:
        if self.diagnostic_log_path is None or not self.diagnostic_log_path.exists():
            return
        try:
            current_size = self.diagnostic_log_path.stat().st_size
            if current_size + incoming_bytes <= self.diagnostic_max_bytes:
                return
            rotated = self.diagnostic_log_path.with_suffix(
                self.diagnostic_log_path.suffix + ".1"
            )
            rotated.unlink(missing_ok=True)
            self.diagnostic_log_path.replace(rotated)
        except OSError:
            return

    def _respect_request_interval(self, cancel_event: Event | None = None) -> None:
        if self._last_request_monotonic is None:
            return
        elapsed = time.monotonic() - self._last_request_monotonic
        remaining = self.minimum_interval_seconds - elapsed
        if remaining <= 0:
            return
        if cancel_event is not None:
            if cancel_event.wait(remaining):
                raise ArxivSyncCancelled("arXiv synchronization cancelled by user")
        else:
            time.sleep(remaining)

    @staticmethod
    def _raise_if_cancelled(cancel_event: Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise ArxivSyncCancelled("arXiv synchronization cancelled by user")


def _redact_header(name: str, value: str) -> str:
    sensitive = {"authorization", "cookie", "proxy-authorization", "set-cookie", "x-api-key"}
    if name.casefold() in sensitive:
        return "<redacted>"
    return value


def _sanitize_proxy(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return "<configured>"
    if not parsed.hostname:
        return "<configured>"
    host = parsed.hostname
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    scheme = f"{parsed.scheme}://" if parsed.scheme else ""
    return f"{scheme}{host}"


def _sanitize_no_proxy(value: str | None) -> str:
    if not value:
        return ""
    entries = [item.strip() for item in value.split(",") if item.strip()]
    return f"<configured:{len(entries)} entries>"


def parse_atom_feed(payload: bytes | str) -> list[Paper]:
    """Parse either arXiv's daily Atom news feed or Search API Atom entries."""

    root = ET.fromstring(payload)
    papers: list[Paper] = []

    for entry in root.findall("atom:entry", NS):
        entry_id = _required_text(entry, "atom:id")
        is_news_feed = entry_id.startswith(_RSS_ID_PREFIX)
        arxiv_id = _extract_arxiv_id(entry_id)

        title = _clean_text(_required_text(entry, "atom:title"))
        raw_summary = _required_text(entry, "atom:summary")
        summary = _clean_news_summary(raw_summary) if is_news_feed else _clean_text(raw_summary)

        published = _parse_datetime(_required_text(entry, "atom:published"))
        if is_news_feed:
            updated = published
        else:
            updated = _parse_datetime(_required_text(entry, "atom:updated"))

        authors, author_affiliations = _extract_authors(entry, is_news_feed=is_news_feed)
        categories = tuple(
            category.attrib["term"]
            for category in entry.findall("atom:category", NS)
            if category.attrib.get("term")
        )

        abstract_url: str | None = None
        pdf_url: str | None = None
        for link in entry.findall("atom:link", NS):
            href = link.attrib.get("href")
            if not href:
                continue
            if link.attrib.get("rel") == "alternate":
                abstract_url = href
            if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
                pdf_url = href

        if is_news_feed:
            abstract_url = f"https://arxiv.org/abs/{arxiv_id}"
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
        else:
            abstract_url = abstract_url or entry_id

        papers.append(
            Paper(
                arxiv_id=arxiv_id,
                title=title,
                summary=summary,
                authors=authors,
                categories=categories,
                published_at=published,
                updated_at=updated,
                abstract_url=abstract_url,
                pdf_url=pdf_url,
                author_affiliations=author_affiliations,
                affiliation_source=("arXiv metadata" if any(author_affiliations) else ""),
            )
        )

    return papers


def parse_oai_arxiv_feed(payload: bytes | str) -> tuple[list[Paper], str | None]:
    """Parse arXiv OAI-PMH `metadataPrefix=arXiv` ListRecords output.

    The parser matches elements by local name rather than hard-coding the arXiv
    metadata namespace, which keeps it tolerant of namespace-prefix differences
    while still requiring the OAI-PMH record/metadata structure.
    """

    root = ET.fromstring(payload)
    papers: list[Paper] = []

    list_records = _first_descendant(root, "ListRecords")
    if list_records is None:
        error = _first_descendant(root, "error")
        if error is not None:
            if error.attrib.get("code") == "noRecordsMatch":
                return [], None
            message = _clean_text(error.text or "Unknown OAI-PMH error")
            raise ValueError(message)
        return [], None

    for record in _children_by_local_name(list_records, "record"):
        header = _child_by_local_name(record, "header")
        if header is None or header.attrib.get("status") == "deleted":
            continue
        metadata = _child_by_local_name(record, "metadata")
        if metadata is None or len(metadata) == 0:
            continue
        arxiv_meta = metadata[0]

        arxiv_id = _child_text_by_local_name(arxiv_meta, "id")
        if not arxiv_id:
            identifier = _child_text_by_local_name(header, "identifier")
            arxiv_id = identifier.removeprefix(_OAI_ID_PREFIX)
        if not arxiv_id:
            continue

        title = _clean_text(_child_text_by_local_name(arxiv_meta, "title"))
        summary = _clean_text(_child_text_by_local_name(arxiv_meta, "abstract"))
        if not title or not summary:
            continue

        created_text = _child_text_by_local_name(arxiv_meta, "created")
        updated_text = _child_text_by_local_name(arxiv_meta, "updated") or created_text
        if not created_text:
            # OAI header datestamp is a modification timestamp, not submission time,
            # but it is a better last-resort value than rejecting the whole record.
            created_text = _child_text_by_local_name(header, "datestamp")
            updated_text = updated_text or created_text
        published = _parse_oai_date(created_text)
        updated = _parse_oai_date(updated_text)

        categories_text = _child_text_by_local_name(arxiv_meta, "categories")
        categories = tuple(part for part in categories_text.split() if part)
        authors, author_affiliations = _extract_oai_authors(arxiv_meta)

        papers.append(
            Paper(
                arxiv_id=arxiv_id,
                title=title,
                summary=summary,
                authors=authors,
                categories=categories,
                published_at=published,
                updated_at=updated,
                abstract_url=f"https://arxiv.org/abs/{arxiv_id}",
                pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
                author_affiliations=author_affiliations,
                affiliation_source=("arXiv metadata" if any(author_affiliations) else ""),
            )
        )

    token_element = _child_by_local_name(list_records, "resumptionToken")
    token = _clean_text(token_element.text or "") if token_element is not None else ""
    return papers, token or None


def _base_arxiv_id(arxiv_id: str) -> str:
    return re.sub(r"v\d+$", "", arxiv_id)


def _extract_arxiv_id(entry_id: str) -> str:
    if entry_id.startswith(_RSS_ID_PREFIX):
        return entry_id.removeprefix(_RSS_ID_PREFIX)
    if "/abs/" in entry_id:
        return entry_id.rsplit("/abs/", maxsplit=1)[-1]
    return entry_id.rsplit(":", maxsplit=1)[-1]


def _extract_authors(
    entry: ET.Element,
    *,
    is_news_feed: bool,
) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    if is_news_feed:
        creator = _clean_text(entry.findtext("dc:creator", default="", namespaces=NS))
        if not creator:
            return (), ()
        authors = tuple(part.strip() for part in creator.split(",") if part.strip())
        return authors, tuple(() for _ in authors)

    authors: list[str] = []
    affiliations: list[tuple[str, ...]] = []
    for author in entry.findall("atom:author", NS):
        name = _clean_text(author.findtext("atom:name", default="", namespaces=NS))
        if not name:
            continue
        author_affiliations = tuple(
            value
            for value in (
                _clean_text(item.text or "")
                for item in author.findall("arxiv:affiliation", NS)
            )
            if value
        )
        authors.append(name)
        affiliations.append(author_affiliations)
    return tuple(authors), tuple(affiliations)


def _extract_oai_authors(
    arxiv_meta: ET.Element,
) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    authors_container = _child_by_local_name(arxiv_meta, "authors")
    if authors_container is None:
        return (), ()

    authors: list[str] = []
    affiliations: list[tuple[str, ...]] = []
    for author in _children_by_local_name(authors_container, "author"):
        forenames = _clean_text(_child_text_by_local_name(author, "forenames"))
        keyname = _clean_text(_child_text_by_local_name(author, "keyname"))
        suffix = _clean_text(_child_text_by_local_name(author, "suffix"))
        name = " ".join(part for part in (forenames, keyname, suffix) if part)
        if not name:
            continue
        author_affiliations = tuple(
            value
            for value in (
                _clean_text(child.text or "")
                for child in _children_by_local_name(author, "affiliation")
            )
            if value
        )
        authors.append(name)
        affiliations.append(author_affiliations)
    return tuple(authors), tuple(affiliations)

def _clean_news_summary(value: str) -> str:
    cleaned = _clean_text(value)
    match = re.match(
        r"^arXiv:\S+\s+Announce\s+Type:\s*\S+\s+Abstract:\s*(.*)$",
        cleaned,
        flags=re.IGNORECASE,
    )
    return match.group(1).strip() if match else cleaned


def _required_text(element: ET.Element, path: str) -> str:
    value = element.findtext(path, default="", namespaces=NS).strip()
    if not value:
        raise ValueError(f"Missing required Atom field: {path}")
    return value


def _local_name(tag: str) -> str:
    return tag.rsplit("}", maxsplit=1)[-1] if "}" in tag else tag


def _children_by_local_name(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _local_name(child.tag) == name]


def _child_by_local_name(element: ET.Element, name: str) -> ET.Element | None:
    return next((child for child in element if _local_name(child.tag) == name), None)


def _first_descendant(element: ET.Element, name: str) -> ET.Element | None:
    return next((item for item in element.iter() if _local_name(item.tag) == name), None)


def _child_text_by_local_name(element: ET.Element, name: str) -> str:
    child = _child_by_local_name(element, name)
    return (child.text or "").strip() if child is not None else ""


def _clean_text(value: str) -> str:
    return " ".join(value.split())


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parse_oai_date(value: str) -> datetime:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("Missing OAI-PMH date")
    if "T" in cleaned:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return datetime.fromisoformat(cleaned).replace(tzinfo=UTC)
