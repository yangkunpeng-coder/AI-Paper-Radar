from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Sequence
from typing import Any

from embodied_ai_radar.domain.analysis_profiles import build_analysis_system_prompt
from embodied_ai_radar.domain.llm import (
    DEFAULT_DEEPSEEK_MODEL,
    PaperAIAnalysis,
    QUICK_READ_ANALYSIS_VERSION,
    SUPPORTED_DEEPSEEK_MODELS,
)
from embodied_ai_radar.domain.models import RankedPaper
from embodied_ai_radar.domain.research_domains import DOMAIN_EMBODIED, get_research_domain

DEFAULT_BASE_URL = "https://api.deepseek.com"
MAX_ANALYSIS_PAPERS = 20


class DeepSeekError(RuntimeError):
    pass


class DeepSeekClient:
    """Minimal stdlib DeepSeek Chat Completions client.

    The API key is deliberately held only in memory for the lifetime of this object.
    Callers are responsible for loading it from secure storage and must not log it.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_DEEPSEEK_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 60.0,
    ) -> None:
        normalized_key = api_key.strip()
        if not normalized_key:
            raise ValueError("DeepSeek API key is required")
        if model not in SUPPORTED_DEEPSEEK_MODELS:
            raise ValueError(f"Unsupported DeepSeek model: {model}")

        self._api_key = normalized_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def test_connection(self) -> None:
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": "Reply with exactly OK. This is an API connectivity test.",
                }
            ],
            "thinking": {"type": "disabled"},
            "max_tokens": 8,
        }
        self._chat_completion(payload)

    def analyze_papers(
        self,
        papers: Sequence[RankedPaper],
        *,
        domain_key: str = DOMAIN_EMBODIED,
        max_papers: int = MAX_ANALYSIS_PAPERS,
    ) -> dict[str, PaperAIAnalysis]:
        selected = list(papers[:max_papers])
        if not selected:
            return {}

        domain = get_research_domain(domain_key)
        paper_payload = [
            {
                "arxiv_id": item.paper.arxiv_id,
                "title": item.paper.title,
                "abstract": item.paper.summary,
                "authors": list(item.paper.authors),
                "categories": list(item.paper.categories),
                "rule_score": item.score,
                "rule_tags": list(item.tags),
            }
            for item in selected
        ]

        system_prompt = build_analysis_system_prompt(domain.key)
        user_prompt = (
            f"当前研究领域：{domain.label}。"
            "请分析以下候选论文，并完成该领域的 Topic 分类：\n"
            + json.dumps(
                paper_payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "max_tokens": 5600,
        }

        response = self._chat_completion(payload)
        content = _extract_message_content(response)
        requested_ids = {item.paper.arxiv_id for item in selected}
        return parse_analysis_json(
            content,
            requested_ids=requested_ids,
            domain_key=domain.key,
        )

    def _chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "EmbodiedAIRadar/0.2",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            message = _http_error_message(exc)
            raise DeepSeekError(f"DeepSeek API returned HTTP {exc.code}: {message}") from exc
        except OSError as exc:
            raise DeepSeekError(f"Unable to reach DeepSeek API: {exc}") from exc

        try:
            parsed = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DeepSeekError("DeepSeek API returned invalid JSON") from exc

        if not isinstance(parsed, dict):
            raise DeepSeekError("DeepSeek API returned an unexpected response shape")
        return parsed


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace")
        payload = json.loads(body)
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                return error["message"][:300]
    except (OSError, json.JSONDecodeError):
        pass
    return exc.reason if isinstance(exc.reason, str) else "request failed"


def _extract_message_content(response: dict[str, Any]) -> str:
    try:
        choices = response["choices"]
        first = choices[0]
        message = first["message"]
        content = message["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise DeepSeekError("DeepSeek response did not contain assistant content") from exc

    if not isinstance(content, str) or not content.strip():
        raise DeepSeekError("DeepSeek response contained empty assistant content")
    return content


def parse_analysis_json(
    content: str,
    *,
    requested_ids: set[str] | None = None,
    domain_key: str = DOMAIN_EMBODIED,
) -> dict[str, PaperAIAnalysis]:
    domain = get_research_domain(domain_key)
    allowed_topics = {topic.key for topic in domain.topics}
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise DeepSeekError("DeepSeek analysis was not valid JSON") from exc

    if (
        not isinstance(payload, dict)
        or set(payload) != {"papers"}
        or not isinstance(payload.get("papers"), list)
    ):
        raise DeepSeekError("DeepSeek analysis JSON must contain only a papers array")

    analyses: dict[str, PaperAIAnalysis] = {}
    for raw in payload["papers"]:
        if not isinstance(raw, dict):
            raise DeepSeekError("DeepSeek analysis contains a non-object paper entry")

        arxiv_id = _required_string(raw, "arxiv_id")
        if requested_ids is not None and arxiv_id not in requested_ids:
            raise DeepSeekError(f"DeepSeek returned an unexpected arXiv id: {arxiv_id}")
        if arxiv_id in analyses:
            raise DeepSeekError(f"DeepSeek returned duplicate arXiv id: {arxiv_id}")

        score = raw.get("relevance_score")
        if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
            raise DeepSeekError(f"Invalid relevance_score for {arxiv_id}")

        raw_tags = raw.get("tags")
        if not isinstance(raw_tags, list) or not all(isinstance(tag, str) for tag in raw_tags):
            raise DeepSeekError(f"Invalid tags for {arxiv_id}")
        tags = tuple(dict.fromkeys(tag.strip() for tag in raw_tags if tag.strip()))

        raw_topic_keys = raw.get("topic_keys")
        if not isinstance(raw_topic_keys, list) or not all(
            isinstance(topic_key, str) for topic_key in raw_topic_keys
        ):
            raise DeepSeekError(f"Invalid topic_keys for {arxiv_id}")
        topic_keys = tuple(
            dict.fromkeys(topic_key.strip() for topic_key in raw_topic_keys if topic_key.strip())
        )
        invalid_topics = sorted(set(topic_keys) - allowed_topics)
        if invalid_topics:
            raise DeepSeekError(
                f"DeepSeek returned invalid topic_keys for {arxiv_id}: {', '.join(invalid_topics)}"
            )

        analyses[arxiv_id] = PaperAIAnalysis(
            arxiv_id=arxiv_id,
            relevance_score=score,
            summary_cn=_required_string(raw, "summary_cn"),
            problem_cn=_required_string(raw, "problem_cn"),
            method_cn=_required_string(raw, "method_cn"),
            contribution_cn=_required_string(raw, "contribution_cn"),
            results_cn=_required_string(raw, "results_cn"),
            recommendation_cn=_required_string(raw, "recommendation_cn"),
            tags=tags,
            domain_key=domain.key,
            topic_keys=topic_keys,
            analysis_version=QUICK_READ_ANALYSIS_VERSION,
        )

    if requested_ids is not None and set(analyses) != requested_ids:
        missing = sorted(requested_ids - set(analyses))
        raise DeepSeekError(f"DeepSeek analysis omitted arXiv ids: {', '.join(missing)}")

    return analyses


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DeepSeekError(f"DeepSeek analysis field {key} must be a non-empty string")
    return value.strip()
