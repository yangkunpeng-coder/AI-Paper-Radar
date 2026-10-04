"""Pure Android tablet layout policy based on Material adaptive width classes."""

from __future__ import annotations

from enum import Enum
from typing import Any

TABLET_MIN_WIDTH_DP = 600.0
EXPANDED_MIN_WIDTH_DP = 840.0
LARGE_MIN_WIDTH_DP = 1200.0
EXTRA_LARGE_MIN_WIDTH_DP = 1600.0


class TabletWidthClass(str, Enum):
    MEDIUM = "medium"
    EXPANDED = "expanded"
    LARGE = "large"
    EXTRA_LARGE = "extra_large"


class TabletLayoutMode(str, Enum):
    SINGLE_PANE = "single_pane"
    LIST_DETAIL = "list_detail"


def _width(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return TABLET_MIN_WIDTH_DP
    return max(TABLET_MIN_WIDTH_DP, result)


def tablet_width_class(width: Any) -> TabletWidthClass:
    value = _width(width)
    if value >= EXTRA_LARGE_MIN_WIDTH_DP:
        return TabletWidthClass.EXTRA_LARGE
    if value >= LARGE_MIN_WIDTH_DP:
        return TabletWidthClass.LARGE
    if value >= EXPANDED_MIN_WIDTH_DP:
        return TabletWidthClass.EXPANDED
    return TabletWidthClass.MEDIUM


def tablet_layout_mode(width: Any) -> TabletLayoutMode:
    if _width(width) >= EXPANDED_MIN_WIDTH_DP:
        return TabletLayoutMode.LIST_DETAIL
    return TabletLayoutMode.SINGLE_PANE


def detail_pane_fraction(width: Any) -> float:
    """Keep the detail pane dominant without making text lines excessively wide."""
    width_class = tablet_width_class(width)
    if width_class in {TabletWidthClass.LARGE, TabletWidthClass.EXTRA_LARGE}:
        return 0.62
    return 0.60
