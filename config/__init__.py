"""Ensures the Celery app is loaded whenever Django starts, so shared_task
resolves against it in web processes as well as workers.
"""

from config.celery import app as celery_app

__all__ = ("celery_app",)
