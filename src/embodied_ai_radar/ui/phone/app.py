"""Android phone presentation entry point.

v1.2.0 keeps the independent Phone boundary as a compatibility shell. Until the
v1.3.0 phone design lands, Android phones deliberately mount the proven Desktop
workspace without desktop-window chrome so routing/build smoke can happen
without duplicating business logic.
"""

from __future__ import annotations

import flet as ft


async def main(page: ft.Page) -> None:
    from embodied_ai_radar.ui.desktop.app import mount

    await mount(page, configure_desktop_window=False)
