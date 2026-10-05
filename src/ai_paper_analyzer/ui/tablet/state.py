"""Tablet-only interaction state with no Flet dependency."""

from __future__ import annotations

from dataclasses import dataclass, field

from ai_paper_analyzer.domain.research_domains import DOMAIN_EMBODIED, RESEARCH_DOMAINS
from ai_paper_analyzer.ui.paper_view import DomainBrowseState


@dataclass(slots=True)
class TabletState:
    active_domain: str = DOMAIN_EMBODIED
    selected_arxiv_id: str | None = None
    settings_open: bool = False
    browse_by_domain: dict[str, DomainBrowseState] = field(
        default_factory=lambda: {
            domain.key: DomainBrowseState() for domain in RESEARCH_DOMAINS
        }
    )

    def browse_state(self, domain_key: str | None = None) -> DomainBrowseState:
        return self.browse_by_domain[domain_key or self.active_domain]

    def remember_selection(self, arxiv_id: str | None) -> None:
        self.selected_arxiv_id = arxiv_id
        self.browse_state().selected_arxiv_id = arxiv_id

    def switch_domain(self, domain_key: str) -> None:
        if domain_key == self.active_domain:
            return
        self.browse_state().selected_arxiv_id = self.selected_arxiv_id
        self.active_domain = domain_key
        self.selected_arxiv_id = self.browse_state().selected_arxiv_id

    def reset_current_browse(self) -> None:
        self.browse_state().reset()
        self.selected_arxiv_id = None
