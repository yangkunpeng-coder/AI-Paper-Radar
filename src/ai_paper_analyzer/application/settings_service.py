from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from ai_paper_analyzer.domain.llm import (
    DEFAULT_DEEPSEEK_MODEL,
    LLMSettings,
    SUPPORTED_DEEPSEEK_MODELS,
)
from ai_paper_analyzer.domain.research_domains import (
    DOMAIN_EMBODIED,
    normalize_domain_key,
    normalize_topic_key,
    research_domain_keys,
)

API_KEY_STORAGE_KEY = "embodied_ai_radar.deepseek_api_key"
MODEL_PREF_KEY = "embodied_ai_radar.deepseek_model"
AUTO_ANALYZE_PREF_KEY = "embodied_ai_radar.deepseek_auto_analyze"
AUTO_SYNC_ON_START_PREF_KEY = "embodied_ai_radar.auto_sync_on_start"
AUTO_SYNC_DOMAINS_PREF_KEY = "embodied_ai_radar.auto_sync_domains"
TASK_NOTIFICATIONS_PREF_KEY = "embodied_ai_radar.task_notifications"
VIEW_DOMAIN_PREF_KEY = "embodied_ai_radar.view_domain"

# Legacy global view keys from 0.7.9-0.8.0. They remain readable and are also
# mirrored on save so a user can move between adjacent versions without losing
# the last visible state. 0.8.1+ stores the authoritative filter state per domain.
VIEW_DAYS_PREF_KEY = "embodied_ai_radar.view_days"
VIEW_FAVORITES_ONLY_PREF_KEY = "embodied_ai_radar.view_favorites_only"
VIEW_FOCUS_TAG_PREF_KEY = "embodied_ai_radar.view_focus_tag"
VIEW_SORT_MODE_PREF_KEY = "embodied_ai_radar.view_sort_mode"
VIEW_STATE_PREFIX = "embodied_ai_radar.view_state"

_ALLOWED_VIEW_DOMAINS = set(research_domain_keys())
_ALLOWED_VIEW_DAYS = {7, 30, 90, 180, 365}
_ALLOWED_SORT_MODES = {"interest", "relevance", "latest"}


@dataclass(frozen=True, slots=True)
class ViewPreferences:
    active_domain: str = DOMAIN_EMBODIED
    days: int = 30
    favorites_only: bool = False
    focus_tag: str = "all"
    sort_mode: str = "interest"


class SecretStore(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str) -> None: ...

    async def remove(self, key: str) -> None: ...

    async def contains_key(self, key: str) -> bool: ...


class PreferenceStore(Protocol):
    async def get(self, key: str) -> Any: ...

    async def set(self, key: str, value: Any) -> None: ...


def _domain_view_key(domain_key: str, field: str) -> str:
    return f"{VIEW_STATE_PREFIX}.{domain_key}.{field}"


def _normalize_auto_sync_domains(raw: Any) -> tuple[str, ...] | None:
    """Normalize a persisted domain selection.

    ``None`` means the preference has never been written and lets callers apply
    the legacy fallback. An explicit empty string/list means the user selected no
    domains (valid only while startup auto-sync is disabled in the UI).
    """

    if raw is None:
        return None
    if isinstance(raw, str):
        candidates = [part.strip() for part in raw.split(",") if part.strip()]
    elif isinstance(raw, (list, tuple, set)):
        candidates = [item for item in raw if isinstance(item, str)]
    else:
        return ()

    selected = set(candidates)
    return tuple(key for key in research_domain_keys() if key in selected)


