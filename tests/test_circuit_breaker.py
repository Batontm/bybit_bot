"""Тесты CircuitBreaker."""
import time

import pytest

from bot.llm.base_client import CircuitBreaker


def test_starts_closed():
    cb = CircuitBreaker('Test')
    assert not cb.is_open
    assert 'CLOSED' in cb.status()


def test_failures_below_threshold_keep_closed():
    cb = CircuitBreaker('Test', failure_threshold=3)
    cb.record_failure()
    cb.record_failure()
    assert not cb.is_open


def test_opens_after_threshold():
    cb = CircuitBreaker('Test', failure_threshold=3, cooldown_sec=60)
    for _ in range(3):
        cb.record_failure()
    assert cb.is_open
    assert 'OPEN' in cb.status()


def test_success_resets_failures():
    cb = CircuitBreaker('Test', failure_threshold=3)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    # После success — счётчик сброшен, ещё 2 failure не должны открыть автомат
    cb.record_failure()
    cb.record_failure()
    assert not cb.is_open


def test_cooldown_reopens_for_retry():
    """После истечения cooldown — `is_open` возвращает False (half-open)."""
    cb = CircuitBreaker('Test', failure_threshold=2, cooldown_sec=0)
    cb.record_failure()
    cb.record_failure()
    # cooldown=0 → сразу half-open
    time.sleep(0.01)
    assert not cb.is_open


def test_manual_reset():
    cb = CircuitBreaker('Test', failure_threshold=2, cooldown_sec=60)
    cb.record_failure()
    cb.record_failure()
    assert cb.is_open
    cb.reset()
    assert not cb.is_open


def test_status_contains_failure_count_when_partial():
    cb = CircuitBreaker('Test', failure_threshold=5)
    cb.record_failure()
    cb.record_failure()
    assert 'failures=2' in cb.status()
