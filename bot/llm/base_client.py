"""
Базовый OpenAI-совместимый LLM-клиент.

Все провайдеры с OpenAI-совместимым API (Perplexity, Groq, DeepSeek)
наследуются от этого класса. Реализует:
- единую логику HTTP-запросов с retry на transient-ошибках
- circuit breaker (выключение провайдера после N подряд неудач)
- унифицированный парсинг и логирование в БД
- общий формат промптов через `bot.llm.prompts`

Подклассу нужно лишь задать метаданные (имя, ключ, URL, модель)
и опционально переопределить `_calculate_cost()` и `_build_prompt()`.
"""
from __future__ import annotations

import asyncio
import time
from typing import Dict, Optional

import httpx
import requests

from config.settings import logger
from config.trading_config import (
    MAX_LLM_REQUESTS_PER_DAY,
    MAX_LLM_COST_PER_MONTH,
)
from ..db.llm_requests_repo import LLMRequestsRepository
from ..error_tracker import error_tracker
from .prompts import (
    SYSTEM_PROMPT_DEFAULT,
    build_analysis_prompt,
    parse_analysis,
)


# HTTP-коды, которые имеет смысл повторять (transient errors).
RETRY_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}

# Параметры retry/circuit breaker (можно вынести в config при необходимости).
DEFAULT_MAX_RETRIES = 2          # 1 основной + 2 повтора = 3 попытки максимум
DEFAULT_RETRY_BASE_DELAY = 1.0   # секунд (экспоненциальный backoff: 1, 2, 4)
DEFAULT_CB_FAILURE_THRESHOLD = 5  # подряд неудач до открытия "автомата"
DEFAULT_CB_COOLDOWN_SEC = 300     # сколько секунд провайдер считается "сломанным"


class CircuitBreaker:
    """Простой circuit breaker без внешних зависимостей.

    Состояния:
    - CLOSED: запросы идут, считаем подряд идущие ошибки.
    - OPEN: после `failure_threshold` подряд ошибок — все запросы отбрасываются
      на `cooldown_sec` секунд.
    - HALF_OPEN (неявно): после cooldown следующий запрос идёт; если успешен —
      состояние возвращается в CLOSED, иначе снова OPEN.
    """

    def __init__(self, name: str,
                 failure_threshold: int = DEFAULT_CB_FAILURE_THRESHOLD,
                 cooldown_sec: int = DEFAULT_CB_COOLDOWN_SEC):
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_sec = cooldown_sec
        self._failures = 0
        self._opened_at: float = 0.0

    @property
    def is_open(self) -> bool:
        if self._opened_at == 0:
            return False
        if time.time() - self._opened_at >= self.cooldown_sec:
            # Cooldown истёк — пробуем снова (half-open).
            return False
        return True

    def record_success(self) -> None:
        if self._failures > 0 or self._opened_at > 0:
            logger.info(f"🟢 [{self.name}] Circuit breaker → CLOSED")
        self._failures = 0
        self._opened_at = 0.0

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold and self._opened_at == 0:
            self._opened_at = time.time()
            logger.warning(
                f"🔴 [{self.name}] Circuit breaker OPEN: "
                f"{self._failures} ошибок подряд, отключаем на {self.cooldown_sec}с"
            )

    def reset(self) -> None:
        self._failures = 0
        self._opened_at = 0.0

    def status(self) -> str:
        if self.is_open:
            remaining = int(self.cooldown_sec - (time.time() - self._opened_at))
            return f"OPEN (cooldown {remaining}s)"
        if self._failures > 0:
            return f"CLOSED (failures={self._failures})"
        return "CLOSED"


