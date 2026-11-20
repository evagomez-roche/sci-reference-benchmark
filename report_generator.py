#!/usr/bin/env python3
"""Self-contained Markdown report for the SCI reference benchmark.

Produces ``results/BENCHMARK_REPORT.md``: a plain Markdown file that renders in
VS Code (Ctrl+Shift+V), GitHub/GitLab or any Markdown viewer. No web server, no
Streamlit, no extra dependencies (stdlib only).

It is called automatically at the end of ``run_benchmark.py`` and can also be
run standalone to rebuild the report from ``results/benchmark_summary.json``::

    python report_generator.py [--summary results/benchmark_summary.json]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_RESULTS_DIR = PROJECT_DIR / "results"
REPORT_NAME = "BENCHMARK_REPORT.md"
BAR_WIDTH = 20

CATEGORY_INFO: dict[str, dict[str, str]] = {
    "api": {
        "title": "API: clinical cohort queries",
        "inside": "Serving the R requests: SQL queries, filtering, JSON serialization, index creation "
                  "(`pagination`) and cache warm-up (`cache`).",
        "outside": "Seeding the in-memory SQLite database (5,000 synthetic patients) and generating the "
                   "Zipf request trace.",
    },
    "batch": {
        "title": "Batch: lab results ETL",
        "inside": "Extract (read CSV), Transform (unit normalization, out-of-range flags, checksum) "
                  "and Load into an on-disk SQLite warehouse.",
        "outside": "Generating the source CSV. The `carbon_aware` time shift is simulated "
                   "(only the region change affects I).",
    },
    "ai": {
        "title": "AI: simulated LLM inference (SCI for AI + CLEER_v2)",
        "inside": "The R simulated inferences run locally, plus remote accelerator energy and embodied "
                  "emissions estimated by CLEER_v2 from the token counts.",
        "outside": "Loading fixtures and warming the shared prompt prefix in the KV-cache.",
    },
}
CATEGORY_ORDER = ("api", "batch", "ai")


# --------------------------------------------------------------------------- formatting
def _sci(v: Any) -> str:
    try:
        return f"{float(v):.3e}"
    except (TypeError, ValueError):
        return "n/a"


def _pct(v: Any) -> str:
    return "n/a" if v is None else f"{float(v):+.1f}%"


def ascii_bar(value: float, max_value: float, width: int = BAR_WIDTH) -> str:
    """`████████░░░░` bar proportional to value / max_value (the worst variant = full bar)."""
    if max_value <= 0:
        return "░" * width
    filled = max(0, min(width, round(value / max_value * width)))
    if value > 0 and filled == 0:
        filled = 1  # never hide a non-zero footprint
    return "█" * filled + "░" * (width - filled)


def _gsf_cell(r: dict[str, Any]) -> str:
    name = r.get("gsf_pattern_name") or ""
    link = r.get("gsf_link") or ""
    if r.get("role") == "baseline":
        return "_Baseline (anti-pattern)_"
    return f"[{name}]({link})" if link else name


def _md_escape(text: str) -> str:
    return str(text).replace("|", "\\|")


def _workload_highlights(r: dict[str, Any]) -> str:
    m = r.get("workload_metrics") or {}
    parts = []
    if "payload_bytes" in m:
        parts.append(f"{m['payload_bytes'] / 1e6:.2f} MB transmitted")
    if "cache_hit_ratio" in m:
        parts.append(f"{m['cache_hit_ratio'] * 100:.0f}% cache hit ratio")
    if "rows_fetched_from_db" in m:
        parts.append(f"{m['rows_fetched_from_db']:,} rows read")
    if "commits" in m:
        parts.append(f"{m['commits']:,} commits")
    sched = m.get("carbon_aware_schedule")
    if sched:
        parts.append(f"window {sched['region']} +{sched['start_offset_hours']} h "
                     f"({sched['forecast_gco2e_per_kwh']} vs {sched['baseline_now_gco2e_per_kwh']} gCO2e/kWh)")
    if "simulated_compute_units" in m:
        parts.append(f"{m['simulated_compute_units']:,} simulated compute units")
    return ", ".join(parts) or "-"


# --------------------------------------------------------------------------- sections
def _header(meta: dict[str, Any], results: list[dict[str, Any]]) -> list[str]:
    methods = sorted({str(r.get("measurement_method")) for r in results if r.get("measurement_method")})
    db = meta.get("telemetry_db") or "n/a"
    lines = [
        "# SCI Reference Benchmark Report",
        "",
        "> Generated automatically by `run_benchmark.py`. Open it in VS Code with **Ctrl+Shift+V** "
        "(or in GitHub/GitLab). No web server is required.",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Date (UTC) | {meta.get('generated_at_utc', 'n/a')} |",
        f"| Detected environment | **{meta.get('environment', 'n/a')}** (host `{meta.get('hostname', 'n/a')}`) |",
        f"| Mode | `{meta.get('mode', 'n/a')}` · repeats: {meta.get('repeats', 'n/a')} (median reported) |",
        f"| Actual measurement method | {', '.join(f'`{m}`' for m in methods) or 'n/a'} |",
        f"| Default grid (I) | `{meta.get('default_country_iso', 'n/a')}` (batch pins `DEU`/`SWE`) |",
        f"| Local embodied rate (M) | {meta.get('embodied_rate_gco2e_per_s', 'n/a')} gCO2e/s |",
        f"| SQLite telemetry | `{db}` |",
        f"| Platform | {meta.get('platform', 'n/a')} · Python {meta.get('python', 'n/a')} · "
        f"roche-green-sdk {meta.get('roche_green_sdk', 'n/a')} · codecarbon {meta.get('codecarbon', 'n/a')} |",
        "",
        "## Formula",
        "",
        r"$$\text{SCI} = \frac{(E \cdot I) + M}{R}$$",
        "",
        "- **E**: energy (kWh) measured by CodeCarbon (`rapl`, `windows_emi`, `tdp_estimate`, ...). "
        "For AI runs, the remote energy estimated by `CLEER_v2` is added.",
        "- **I**: carbon intensity of the country grid (gCO2e/kWh), shown in the *Region* column.",
        "- **M**: embodied hardware emissions (gCO2e).",
        "- **R**: functional unit, **identical** for each pattern and its baseline.",
        "",
        r"$$\text{Saving (\%)} = \frac{SCI_{baseline} - SCI_{pattern}}{SCI_{baseline}} \times 100$$",
        "",
        "## System Boundaries",
        "",
        "| Workload | Inside the boundary (measured) | Outside the boundary |",
        "|---|---|---|",
    ]
    for cat in CATEGORY_ORDER:
        info = CATEGORY_INFO[cat]
        lines.append(f"| {cat.upper()} | {info['inside']} | {info['outside']} |")
    lines.append("")
    return lines


def _category_section(cat: str, rows: list[dict[str, Any]]) -> list[str]:
    info = CATEGORY_INFO.get(cat, {"title": cat})
    first = rows[0]
    max_sci = max(float(r["sci_gco2e_per_unit"]) for r in rows)
    lines = [
        f"### {info['title']}",
        "",
        f"Functional unit: **R = {first['functional_units_R']} `{first['functional_unit_name']}`** "
        "(same R for every variant).",
        "",
        "| Variant | Official GSF pattern | Time (s) | E (kWh) | M (gCO₂e) | Region (I) | "
        f"**SCI (gCO₂e / {first['functional_unit_name']})** | **Saving vs baseline** | Relative SCI |",
        "|---|---|---:|---:|---:|:---:|---:|---:|---|",
    ]
    for r in rows:
        saving = "baseline" if r["role"] == "baseline" else f"**{_pct(r.get('sci_savings_vs_baseline_pct'))}**"
        lines.append(
            f"| `{r['flag']}` | {_gsf_cell(r)} | {float(r['duration_s']):.2f} | {_sci(r['energy_kwh'])} | "
            f"{_sci(r['embodied_gco2e'])} | {r.get('region', 'n/a')} | **{_sci(r['sci_gco2e_per_unit'])}** | "
            f"{saving} | `{ascii_bar(float(r['sci_gco2e_per_unit']), max_sci)}` |"
        )
    lines += ["", "<details><summary>Variant details</summary>", "",
              "| Variant | What it does | GSF lifecycle | Priority Score | Workload metrics |",
              "|---|---|---|---:|---|"]
    for r in rows:
        score = r.get("priority_score")
        lines.append(f"| `{r['flag']}` | {_md_escape(r.get('summary', ''))} | {r.get('sw_lifecycle') or '-'} | "
                     f"{'-' if score is None else f'{float(score):g}'} | {_md_escape(_workload_highlights(r))} |")
    lines += ["", "</details>", ""]
    return lines


def _ai_section(rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        "## SCI for AI Breakdown (`CLEER_v2`)",
        "",
        "`BASELINE_SCI_GCO2E` is the cost per inference **without** prefix caching; `CLEER_SCI_GCO2E` is "
        "the cost **with** the 85% discount applied to `cached_tokens`.",
        "",
        "| Variant | Model | Prompt / cached / completion tokens (total) | BASELINE_SCI_GCO2E | "
        "CLEER_SCI_GCO2E | Caching gain | |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    for r in rows:
        m = r.get("workload_metrics") or {}
        base, cleer = float(r["cleer_baseline_sci_gco2e"]), float(r["cleer_sci_gco2e"])
        gain = float(r.get("cleer_cache_yield_pct") or 0.0)
        tokens = (f"{m.get('prompt_tokens', 0):,} / {m.get('cached_tokens', 0):,} / "
                  f"{m.get('completion_tokens', 0):,}")
        lines.append(f"| `{r['flag']}` | `{r.get('model_name')}` | {tokens} | {_sci(base)} | {_sci(cleer)} | "
                     f"**{gain:.1f}%** | `{ascii_bar(gain, 100.0)}` |")
    lines += [
        "",
        "> **Note:** CLEER_v2 estimates energy per token with constants that do not depend on model size, so "
        "`model_rightsizing` only reflects the reduction in `completion_tokens`. The real compute saving "
        "(see *simulated compute units* in the variant details) is larger, so that figure is a lower bound.",
        "",
    ]
    return lines


def _guide(results: list[dict[str, Any]]) -> list[str]:
    run_flags = {r["flag"] for r in results}
    lines = [
        "## Quick Experimentation Guide",
        "",
        "Each pattern is always compared with the baseline of its category using the same R:",
        "",
        "```bash",
        "python run_benchmark.py --list                          # flags + official GSF metadata",
        "python run_benchmark.py --compare-all --repeats 1       # all 9 experiments (quick demo)",
        "python run_benchmark.py --compare-all --repeats 5       # more robust results (median)",
        "",
        "# One pattern against its baseline",
        "python run_benchmark.py --pattern cache",
        "python run_benchmark.py --pattern pagination",
        "python run_benchmark.py --pattern batching",
        "python run_benchmark.py --pattern carbon_aware",
        "python run_benchmark.py --pattern model_rightsizing",
        "python run_benchmark.py --pattern prompt_compression",
        "",
        "# Change the environment assumptions",
        "python run_benchmark.py --pattern cache --country-iso SWE    # different power grid (I)",
        "python run_benchmark.py --pattern cache --embodied-rate 0.01 # more carbon-intensive hardware (M)",
        "python run_benchmark.py --compare-all --no-db               # do not persist SQLite telemetry",
        "",
        "# Rebuild this report from results/benchmark_summary.json",
        "python report_generator.py",
        "```",
        "",
    ]
    missing = [c for c in CATEGORY_ORDER if not any(r["category"] == c for r in results)]
    if missing:
        lines += ["Categories **not run** in this execution: "
                  + ", ".join(f"`{c}`" for c in missing)
                  + ". Use `--compare-all` to see all of them.", ""]
    if run_flags:
        lines += [f"Variants included in this report: {', '.join(f'`{f}`' for f in sorted(run_flags))}.", ""]
    return lines


# --------------------------------------------------------------------------- public API
def render_report(results: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    lines = _header(meta, results)
    lines += ["## Comparison by Category", ""]
    by_cat: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r)
    for cat in [c for c in CATEGORY_ORDER if c in by_cat] + [c for c in by_cat if c not in CATEGORY_ORDER]:
        lines += _category_section(cat, by_cat[cat])
    if "ai" in by_cat:
        lines += _ai_section(by_cat["ai"])
    lines += _guide(results)
    return "\n".join(lines)


def generate_report(results: list[dict[str, Any]], meta: dict[str, Any],
                    results_dir: Path = DEFAULT_RESULTS_DIR) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / REPORT_NAME
    path.write_text(render_report(results, meta), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Rebuild BENCHMARK_REPORT.md from benchmark_summary.json")
    p.add_argument("--summary", type=Path, default=DEFAULT_RESULTS_DIR / "benchmark_summary.json")
    args = p.parse_args(argv)
    data = json.loads(args.summary.read_text(encoding="utf-8"))
    path = generate_report(data["results"], data["metadata"], args.summary.resolve().parent)
    print(f"Report written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
