from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from threading import Event

from ai_paper_analyzer.application.library_service import LibraryService
from ai_paper_analyzer.domain.library import SyncState


@dataclass(slots=True)
class DailySyncGate:
    """Best-effort in-app wall-clock gate for one daily sync trigger.

    This is intentionally *not* a reliable background scheduler: it only fires
    while the desktop app process is alive. ``prime()`` marks today as already
    handled when the app starts after the target time, so a late launch does not
    unexpectedly duplicate the normal startup sync. If the app was already open
    before the target time (or resumes later after sleep), ``should_trigger()``
    allows one trigger for that local calendar day.
    """

    hour: int = 22
    minute: int = 0
    last_triggered_date: date | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.hour <= 23:
            raise ValueError("hour must be between 0 and 23")
        if not 0 <= self.minute <= 59:
            raise ValueError("minute must be between 0 and 59")

    @property
    def target_time(self) -> time:
        return time(self.hour, self.minute)

    def prime(self, now: datetime) -> None:
        """Initialize the gate without firing a missed run on late app launch."""

        if now.time() >= self.target_time:
            self.last_triggered_date = now.date()

    def should_trigger(self, now: datetime) -> bool:
        """Return True exactly once per local day after the configured time."""

        if now.time() < self.target_time:
            return False
        if self.last_triggered_date == now.date():
            return False
        self.last_triggered_date = now.date()
        return True


@dataclass(frozen=True, slots=True)
class SyncInterval:
    from_date: date
    until_date: date


def plan_sync_intervals(
    state: SyncState,
    *,
    desired_start: date,
    today: date,
    overlap_days: int = 2,
) -> tuple[SyncInterval, ...]:
    """Plan history backfill plus a small overlapping incremental refresh.

    Coverage is intentionally kept contiguous. A two-day overlap makes the sync
    tolerant of arXiv date boundaries and revisions; SQLite upserts make overlap
    idempotent.
    """

    if desired_start > today:
        raise ValueError("desired_start must not be after today")
    if overlap_days < 0:
        raise ValueError("overlap_days must be non-negative")

    earliest = state.earliest_covered_date
    latest = state.latest_covered_date

    if earliest is None or latest is None:
        return (SyncInterval(desired_start, today),)

    intervals: list[SyncInterval] = []

    if earliest > desired_start:
        intervals.append(SyncInterval(desired_start, min(today, earliest)))

    anchor = state.last_successful_sync.date() if state.last_successful_sync else latest
    incremental_start = max(desired_start, anchor - timedelta(days=overlap_days))
    intervals.append(SyncInterval(incremental_start, today))

    return _merge_intervals(intervals)


def plan_incremental_intervals(
    state: SyncState,
    *,
    today: date,
    overlap_days: int = 2,
    bootstrap_days: int = 7,
) -> tuple[SyncInterval, ...]:
    """Plan the ordinary latest-only refresh independently of browse filters.

    Once a durable checkpoint exists, ordinary synchronization starts from the
    latest successful checkpoint with a small overlap and advances to ``today``.
    On a brand-new library there is no checkpoint to anchor to, so bootstrap a
    deliberately small recent window. Historical coverage is handled separately
    by :func:`plan_sync_intervals` / ``SyncCoordinator.plan_backfill``.
    """

    if overlap_days < 0:
        raise ValueError("overlap_days must be non-negative")
    if bootstrap_days < 1:
        raise ValueError("bootstrap_days must be positive")

    latest = state.latest_covered_date
    if state.last_successful_sync is not None:
        anchor = state.last_successful_sync.date()
    elif latest is not None:
        anchor = latest
    else:
        return (
            SyncInterval(
                today - timedelta(days=bootstrap_days - 1),
                today,
            ),
        )

    incremental_start = min(today, anchor) - timedelta(days=overlap_days)
    return (SyncInterval(incremental_start, today),)


def chunk_sync_intervals(
    intervals: tuple[SyncInterval, ...] | list[SyncInterval],
    *,
    max_days: int = 31,
) -> tuple[SyncInterval, ...]:
    """Split sync work into cancel-safe chunks, newest first.

    Processing newest-to-oldest keeps the durable coverage range contiguous. After
    each completed chunk the repository can safely move its coverage boundary. If
    the user cancels, the next run naturally plans only the remaining older gap.
    """

    if max_days < 1:
        raise ValueError("max_days must be positive")

    chunks: list[SyncInterval] = []
    for interval in intervals:
        if interval.from_date > interval.until_date:
            raise ValueError("sync interval start must not be after end")
        cursor_end = interval.until_date
        while cursor_end >= interval.from_date:
            cursor_start = max(
                interval.from_date,
                cursor_end - timedelta(days=max_days - 1),
            )
            chunks.append(SyncInterval(cursor_start, cursor_end))
            cursor_end = cursor_start - timedelta(days=1)

    chunks.sort(key=lambda item: item.until_date, reverse=True)
    return tuple(chunks)


def _merge_intervals(intervals: list[SyncInterval]) -> tuple[SyncInterval, ...]:
    if not intervals:
        return ()

    ordered = sorted(intervals, key=lambda item: item.from_date)
    merged: list[SyncInterval] = [ordered[0]]
    for current in ordered[1:]:
        previous = merged[-1]
        if current.from_date <= previous.until_date + timedelta(days=1):
            merged[-1] = SyncInterval(
                previous.from_date,
                max(previous.until_date, current.until_date),
            )
        else:
            merged.append(current)
    return tuple(merged)


