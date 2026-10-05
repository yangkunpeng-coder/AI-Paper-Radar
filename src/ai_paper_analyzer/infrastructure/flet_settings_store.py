from __future__ import annotations

from typing import Any

import flet as ft
import flet_secure_storage as fss


class FletSecureSecretStore:
    def __init__(self, storage: fss.SecureStorage) -> None:
        self._storage = storage

    async def get(self, key: str) -> str | None:
        return await self._storage.get(key)

    async def set(self, key: str, value: str) -> None:
        await self._storage.set(key, value)

    async def remove(self, key: str) -> None:
        await self._storage.remove(key)

    async def contains_key(self, key: str) -> bool:
        return await self._storage.contains_key(key)


class FletPreferenceStore:
    def __init__(self, preferences: ft.SharedPreferences) -> None:
        self._preferences = preferences

    async def get(self, key: str) -> Any:
        return await self._preferences.get(key)

    async def set(self, key: str, value: Any) -> None:
        await self._preferences.set(key, value)
