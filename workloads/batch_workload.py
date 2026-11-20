"""Batch/ETL workload: R = 10000 ``processed_records`` of synthetic lab results.

Pipeline (identical business logic in every variant):

    Extract  : read a CSV export of lab results produced by a "source system"
    Transform: normalise units (glucose mg/dL -> mmol/L), flag out-of-range values,
               compute a per-record integrity checksum
    Load     : persist into an on-disk SQLite "clinical data warehouse"

Variants
--------
* ``batch_baseline`` ANTI-PATTERN: one ``INSERT`` + ``commit()`` per record (one
  fsync per record) on a carbon-intensive grid (``country_iso_code="DEU"``).
* ``batching``       GSF "Queue non-urgent processing requests": the same records
  loaded with ``executemany`` inside ONE transaction. Still on ``DEU`` so the
  saving is purely attributable to the code change.
* ``carbon_aware``   GSF "Use carbon-aware scheduling and region selection":
  batching + a deterministic scheduler that reads a day-ahead carbon forecast
  fixture and shifts the job in time/space to the greenest window (``SWE``).

System boundary: generating the source CSV is fixture work (outside). Reading
the CSV, transforming and loading into the warehouse is measured. The carbon-aware
time shift is *simulated* (no real waiting): the run is executed immediately and
attributed to the selected region through ``country_iso_code``.
"""
from __future__ import annotations

import csv
import hashlib
import json
import random
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from .base import Variant, WorkloadCategory

CATEGORY_NAME = "batch"
FUNCTIONAL_UNIT_NAME = "processed_records"
R_BATCH = 10000

SEED = 777
BASELINE_REGION = "DEU"
CARBON_AWARE_CANDIDATE_REGIONS = ("DEU", "SWE")
CARBON_AWARE_DEADLINE_HOURS = 24
FORECAST_FILE = Path(__file__).resolve().parent / "fixtures" / "grid_carbon_forecast.json"

# analyte -> (unit, low, high, generator range)
ANALYTES = {
    "glucose": ("mg/dL", 70.0, 140.0, (55.0, 260.0)),
    "hba1c": ("%", 4.0, 6.5, (4.2, 11.0)),
    "creatinine": ("mg/dL", 0.6, 1.3, (0.4, 3.2)),
    "alt": ("U/L", 7.0, 56.0, (5.0, 140.0)),
    "potassium": ("mmol/L", 3.5, 5.1, (2.8, 6.4)),
}
GLUCOSE_MGDL_TO_MMOLL = 1.0 / 18.0

DDL = """
CREATE TABLE IF NOT EXISTS lab_results (
    record_id INTEGER PRIMARY KEY, patient_id TEXT, analyte TEXT, value REAL,
    unit TEXT, collected_at TEXT, out_of_range INTEGER, checksum TEXT
)
"""
INSERT_SQL = "INSERT INTO lab_results VALUES (?,?,?,?,?,?,?,?)"


# --------------------------------------------------------------------------- fixtures
@dataclass
class BatchState:
    workdir: Path
    source_csv: Path
    warehouse_db: Path
    region: str = BASELINE_REGION
    schedule: dict[str, Any] = field(default_factory=dict)


def _write_source_csv(path: Path, n: int) -> None:
    rng = random.Random(SEED)
    names = list(ANALYTES)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["record_id", "patient_id", "analyte", "value", "unit", "collected_at"])
        for rid in range(1, n + 1):
            analyte = rng.choice(names)
            unit, _, _, (lo, hi) = ANALYTES[analyte]
            w.writerow([rid, f"PT-{rng.randint(1, 2500):05d}", analyte, round(rng.uniform(lo, hi), 2),
                        unit, f"2026-01-{rng.randint(1, 28):02d}T{rng.randint(0, 23):02d}:00:00Z"])


@contextmanager
def _prepare(r: int) -> Iterator[BatchState]:
    # TemporaryDirectory is portable (Windows %TEMP%, Linux $TMPDIR / node-local /tmp on sHPC)
    with tempfile.TemporaryDirectory(prefix="sci_batch_") as tmp:
        workdir = Path(tmp)
        state = BatchState(workdir=workdir, source_csv=workdir / "lab_results_export.csv",
                           warehouse_db=workdir / "warehouse.db")
        _write_source_csv(state.source_csv, r)
        with sqlite3.connect(state.warehouse_db) as conn:
            conn.execute(DDL)
        conn.close()
        yield state


