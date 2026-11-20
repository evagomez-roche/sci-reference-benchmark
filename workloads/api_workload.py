"""API workload: R = 1000 ``api_requests`` against a synthetic clinical cohort.

Endpoint being simulated::

    GET /cohort?site=<SITE>&condition=<CONDITION>

A client asks for the patients of one clinical site with one condition. The
request trace is deterministic (seeded) and Zipf-distributed, like real traffic:
a few cohorts are requested very often, which is what makes caching worthwhile.

Variants
--------
* ``api_baseline``  ANTI-PATTERN: ``SELECT *`` over the whole table without index,
  filtering in Python, no pagination, no cache, pretty JSON (``indent=2``).
* ``cache``         GSF "Cache static data": same handler as the baseline wrapped
  in an in-memory LRU cache -> recurrent cohorts never hit the DB again.
* ``pagination``    GSF "Reduce transmitted data": filter early in SQL with a
  composite index, project only the needed columns, ``LIMIT`` a page and emit
  compact JSON.

System boundary: seeding the in-memory database is fixture work (outside the
measurement). Serving the R requests -- including the index creation of the
``pagination`` variant and the cache warm-up of ``cache`` -- is measured.
"""
from __future__ import annotations

import json
import random
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterator

from .base import Variant, WorkloadCategory

CATEGORY_NAME = "api"
FUNCTIONAL_UNIT_NAME = "api_requests"
R_API = 1000

SEED = 20240611
SYNTHETIC_PATIENTS = 5000
PAGE_SIZE = 50
LRU_MAXSIZE = 128
SITES = [f"SITE-{i:02d}" for i in range(1, 13)]
CONDITIONS = [
    "type2_diabetes", "hypertension", "copd", "asthma",
    "ckd", "heart_failure", "nsclc", "rheumatoid_arthritis",
]
PAGE_COLUMNS = ("patient_id", "age", "sex", "hba1c", "systolic_bp")


