"""Top-level presentation router for one-codebase multi-platform UI."""

from __future__ import annotations

import asyncio
import logging
import os

import flet as ft

from ai_paper_analyzer.infrastructure.app_logging import configure_logging
from ai_paper_analyzer.ui.platform_profile import UISurface, select_ui_surface

LOGGER = logging.getLogger(__name__)


async def main(page: ft.Page) -> None:
    """Route a Flet session to the presentation designed for its device class."""
    await asyncio.to_thread(configure_logging)
    surface = select_ui_surface(
        platform=page.platform or os.environ.get("FLET_PLATFORM"),
        width=page.width,
        height=page.height,
    )

    LOGGER.info("Starting presentation: %s", surface.value)
    try:
        if surface is UISurface.TABLET:
            from ai_paper_analyzer.ui.tablet.app import main as platform_main
        elif surface is UISurface.PHONE:
            from ai_paper_analyzer.ui.phone.app import main as platform_main
        else:
            from ai_paper_analyzer.ui.desktop.app import main as platform_main

        await platform_main(page)
    except Exception:
        LOGGER.exception("Presentation startup failed: %s", surface.value)
        raise
