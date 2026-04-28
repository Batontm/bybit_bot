"""
Клиент DeepSeek (китайский LLM, очень дёшевый, работает из РФ).

Цены (на 2025):
- deepseek-chat (V3):     $0.14 / 1M input,  $0.28 / 1M output
- deepseek-reasoner (R1): $0.55 / 1M input,  $2.19 / 1M output

При среднем размере промпта ~$0.0003 за анализ → $1 ≈ 3000 анализов.
Документация: https://api-docs.deepseek.com/
"""
from config.api_config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL
from .llm.base_client import OpenAICompatibleClient


class DeepSeekClient(OpenAICompatibleClient):
    """Платный, поэтому проверяем месячный бюджет."""

    PRICING = {
        "deepseek-chat":     {"input": 0.14, "output": 0.28},
        "deepseek-reasoner": {"input": 0.55, "output": 2.19},
    }

    def __init__(self):
        super().__init__(
            name="DeepSeek",
            api_key=DEEPSEEK_API_KEY,
            base_url=DEEPSEEK_BASE_URL,
            model=DEEPSEEK_MODEL,
            timeout=60.0,            # DeepSeek иногда отвечает медленнее Groq
            check_cost_limit=True,
        )


deepseek_client = DeepSeekClient()
