"""
Клиент Groq (быстрый бесплатный LLM на LPU).

Free tier: ~30 req/min для llama-3.3-70b-versatile.
ВНИМАНИЕ: Groq блокирует IP из РФ/IR/KP/SY/CU/VE.
Документация: https://console.groq.com/docs/quickstart
"""
from config.api_config import GROQ_API_KEY, GROQ_BASE_URL, GROQ_MODEL
from .llm.base_client import OpenAICompatibleClient


class GroqClient(OpenAICompatibleClient):
    """Free tier — стоимость = 0."""

    def __init__(self):
        super().__init__(
            name="Groq",
            api_key=GROQ_API_KEY,
            base_url=GROQ_BASE_URL,
            model=GROQ_MODEL,
            check_cost_limit=False,  # бесплатный
        )


groq_client = GroqClient()
