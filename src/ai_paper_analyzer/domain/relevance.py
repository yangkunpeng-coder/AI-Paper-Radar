from __future__ import annotations

import re
from dataclasses import dataclass

from ai_paper_analyzer.domain.models import Paper, RankedPaper


RANKING_ALGORITHM_VERSION = "embodied-ranking-v1"


@dataclass(frozen=True, slots=True)
class _Rule:
    pattern: re.Pattern[str]
    points: int
    tag: str
    label: str


def _rule(pattern: str, points: int, tag: str, label: str) -> _Rule:
    return _Rule(re.compile(pattern, re.IGNORECASE), points, tag, label)


# Layer 1: broad embodied-intelligence relevance. This answers “is this paper really
# in embodied AI / robotics?” and remains intentionally conservative so generic CV/ML
# papers do not enter the candidate library just because they mention a world model.
EMBODIED_RULES: tuple[_Rule, ...] = (
    _rule(r"\bembodied (?:ai|intelligence|agent|agents)\b", 55, "Embodied AI", "embodied"),
    _rule(r"\bvision[- ]language[- ]action\b|\bVLA\b", 50, "VLA", "VLA"),
    _rule(r"\brobot(?:ic)? manipulation\b|\bmanipulation policy\b", 35, "Manipulation", "manipulation"),
    _rule(r"\bhumanoid(?: robot| robots)?\b|\bwhole[- ]body control\b", 35, "Humanoid", "humanoid"),
    _rule(r"\brobot learning\b|\brobot policy\b|\bgeneralist robot\b", 30, "Robot Learning", "robot learning"),
    _rule(r"\bmobile manipulation\b", 30, "Manipulation", "mobile manipulation"),
    _rule(r"\bvisuomotor\b|\bimitation learning\b", 24, "Robot Learning", "visuomotor/imitation"),
    _rule(r"\blocomo(?:tion|tive)\b", 22, "Humanoid", "locomotion"),
    _rule(r"\bnavigation\b", 20, "Navigation", "navigation"),
    _rule(r"\bsim[- ]to[- ]real\b|\bsim2real\b", 18, "Robot Learning", "sim-to-real"),
    _rule(r"\bworld model(?:s)?\b", 15, "World Model", "world model"),
)

# Layer 2: the user's research-interest match. This deliberately emphasizes action
# models and VLA-style robot policy learning, rather than treating every embodied AI
# paper as equally relevant.
INTEREST_RULES: tuple[_Rule, ...] = (
    _rule(r"\bvision[- ]language[- ]action\b|\bVLA\b", 55, "VLA", "VLA"),
    _rule(r"\baction model(?:s)?\b|\baction[- ]conditioned model(?:s)?\b", 45, "Action Model", "action model"),
    _rule(r"\baction prediction\b|\bpredict(?:ing|ive)? actions?\b", 40, "Action Prediction", "action prediction"),
    _rule(r"\baction generation\b|\bgenerat(?:e|ing|ive) actions?\b", 40, "Action Generation", "action generation"),
    _rule(r"\baction representation(?:s)?\b|\blatent action(?:s)?\b", 38, "Action Representation", "action representation"),
    _rule(r"\baction tokeni[sz](?:ation|er|ers|e|ed|ing)\b|\baction token(?:s)?\b", 38, "Action Tokenization", "action tokenization"),
    _rule(r"\baction chunk(?:ing|s)?\b|\bchunked actions?\b", 38, "Action Chunking", "action chunking"),
    _rule(r"\bdiffusion polic(?:y|ies)\b|\baction diffusion\b", 44, "Diffusion Policy", "diffusion policy"),
    _rule(r"\bflow[- ]matching polic(?:y|ies)\b|\bflow matching for actions?\b", 40, "Flow Matching Policy", "flow matching policy"),
    _rule(r"\bgeneralist robot(?:ic)? polic(?:y|ies)\b|\bgeneralist robot\b", 40, "Generalist Robot Policy", "generalist robot policy"),
    _rule(r"\brobot foundation model(?:s)?\b|\bfoundation model(?:s)? for robot(?:ics)?\b", 40, "Robot Foundation Model", "robot foundation model"),
    _rule(r"\bvisuomotor polic(?:y|ies)\b|\bvision[- ]based polic(?:y|ies)\b", 28, "Visuomotor Policy", "visuomotor policy"),
    _rule(r"\brobot polic(?:y|ies)\b|\bpolicy learning\b", 24, "Robot Policy", "robot policy"),
    _rule(r"\bbehavior cloning\b|\bbehaviour cloning\b|\bimitation learning\b", 20, "Imitation Learning", "imitation learning"),
    _rule(r"\bworld model(?:s)?\b", 18, "World Model", "world model"),
)

ANCHOR_PATTERN = re.compile(
    r"\brobot(?:s|ic|ics)?\b|\bembodied\b|\bhumanoid\b|\bmanipulation\b|"
    r"\blocomo(?:tion|tive)\b|\bnavigation\b|\bvisuomotor\b|"
    r"\bvision[- ]language[- ]action\b|\bVLA\b",
    re.IGNORECASE,
)


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


def rank_paper(paper: Paper) -> RankedPaper:
    """Return two deterministic, explainable scores.

    ``score`` is broad embodied-AI relevance and controls candidate inclusion.
    ``interest_score`` is the user's personal research-interest match, emphasizing
    VLA/action-model/policy-learning work. Generic action recognition or world-model
    papers are capped unless there is a robotics/embodiment anchor.
    """

    text = f"{paper.title}\n{paper.summary}"
    embodied_score, embodied_tags, embodied_terms = _apply_rules(text, EMBODIED_RULES)
    interest_score, interest_tags, interest_terms = _apply_rules(text, INTEREST_RULES)

    has_anchor = bool(ANCHOR_PATTERN.search(text))
    is_robotics_category = "cs.RO" in paper.categories

    if is_robotics_category and has_anchor:
        embodied_score += 10
        interest_score += 8
    elif is_robotics_category:
        embodied_score += 5
        interest_score += 3

    # Precision guards: a generic CV paper about "world models" or "action
    # prediction" should not become a high-priority embodied candidate by wording
    # alone unless it also contains a robot/embodiment anchor.
    if not has_anchor:
        embodied_score = min(embodied_score, 20)
        interest_score = min(interest_score, 20)

    return RankedPaper(
        paper=paper,
        score=min(embodied_score, 100),
        tags=embodied_tags,
        matched_terms=embodied_terms,
        interest_score=min(interest_score, 100),
        interest_tags=interest_tags,
        interest_matched_terms=interest_terms,
    )


def is_relevant(paper: Paper, *, minimum_score: int = 30) -> bool:
    return rank_paper(paper).score >= minimum_score
