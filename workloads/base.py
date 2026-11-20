"""Common contracts shared by every workload of the SCI reference benchmark.

Every experiment ("variant") is split in two phases so that the *system boundary*
is explicit and identical for a pattern and its baseline:

* ``prepare(R)``  -> context manager that builds fixtures (synthetic data, temp
  files, warm KV-cache prefix...). It runs OUTSIDE ``GreenLogger`` because it
  represents pre-existing state of the system, not the work being measured.
* ``execute(state)`` -> the measured work. It runs INSIDE ``GreenLogger`` and must
  process exactly ``R`` functional units, returning ``units_processed == R``.

``logger_kwargs(state)`` returns the extra ``GreenLogger`` arguments that are
specific to the variant (``country_iso_code``, ``is_ai``, token counts, ...).
"""
from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class Variant:
    flag: str
    category: str
    is_baseline: bool
    summary: str
    prepare: Callable[[int], AbstractContextManager[Any]]
    execute: Callable[[Any], dict[str, Any]]
    logger_kwargs: Callable[[Any], dict[str, Any]] = field(default=lambda state: {})


@dataclass(frozen=True)
class WorkloadCategory:
    """A family of experiments that share ONE functional unit (same R, same name)."""

    name: str
    functional_unit_name: str
    functional_units: int
    baseline_flag: str
    variants: dict[str, Variant]

    def __post_init__(self) -> None:
        if self.baseline_flag not in self.variants:
            raise ValueError(f"Baseline '{self.baseline_flag}' missing in category '{self.name}'")
        for v in self.variants.values():
            if v.category != self.name:
                raise ValueError(f"Variant '{v.flag}' declares category '{v.category}' != '{self.name}'")
