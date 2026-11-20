# SCI Reference Benchmark: Roche GreenOps

An **autonomous, sandboxed reference benchmark** for Roche engineers to test, validate and
experiment with:

- **Software Carbon Intensity (SCI)**: the Green Software Foundation (GSF) specification, ISO/IEC 21031:2024;
- **SCI for AI (CLEER_v2)**: token-based carbon accounting for LLM inference;
- **Hardware energy tracking**: Intel `RAPL` on Linux, `windows_emi` on Windows, TDP estimation as a fallback;
- **GSF Green Software Patterns**: measuring how much each pattern actually reduces the SCI.

It runs entirely on your machine. You do **not** need production infrastructure, paid external
APIs, a Docker daemon, a web server or access to any central telemetry database. All data is
synthetic and deterministic (fixed seeds), and no real patient data is involved.

```bash
python run_benchmark.py --pattern cache --repeats 1
# -> open results/BENCHMARK_REPORT.md in VS Code and press Ctrl+Shift+V
```

---

## Table of contents

1. [Why this project exists](#1-why-this-project-exists)
2. [Purpose & Architecture](#2-purpose--architecture)
3. [Methodology & Formulas](#3-methodology--formulas)
4. [Quickstart & CLI Playground](#4-quickstart--cli-playground)
5. [Reading the Visual Report](#5-reading-the-visual-report-benchmark_reportmd)
6. [How to Apply These Patterns to Your Own Roche Project](#6-how-to-apply-these-patterns-to-your-own-roche-project)
7. [Reference Results & Interpretation](#7-reference-results--interpretation)
8. [Extending the Benchmark](#8-extending-the-benchmark)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. Why this project exists

Teams adopting the `roche-green-sdk` usually ask the same questions:

- *What does an SCI number actually mean, and how is it computed?*
- *Does pattern X really reduce emissions, or does it only make the code faster?*
- *How do prompt caching and model right-sizing show up in SCI for AI?*
- *Will my measurement behave the same on my laptop and on the sHPC cluster?*

This repository answers those questions with **controlled, reproducible experiments**. Each GSF
pattern is implemented next to a deliberately inefficient **baseline (anti-pattern)**, both
are measured with the same `GreenLogger` and the same functional unit, and the savings are
reported side by side. It is a safe playground: telemetry stays in a local SQLite file under
`results/`, or in a throw-away temporary file with `--no-db`.

Design constraints:

| Constraint | How it is met |
|---|---|
| Portable (Windows/macOS/Linux laptop + Linux sHPC) | Python 3.12, `pathlib` everywhere, no absolute paths, `tempfile` for scratch data |
| Works offline / air-gapped nodes | A `country_iso_code` is always set, so CodeCarbon runs as `OfflineEmissionsTracker` |
| No paid APIs | The LLM is a deterministic local simulator; AI carbon comes from the CLEER_v2 model |
| No Docker daemon | Pure Python, standard library plus `roche-green-sdk` |
| Never touches central telemetry | Writes only to `results/green_telemetry.db` (or a temporary DB with `--no-db`) |

---

## 2. Purpose & Architecture

```
sci_reference_benchmark/
├── run_benchmark.py              # CLI orchestrator: plan -> measure -> compare -> export -> report
├── patterns.py                   # Feature-flag registry -> official GSF metadata (load_patterns_db)
├── report_generator.py           # Self-contained Markdown report (stdlib only; also runnable standalone)
├── workloads/
│   ├── base.py                   # Variant / WorkloadCategory contract (prepare / execute / logger_kwargs)
│   ├── api_workload.py           # R = 1000  api_requests
│   ├── batch_workload.py         # R = 10000 processed_records
│   ├── ai_inference_workload.py  # R = 100   ai_inferences (is_ai=True)
│   └── fixtures/
│       ├── clinical_notes.json        # synthetic clinical notes for the LLM simulator
│       └── grid_carbon_forecast.json  # synthetic 24 h DEU/SWE carbon-intensity forecast
├── gsf_references/               # upstream GSF reference material (read-only)
├── requirements.txt
└── results/                      # created on first run
    ├── BENCHMARK_REPORT.md       # visual report (tables, ASCII bars, CLEER breakdown)
    ├── benchmark_summary.csv     # one row per variant (median of repeats)
    ├── benchmark_summary.json    # same + run metadata + workload metrics
    └── green_telemetry.db        # CO_SOFTWARE_CARBON_INTENSITY rows (not created with --no-db)
```

### How the pieces interact

```
run_benchmark.py
 ├─ 1. select_runs()                 --compare-all | --pattern X  (a pattern always runs with its baseline)
 ├─ 2. for each repeat, for each flag (baseline/pattern interleaved):
 │      with variant.prepare(R) as state:                 # workloads/*.py  - fixtures, NOT measured
 │          with GreenLogger(..., functional_units=R):    # roche_green     - CodeCarbon + M + CLEER_v2
 │              metrics = variant.execute(state)          # workloads/*.py  - measured work
 │              assert units_processed == R (+ token counts for AI)
 ├─ 3. read the telemetry row (SCI, E, M, CLEER columns) from SQLite
 ├─ 4. median per variant -> % saving vs baseline -> benchmark_summary.csv / .json
 │      GSF metadata per flag <- patterns.py <- roche_green.suggest.load_patterns_db()
 └─ 5. report_generator.generate_report() -> results/BENCHMARK_REPORT.md
```

- **`roche_green.GreenLogger`** is the measurement engine. As a context manager it starts
  CodeCarbon, computes embodied emissions `M`, applies `CLEER_v2` when `is_ai=True`, and
  writes one row to `CO_SOFTWARE_CARBON_INTENSITY` in `db_path`.
- **`roche_green.suggest.load_patterns_db()`** returns the GSF catalogue (`GSF Patterns - Patterns.xlsx`)
  as a `pandas.DataFrame`. `patterns.py` resolves every flag against it at runtime, so names,
  lifecycle phases, Priority Scores and links are never hard-coded.
- **`workloads/`** holds the experiments. A variant is defined by `prepare(R)` (fixtures, not
  measured), `execute(state)` (measured) and `logger_kwargs(state)` (region, AI token counts).
  The functional unit `R` belongs to the **category**, so a pattern and its baseline can never
  diverge.

### Feature flags

| Category (R) | Flag | Role | Official GSF pattern (resolved at runtime) |
|---|---|---|---|
| API (1000 `api_requests`) | `api_baseline` | anti-pattern | `SELECT *` without index or pagination, no cache, `indent=2` JSON |
| | `cache` | pattern | Cache static data |
| | `pagination` | pattern | Reduce transmitted data (related: Avoid tracking unnecessary data) |
| Batch (10000 `processed_records`) | `batch_baseline` | anti-pattern | `commit()` per record on a carbon-intensive grid (`DEU`) |
| | `batching` | pattern | Queue non-urgent processing requests (related: Keep request counts low) |
| | `carbon_aware` | pattern | Use carbon-aware scheduling and region selection for AI workloads (related: Time-shift Kubernetes cron jobs) |
| AI (100 `ai_inferences`) | `ai_baseline` | anti-pattern | `llama-3-70b-fp16`, monolithic uncached prompt |
| | `model_rightsizing` | pattern | Use right-sized and energy-efficient AI models |
| | `prompt_compression` | pattern | Prompt/token-caching pattern via keyword lookup (related: Compress transmitted data) |

**Lookup rules.** Each flag has an ordered list of rules: exact name, *name contains*, or
*description contains*. The first rule that matches wins, and ties go to the highest Priority
Score. `prompt_compression` searches, in this order: an exact `"Optimize prompts"` pattern →
any name containing `"prompt"` → any name containing `"caching for repetitive AI"` → a pattern
whose description mentions both `"prompt"` and `"caching"`. The current catalogue has no
dedicated prompt pattern, so it resolves to **"Optimize agent orchestration to reduce
unnecessary model calls"**, whose description covers *caching and prompt batching* for LLM
calls. If a dedicated prompt pattern is added to the catalogue later, it will be selected
automatically. `python run_benchmark.py --list` shows the resolution and which rule matched.

---

## 3. Methodology & Formulas

### 3.1 Software Carbon Intensity

$$SCI = \frac{(E \cdot I) + M}{R}$$

| Symbol | Meaning | Source in this benchmark |
|---|---|---|
| **E** | Energy consumed (kWh) | CodeCarbon inside `GreenLogger` (`rapl`, `windows_emi`, `tdp_estimate`). For AI, the remote energy from CLEER_v2 is added. |
| **I** | Grid carbon intensity (gCO2e/kWh) | Country average for `country_iso_code` (`CHE` by default; batch pins `DEU` / `SWE`). |
| **M** | Embodied hardware emissions (gCO2e) | Non-AI: `embodied_carbon_rate` (gCO2e/s) × wall time. AI: CLEER_v2 embodied fallback. |
| **R** | Functional unit | `api_requests` = 1000, `processed_records` = 10000, `ai_inferences` = 100 |

SCI is a **rate**: grams of CO2e per unit of useful work, not a total. That is what makes a
pattern comparable with its baseline, and why `R` must be identical for both. The saving is:

$$\text{Saving (\%)} = \frac{SCI_{baseline} - SCI_{pattern}}{SCI_{baseline}} \times 100$$

### 3.2 Functional units

| Category | R | What one unit means | Functional-equivalence guarantee |
|---|---|---|---|
| API | 1000 `api_requests` | One `GET /cohort?site=&condition=` request answered | `cache` returns byte-identical responses; `pagination` returns the first page of the same cohort (the baseline over-delivers, which is the anti-pattern) |
| Batch | 10000 `processed_records` | One lab result extracted, transformed and loaded | Every variant loads the same 10000 transformed rows (`COUNT(*)` checked) |
| AI | 100 `ai_inferences` | One adverse-event extraction from a clinical note | Prompts are built with exactly the declared token counts, which are verified after each run |

R is validated **inside** the `GreenLogger` block, so an invalid run is stored as `[FAILED]`.

### 3.3 System boundaries

Every experiment is split into `prepare(R)`, which runs outside the measurement, and
`execute(state)`, which runs inside `GreenLogger`.

| Workload | Inside the boundary (measured) | Outside the boundary |
|---|---|---|
| API | Serving the 1000 requests: SQL, filtering, JSON serialization, **index creation** (`pagination`), **cache warm-up** (`cache`) | Seeding the in-memory SQLite DB (5000 synthetic patients) and generating the Zipf request trace |
| Batch | Extract (read CSV), Transform (unit normalization, range flags, checksum), Load into an on-disk SQLite warehouse | Generating the source CSV. The `carbon_aware` time shift is **simulated**: the window is chosen and its region is attributed, but the run does not wait |
| AI | The 100 simulated inferences (prefill + decode) locally, plus remote energy and embodied emissions via CLEER_v2 | Loading fixtures and warming the shared prompt prefix in the KV-cache (a long-running inference server already holds it) |

Excluded everywhere: client-server networking, end-user devices, storage outside the process,
and model training (training appears only as an amortization factor inside CLEER_v2).

### 3.4 SCI for AI: how CLEER_v2 accounts for tokens

With `is_ai=True`, `GreenLogger` estimates the remote inference footprint from the token counts:

| Token type | Accelerator energy | Notes |
|---|---|---|
| `prompt_tokens` (fresh input) | 0.32 Wh per 1k tokens | Full prefill cost |
| `cached_tokens` (KV-cache / prefix hit) | 0.32 × **0.15** Wh per 1k tokens | **85% discount**: cached prefixes skip most of the prefill |
| `completion_tokens` (output) | 0.96 Wh per 1k tokens | Decoding is about 3× more expensive per token |

The accelerator energy is then scaled by development/training amortization (×1.5), host
overhead (×1.5), idle capacity (×1.1) and datacenter PUE (×1.45), and multiplied by a grid
intensity of 350 gCO2e/kWh. **Embodied emissions (M)** are derived from the same energy figures
(≈97 gCO2e/kWh across accelerator and facility shares). The SDK stores:

- `BASELINE_SCI_GCO2E`: SCI if the cached tokens had been processed as fresh input;
- `CLEER_SCI_GCO2E`: SCI with the cache discount, including M;
- `OPTIMIZATION_YIELD_PCT`: the gain attributable to caching;
- `SCI_SCORE_GCO2E_TX`: local measured carbon + CLEER remote carbon + M, divided by R.

| AI flag | Model | prompt | cached | completion (per inference, × R) |
|---|---|---:|---:|---:|
| `ai_baseline` | `llama-3-70b-fp16` | 1800 | 0 | 350 |
| `model_rightsizing` | `llama-3-8b-int8-quantized` | 1800 | 0 | 250 |
| `prompt_compression` | `llama-3-70b-fp16` (same model, to isolate the effect) | 320 | 1100 | 180 |

---

## 4. Quickstart & CLI Playground

### 4.1 Local setup (Windows / macOS / Linux)

```bash
cd sci_reference_benchmark
python3.12 -m venv .venv
source .venv/bin/activate            # Windows (PowerShell): .venv\Scripts\Activate.ps1
pip install -r requirements.txt      # or, from the workspace source: pip install ../green_sdk
python run_benchmark.py --list       # sanity check: flags + GSF metadata
```

### 4.2 CLI playground

```bash
# Discover
python run_benchmark.py --list                                  # flags, GSF names, links, lookup rule used

# Run everything
python run_benchmark.py --compare-all --repeats 1               # quick demo (~1.5 min on a laptop)
python run_benchmark.py --compare-all                           # default: 3 repeats, median reported
python run_benchmark.py --compare-all --repeats 5               # more robust statistics

# One pattern vs. its baseline (same R)
python run_benchmark.py --pattern cache
python run_benchmark.py --pattern pagination
python run_benchmark.py --pattern batching
python run_benchmark.py --pattern carbon_aware
python run_benchmark.py --pattern model_rightsizing
python run_benchmark.py --pattern prompt_compression --repeats 5

# What-if scenarios
python run_benchmark.py --pattern cache --country-iso SWE       # cleaner grid (I) for API/AI runs
python run_benchmark.py --pattern cache --embodied-rate 0.01    # more carbon-intensive hardware (M)
python run_benchmark.py --compare-all --no-db                   # no persistent telemetry at all

# Rebuild the report from the last benchmark_summary.json
python report_generator.py
```

| Option | Default | Description |
|---|---|---|
| `--list` | | List flags with official GSF metadata and exit |
| `--compare-all` | | Run all 9 variants |
| `--pattern <name>` | | Run one pattern together with the baseline of its category |
| `--repeats N` | `3` | Interleaved baseline/pattern repetitions; the **median** is reported |
| `--country-iso` | `CHE` | ISO 3166 alpha-3 grid for API and AI runs (batch pins `DEU`/`SWE` by design) |
| `--embodied-rate` | `0.0024` | Local embodied rate in gCO2e/s for M (≈300 kgCO2e laptop over 4 years) |
| `--no-db` | off | `GreenLogger` writes to a temporary SQLite file (`tempfile.TemporaryDirectory`) that is deleted at the end, leaving no residue. CSV, JSON and the report are still produced |
| `--environment` | auto | `sHPC` if `SLURM_JOB_ID` is set, `/apps/rocs` exists, or the hostname starts with a prefix listed in `SCI_SHPC_HOSTNAMES` (comma-separated); otherwise `Local` |
| `--results-dir` | `./results` | Output directory |
| `--project-prefix` | `SCI-Reference-Benchmark` | Prefix of `PROJECT_NAME` in the telemetry rows |

### 4.3 Running on Roche's sHPC cluster (SLURM, no Docker)

```bash
#!/bin/bash
#SBATCH --job-name=sci-benchmark
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
#SBATCH --time=00:30:00
#SBATCH --exclusive          # recommended: RAPL measures the whole socket
cd "$SLURM_SUBMIT_DIR"
source .venv/bin/activate
python run_benchmark.py --compare-all --repeats 5
```

The environment is detected automatically (`env=sHPC` in the console and in the report). If
your user cannot read `/sys/class/powercap/intel-rapl`, CodeCarbon falls back to TDP
estimation; the report's *Actual measurement method* field shows which method was used.

### 4.4 Outputs

| File | Content | Lifecycle |
|---|---|---|
| `results/BENCHMARK_REPORT.md` | Visual report (see section 5) | Overwritten on each run |
| `results/benchmark_summary.csv` | One row per variant: SCI, E, M, region, savings, GSF metadata | Overwritten |
| `results/benchmark_summary.json` | Same, plus run metadata (versions, platform, host), workload metrics (bytes transmitted, cache hit ratio, commits, carbon-aware schedule, tokens) and telemetry row IDs | Overwritten |
| `results/green_telemetry.db` | One `CO_SOFTWARE_CARBON_INTENSITY` row per execution, written by `GreenLogger` | **Appended** (not created with `--no-db`) |

---

## 5. Reading the Visual Report (`BENCHMARK_REPORT.md`)

Every run, whether `--compare-all` or `--pattern <name>`, ends by printing the path to
`results/BENCHMARK_REPORT.md`. It is plain Markdown:

- **VS Code**: open the file and press **Ctrl+Shift+V** (**Cmd+Shift+V** on macOS), or
  **Ctrl+K V** for a side-by-side preview that refreshes automatically after each run.
- **GitLab / GitHub**: commit or upload the file; it renders natively, including the LaTeX formulas.
- Any other Markdown viewer works too. No Streamlit, dashboard or web server is needed.

What you will find, top to bottom:

1. **Header**: date, detected environment (`Local` / `sHPC`) and host, the actual measurement
   method (`rapl`, `windows_emi`, `tdp_estimate`, `... + CLEER_v2`), grid, embodied rate,
   telemetry location, and platform and package versions.
2. **Formula and system boundaries**: the SCI formula and what is or is not measured for each workload.
3. **Comparison by category** (API, Batch, AI), one table each with the same R:

   | Column | How to read it |
   |---|---|
   | Variant / Official GSF pattern | Flag name and the clickable GSF catalogue link |
   | Time (s), E (kWh), M (gCO₂e), Region (I) | The ingredients of the SCI formula |
   | **SCI (gCO₂e / unit)** | The metric to compare: lower is better |
   | **Saving vs baseline** | Positive = the pattern emits less per functional unit |
   | Relative SCI `████░░░░` | Bar proportional to SCI; the worst variant fills the bar |

   A collapsible *Variant details* block adds the GSF lifecycle phase, the Priority Score and
   workload metrics that explain *why* the saving happens (MB transmitted, cache hit ratio,
   commits, chosen carbon window, simulated compute units).
4. **SCI for AI breakdown (CLEER_v2)**: model, token counts, `BASELINE_SCI_GCO2E` vs
   `CLEER_SCI_GCO2E`, and the % gain from prefix/prompt caching.
5. **Quick experimentation guide**: ready-to-run commands for the next experiment.

**Tip:** compare **percentages within the same environment**. Absolute SCI values depend on
hardware and sensor (a server with RAPL and a laptop with TDP estimation are not comparable),
but relative savings are.

---

## 6. How to Apply These Patterns to Your Own Roche Project

Use the same three-step workflow that this benchmark automates:

> **Measure the baseline SCI → apply a GSF pattern → re-measure and compare** (same R, same environment).

### 6.1 Traditional code (Path A: CodeCarbon + embodied rate)

```python
import sqlite3
from roche_green import GreenLogger

DB = "green_results/green_telemetry.db"   # keep experiments local; never point at a central DB


def measure(step_name: str, fn, items, unit_name: str) -> float:
    """Runs fn(items) under GreenLogger and returns SCI in gCO2e per functional unit."""
    with GreenLogger(
        project_id="my-roche-project - Local",
        step_name=step_name,
        functional_unit_name=unit_name,    # a real unit, never "transaction" or "test"
        functional_units=len(items),       # R: identical for baseline and optimized runs
        country_iso_code="CHE",            # offline, reproducible grid intensity
        embodied_carbon_rate=0.0024,       # gCO2e/s for M (laptop-class hardware)
        db_path=DB,
    ) as logger:
        fn(items)
    conn = sqlite3.connect(DB)
    try:
        return conn.execute(
            "SELECT SCI_SCORE_GCO2E_TX FROM CO_SOFTWARE_CARBON_INTENSITY WHERE ID = ?",
            (logger.row_id,),
        ).fetchone()[0]
    finally:
        conn.close()


records = load_my_records()                                              # your data
sci_before = measure("etl (BASELINE)", etl_row_by_row, records, "processed_records")
sci_after = measure("etl (POST-OPTIMIZATION)", etl_batched, records, "processed_records")
print(f"SCI saving: {(sci_before - sci_after) / sci_before * 100:.1f}%")
```

(The `green_results/` directory must exist before the first run.)

### 6.2 AI inference call (Path B: SCI for AI with CLEER_v2)

```python
from roche_green import GreenLogger

with GreenLogger(
    project_id="my-genai-assistant - Local",
    step_name="summarize (POST-OPTIMIZATION)",
    functional_unit_name="ai_inferences",
    functional_units=len(documents),
    is_ai=True,
    model_name="llama-3-8b-int8-quantized",   # required when is_ai=True
    provider="AWS",
    country_iso_code="CHE",
    db_path="green_results/green_telemetry.db",
) as logger:
    usage = {"prompt": 0, "cached": 0, "completion": 0}
    for doc in documents:
        response = my_llm_client.generate(doc)          # your model / endpoint
        usage["prompt"] += response.usage.prompt_tokens
        usage["cached"] += response.usage.cached_tokens  # prefix/KV-cache hits (85% discount)
        usage["completion"] += response.usage.completion_tokens
    # Token counts are only known after the calls. roche-green-sdk 2.0.2 reads them on exit,
    # so set them before the block ends. If you know them upfront, pass prompt_tokens=,
    # cached_tokens= and completion_tokens= to the constructor instead.
    logger.prompt_tokens = usage["prompt"]
    logger.cached_tokens = usage["cached"]
    logger.completion_tokens = usage["completion"]
```

### 6.3 Checklist

1. **Pick a meaningful functional unit** (requests, records, inferences, reports) and keep R
   identical across the before/after runs.
2. **Fix `country_iso_code`** so comparisons are not skewed by grid lookups, and to run offline.
3. **Measure the baseline first** with `step_name="... (BASELINE)"`.
4. **Choose a pattern**: `python run_benchmark.py --list`, or `roche-green suggest`
   (equivalently `python -m roche_green suggest`) for the interactive GSF triage assistant.
5. **Re-measure** with `step_name="... (POST-OPTIMIZATION)"`, repeat 3–5 times and compare medians.
6. **Report the % saving** together with the measurement method and the system boundary.

---

## 7. Reference Results & Interpretation

Example run (`--compare-all --repeats 1`, Windows 11 laptop, `windows_emi`). Absolute numbers
depend on hardware; **compare percentages within the same environment**.

| Flag | SCI (gCO2e/unit) | Saving vs baseline | Time |
|---|---:|---:|---:|
| `api_baseline` | 4.2e-05 | — | 17.0 s |
| `cache` | 8.4e-06 | +80.0% | 3.4 s |
| `pagination` | 5.7e-06 | +86.5% | 2.4 s |
| `batch_baseline` | 2.6e-05 | — | 54.8 s |
| `batching` | 5.3e-07 | +98.0% | 2.1 s |
| `carbon_aware` | 5.0e-07 | +98.1% | 2.1 s |
| `ai_baseline` | 2.05 | — | 2.4 s |
| `model_rightsizing` | 1.83 | +10.5% | 2.2 s |
| `prompt_compression` | 0.74 | +64.0% (CLEER caching gain 47.7%) | 2.1 s |

Lessons and known limitations (read these before quoting numbers):

1. **M dominates short jobs.** `GreenLogger` charges M by wall time, including the ~2 s fixed
   CodeCarbon start/stop overhead. This bias is conservative: it penalizes fast patterns, so
   their real savings are larger than reported.
2. **`carbon_aware` vs `batching` looks small in absolute terms** because, after batching, the
   operational part (E·I) is already tiny compared with M. Time or region shifting pays off on
   long, energy-intensive jobs. CodeCarbon offline also uses the country's **annual average**
   intensity, so only the **region** change affects SCI; the chosen time window is recorded in
   the JSON (`carbon_aware_schedule`).
3. **CLEER_v2 is model-size agnostic.** It uses fixed per-token energy constants, so
   `model_rightsizing` only captures the drop in `completion_tokens` (≈10%), whereas the real
   compute reduction of an 8B int8 model vs a 70B fp16 model is about 15× (see
   `simulated_compute_units` in the JSON). Treat the figure as a **lower bound**.
4. **The LLM simulator is deterministic and local.** It represents client/runtime-side cost,
   not a GPU benchmark. AI figures are dominated by the CLEER remote estimate.
5. **Use `--repeats ≥ 3`** (median) and `--exclusive` nodes on sHPC to reduce noise.

---

## 8. Extending the Benchmark

1. Implement `prepare(R)` / `execute(state)` (and optionally `logger_kwargs(state)`) in the
   workload module of the right category and register the `Variant` in `CATEGORY.variants`.
   `execute` must return `{"units_processed": R, ...}`.
2. Add a `PatternFlag` to `patterns.py` with lookup rules: `exact("<Pattern Name>")`, or
   `name_has(...)` / `desc_has(...)` for keyword matching.
3. Run `python run_benchmark.py --list`. It validates that both registries are consistent and
   shows the resolved GSF pattern. Then run `--pattern <new_flag>` to compare it against its
   baseline with the same R.

---

## 9. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ModuleNotFoundError: roche_green` | Install the SDK: `pip install -r requirements.txt` or `pip install ../green_sdk` |
| `--list` shows `(no catalogue match ...)` | `GSF Patterns - Patterns.xlsx` missing from the installed SDK, or `openpyxl` not installed |
| Measurement method is `tdp_estimate` on Linux | No read access to RAPL counters; ask for `powercap` read permissions or accept the estimate |
| Region shows `CHE` for API/AI | Expected: this is the default `--country-iso`; batch pins `DEU` / `SWE` on purpose |
| Large run-to-run variance | Close other applications, use `--repeats 5`, and use exclusive nodes on sHPC |
| Report numbers differ from a colleague's | Compare % savings, not absolute SCI; check the *Actual measurement method* in the report header |
| A `sci_nodb_*` folder remains in the temp directory (Windows) | The SDK leaves SQLite connections to be closed by the garbage collector; the runner forces collection and warns if cleanup still fails. Delete the folder manually |

*Validated with `roche-green-sdk` 2.0.2 sources and CodeCarbon 3.3.2. Target runtime: Python 3.12.*