class SettingsService:
    def __init__(self, secrets: SecretStore, preferences: PreferenceStore) -> None:
        self._secrets = secrets
        self._preferences = preferences

    async def load(self) -> LLMSettings:
        raw_model = await self._preferences.get(MODEL_PREF_KEY)
        model = raw_model if raw_model in SUPPORTED_DEEPSEEK_MODELS else DEFAULT_DEEPSEEK_MODEL
        raw_auto = await self._preferences.get(AUTO_ANALYZE_PREF_KEY)
        raw_auto_sync = await self._preferences.get(AUTO_SYNC_ON_START_PREF_KEY)
        raw_task_notifications = await self._preferences.get(TASK_NOTIFICATIONS_PREF_KEY)
        has_api_key = await self._secrets.contains_key(API_KEY_STORAGE_KEY)
        return LLMSettings(
            model=model,
            auto_analyze=bool(raw_auto) if raw_auto is not None else False,
            auto_sync_on_start=bool(raw_auto_sync) if raw_auto_sync is not None else True,
            task_notifications=(
                bool(raw_task_notifications) if raw_task_notifications is not None else True
            ),
            has_api_key=has_api_key,
        )

    async def _load_domain_view_preferences(
        self,
        domain_key: str,
        *,
        allow_legacy_fallback: bool,
    ) -> ViewPreferences:
        normalized_domain = normalize_domain_key(domain_key)
        raw_days = await self._preferences.get(_domain_view_key(normalized_domain, "days"))
        raw_favorites = await self._preferences.get(
            _domain_view_key(normalized_domain, "favorites_only")
        )
        raw_focus = await self._preferences.get(_domain_view_key(normalized_domain, "focus_tag"))
        raw_sort = await self._preferences.get(_domain_view_key(normalized_domain, "sort_mode"))

        has_domain_state = any(
            value is not None for value in (raw_days, raw_favorites, raw_focus, raw_sort)
        )
        if allow_legacy_fallback and not has_domain_state:
            raw_legacy_domain = await self._preferences.get(VIEW_DOMAIN_PREF_KEY)
            legacy_domain = normalize_domain_key(
                raw_legacy_domain if isinstance(raw_legacy_domain, str) else None
            )
            if legacy_domain == normalized_domain:
                raw_days = await self._preferences.get(VIEW_DAYS_PREF_KEY)
                raw_favorites = await self._preferences.get(VIEW_FAVORITES_ONLY_PREF_KEY)
                raw_focus = await self._preferences.get(VIEW_FOCUS_TAG_PREF_KEY)
                raw_sort = await self._preferences.get(VIEW_SORT_MODE_PREF_KEY)

        days = raw_days if type(raw_days) is int and raw_days in _ALLOWED_VIEW_DAYS else 30
        favorites_only = raw_favorites if isinstance(raw_favorites, bool) else False
        focus_tag = normalize_topic_key(
            normalized_domain,
            raw_focus if isinstance(raw_focus, str) else None,
        )
        sort_mode = (
            raw_sort
            if isinstance(raw_sort, str) and raw_sort in _ALLOWED_SORT_MODES
            else "interest"
        )
        return ViewPreferences(
            active_domain=normalized_domain,
            days=days,
            favorites_only=favorites_only,
            focus_tag=focus_tag,
            sort_mode=sort_mode,
        )

    async def load_view_preferences(self) -> ViewPreferences:
        raw_domain = await self._preferences.get(VIEW_DOMAIN_PREF_KEY)
        active_domain = normalize_domain_key(raw_domain if isinstance(raw_domain, str) else None)
        return await self._load_domain_view_preferences(
            active_domain,
            allow_legacy_fallback=True,
        )

    async def load_domain_view_preferences(self, domain_key: str) -> ViewPreferences:
        normalized_domain = normalize_domain_key(domain_key)
        return await self._load_domain_view_preferences(
            normalized_domain,
            allow_legacy_fallback=True,
        )

    async def save_active_domain(self, domain_key: str) -> str:
        """Persist only the last active top-level research domain.

        Domain-specific filter state is already saved when the user changes a
        range, scope, topic, or sort option. Keeping domain navigation to one
        preference write avoids re-writing every filter key on each tab switch.
        """

        if domain_key not in _ALLOWED_VIEW_DOMAINS:
            raise ValueError(f"Unsupported research domain: {domain_key}")
        normalized_domain = normalize_domain_key(domain_key)
        await self._preferences.set(VIEW_DOMAIN_PREF_KEY, normalized_domain)
        return normalized_domain

    async def save_view_preferences(
        self,
        *,
        active_domain: str = DOMAIN_EMBODIED,
        days: int,
        favorites_only: bool,
        focus_tag: str,
        sort_mode: str,
    ) -> ViewPreferences:
        if active_domain not in _ALLOWED_VIEW_DOMAINS:
            raise ValueError(f"Unsupported research domain: {active_domain}")
        if days not in _ALLOWED_VIEW_DAYS:
            raise ValueError(f"Unsupported view range: {days}")
        if sort_mode not in _ALLOWED_SORT_MODES:
            raise ValueError(f"Unsupported sort mode: {sort_mode}")

        normalized_focus = normalize_topic_key(active_domain, focus_tag)
        values = {
            "days": days,
            "favorites_only": bool(favorites_only),
            "focus_tag": normalized_focus,
            "sort_mode": sort_mode,
        }
        await self._preferences.set(VIEW_DOMAIN_PREF_KEY, active_domain)
        for field, value in values.items():
            await self._preferences.set(_domain_view_key(active_domain, field), value)

        # Mirror the active domain into the legacy global keys for adjacent-version
        # compatibility. New code always reads the per-domain values first.
        await self._preferences.set(VIEW_DAYS_PREF_KEY, days)
        await self._preferences.set(VIEW_FAVORITES_ONLY_PREF_KEY, bool(favorites_only))
        await self._preferences.set(VIEW_FOCUS_TAG_PREF_KEY, normalized_focus)
        await self._preferences.set(VIEW_SORT_MODE_PREF_KEY, sort_mode)
        return ViewPreferences(
            active_domain=active_domain,
            days=days,
            favorites_only=bool(favorites_only),
            focus_tag=normalized_focus,
            sort_mode=sort_mode,
        )

    async def load_auto_sync_domain_keys(
        self,
        *,
        fallback_domain: str = DOMAIN_EMBODIED,
    ) -> tuple[str, ...]:
        # v1.2.3+: auto sync is intentionally simple: when enabled it always
        # covers every built-in research domain.  Ignore legacy subset choices
        # so an old preference cannot silently leave a domain unsynchronized.
        del fallback_domain
        normalized = research_domain_keys()
        await self._preferences.set(AUTO_SYNC_DOMAINS_PREF_KEY, ",".join(normalized))
        return normalized

    async def save_auto_sync_domain_keys(self, domain_keys: Iterable[str]) -> tuple[str, ...]:
        # Keep accepting the argument for adjacent-version/API compatibility,
        # but persist the product contract rather than a user-selected subset.
        requested = tuple(domain_keys)
        unsupported = [key for key in requested if key not in _ALLOWED_VIEW_DOMAINS]
        if unsupported:
            raise ValueError(f"Unsupported auto-sync domain: {unsupported[0]}")
        normalized = research_domain_keys()
        await self._preferences.set(AUTO_SYNC_DOMAINS_PREF_KEY, ",".join(normalized))
        return normalized

    async def save(
        self,
        *,
        model: str,
        auto_analyze: bool,
        auto_sync_on_start: bool = True,
        auto_sync_domain_keys: Iterable[str] | None = None,
        task_notifications: bool = True,
        api_key: str | None = None,
    ) -> LLMSettings:
        if model not in SUPPORTED_DEEPSEEK_MODELS:
            raise ValueError(f"Unsupported DeepSeek model: {model}")

        await self._preferences.set(MODEL_PREF_KEY, model)
        await self._preferences.set(AUTO_ANALYZE_PREF_KEY, auto_analyze)
        await self._preferences.set(AUTO_SYNC_ON_START_PREF_KEY, auto_sync_on_start)
        await self._preferences.set(TASK_NOTIFICATIONS_PREF_KEY, task_notifications)
        if auto_sync_domain_keys is not None:
            await self.save_auto_sync_domain_keys(auto_sync_domain_keys)

        normalized_key = (api_key or "").strip()
        if normalized_key:
            await self._secrets.set(API_KEY_STORAGE_KEY, normalized_key)

        return await self.load()

    async def get_api_key(self) -> str | None:
        value = await self._secrets.get(API_KEY_STORAGE_KEY)
        return value.strip() if value else None

    async def delete_api_key(self) -> LLMSettings:
        await self._secrets.remove(API_KEY_STORAGE_KEY)
        return await self.load()
