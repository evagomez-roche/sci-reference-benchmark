"""Workload registry of the SCI reference benchmark."""
from __future__ import annotations

from .base import Variant, WorkloadCategory
from . import ai_inference_workload, api_workload, batch_workload

CATEGORIES: dict[str, WorkloadCategory] = {
    c.name: c
    for c in (api_workload.CATEGORY, batch_workload.CATEGORY, ai_inference_workload.CATEGORY)
}

VARIANTS: dict[str, Variant] = {
    flag: variant for c in CATEGORIES.values() for flag, variant in c.variants.items()
}

__all__ = ["CATEGORIES", "VARIANTS", "Variant", "WorkloadCategory"]
