from __future__ import annotations

import re
from dataclasses import dataclass

from ai_paper_analyzer.domain.models import Paper, RankedPaper
from ai_paper_analyzer.domain.relevance import rank_paper as rank_embodied_paper
from ai_paper_analyzer.domain.research_domains import (
    DOMAIN_AGENTS,
    DOMAIN_EMBODIED,
    DOMAIN_LLM,
    get_research_domain,
)


@dataclass(frozen=True, slots=True)
class _Rule:
    pattern: re.Pattern[str]
    points: int
    tag: str
    label: str


def _rule(pattern: str, points: int, tag: str, label: str) -> _Rule:
    return _Rule(re.compile(pattern, re.IGNORECASE), points, tag, label)


def _apply_rules(text: str, rules: tuple[_Rule, ...]) -> tuple[int, tuple[str, ...], tuple[str, ...]]:
    score = 0
    tags: list[str] = []
    matched_terms: list[str] = []
    for rule in rules:
        if rule.pattern.search(text):
            score += rule.points
            if rule.tag not in tags:
                tags.append(rule.tag)
            if rule.label not in matched_terms:
                matched_terms.append(rule.label)
    return score, tuple(tags), tuple(matched_terms)


AGENT_RULES: tuple[_Rule, ...] = (
    _rule(r"\b(?:llm|language|foundation model)[- ]?(?:based )?agents?\b", 55, "Agent Framework", "LLM agent"),
    _rule(r"\bagentic\b|\bautonomous agents?\b", 45, "Agent Framework", "agentic/autonomous agent"),
    _rule(r"\bmulti[- ]agent\b", 38, "Multi-Agent", "multi-agent"),
    _rule(r"\btool (?:use|using|calling)\b|\bfunction calling\b", 34, "Tool Use", "tool use"),
    _rule(r"\bweb agents?\b|\bbrowser agents?\b|\bgui agents?\b|\bcomputer[- ]use agents?\b", 40, "Web / GUI Agent", "web/GUI agent"),
    _rule(r"\bcode agents?\b|\bcoding agents?\b|\bsoftware engineering agents?\b", 40, "Code Agent", "code agent"),
    _rule(r"\bagent(?:ic)? memory\b|\blong[- ]term memory\b", 30, "Memory", "agent memory"),
    _rule(r"\bagent(?:ic)? planning\b|\bplanning agents?\b|\btask planning\b", 28, "Planning", "agent planning"),
    _rule(r"\bagentic rag\b|\bagent(?:ic)? retrieval[- ]augmented generation\b", 38, "Agentic RAG", "agentic RAG"),
    _rule(r"\b(?:ai|intelligent) agents?\b", 30, "Agent Framework", "AI agent"),
)

AGENT_INTEREST_RULES: tuple[_Rule, ...] = (
    _rule(r"\btool (?:use|using|calling)\b|\bfunction calling\b", 45, "Tool Use", "tool use"),
    _rule(r"\bagent(?:ic)? planning\b|\bplanning agents?\b", 40, "Planning", "planning"),
    _rule(r"\bagent(?:ic)? memory\b|\blong[- ]term memory\b", 38, "Memory", "memory"),
    _rule(r"\bmulti[- ]agent\b", 38, "Multi-Agent", "multi-agent"),
    _rule(r"\bweb agents?\b|\bbrowser agents?\b|\bgui agents?\b|\bcomputer[- ]use agents?\b", 42, "Web / GUI Agent", "web/GUI agent"),
    _rule(r"\bcode agents?\b|\bcoding agents?\b|\bsoftware engineering agents?\b", 42, "Code Agent", "code agent"),
    _rule(r"\bagentic rag\b", 42, "Agentic RAG", "agentic RAG"),
    _rule(r"\b(?:llm|language|foundation model)[- ]?(?:based )?agents?\b", 30, "Agent Framework", "LLM agent"),
)

AGENT_ANCHOR = re.compile(
    r"\bagents?\b|\bagentic\b|\bmulti[- ]agent\b|\btool (?:use|calling)\b|"
    r"\bweb agents?\b|\bgui agents?\b|\bcode agents?\b",
    re.IGNORECASE,
)

