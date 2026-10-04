"""Celery entry points for the brand app. Thin: the work is in services."""

from __future__ import annotations

from celery import shared_task

from brand.models import BrandImport, ImportStatus
from brand.services.imports import run_import


@shared_task
def run_brand_import(import_id: int) -> None:
    run = BrandImport.objects.filter(pk=import_id, status=ImportStatus.QUEUED).first()
    if run is not None:
        run_import(run)