def select_greenest_window(forecast_file: Path = FORECAST_FILE,
                           regions: tuple[str, ...] = CARBON_AWARE_CANDIDATE_REGIONS,
                           deadline_hours: int = CARBON_AWARE_DEADLINE_HOURS) -> dict[str, Any]:
    """Pick the (region, start hour) with the lowest forecast intensity before the deadline."""
    forecast = json.loads(forecast_file.read_text(encoding="utf-8"))
    best: dict[str, Any] | None = None
    for region in regions:
        for hour, intensity in enumerate(forecast["regions"][region][:deadline_hours]):
            if best is None or intensity < best["forecast_gco2e_per_kwh"]:
                best = {"region": region, "start_offset_hours": hour,
                        "forecast_gco2e_per_kwh": intensity}
    assert best is not None
    best["baseline_now_gco2e_per_kwh"] = forecast["regions"][BASELINE_REGION][0]
    best["forecast_start_utc"] = forecast["forecast_start_utc"]
    return best


@contextmanager
def _prepare_carbon_aware(r: int) -> Iterator[BatchState]:
    with _prepare(r) as state:
        state.schedule = select_greenest_window()
        state.region = state.schedule["region"]
        yield state


# --------------------------------------------------------------------------- ETL logic
def _extract(path: Path) -> Iterator[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        yield from csv.DictReader(fh)


def _transform(rec: dict[str, str]) -> tuple:
    analyte = rec["analyte"]
    value = float(rec["value"])
    unit = rec["unit"]
    _, low, high, _ = ANALYTES[analyte]
    out_of_range = int(not (low <= value <= high))
    if analyte == "glucose" and unit == "mg/dL":
        value, unit = round(value * GLUCOSE_MGDL_TO_MMOLL, 3), "mmol/L"
    checksum = hashlib.sha256(
        f"{rec['record_id']}|{rec['patient_id']}|{analyte}|{value}|{unit}".encode()
    ).hexdigest()[:16]
    return (int(rec["record_id"]), rec["patient_id"], analyte, value, unit,
            rec["collected_at"], out_of_range, checksum)


def _count_rows(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM lab_results").fetchone()[0]
    finally:
        conn.close()


def _run_row_by_row(state: BatchState) -> dict[str, Any]:
    conn = sqlite3.connect(state.warehouse_db)  # default journal + synchronous=FULL
    commits = 0
    try:
        for rec in _extract(state.source_csv):
            conn.execute(INSERT_SQL, _transform(rec))
            conn.commit()  # anti-pattern: one durable transaction (fsync) per record
            commits += 1
    finally:
        conn.close()
    return {"units_processed": _count_rows(state.warehouse_db), "commits": commits,
            "region": state.region}


def _run_batched(state: BatchState) -> dict[str, Any]:
    conn = sqlite3.connect(state.warehouse_db)
    try:
        with conn:  # ONE transaction -> one commit for the whole batch
            conn.executemany(INSERT_SQL, (_transform(rec) for rec in _extract(state.source_csv)))
    finally:
        conn.close()
    metrics: dict[str, Any] = {"units_processed": _count_rows(state.warehouse_db), "commits": 1,
                               "region": state.region}
    if state.schedule:
        metrics["carbon_aware_schedule"] = state.schedule
    return metrics


# --------------------------------------------------------------------------- registry
CATEGORY = WorkloadCategory(
    name=CATEGORY_NAME,
    functional_unit_name=FUNCTIONAL_UNIT_NAME,
    functional_units=R_BATCH,
    baseline_flag="batch_baseline",
    variants={
        "batch_baseline": Variant(
            flag="batch_baseline", category=CATEGORY_NAME, is_baseline=True,
            summary="commit() per record on disk, carbon-intensive grid (DEU)",
            prepare=_prepare, execute=_run_row_by_row,
            logger_kwargs=lambda s: {"country_iso_code": BASELINE_REGION}),
        "batching": Variant(
            flag="batching", category=CATEGORY_NAME, is_baseline=False,
            summary="executemany in a single transaction (same DEU grid)",
            prepare=_prepare, execute=_run_batched,
            logger_kwargs=lambda s: {"country_iso_code": BASELINE_REGION}),
        "carbon_aware": Variant(
            flag="carbon_aware", category=CATEGORY_NAME, is_baseline=False,
            summary="batching + time/region shift to greenest forecast window (SWE)",
            prepare=_prepare_carbon_aware, execute=_run_batched,
            logger_kwargs=lambda s: {"country_iso_code": s.region}),
    },
)