LLM_RULES: tuple[_Rule, ...] = (
    _rule(r"\blarge language models?\b|\bLLMs?\b", 55, "Large Language Model", "large language model"),
    _rule(r"\bfoundation language models?\b", 48, "Large Language Model", "foundation language model"),
    _rule(r"\bmultimodal (?:large )?language models?\b|\bMLLMs?\b", 42, "Multimodal LLM", "multimodal LLM"),
    _rule(r"\bretrieval[- ]augmented generation\b|\bRAG\b", 30, "RAG", "RAG"),
    _rule(r"\blong[- ]context\b|\blong context\b|\bcontext window\b", 28, "Long Context", "long context"),
    _rule(r"\bmixture[- ]of[- ]experts\b|\bMoE\b", 28, "MoE", "mixture of experts"),
    _rule(r"\bchain[- ]of[- ]thought\b|\breasoning models?\b|\btest[- ]time compute\b", 28, "Reasoning", "reasoning"),
    _rule(r"\bpre[- ]?training\b|\bpretraining\b", 20, "Pretraining", "pretraining"),
    _rule(r"\bpost[- ]training\b|\binstruction tuning\b|\bRLHF\b|\bDPO\b", 24, "Post-training", "post-training"),
    _rule(r"\blanguage models?\b", 25, "Language Model", "language model"),
)

LLM_INTEREST_RULES: tuple[_Rule, ...] = (
    _rule(r"\bchain[- ]of[- ]thought\b|\breasoning models?\b|\btest[- ]time compute\b", 45, "Reasoning", "reasoning"),
    _rule(r"\bretrieval[- ]augmented generation\b|\bRAG\b", 40, "RAG", "RAG"),
    _rule(r"\blong[- ]context\b|\blong context\b|\bcontext window\b", 38, "Long Context", "long context"),
    _rule(r"\bmixture[- ]of[- ]experts\b|\bMoE\b", 38, "MoE", "mixture of experts"),
    _rule(r"\bpost[- ]training\b|\binstruction tuning\b|\bRLHF\b|\bDPO\b", 36, "Post-training", "post-training"),
    _rule(r"\bpre[- ]?training\b|\bpretraining\b", 32, "Pretraining", "pretraining"),
    _rule(r"\bmultimodal (?:large )?language models?\b|\bMLLMs?\b", 40, "Multimodal LLM", "multimodal LLM"),
    _rule(r"\bquantization\b|\bmodel compression\b|\bspeculative decoding\b|\befficient inference\b", 34, "Efficient LLM", "efficient LLM"),
)

LLM_ANCHOR = re.compile(
    r"\blarge language models?\b|\bLLMs?\b|\bfoundation language models?\b|"
    r"\bmultimodal (?:large )?language models?\b|\blanguage models?\b",
    re.IGNORECASE,
)

TOPIC_PATTERNS: dict[str, dict[str, re.Pattern[str]]] = {
    DOMAIN_EMBODIED: {
        "vla": re.compile(r"\bvision[- ]language[- ]action\b|\bVLA\b", re.I),
        "action_model": re.compile(r"\baction models?\b|\baction[- ]conditioned models?\b", re.I),
        "action_representation": re.compile(r"\baction representations?\b|\blatent actions?\b|\baction token", re.I),
        "diffusion_policy": re.compile(r"\bdiffusion polic(?:y|ies)\b|\baction diffusion\b", re.I),
        "flow_matching_policy": re.compile(r"\bflow[- ]matching polic(?:y|ies)\b|\bflow matching for actions?\b", re.I),
        "robot_policy": re.compile(r"\brobot polic(?:y|ies)\b|\bpolicy learning\b|\bgeneralist robot\b", re.I),
        "manipulation": re.compile(r"\bmanipulation\b|\bmobile manipulation\b", re.I),
        "humanoid": re.compile(r"\bhumanoid\b|\bwhole[- ]body\b|\blocomo(?:tion|tive)\b", re.I),
        "navigation": re.compile(r"\bnavigation\b", re.I),
        "world_model": re.compile(r"\bworld models?\b", re.I),
    },
    DOMAIN_AGENTS: {
        "agent_framework": re.compile(r"\b(?:llm|language|foundation model)[- ]?(?:based )?agents?\b|\bagentic\b|\bautonomous agents?\b|\bAI agents?\b", re.I),
        "planning": re.compile(r"\bplanning\b|\bagent(?:ic)? planning\b|\bplanning agents?\b|\btask planning\b", re.I),
        "tool_use": re.compile(r"\btool (?:use|using|calling)\b|\bfunction calling\b", re.I),
        "memory": re.compile(r"\bagent(?:ic)? memory\b|\blong[- ]term memory\b", re.I),
        "multi_agent": re.compile(r"\bmulti[- ]agent\b", re.I),
        "web_gui_agent": re.compile(r"\bweb agents?\b|\bbrowser agents?\b|\bgui agents?\b|\bcomputer[- ]use agents?\b", re.I),
        "code_agent": re.compile(r"\bcode agents?\b|\bcoding agents?\b|\bsoftware engineering agents?\b", re.I),
        "agentic_rag": re.compile(r"\bagentic rag\b|\bagent(?:ic)? retrieval[- ]augmented generation\b", re.I),
    },
    DOMAIN_LLM: {
        "pretraining": re.compile(r"\bpre[- ]?training\b|\bpretraining\b", re.I),
        "post_training": re.compile(r"\bpost[- ]training\b|\binstruction tuning\b|\bRLHF\b|\bDPO\b", re.I),
        "reasoning": re.compile(r"\breasoning\b|\bchain[- ]of[- ]thought\b|\btest[- ]time compute\b", re.I),
        "rag": re.compile(r"\bretrieval[- ]augmented generation\b|\bRAG\b", re.I),
        "long_context": re.compile(r"\blong[- ]context\b|\blong context\b|\bcontext window\b", re.I),
        "moe": re.compile(r"\bmixture[- ]of[- ]experts\b|\bMoE\b", re.I),
        "efficient_llm": re.compile(r"\bquantization\b|\bmodel compression\b|\bspeculative decoding\b|\befficient inference\b", re.I),
        "multimodal_llm": re.compile(r"\bmultimodal (?:large )?language models?\b|\bMLLMs?\b|\bvision[- ]language models?\b", re.I),
    },
}