class OpenAICompatibleClient:
    """Базовый клиент для OpenAI-совместимых API.

    Параметры конструктора:
        name: Человекочитаемое имя провайдера (для логов/трекера ошибок).
        api_key: API-ключ (если пустой — клиент помечается как unavailable).
        base_url: Базовый URL до `/chat/completions`.
        model: Имя модели.
        timeout: Таймаут HTTP-запроса в секундах.
        max_tokens: Лимит токенов в ответе.
        temperature: Температура.
        system_prompt: Системный промпт.
        with_web_search: Если True — в промпт добавляется блок с инструкциями
            использовать web search (для Perplexity и подобных).
        check_cost_limit: Если True — `check_limits()` также проверяет
            месячный бюджет (для платных провайдеров).
    """

    # Прайс-лист переопределяется в подклассах: {model_name: {"input": $/1M, "output": $/1M}}
    PRICING: Dict[str, Dict[str, float]] = {}

    def __init__(
        self,
        name: str,
        api_key: str,
        base_url: str,
        model: str,
        *,
        timeout: float = 30.0,
        max_tokens: int = 600,
        temperature: float = 0.2,
        system_prompt: str = SYSTEM_PROMPT_DEFAULT,
        with_web_search: bool = False,
        check_cost_limit: bool = True,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ):
        self.name = name
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.system_prompt = system_prompt
        self.with_web_search = with_web_search
        self.check_cost_limit = check_cost_limit
        self.max_retries = max_retries

        self.llm_repo = LLMRequestsRepository()
        self.circuit_breaker = CircuitBreaker(name)

        self.is_available = bool(self.api_key)
        if not self.is_available:
            logger.warning(f"⚠️ {self.name} API key не установлен")

    # -------- public API --------

    def check_limits(self) -> tuple[bool, str]:
        """Проверить дневной/месячный лимиты использования."""
        try:
            daily_stats = self.llm_repo.get_daily_stats()
            if daily_stats['total_requests'] >= MAX_LLM_REQUESTS_PER_DAY:
                return False, f"Достигнут дневной лимит запросов ({MAX_LLM_REQUESTS_PER_DAY})"

            if self.check_cost_limit:
                monthly_cost = self.llm_repo.get_monthly_cost()
                if monthly_cost >= MAX_LLM_COST_PER_MONTH:
                    return False, f"Достигнут месячный бюджет (${MAX_LLM_COST_PER_MONTH})"
        except Exception:
            # Не блокируем работу из-за ошибки чтения статистики.
            pass
        return True, ""

    async def analyze_pair(self, pair: str, timeframe: str,
                           market_data: Dict) -> Optional[Dict]:
        """Запросить анализ пары у LLM.

        Возвращает словарь с ключами `score`, `signal`, `target`, `stop_loss`,
        `summary`, `cost_usd`, `provider` или `None` при любой неустранимой ошибке.
        """
        if not self.is_available:
            error_tracker.add_error(self.name, "ConfigError", "API ключ не установлен")
            return None

        if self.circuit_breaker.is_open:
            logger.debug(f"🔴 [{self.name}] Circuit breaker открыт, пропускаем запрос")
            return None

        can_use, reason = self.check_limits()
        if not can_use:
            logger.warning(f"⚠️ {self.name} недоступен: {reason}")
            return None

        prompt = self._build_prompt(pair, timeframe, market_data)
        payload = self._build_payload(prompt)
        headers = self._build_headers()

        # --- запрос с retry ---
        last_error: Optional[str] = None
        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(
                        f"{self.base_url}/chat/completions",
                        headers=headers,
                        json=payload,
                    )

                if response.status_code == 200:
                    return self._handle_success(response, pair, timeframe)

                # Не-200: возможно retry-able
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                if response.status_code in RETRY_STATUS_CODES and attempt < self.max_retries:
                    delay = DEFAULT_RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        f"⚠️ [{self.name}] {last_error} — retry {attempt + 1}/{self.max_retries} через {delay}с"
                    )
                    await asyncio.sleep(delay)
                    continue

                # Не-retry-able или попытки исчерпаны
                return self._handle_failure(pair, timeframe, "APIError", last_error)

            except (httpx.TimeoutException, httpx.NetworkError) as e:
                last_error = f"{type(e).__name__}: {e}"
                if attempt < self.max_retries:
                    delay = DEFAULT_RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        f"⏱️ [{self.name}] {last_error} — retry {attempt + 1}/{self.max_retries} через {delay}с"
                    )
                    await asyncio.sleep(delay)
                    continue
                return self._handle_failure(pair, timeframe, "TimeoutError", last_error)

            except Exception as e:
                # Неожиданное исключение — не повторяем, выходим.
                return self._handle_failure(pair, timeframe, type(e).__name__, str(e))

        # Сюда не должны попадать, но на всякий случай:
        return self._handle_failure(pair, timeframe, "UnknownError", last_error or "no response")

    def test_connection(self) -> bool:
        """Синхронный тестовый запрос — для startup-проверки бота."""
        if not self.is_available:
            return False

        try:
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers=self._build_headers(),
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": "Hello"}],
                    "max_tokens": 10,
                },
                timeout=15,
            )
            if response.status_code == 200:
                logger.info(f"✅ {self.name} API доступен (модель: {self.model})")
                return True
            logger.error(
                f"❌ {self.name} API недоступен: {response.status_code} | {response.text[:200]}"
            )
            return False
        except Exception as e:
            logger.error(f"❌ Ошибка проверки {self.name}: {e}")
            return False

    # -------- helpers / hooks для подклассов --------

    def _build_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _build_payload(self, prompt: str) -> Dict:
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }

    def _build_prompt(self, pair: str, timeframe: str, market_data: Dict) -> str:
        """Построить промпт. Подкласс может переопределить для кастомизации."""
        return build_analysis_prompt(
            pair, timeframe, market_data, with_web_search=self.with_web_search
        )

    def _calculate_cost(self, usage: Dict) -> float:
        """Расчёт стоимости запроса по usage. По умолчанию — 0 (бесплатные провайдеры).

        Подкласс с тарифами должен переопределить или указать `PRICING`.
        """
        if not usage or not self.PRICING:
            return 0.0
        prices = self.PRICING.get(self.model)
        if not prices:
            return 0.0
        input_tokens = usage.get('prompt_tokens', 0)
        output_tokens = usage.get('completion_tokens', 0)
        return (
            (input_tokens / 1_000_000) * prices['input']
            + (output_tokens / 1_000_000) * prices['output']
        )

    # -------- internal handlers --------

    def _handle_success(self, response: httpx.Response, pair: str,
                        timeframe: str) -> Dict:
        result = response.json()
        content = result['choices'][0]['message']['content']
        analysis = parse_analysis(content)

        usage = result.get('usage', {})
        cost_usd = self._calculate_cost(usage)

        try:
            self.llm_repo.create_request({
                'pair': pair,
                'timeframe': timeframe,
                'prompt_type': 'analysis',
                'score': analysis['score'],
                'signal': analysis['signal'],
                'summary': analysis['summary'],
                'cost_usd': cost_usd,
                'success': True,
            })
        except Exception as e:
            logger.debug(f"Не удалось записать LLM-запрос в БД: {e}")

        analysis['cost_usd'] = cost_usd
        analysis['provider'] = self.name.lower()

        cost_str = f"${cost_usd:.4f}" if cost_usd > 0 else "$0.00"
        logger.info(
            f"✅ {self.name} анализ: {pair} | Score: {analysis['score']} | "
            f"Signal: {analysis['signal']} | {cost_str}"
        )

        self.circuit_breaker.record_success()
        return analysis

    def _handle_failure(self, pair: str, timeframe: str,
                        error_type: str, error_msg: Optional[str]) -> None:
        msg = error_msg or "unknown error"
        error_tracker.add_error(self.name, error_type, msg)
        logger.error(f"❌ {self.name} ошибка: {error_type}: {msg}")

        try:
            self.llm_repo.create_request({
                'pair': pair,
                'timeframe': timeframe,
                'prompt_type': 'analysis',
                'success': False,
                'error_code': error_type,
                'error_message': msg,
            })
        except Exception:
            pass

        self.circuit_breaker.record_failure()
        return None
