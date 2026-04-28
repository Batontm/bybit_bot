"""
Клиент Perplexity (LLM с реальным доступом к web search через `sonar`).

Цены (на 2025): sonar модели ~$1 / 1M входных и выходных токенов
(точная цена зависит от подписки, ESTIMATED_COST_PER_REQUEST используется
как fallback если usage в ответе не пришёл).

Документация: https://docs.perplexity.ai/
"""
from typing import Dict

from config.api_config import (
    PERPLEXITY_API_KEY,
    PERPLEXITY_BASE_URL,
    PERPLEXITY_MODEL,
)
from config.trading_config import ESTIMATED_COST_PER_REQUEST
from .llm.base_client import OpenAICompatibleClient
from .llm.prompts import SYSTEM_PROMPT_WITH_WEB_SEARCH


class PerplexityClient(OpenAICompatibleClient):
    """Использует web search и более длинный max_tokens для развёрнутых ответов."""

    # ~$1 / 1M входных токенов и $1 / 1M выходных для sonar (приблизительно).
    PRICING = {
        "sonar":            {"input": 1.0, "output": 1.0},
        "sonar-pro":        {"input": 3.0, "output": 15.0},
        "sonar-reasoning":  {"input": 1.0, "output": 5.0},
    }

    def __init__(self):
        super().__init__(
            name="Perplexity",
            api_key=PERPLEXITY_API_KEY,
            base_url=PERPLEXITY_BASE_URL,
            model=PERPLEXITY_MODEL,
            max_tokens=1000,                          # web-search ответы длиннее
            system_prompt=SYSTEM_PROMPT_WITH_WEB_SEARCH,
            with_web_search=True,
            check_cost_limit=True,
        )

    def _calculate_cost(self, usage: Dict) -> float:
        """Если usage отсутствует — используем фиксированную оценку."""
        cost = super()._calculate_cost(usage)
        if cost == 0.0 and not usage:
            return ESTIMATED_COST_PER_REQUEST
        return cost


perplexity_client = PerplexityClient()
