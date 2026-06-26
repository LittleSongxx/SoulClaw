"""Celery application wiring."""

from __future__ import annotations

from backend.infra.config import get_settings

settings = get_settings()

try:
    from celery import Celery
except ModuleNotFoundError:  # pragma: no cover - exercised only when optional worker deps are absent
    Celery = None


class _FallbackTask:
    def __init__(self, fn, name: str) -> None:
        self.fn = fn
        self.name = name

    def __call__(self, *args, **kwargs):
        return self.fn(*args, **kwargs)

    def delay(self, *args, **kwargs):
        result = self.fn(*args, **kwargs)
        return type("EagerResult", (), {"id": f"eager-{self.name}", "result": result})()

    def apply_async(self, args=None, kwargs=None, task_id=None):
        result = self.fn(*(args or ()), **(kwargs or {}))
        return type("EagerResult", (), {"id": task_id or f"eager-{self.name}", "result": result})()


class _FallbackCelery:
    def task(self, name: str):
        def decorator(fn):
            return _FallbackTask(fn, name)

        return decorator


if Celery is None:
    celery_app = _FallbackCelery()
else:
    celery_app = Celery(
        "zlagent",
        broker=settings.resolved_celery_broker_url,
        backend=settings.resolved_celery_result_backend,
        include=["backend.worker.tasks"],
    )
    celery_app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        task_track_started=True,
        task_always_eager=settings.queue_eager,
        task_eager_propagates=False,
        timezone="UTC",
    )