def rank_paper_for_domain(paper: Paper, domain_key: str) -> RankedPaper:
    """Rank a paper for one research domain without changing shared paper metadata."""

    if domain_key == DOMAIN_EMBODIED:
        ranked = rank_embodied_paper(paper)
    else:
        text = f"{paper.title}\n{paper.summary}"
        if domain_key == DOMAIN_AGENTS:
            rules = AGENT_RULES
            interest_rules = AGENT_INTEREST_RULES
            anchor = AGENT_ANCHOR
            preferred_categories = {"cs.AI", "cs.CL", "cs.SE"}
        elif domain_key == DOMAIN_LLM:
            rules = LLM_RULES
            interest_rules = LLM_INTEREST_RULES
            anchor = LLM_ANCHOR
            preferred_categories = {"cs.CL", "cs.AI", "cs.LG"}
        else:
            raise ValueError(f"Unknown research domain: {domain_key}")

        score, tags, matched_terms = _apply_rules(text, rules)
        interest_score, interest_tags, interest_terms = _apply_rules(text, interest_rules)
        has_anchor = bool(anchor.search(text))
        if has_anchor and preferred_categories.intersection(paper.categories):
            score += 8
            interest_score += 5
        if not has_anchor:
            score = min(score, 20)
            interest_score = min(interest_score, 20)

        ranked = RankedPaper(
            paper=paper,
            score=min(score, 100),
            tags=tags,
            matched_terms=matched_terms,
            interest_score=min(interest_score, 100),
            interest_tags=interest_tags,
            interest_matched_terms=interest_terms,
        )

    # Persist/display the configured topic labels alongside rule tags so the current
    # local filter remains compatible while paper_topics stores canonical keys.
    domain = get_research_domain(domain_key)
    topic_keys = classify_topic_keys(paper, domain_key)
    topic_labels = tuple(
        topic.label for key in topic_keys if (topic := domain.topic(key)) is not None
    )
    merged_tags = tuple(dict.fromkeys((*ranked.tags, *topic_labels)))
    return RankedPaper(
        paper=ranked.paper,
        score=ranked.score,
        tags=merged_tags,
        matched_terms=ranked.matched_terms,
        interest_score=ranked.interest_score,
        interest_tags=ranked.interest_tags,
        interest_matched_terms=ranked.interest_matched_terms,
    )


def classify_topic_keys(paper: Paper, domain_key: str) -> tuple[str, ...]:
    """Return configured topic keys matched by deterministic title/abstract rules."""

    profile = get_research_domain(domain_key)
    patterns = TOPIC_PATTERNS.get(profile.key, {})
    text = f"{paper.title}\n{paper.summary}"
    return tuple(topic.key for topic in profile.topics if patterns.get(topic.key) and patterns[topic.key].search(text))