# --------------------------------------------------------------------------- fixtures
def _build_database(rng: random.Random) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE patients (
            patient_id INTEGER PRIMARY KEY,
            site_id TEXT, condition TEXT, age INTEGER, sex TEXT,
            enrollment_date TEXT, bmi REAL, systolic_bp INTEGER, diastolic_bp INTEGER,
            hba1c REAL, ldl REAL, egfr REAL, smoker INTEGER, clinical_notes TEXT
        )
        """
    )
    note_words = ("stable", "follow-up", "adherent", "dose", "titration", "no", "adverse",
                  "event", "reported", "labs", "within", "range", "review", "in", "weeks")
    rows = []
    for pid in range(1, SYNTHETIC_PATIENTS + 1):
        rows.append((
            pid,
            rng.choice(SITES),
            rng.choice(CONDITIONS),
            rng.randint(18, 90),
            rng.choice(("F", "M")),
            f"2023-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
            round(rng.uniform(17.0, 42.0), 1),
            rng.randint(95, 190),
            rng.randint(55, 110),
            round(rng.uniform(4.5, 11.5), 1),
            round(rng.uniform(1.0, 5.5), 2),
            round(rng.uniform(15.0, 120.0), 1),
            rng.randint(0, 1),
            " ".join(rng.choice(note_words) for _ in range(40)),  # wide text column
        ))
    conn.executemany("INSERT INTO patients VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return conn


def _build_request_trace(rng: random.Random, n_requests: int) -> list[tuple[str, str]]:
    """Zipf-like popularity over the 96 (site, condition) cohorts."""
    keys = [(s, c) for s in SITES for c in CONDITIONS]
    rng.shuffle(keys)
    weights = [1.0 / (rank ** 1.1) for rank in range(1, len(keys) + 1)]
    return rng.choices(keys, weights=weights, k=n_requests)


@dataclass
class ApiState:
    conn: sqlite3.Connection
    trace: list[tuple[str, str]]


@contextmanager
def _prepare(r: int) -> Iterator[ApiState]:
    rng = random.Random(SEED)
    conn = _build_database(rng)
    try:
        yield ApiState(conn=conn, trace=_build_request_trace(rng, r))
    finally:
        conn.close()


# --------------------------------------------------------------------------- handlers
def _baseline_handler(conn: sqlite3.Connection, site: str, condition: str) -> tuple[str, int]:
    """Anti-pattern: fetch the whole table, filter late, return everything, pretty JSON."""
    cur = conn.execute("SELECT * FROM patients")
    columns = [d[0] for d in cur.description]
    all_rows = cur.fetchall()
    matches = [dict(zip(columns, row)) for row in all_rows if row[1] == site and row[2] == condition]
    body = json.dumps({"site": site, "condition": condition, "count": len(matches),
                       "patients": matches}, indent=2)
    return body, len(all_rows)


def _run_baseline(state: ApiState) -> dict[str, Any]:
    payload_bytes = rows_fetched = 0
    for site, condition in state.trace:
        body, fetched = _baseline_handler(state.conn, site, condition)
        payload_bytes += len(body.encode("utf-8"))
        rows_fetched += fetched
    return {"units_processed": len(state.trace), "payload_bytes": payload_bytes,
            "rows_fetched_from_db": rows_fetched}


def _run_cache(state: ApiState) -> dict[str, Any]:
    rows_fetched = 0

    @lru_cache(maxsize=LRU_MAXSIZE)
    def cached_handler(site: str, condition: str) -> str:
        nonlocal rows_fetched
        body, fetched = _baseline_handler(state.conn, site, condition)
        rows_fetched += fetched
        return body

    payload_bytes = 0
    for site, condition in state.trace:
        payload_bytes += len(cached_handler(site, condition).encode("utf-8"))
    info = cached_handler.cache_info()
    return {"units_processed": len(state.trace), "payload_bytes": payload_bytes,
            "rows_fetched_from_db": rows_fetched, "cache_hits": info.hits,
            "cache_misses": info.misses,
            "cache_hit_ratio": round(info.hits / max(1, info.hits + info.misses), 4)}


def _run_pagination(state: ApiState) -> dict[str, Any]:
    conn = state.conn
    conn.execute("CREATE INDEX IF NOT EXISTS ix_patients_site_cond "
                 "ON patients(site_id, condition, patient_id)")
    sql = (f"SELECT {', '.join(PAGE_COLUMNS)} FROM patients "
           "WHERE site_id = ? AND condition = ? ORDER BY patient_id LIMIT ? OFFSET 0")
    payload_bytes = rows_fetched = 0
    for site, condition in state.trace:
        rows = conn.execute(sql, (site, condition, PAGE_SIZE)).fetchall()
        rows_fetched += len(rows)
        body = json.dumps({"site": site, "condition": condition, "page": 1,
                           "page_size": PAGE_SIZE, "columns": PAGE_COLUMNS, "rows": rows},
                          separators=(",", ":"))
        payload_bytes += len(body.encode("utf-8"))
    return {"units_processed": len(state.trace), "payload_bytes": payload_bytes,
            "rows_fetched_from_db": rows_fetched}


# --------------------------------------------------------------------------- registry
CATEGORY = WorkloadCategory(
    name=CATEGORY_NAME,
    functional_unit_name=FUNCTIONAL_UNIT_NAME,
    functional_units=R_API,
    baseline_flag="api_baseline",
    variants={
        "api_baseline": Variant(
            flag="api_baseline", category=CATEGORY_NAME, is_baseline=True,
            summary="SELECT * without index/pagination, no cache, JSON indent=2",
            prepare=_prepare, execute=_run_baseline),
        "cache": Variant(
            flag="cache", category=CATEGORY_NAME, is_baseline=False,
            summary=f"In-memory LRU cache (maxsize={LRU_MAXSIZE}) for recurrent cohorts",
            prepare=_prepare, execute=_run_cache),
        "pagination": Variant(
            flag="pagination", category=CATEGORY_NAME, is_baseline=False,
            summary=f"Early SQL filter + index + column projection + LIMIT {PAGE_SIZE} + compact JSON",
            prepare=_prepare, execute=_run_pagination),
    },
)
