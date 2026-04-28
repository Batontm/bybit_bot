"""Тесты декларативной конфигурации задач планировщика.

Цель: убедиться, что в `_JOBS` нет опечаток в именах методов
(они привязываются к контроллеру через `getattr` в runtime — без
теста опечатка вылезет только при старте бота).
"""
from bot.services.job_scheduler import _build_jobs, _build_arbitrage_jobs


# Минимальный мок контроллера — нужен для проверки наличия методов.
# Импортируем настоящий BotController — он создаётся как singleton
# при импорте, поэтому тесты выполняются на реальном экземпляре
# (его инициализация не делает сетевых вызовов).
def test_all_base_job_methods_exist_on_controller():
    from bot.controller import controller
    for job_id, name, method_name, _trigger in _build_jobs():
        assert hasattr(controller, method_name), (
            f"Job {job_id!r}: метод {method_name!r} не найден на BotController"
        )
        assert callable(getattr(controller, method_name))


def test_all_arbitrage_job_methods_exist_on_controller():
    from bot.controller import controller
    for job_id, name, method_name, _trigger in _build_arbitrage_jobs():
        assert hasattr(controller, method_name), (
            f"Arbitrage job {job_id!r}: метод {method_name!r} не найден"
        )


def test_no_duplicate_job_ids():
    """ID задач должны быть уникальны (иначе APScheduler выбросит ошибку)."""
    all_jobs = _build_jobs() + _build_arbitrage_jobs()
    ids = [j[0] for j in all_jobs]
    assert len(ids) == len(set(ids)), f"Дубли job_id: {ids}"


def test_job_specs_have_required_fields():
    """Каждая запись — кортеж из 4 элементов с непустыми строками."""
    for job_id, name, method_name, trigger in _build_jobs():
        assert isinstance(job_id, str) and job_id
        assert isinstance(name, str) and name
        assert isinstance(method_name, str) and method_name.startswith('_')
        assert trigger is not None
