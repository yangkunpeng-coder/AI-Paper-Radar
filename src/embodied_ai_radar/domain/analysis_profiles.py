from __future__ import annotations

from dataclasses import dataclass

from embodied_ai_radar.domain.research_domains import (
    DOMAIN_AGENTS,
    DOMAIN_EMBODIED,
    DOMAIN_LLM,
    get_research_domain,
)


@dataclass(frozen=True, slots=True)
class DomainAnalysisProfile:
    domain_key: str
    role_description: str
    focus_points: tuple[str, ...]


_ANALYSIS_PROFILES: dict[str, DomainAnalysisProfile] = {
    DOMAIN_EMBODIED: DomainAnalysisProfile(
        domain_key=DOMAIN_EMBODIED,
        role_description="具身智能论文快速解读助手",
        focus_points=(
            "优先识别论文要解决的具身/机器人问题",
            "摘要明确说明时，提炼 VLA、Action Model、Policy、World Model 或控制方法的核心思路",
            "只提取摘要明确声称的贡献与结果，不补写正文中的机器人设置、benchmark 或数值",
        ),
    ),
    DOMAIN_AGENTS: DomainAnalysisProfile(
        domain_key=DOMAIN_AGENTS,
        role_description="智能体（AI Agent）论文快速解读助手",
        focus_points=(
            "优先识别 Agent 面向的任务和要解决的问题",
            "摘要明确说明时，提炼 planning、tool use、memory、multi-agent、Web/GUI、code 或 Agentic RAG 的核心机制",
            "只提取摘要明确声称的贡献与结果，不推测正文中的工具链、评测或成本",
        ),
    ),
    DOMAIN_LLM: DomainAnalysisProfile(
        domain_key=DOMAIN_LLM,
        role_description="大语言模型论文快速解读助手",
        focus_points=(
            "优先识别论文试图解决的 LLM 能力、训练或推理问题",
            "摘要明确说明时，提炼 pretraining、post-training、reasoning、RAG、long context、MoE、效率或多模态方法",
            "只提取摘要明确声称的贡献与结果，不推测正文中的训练数据、完整 benchmark 或成本",
        ),
    ),
}


def get_analysis_profile(domain_key: str | None) -> DomainAnalysisProfile:
    domain = get_research_domain(domain_key)
    return _ANALYSIS_PROFILES[domain.key]


def build_analysis_system_prompt(domain_key: str | None) -> str:
    """Build a concise title+abstract-only quick-read prompt."""

    domain = get_research_domain(domain_key)
    profile = get_analysis_profile(domain.key)
    allowed_topics = "、".join(
        f"{topic.key}={topic.label}" for topic in domain.topics
    )
    focus = "；".join(profile.focus_points)
    return (
        f"你是{profile.role_description}。当前研究领域是“{domain.label}”。"
        "你的任务不是评审全文，而是帮助用户在几十秒内看懂 Title + Abstract，"
        "并判断是否值得继续打开 PDF。"
        "你只能使用输入中明确提供的 title、abstract、authors、categories 和规则标签。"
        "绝对不要假设你读过论文正文，也不要补写摘要没有说明的模型细节、数据集、"
        "benchmark、实验数值、消融、训练成本、复现难度、局限或 SOTA 结论。"
        "如果某项在标题和摘要中没有足够信息，必须直接写“摘要未说明”。"
        f"当前领域提炼重点：{focus}。"
        "relevance_score 只表示论文与当前研究领域的相关度，不表示论文质量。"
        f"topic_keys 只能从以下 canonical key 中选择零个或多个：{allowed_topics}。"
        "不要创造新的 topic key；信息不足时返回空数组。"
        "tags 只给少量高价值检索词。"
        "输出必须是一个 JSON object，且只能包含键 papers。papers 是数组，"
        "每个输入 arxiv_id 必须且只能出现一次。每项字段必须完整包含："
        "arxiv_id、relevance_score(0-100整数)、"
        "summary_cn(一句话看懂：1-2句中文，直说这篇论文做了什么)、"
        "problem_cn(解决什么问题：1-2句，解释作者为什么要做)、"
        "method_cn(提出什么方法：1-3句，只写摘要明确给出的方法主线)、"
        "contribution_cn(主要贡献：最多3点，用简短中文分行表示；只能来自摘要明确声称)、"
        "results_cn(摘要报告的结果：只写摘要明确给出的结论/数字；没有则写摘要未说明)、"
        "recommendation_cn(为什么值得关注：1句，只说明与当前领域/方向的价值，不做全文级评价)、"
        "tags(字符串数组)、topic_keys(canonical key 字符串数组)。"
        "不要输出创新性评分、证据强度、复现难度、实验完整度或任何全文级判断。"
        "不要输出 Markdown、代码围栏或 JSON 之外的解释。"
    )
