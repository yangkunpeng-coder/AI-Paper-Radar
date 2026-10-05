"""Shared mobile presentation service composition.

Tablet and Phone own separate layouts, but they share the same application and
infrastructure services.  Keeping this wiring outside either presentation avoids
copying SQLite/DeepSeek/arXiv setup when the Phone UI lands in v1.3.0.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import flet as ft
import flet_secure_storage as fss

from ai_paper_analyzer.application.analysis_service import AnalysisService
from ai_paper_analyzer.application.browse_controller import BrowseController
from ai_paper_analyzer.application.library_service import LibraryService
from ai_paper_analyzer.application.radar_service import RadarService
from ai_paper_analyzer.application.settings_service import SettingsService
from ai_paper_analyzer.application.sync_service import SyncCoordinator
from ai_paper_analyzer.application.task_registry import TaskRegistry
from ai_paper_analyzer.infrastructure.arxiv_client import ArxivClient
from ai_paper_analyzer.infrastructure.deepseek_client import DeepSeekClient
from ai_paper_analyzer.infrastructure.flet_settings_store import (
    FletPreferenceStore,
    FletSecureSecretStore,
)
from ai_paper_analyzer.infrastructure.sqlite_library import SQLitePaperRepository


def mobile_storage_data_dir() -> Path:
    storage_data = os.environ.get("FLET_APP_STORAGE_DATA")
    if storage_data:
        return Path(storage_data)
    return Path.cwd() / ".flet" / "storage" / "data"


def mobile_database_path() -> Path:
    return mobile_storage_data_dir() / "embodied_ai_radar.db"


def mobile_arxiv_diagnostic_path() -> Path:
    return mobile_storage_data_dir() / "arxiv_http_diagnostic.log"


@dataclass(slots=True)
class MobileServices:
    arxiv_client: ArxivClient
    radar_service: RadarService
    repository: SQLitePaperRepository
    library_service: LibraryService
    browse_controller: BrowseController
    sync_coordinator: SyncCoordinator
    analysis_service: AnalysisService
    settings_service: SettingsService
    task_registry: TaskRegistry

    def deepseek_client(self, *, api_key: str, model: str) -> DeepSeekClient:
        return DeepSeekClient(api_key=api_key, model=model)


async def create_mobile_services(page: ft.Page) -> MobileServices:
    """Create shared application/infrastructure services for a mobile session."""

    arxiv_client = ArxivClient(diagnostic_log_path=mobile_arxiv_diagnostic_path())
    radar_service = RadarService(arxiv_client)
    repository = SQLitePaperRepository(mobile_database_path())
    library_service = LibraryService(repository)
    browse_controller = BrowseController(library_service)
    sync_coordinator = SyncCoordinator(library_service)
    analysis_service = AnalysisService(radar_service, library_service)

    secure_storage = fss.SecureStorage(
        android_options=fss.AndroidOptions(
            reset_on_error=True,
            migrate_on_algorithm_change=True,
        )
    )
    settings_service = SettingsService(
        FletSecureSecretStore(secure_storage),
        FletPreferenceStore(ft.SharedPreferences()),
    )

    return MobileServices(
        arxiv_client=arxiv_client,
        radar_service=radar_service,
        repository=repository,
        library_service=library_service,
        browse_controller=browse_controller,
        sync_coordinator=sync_coordinator,
        analysis_service=analysis_service,
        settings_service=settings_service,
        task_registry=TaskRegistry(),
    )
