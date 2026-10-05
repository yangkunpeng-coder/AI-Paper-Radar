"""Compatibility entry point.

New code should import :mod:`ai_paper_analyzer.ui.app_factory`.  This module is
kept so older launchers/imports continue to route through the same factory.
"""

from ai_paper_analyzer.ui.app_factory import main

__all__ = ["main"]
