"""Top-level presentation router for one-codebase multi-platform UI."""

from __future__ import annotations

import os

import flet as ft

from embodied_ai_radar.ui.platform_profile import UISurface, select_ui_surface


async def main(page: ft.Page) -> None:
    """Route a Flet session to the presentation designed for its device class."""
    surface = select_ui_surface(
        platform=page.platform or os.environ.get("FLET_PLATFORM"),
        width=page.width,
        height=page.height,
    )

    if surface is UISurface.TABLET:
        from embodied_ai_radar.ui.tablet.app import main as platform_main
    elif surface is UISurface.PHONE:
        from embodied_ai_radar.ui.phone.app import main as platform_main
    else:
        from embodied_ai_radar.ui.desktop.app import main as platform_main

    await platform_main(page)
