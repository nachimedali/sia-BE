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


@shared_task
def run_product_import(import_id: int) -> None:
    from brand.models import ProductImport
    from brand.services.product_imports import run_product_import as read_product_page

    run = ProductImport.objects.filter(pk=import_id, status=ImportStatus.QUEUED).first()
    if run is not None:
        read_product_page(run)
