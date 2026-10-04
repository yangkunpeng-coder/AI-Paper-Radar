from __future__ import annotations

from dataclasses import dataclass

DOMAIN_EMBODIED = "embodied_ai"
DOMAIN_AGENTS = "agents"
DOMAIN_LLM = "llm"
TOPIC_ALL = "all"


@dataclass(frozen=True, slots=True)
class ResearchTopic:
    """A user-facing research direction within a top-level research domain."""

    key: str
    label: str
    filter_tag: str | None = None

    @property
    def paper_filter_tag(self) -> str:
        return self.filter_tag or self.label


@dataclass(frozen=True, slots=True)
class ResearchDomain:
    """Static product configuration for one top-level research domain."""

    key: str
    label: str
    topics: tuple[ResearchTopic, ...]
    data_enabled: bool = False
    sync_categories: tuple[str, ...] = ()

    def topic(self, topic_key: str) -> ResearchTopic | None:
        normalized = topic_key.strip().casefold()
        for topic in self.topics:
            aliases = {
                topic.key.casefold(),
                topic.label.casefold(),
                topic.paper_filter_tag.casefold(),
            }
            if normalized in aliases:
                return topic
        return None

    def normalize_topic_key(self, topic_key: str | None) -> str:
        normalized = (topic_key or "").strip()
        if not normalized or normalized.casefold() == TOPIC_ALL:
            return TOPIC_ALL
        topic = self.topic(normalized)
        return topic.key if topic is not None else TOPIC_ALL

    def paper_filter_tag(self, topic_key: str | None) -> str:
        normalized = self.normalize_topic_key(topic_key)
        if normalized == TOPIC_ALL:
            return TOPIC_ALL
        topic = self.topic(normalized)
        return topic.paper_filter_tag if topic is not None else TOPIC_ALL


@dataclass(frozen=True, slots=True)
class PaperResearchTaxonomy:
    """Persisted many-to-many research classification for one paper."""

    arxiv_id: str
    domain_keys: tuple[str, ...] = ()
    topic_keys: tuple[tuple[str, str], ...] = ()

    def topics_for(self, domain_key: str) -> tuple[str, ...]:
        return tuple(
            topic_key
            for item_domain_key, topic_key in self.topic_keys
            if item_domain_key == domain_key
        )


RESEARCH_DOMAINS: tuple[ResearchDomain, ...] = (
    ResearchDomain(
        key=DOMAIN_EMBODIED,
        label="具身智能",
        data_enabled=True,
        sync_categories=("cs.RO", "cs.AI", "cs.CV", "cs.LG"),
        topics=(
            ResearchTopic("vla", "VLA", "VLA"),
            ResearchTopic("action_model", "Action Model", "Action Model"),
            ResearchTopic(
                "action_representation",
                "Action Representation",
                "Action Representation",
            ),
            ResearchTopic("diffusion_policy", "Diffusion Policy", "Diffusion Policy"),
            ResearchTopic(
                "flow_matching_policy",
                "Flow Matching Policy",
                "Flow Matching Policy",
            ),
            ResearchTopic("robot_policy", "Robot Policy", "Robot Policy"),
            ResearchTopic("manipulation", "Manipulation", "Manipulation"),
            ResearchTopic("humanoid", "Humanoid", "Humanoid"),
            ResearchTopic("navigation", "Navigation", "Navigation"),
            ResearchTopic("world_model", "World Model", "World Model"),
        ),
    ),
    ResearchDomain(
        key=DOMAIN_AGENTS,
        label="智能体",
        data_enabled=True,
        sync_categories=("cs.AI", "cs.CL", "cs.SE"),
        topics=(
            ResearchTopic("agent_framework", "Agent Framework"),
            ResearchTopic("planning", "Planning"),
            ResearchTopic("tool_use", "Tool Use"),
            ResearchTopic("memory", "Memory"),
            ResearchTopic("multi_agent", "Multi-Agent"),
            ResearchTopic("web_gui_agent", "Web / GUI Agent"),
            ResearchTopic("code_agent", "Code Agent"),
            ResearchTopic("agentic_rag", "Agentic RAG"),
        ),
    ),
    ResearchDomain(
        key=DOMAIN_LLM,
        label="大语言模型",
        data_enabled=True,
        sync_categories=("cs.CL", "cs.AI", "cs.LG"),
        topics=(
            ResearchTopic("pretraining", "Pretraining"),
            ResearchTopic("post_training", "Post-training"),
            ResearchTopic("reasoning", "Reasoning"),
            ResearchTopic("rag", "RAG"),
            ResearchTopic("long_context", "Long Context"),
            ResearchTopic("moe", "MoE"),
            ResearchTopic("efficient_llm", "Efficient LLM"),
            ResearchTopic("multimodal_llm", "Multimodal LLM"),
        ),
    ),
)

_DOMAIN_BY_KEY = {domain.key: domain for domain in RESEARCH_DOMAINS}


def research_domain_keys() -> tuple[str, ...]:
    return tuple(domain.key for domain in RESEARCH_DOMAINS)


def get_research_domain(domain_key: str | None) -> ResearchDomain:
    if domain_key in _DOMAIN_BY_KEY:
        return _DOMAIN_BY_KEY[domain_key]
    return _DOMAIN_BY_KEY[DOMAIN_EMBODIED]


def normalize_domain_key(domain_key: str | None) -> str:
    return get_research_domain(domain_key).key


def normalize_topic_key(domain_key: str | None, topic_key: str | None) -> str:
    return get_research_domain(domain_key).normalize_topic_key(topic_key)


def topic_filter_tag(domain_key: str | None, topic_key: str | None) -> str:
    return get_research_domain(domain_key).paper_filter_tag(topic_key)
