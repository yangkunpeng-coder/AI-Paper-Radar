from __future__ import annotations

from dataclasses import dataclass

from ai_paper_analyzer.domain.research_domains import (
    DOMAIN_EMBODIED,
    get_research_domain,
)

SUPPORTED_DEEPSEEK_MODELS: tuple[str, ...] = (
    "deepseek-flash",
    "deepseek-v4-pro",
)
DEFAULT_DEEPSEEK_MODEL = "deepseek-flash"
QUICK_READ_ANALYSIS_VERSION = 2


@dataclass(frozen=True, slots=True)
class LLMSettings:
    model: str = DEFAULT_DEEPSEEK_MODEL
    auto_analyze: bool = False
    auto_sync_on_start: bool = True
    task_notifications: bool = True
    has_api_key: bool = False

    def __post_init__(self) -> None:
        if self.model not in SUPPORTED_DEEPSEEK_MODELS:
            raise ValueError(f"Unsupported DeepSeek model: {self.model}")


@dataclass(frozen=True, slots=True)
class PaperAIAnalysis:
    arxiv_id: str
    relevance_score: int
    summary_cn: str
    contribution_cn: str
    recommendation_cn: str
    tags: tuple[str, ...]
    domain_key: str = DOMAIN_EMBODIED
    topic_keys: tuple[str, ...] = ()
    problem_cn: str = ""
    method_cn: str = ""
    results_cn: str = ""
    analysis_version: int = 1

    def __post_init__(self) -> None:
        if not 0 <= self.relevance_score <= 100:
            raise ValueError("relevance_score must be between 0 and 100")
        if not self.arxiv_id.strip():
            raise ValueError("arxiv_id is required")
        if self.analysis_version not in {1, QUICK_READ_ANALYSIS_VERSION}:
            raise ValueError(f"Unsupported analysis version: {self.analysis_version}")
        if self.analysis_version == QUICK_READ_ANALYSIS_VERSION:
            required = {
                "summary_cn": self.summary_cn,
                "problem_cn": self.problem_cn,
                "method_cn": self.method_cn,
                "contribution_cn": self.contribution_cn,
                "results_cn": self.results_cn,
                "recommendation_cn": self.recommendation_cn,
            }
            missing = [name for name, value in required.items() if not value.strip()]
            if missing:
                raise ValueError(
                    "Quick-read analysis fields must be non-empty: " + ", ".join(missing)
                )

        domain = get_research_domain(self.domain_key)
        if domain.key != self.domain_key:
            raise ValueError(f"Unsupported research domain: {self.domain_key}")
        normalized_topics = tuple(dict.fromkeys(self.topic_keys))
        for topic_key in normalized_topics:
            topic = domain.topic(topic_key)
            if topic is None or topic.key != topic_key:
                raise ValueError(
                    f"Unsupported topic for domain {self.domain_key}: {topic_key}"
                )
        if normalized_topics != self.topic_keys:
            object.__setattr__(self, "topic_keys", normalized_topics)

    @property
    def is_quick_read(self) -> bool:
        return self.analysis_version == QUICK_READ_ANALYSIS_VERSION
