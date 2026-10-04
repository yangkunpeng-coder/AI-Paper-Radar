"""Pure platform/device classification for presentation routing.

This module intentionally has no Flet dependency so the routing policy can be
unit-tested in a normal Python environment. Android phone/tablet classification
uses the shortest available logical page dimension, which is stable across
portrait/landscape rotation and mirrors the product's 600dp split.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

ANDROID_TABLET_MIN_DP = 600.0


class UISurface(str, Enum):
    DESKTOP = "desktop"
    TABLET = "tablet"
    PHONE = "phone"


def normalize_platform(platform: Any) -> str:
    """Return a lowercase platform key from Flet PagePlatform or a string."""
    if platform is None:
        return ""
    value = getattr(platform, "value", platform)
    normalized = str(value).strip().lower()
    if "." in normalized:
        normalized = normalized.rsplit(".", 1)[-1]
    return normalized


def _positive_dimension(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def select_ui_surface(
    *,
    platform: Any,
    width: Any = None,
    height: Any = None,
) -> UISurface:
    """Choose the presentation surface for the current Flet session.

    Windows and other non-Android platforms keep the mature Desktop UI. Android
    uses the shortest available logical page dimension: >=600dp routes to the
    Tablet presentation, otherwise to Phone. If dimensions are unavailable, the
    conservative mobile fallback is Phone.
    """
    if normalize_platform(platform) != "android":
        return UISurface.DESKTOP

    dimensions = [
        dimension
        for dimension in (_positive_dimension(width), _positive_dimension(height))
        if dimension is not None
    ]
    if not dimensions:
        return UISurface.PHONE

    shortest_side = min(dimensions)
    if shortest_side >= ANDROID_TABLET_MIN_DP:
        return UISurface.TABLET
    return UISurface.PHONE
