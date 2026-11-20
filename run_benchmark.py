#!/usr/bin/env python3
"""SCI / SCI-for-AI reference benchmark runner.

Examples
--------
    python run_benchmark.py --compare-all
    python run_benchmark.py --pattern cache --repeats 5
    python run_benchmark.py --compare-all --no-db
    python run_benchmark.py --list

Every experiment is wrapped in ``roche_green.GreenLogger``. By default telemetry is
persisted to ``results/green_telemetry.db`` (table ``CO_SOFTWARE_CARBON_INTENSITY``);
with ``--no-db`` a throw-away SQLite file in a temporary directory is used instead.

At the end of every run a self-contained visual report is written to
``results/BENCHMARK_REPORT.md`` (open it in VS Code with Ctrl+Shift+V; no web
server needed), together with ``benchmark_summary.csv`` / ``.json``.
"""
from __future__ import annotations

import argparse
import csv
import gc
import json
import logging
import os
import platform
import socket
import sqlite3
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Any

from roche_green import GreenLogger

import patterns
import report_generator
from workloads import CATEGORIES, VARIANTS, WorkloadCategory

logging.getLogger("codecarbon").setLevel(logging.ERROR)  # keep the console table readable

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_RESULTS_DIR = PROJECT_DIR / "results"
DB_NAME = "green_telemetry.db"
DEFAULT_PROJECT_PREFIX = "SCI-Reference-Benchmark"
DEFAULT_COUNTRY = "CHE"
# sHPC markers: SLURM job, the cluster software tree, or hostname prefixes given in
# SCI_SHPC_HOSTNAMES (comma-separated, e.g. "rocs,shpc-login")
SHPC_APPS_DIR = Path("/apps/rocs")
SHPC_HOSTNAMES_ENV = "SCI_SHPC_HOSTNAMES"
# Laptop-class embodied rate: ~300 kgCO2e manufacturing / 4-year life -> ~0.0024 gCO2e/s
DEFAULT_EMBODIED_RATE = 0.0024

ROW_COLUMNS = (
    "ID", "SCI_SCORE_GCO2E_TX", "ENERGY_CONSUMED_KWH", "TOTAL_CARBON_FOOTPRINT_GCO2E",
    "EMBODIED_EMISSIONS_GCO2E", "BASELINE_SCI_GCO2E", "CLEER_SCI_GCO2E",
    "OPTIMIZATION_YIELD_PCT", "MEASUREMENT_METHOD", "REGION",
)


# --------------------------------------------------------------------------- helpers
def detect_environment() -> str:
    """'sHPC' inside a SLURM job, on a node exposing /apps/rocs or a known cluster hostname."""
    if os.environ.get("SLURM_JOB_ID"):
        return "sHPC"
    try:
        if SHPC_APPS_DIR.exists():
            return "sHPC"
    except OSError:
        pass
    host = socket.gethostname().lower()
    prefixes = [h.strip().lower() for h in os.environ.get(SHPC_HOSTNAMES_ENV, "").split(",") if h.strip()]
    if any(host.startswith(pfx) for pfx in prefixes):
        return "sHPC"
    return "Local"


def pkg_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "unknown"


def validate_registry() -> None:
    """Feature flags, workloads and functional units must be consistent."""
    if set(patterns.FLAGS) != set(VARIANTS):
        raise SystemExit(f"Flag registry mismatch: {set(patterns.FLAGS) ^ set(VARIANTS)}")
    for flag, spec in patterns.FLAGS.items():
        v = VARIANTS[flag]
        if (spec.category, spec.is_baseline) != (v.category, v.is_baseline):
            raise SystemExit(f"Flag '{flag}' is declared differently in patterns.py and workloads/")
    for cat in CATEGORIES.values():
        if patterns.baseline_of(cat.name) != cat.baseline_flag:
            raise SystemExit(f"Category '{cat.name}' has inconsistent baseline flag")