class SyncJobRegistry:
    """Track independent per-domain sync jobs and their cancellation signals.

    The registry intentionally contains no Flet or asyncio task objects so it can
    remain in the application layer and be unit tested. UI code owns task
    scheduling while this class guarantees that one domain cannot accidentally
    start twice and that cancelling one domain never affects another.
    """

    def __init__(self) -> None:
        self._cancel_events: dict[str, Event] = {}

    def start(self, domain_key: str) -> Event | None:
        normalized = domain_key.strip()
        if not normalized:
            raise ValueError("domain_key is required")
        if normalized in self._cancel_events:
            return None
        event = Event()
        self._cancel_events[normalized] = event
        return event

    def is_active(self, domain_key: str) -> bool:
        return domain_key in self._cancel_events

    def cancel(self, domain_key: str) -> bool:
        event = self._cancel_events.get(domain_key)
        if event is None:
            return False
        event.set()
        return True

    def cancel_event(self, domain_key: str) -> Event | None:
        return self._cancel_events.get(domain_key)

    def finish(self, domain_key: str) -> None:
        self._cancel_events.pop(domain_key, None)

    @property
    def active_domains(self) -> tuple[str, ...]:
        return tuple(self._cancel_events)

@dataclass(frozen=True, slots=True)
class SyncPlan:
    state: SyncState
    intervals: tuple[SyncInterval, ...]
    desired_start: date
    today: date


def historical_coverage_gap(
    state: SyncState,
    *,
    desired_start: date,
    today: date,
) -> SyncInterval | None:
    """Return the older uncovered range, if the local durable coverage is short.

    Browse-range changes are local-only.  The UI can use this helper to decide
    whether an *expanded* range deserves an explicit historical-fetch prompt,
    without turning the filter change itself into a network request.
    """

    if desired_start > today:
        raise ValueError("desired_start must not be after today")
    earliest = state.earliest_covered_date
    if earliest is not None and earliest <= desired_start:
        return None
    if earliest is None:
        return SyncInterval(desired_start, today)
    missing_until = min(today, earliest - timedelta(days=1))
    if missing_until < desired_start:
        return None
    return SyncInterval(desired_start, missing_until)


class SyncCoordinator:
    """Application-layer sync lifecycle helper independent from Flet widgets."""

    def __init__(
        self,
        library: LibraryService,
        *,
        max_chunk_days: int = 31,
        bootstrap_days: int = 7,
        bootstrap_chunk_days: int = 1,
        overlap_days: int = 2,
    ) -> None:
        if max_chunk_days < 1:
            raise ValueError("max_chunk_days must be positive")
        if bootstrap_days < 1:
            raise ValueError("bootstrap_days must be positive")
        if bootstrap_chunk_days < 1:
            raise ValueError("bootstrap_chunk_days must be positive")
        if overlap_days < 0:
            raise ValueError("overlap_days must be non-negative")
        self.library = library
        self.max_chunk_days = max_chunk_days
        self.bootstrap_days = bootstrap_days
        self.bootstrap_chunk_days = bootstrap_chunk_days
        self.overlap_days = overlap_days

    def historical_gap(
        self,
        *,
        domain_key: str,
        desired_start: date,
        today: date,
    ) -> SyncInterval | None:
        state = self.library.sync_state(domain_key=domain_key)
        return historical_coverage_gap(
            state,
            desired_start=desired_start,
            today=today,
        )

    def plan_backfill(self, *, domain_key: str, desired_start: date, today: date) -> SyncPlan:
        state = self.library.sync_state(domain_key=domain_key)
        base = plan_sync_intervals(
            state,
            desired_start=desired_start,
            today=today,
            overlap_days=self.overlap_days,
        )
        return SyncPlan(
            state=state,
            intervals=chunk_sync_intervals(base, max_days=self.max_chunk_days),
            desired_start=desired_start,
            today=today,
        )

    def plan_incremental(self, *, domain_key: str, today: date) -> SyncPlan:
        state = self.library.sync_state(domain_key=domain_key)
        base = plan_incremental_intervals(
            state,
            today=today,
            overlap_days=self.overlap_days,
            bootstrap_days=self.bootstrap_days,
        )
        has_checkpoint = (
            state.last_successful_sync is not None
            or state.latest_covered_date is not None
        )
        chunk_days = self.max_chunk_days if has_checkpoint else self.bootstrap_chunk_days
        intervals = chunk_sync_intervals(base, max_days=chunk_days)
        return SyncPlan(
            state=state,
            intervals=intervals,
            desired_start=base[-1].from_date,
            today=today,
        )

    def plan(self, *, domain_key: str, desired_start: date, today: date) -> SyncPlan:
        """Compatibility alias for explicit historical backfill planning."""

        return self.plan_backfill(
            domain_key=domain_key,
            desired_start=desired_start,
            today=today,
        )

    def begin(self, *, domain_key: str) -> SyncState:
        return self.library.begin_sync(domain_key=domain_key)

    def checkpoint(
        self,
        *,
        domain_key: str,
        from_date: date,
        until_date: date,
    ) -> SyncState:
        return self.library.record_sync_checkpoint(
            from_date=from_date,
            until_date=until_date,
            domain_key=domain_key,
        )

    def finish(self, *, domain_key: str) -> SyncState:
        return self.library.finish_sync(domain_key=domain_key)

    def cancel(self, *, domain_key: str) -> SyncState:
        return self.library.cancel_sync(domain_key=domain_key)

    def fail(self, message: str, *, domain_key: str) -> SyncState:
        return self.library.fail_sync(message, domain_key=domain_key)

