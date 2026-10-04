"""Compatibility entry point.

New code should import :mod:`embodied_ai_radar.ui.app_factory`.  This module is
kept so older launchers/imports continue to route through the same factory.
"""

from embodied_ai_radar.ui.app_factory import main

__all__ = ["main"]