def fetch_row(db_path: Path, row_id: int | None, project: str) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    try:
        cols = ", ".join(ROW_COLUMNS)
        if row_id:
            row = conn.execute(f"SELECT {cols} FROM CO_SOFTWARE_CARBON_INTENSITY WHERE ID = ?",
                               (row_id,)).fetchone()
        else:  # fallback for SDK builds that do not expose logger.row_id
            row = conn.execute(f"SELECT {cols} FROM CO_SOFTWARE_CARBON_INTENSITY "
                               "WHERE PROJECT_NAME = ? ORDER BY ID DESC LIMIT 1", (project,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise RuntimeError("GreenLogger did not write a telemetry row")
    return dict(zip(ROW_COLUMNS, row))


def select_runs(args: argparse.Namespace) -> list[tuple[WorkloadCategory, list[str]]]:
    if args.compare_all:
        return [(c, list(c.variants)) for c in CATEGORIES.values()]
    cat = CATEGORIES[VARIANTS[args.pattern].category]
    flags = [cat.baseline_flag] + ([args.pattern] if args.pattern != cat.baseline_flag else [])
    return [(cat, flags)]


# --------------------------------------------------------------------------- execution
def run_one(category: WorkloadCategory, flag: str, args: argparse.Namespace,
            db_path: Path) -> dict[str, Any]:
    variant = category.variants[flag]
    r = category.functional_units  # R comes from the CATEGORY -> identical for pattern & baseline
    project = f"{args.project_prefix}-{category.name.upper()} - {args.environment}"
    step = f"{flag} ({'BASELINE' if variant.is_baseline else 'POST-OPTIMIZATION'})"

    with variant.prepare(r) as state:
        logger_kwargs: dict[str, Any] = {"country_iso_code": args.country_iso}
        logger_kwargs.update(variant.logger_kwargs(state))
        t0 = time.perf_counter()
        with GreenLogger(
            project_id=project,
            step_name=step,
            functional_unit_name=category.functional_unit_name,
            functional_units=r,
            embodied_carbon_rate=args.embodied_rate,
            db_path=str(db_path),
            **logger_kwargs,
        ) as logger:
            metrics = variant.execute(state)
            # Validation INSIDE the logger: a broken run is flagged [FAILED] in the DB.
            if metrics["units_processed"] != r:
                raise AssertionError(f"{flag}: processed {metrics['units_processed']} units, expected R={r}")
            if logger_kwargs.get("is_ai"):
                for key in ("prompt_tokens", "cached_tokens", "completion_tokens"):
                    if metrics[key] != logger_kwargs[key]:
                        raise AssertionError(f"{flag}: simulated {key}={metrics[key]} "
                                             f"!= declared {logger_kwargs[key]}")
        elapsed = time.perf_counter() - t0

    row = fetch_row(db_path, getattr(logger, "row_id", None), project)
    return {"row": row, "metrics": metrics, "duration_s": elapsed, "project": project,
            "step_name": step, "country_iso_code": logger_kwargs.get("country_iso_code"),
            "model_name": logger_kwargs.get("model_name")}


def aggregate(category: WorkloadCategory, flag: str, runs: list[dict[str, Any]]) -> dict[str, Any]:
    def med(key: str) -> float:
        return statistics.median(float(run["row"][key] or 0.0) for run in runs)

    scis = [float(run["row"]["SCI_SCORE_GCO2E_TX"] or 0.0) for run in runs]
    gsf = patterns.enrich(flag)
    return {
        "category": category.name,
        "flag": flag,
        "role": "baseline" if gsf["is_baseline"] else "pattern",
        "gsf_pattern_name": gsf["gsf_pattern_name"],
        "gsf_related_patterns": gsf["related_patterns"],
        "gsf_matched_by": gsf["matched_by"],
        "sw_lifecycle": gsf["sw_lifecycle"],
        "priority_score": gsf["priority_score"],
        "gsf_link": gsf["gsf_link"],
        "summary": category.variants[flag].summary,
        "functional_unit_name": category.functional_unit_name,
        "functional_units_R": category.functional_units,
        "repeats": len(runs),
        "sci_gco2e_per_unit": statistics.median(scis),
        "sci_min": min(scis),
        "sci_max": max(scis),
        "energy_kwh": med("ENERGY_CONSUMED_KWH"),
        "operational_carbon_gco2e": med("TOTAL_CARBON_FOOTPRINT_GCO2E"),
        "embodied_gco2e": med("EMBODIED_EMISSIONS_GCO2E"),
        "cleer_baseline_sci_gco2e": med("BASELINE_SCI_GCO2E"),
        "cleer_sci_gco2e": med("CLEER_SCI_GCO2E"),
        "cleer_cache_yield_pct": med("OPTIMIZATION_YIELD_PCT"),
        "duration_s": statistics.median(run["duration_s"] for run in runs),
        "region": runs[0]["row"]["REGION"],
        "measurement_method": runs[0]["row"]["MEASUREMENT_METHOD"],
        "model_name": runs[0]["model_name"],
        "project_name": runs[0]["project"],
        "telemetry_row_ids": [run["row"]["ID"] for run in runs],
        "workload_metrics": runs[-1]["metrics"],
        "sci_savings_vs_baseline_pct": None,  # filled in compute_savings()
    }


def compute_savings(results: list[dict[str, Any]]) -> None:
    baselines = {r["category"]: r for r in results if r["role"] == "baseline"}
    for res in results:
        base = baselines.get(res["category"])
        if base is None:
            continue
        assert base["functional_units_R"] == res["functional_units_R"], "R mismatch vs baseline"
        b = base["sci_gco2e_per_unit"]
        res["baseline_flag"] = base["flag"]
        res["baseline_sci_gco2e_per_unit"] = b
        res["sci_savings_vs_baseline_pct"] = ((b - res["sci_gco2e_per_unit"]) / b * 100.0) if b > 0 else None


# --------------------------------------------------------------------------- reporting
def _fmt_pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+.1f}%"


def print_table(results: list[dict[str, Any]]) -> None:
    headers = ("Category", "Flag", "GSF pattern", "FU (R)", "SCI gCO2e/unit", "E (kWh)",
               "M (gCO2e)", "Region", "Time (s)", "SCI saving")
    rows = []
    for r in results:
        gsf = r["gsf_pattern_name"]
        rows.append((r["category"], r["flag"], gsf if len(gsf) <= 38 else gsf[:35] + "...",
                     f"{r['functional_units_R']} {r['functional_unit_name']}",
                     f"{r['sci_gco2e_per_unit']:.4e}", f"{r['energy_kwh']:.3e}",
                     f"{r['embodied_gco2e']:.3e}", str(r["region"]), f"{r['duration_s']:.2f}",
                     "baseline" if r["role"] == "baseline" else _fmt_pct(r["sci_savings_vs_baseline_pct"])))
    widths = [max(len(h), *(len(row[i]) for row in rows)) for i, h in enumerate(headers)]
    sep = "-+-".join("-" * w for w in widths)
    print("\n" + " | ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print(sep)
    last_cat = None
    for row in rows:
        if last_cat is not None and row[0] != last_cat:
            print(sep)
        last_cat = row[0]
        print(" | ".join(c.ljust(w) for c, w in zip(row, widths)))

    ai = [r for r in results if r["category"] == "ai"]
    if ai:
        print("\nSCI for AI - CLEER_v2 detail (per ai_inference):")
        print(f"  {'Flag':<20} {'Model':<27} {'CLEER no-cache SCI':>19} {'CLEER SCI':>12} {'Cache yield':>12}")
        for r in ai:
            print(f"  {r['flag']:<20} {str(r['model_name']):<27} {r['cleer_baseline_sci_gco2e']:>19.4e} "
                  f"{r['cleer_sci_gco2e']:>12.4e} {r['cleer_cache_yield_pct']:>11.1f}%")


CSV_FIELDS = (
    "category", "flag", "role", "gsf_pattern_name", "sw_lifecycle", "priority_score", "gsf_link",
    "functional_unit_name", "functional_units_R", "repeats", "sci_gco2e_per_unit", "sci_min", "sci_max",
    "baseline_flag", "baseline_sci_gco2e_per_unit", "sci_savings_vs_baseline_pct", "energy_kwh",
    "operational_carbon_gco2e", "embodied_gco2e", "cleer_baseline_sci_gco2e", "cleer_sci_gco2e",
    "cleer_cache_yield_pct", "duration_s", "region", "measurement_method", "model_name", "project_name",
)


def export(results: list[dict[str, Any]], results_dir: Path, meta: dict[str, Any]) -> tuple[Path, Path]:
    csv_path = results_dir / "benchmark_summary.csv"
    json_path = results_dir / "benchmark_summary.json"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
    json_path.write_text(json.dumps({"metadata": meta, "results": results}, indent=2, default=str),
                         encoding="utf-8")
    return csv_path, json_path


def execute_plan(plan: list[tuple[WorkloadCategory, list[str]]], args: argparse.Namespace,
                 db_path: Path, total: int) -> dict[str, list[dict[str, Any]]]:
    runs: dict[str, list[dict[str, Any]]] = {flag: [] for _, flags in plan for flag in flags}
    n = 0
    for rep in range(1, args.repeats + 1):
        for category, flags in plan:
            for flag in flags:  # interleaved baseline/pattern order limits thermal drift bias
                n += 1
                print(f"  [{n:>3}/{total}] rep {rep} {category.name:<5} {flag:<20} ...", end=" ", flush=True)
                run = run_one(category, flag, args, db_path)
                runs[flag].append(run)
                print(f"SCI={run['row']['SCI_SCORE_GCO2E_TX']:.4e} gCO2e/{category.functional_unit_name} "
                      f"({run['duration_s']:.2f}s, {run['row']['MEASUREMENT_METHOD']})")
    return runs


# --------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="SCI / SCI-for-AI (CLEER) reference benchmark for GSF patterns.")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--compare-all", action="store_true", help="Run every pattern and baseline.")
    mode.add_argument("--pattern", choices=sorted(patterns.FLAGS),
                      help="Run one pattern together with the baseline of its category.")
    mode.add_argument("--list", action="store_true", help="List feature flags with GSF metadata and exit.")
    p.add_argument("--repeats", type=int, default=3,
                   help="Repetitions per experiment (median is reported). Default: 3.")
    p.add_argument("--country-iso", default=DEFAULT_COUNTRY,
                   help="ISO-3166 alpha-3 grid for runs without a fixed region (API, AI). Default: CHE.")
    p.add_argument("--embodied-rate", type=float, default=DEFAULT_EMBODIED_RATE,
                   help="Local hardware embodied carbon rate in gCO2e/s for M (non-AI). Default: 0.0024.")
    p.add_argument("--environment", default=detect_environment(),
                   help="Environment label (auto: sHPC if SLURM_JOB_ID, /apps/rocs or SCI_SHPC_HOSTNAMES match; else Local).")
    p.add_argument("--project-prefix", default=DEFAULT_PROJECT_PREFIX)
    p.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    p.add_argument("--no-db", action="store_true",
                   help="Do not persist telemetry: GreenLogger writes to a temporary SQLite file "
                        "that is deleted at the end. CSV/JSON/Markdown reports are still produced.")
    return p


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    validate_registry()

    if args.list:
        for flag in patterns.FLAGS:
            m = patterns.enrich(flag)
            cat = CATEGORIES[m["category"]]
            print(f"{flag:<20} [{m['category']}, R={cat.functional_units} {cat.functional_unit_name}] "
                  f"{m['gsf_pattern_name']} | {m['sw_lifecycle']} | score={m['priority_score']} | {m['gsf_link']}")
            if m["matched_by"] and not m["matched_by"].startswith("name =="):
                print(f"{'':<20}   (resolved by keyword lookup: {m['matched_by']})")
        return 0
    if args.repeats < 1:
        raise SystemExit("--repeats must be >= 1")

    results_dir: Path = args.results_dir.resolve()
    results_dir.mkdir(parents=True, exist_ok=True)
    plan = select_runs(args)
    total = sum(len(flags) for _, flags in plan) * args.repeats
    print(f"SCI reference benchmark | env={args.environment} | repeats={args.repeats} | runs={total}")

    if args.no_db:
        # Throw-away SQLite: GreenLogger works as usual but nothing persists after the run.
        tmp_dir = tempfile.TemporaryDirectory(prefix="sci_nodb_")
        try:
            db_path = Path(tmp_dir.name) / DB_NAME
            print("Telemetry DB: temporary (--no-db), deleted at the end of the run")
            runs = execute_plan(plan, args, db_path, total)
        finally:
            # SQLite connections opened via `with sqlite3.connect(...)` are not closed by the
            # context manager; on Windows the open handle blocks deletion until they are
            # garbage-collected, so collect them before removing the temporary directory.
            gc.collect()
            try:
                tmp_dir.cleanup()
            except OSError as exc:
                print(f"WARNING: could not delete temporary telemetry dir {tmp_dir.name}: {exc}")
        db_label = "temporary (--no-db), not persisted"
    else:
        db_path = results_dir / DB_NAME
        print(f"Telemetry DB: {db_path}")
        runs = execute_plan(plan, args, db_path, total)
        db_label = str(db_path)

    results = [aggregate(cat, flag, runs[flag]) for cat, flags in plan for flag in flags]
    compute_savings(results)
    print_table(results)

    meta = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "compare-all" if args.compare_all else f"pattern:{args.pattern}",
        "repeats": args.repeats, "environment": args.environment, "hostname": socket.gethostname(),
        "default_country_iso": args.country_iso, "embodied_rate_gco2e_per_s": args.embodied_rate,
        "python": platform.python_version(), "platform": platform.platform(),
        "roche_green_sdk": pkg_version("roche-green-sdk"), "codecarbon": pkg_version("codecarbon"),
        "telemetry_db": db_label, "sci_formula": "SCI = ((E * I) + M) / R",
    }
    csv_path, json_path = export(results, results_dir, meta)
    report_path = report_generator.generate_report(results, meta, results_dir)

    print(f"\nExported: {csv_path}\n          {json_path}")
    print(f"Telemetry: {db_label}")
    print("\n" + "=" * 78)
    print(f"  Visual report: {report_path}")
    print("  Open it directly in your editor (VS Code: Ctrl+Shift+V) or in Git.")
    print("  No web server or dashboard needed.")
    print("=" * 78)
    return 0

if __name__ == "__main__":
    sys.exit(main())
