# SCI Reference Benchmark Report

> Generated automatically by `run_benchmark.py`. Open it in VS Code with **Ctrl+Shift+V** (or in GitHub/GitLab). No web server is required.

| Field | Value |
|---|---|
| Date (UTC) | 2026-11-19T13:50:40+00:00 |
| Detected environment | **sHPC** (host `rkans023is09`) |
| Mode | `compare-all` · repeats: 3 (median reported) |
| Actual measurement method | `rapl`, `rapl + CLEER_v2` |
| Default grid (I) | `CHE` (batch pins `DEU`/`SWE`) |
| Local embodied rate (M) | 0.0024 gCO2e/s |
| SQLite telemetry | `temporary (--no-db), not persisted` |
| Platform | Linux-4.18.0-553.8.1.el8_10.x86_64-x86_64-with-glibc2.37 · Python 3.12.3 · roche-green-sdk 2.0.2 · codecarbon 3.3.2 |

## Formula

$$\text{SCI} = \frac{(E \cdot I) + M}{R}$$

- **E**: energy (kWh) measured by CodeCarbon (`rapl`, `windows_emi`, `tdp_estimate`, ...). For AI runs, the remote energy estimated by `CLEER_v2` is added.
- **I**: carbon intensity of the country grid (gCO2e/kWh), shown in the *Region* column.
- **M**: embodied hardware emissions (gCO2e).
- **R**: functional unit, **identical** for each pattern and its baseline.

$$\text{Saving (\%)} = \frac{SCI_{baseline} - SCI_{pattern}}{SCI_{baseline}} \times 100$$

## System Boundaries

| Workload | Inside the boundary (measured) | Outside the boundary |
|---|---|---|
| API | Serving the R requests: SQL queries, filtering, JSON serialization, index creation (`pagination`) and cache warm-up (`cache`). | Seeding the in-memory SQLite database (5,000 synthetic patients) and generating the Zipf request trace. |
| BATCH | Extract (read CSV), Transform (unit normalization, out-of-range flags, checksum) and Load into an on-disk SQLite warehouse. | Generating the source CSV. The `carbon_aware` time shift is simulated (only the region change affects I). |
| AI | The R simulated inferences run locally, plus remote accelerator energy and embodied emissions estimated by CLEER_v2 from the token counts. | Loading fixtures and warming the shared prompt prefix in the KV-cache. |

## Comparison by Category

### API: clinical cohort queries

Functional unit: **R = 1000 `api_requests`** (same R for every variant).

| Variant | Official GSF pattern | Time (s) | E (kWh) | M (gCO₂e) | Region (I) | **SCI (gCO₂e / api_requests)** | **Saving vs baseline** | Relative SCI |
|---|---|---:|---:|---:|:---:|---:|---:|---|
| `api_baseline` | _Baseline (anti-pattern)_ | 9.44 | 9.746e-04 | 2.265e-02 | CHE | **5.660e-05** | baseline | `████████████████████` |
| `cache` | [Cache static data](https://patterns.greensoftware.foundation/development/data-handling/cache-static-data) | 0.89 | 8.688e-05 | 2.127e-03 | CHE | **5.153e-06** | **+90.9%** | `██░░░░░░░░░░░░░░░░░░` |
| `pagination` | [Reduce transmitted data](https://patterns.greensoftware.foundation/development/data-handling/reduce-transmitted-data) | 0.13 | 7.928e-06 | 3.079e-04 | CHE | **5.841e-07** | **+99.0%** | `█░░░░░░░░░░░░░░░░░░░` |

<details><summary>Variant details</summary>

| Variant | What it does | GSF lifecycle | Priority Score | Workload metrics |
|---|---|---|---:|---|
| `api_baseline` | SELECT * without index/pagination, no cache, JSON indent=2 | N/A | - | 33.54 MB transmitted, 5,000,000 rows read |
| `cache` | In-memory LRU cache (maxsize=128) for recurrent cohorts | Development | 15 | 33.54 MB transmitted, 91% cache hit ratio, 450,000 rows read |
| `pagination` | Early SQL filter + index + column projection + LIMIT 50 + compact JSON | Development | 8 | 1.21 MB transmitted, 48,965 rows read |

</details>

### Batch: lab results ETL

Functional unit: **R = 10000 `processed_records`** (same R for every variant).

| Variant | Official GSF pattern | Time (s) | E (kWh) | M (gCO₂e) | Region (I) | **SCI (gCO₂e / processed_records)** | **Saving vs baseline** | Relative SCI |
|---|---|---:|---:|---:|:---:|---:|---:|---|
| `batch_baseline` | _Baseline (anti-pattern)_ | 3.18 | 3.267e-04 | 7.628e-03 | DEU | **1.321e-05** | baseline | `████████████████████` |
| `batching` | [Queue non-urgent processing requests](https://patterns.greensoftware.foundation/architecture/system-topology/queue-non-urgent-requests) | 0.11 | 6.796e-06 | 2.562e-04 | DEU | **2.845e-07** | **+97.8%** | `█░░░░░░░░░░░░░░░░░░░` |
| `carbon_aware` | [Use carbon-aware scheduling and region selection for AI workloads](https://patterns.greensoftware.foundation/operations/carbon-aware-ai-scheduling) | 0.10 | 6.809e-06 | 2.274e-04 | SWE | **5.088e-08** | **+99.6%** | `█░░░░░░░░░░░░░░░░░░░` |

<details><summary>Variant details</summary>

| Variant | What it does | GSF lifecycle | Priority Score | Workload metrics |
|---|---|---|---:|---|
| `batch_baseline` | commit() per record on disk, carbon-intensive grid (DEU) | N/A | - | 10,000 commits |
| `batching` | executemany in a single transaction (same DEU grid) | Architecture | 6 | 1 commits |
| `carbon_aware` | batching + time/region shift to greenest forecast window (SWE) | Operations | 9 | 1 commits, window SWE +5 h (23 vs 418 gCO2e/kWh) |

</details>

### AI: simulated LLM inference (SCI for AI + CLEER_v2)

Functional unit: **R = 100 `ai_inferences`** (same R for every variant).

| Variant | Official GSF pattern | Time (s) | E (kWh) | M (gCO₂e) | Region (I) | **SCI (gCO₂e / ai_inferences)** | **Saving vs baseline** | Relative SCI |
|---|---|---:|---:|---:|:---:|---:|---:|---|
| `ai_baseline` | _Baseline (anti-pattern)_ | 0.54 | 5.158e-01 | 2.413e+01 | CHE | **2.046e+00** | baseline | `████████████████████` |
| `model_rightsizing` | [Use right-sized and energy-efficient AI models](https://patterns.greensoftware.foundation/development/right-sized-energy-efficient-ai-models) | 0.19 | 4.615e-01 | 2.159e+01 | CHE | **1.831e+00** | **+10.5%** | `██████████████████░░` |
| `prompt_compression` | [Optimize agent orchestration to reduce unnecessary model calls](https://patterns.greensoftware.foundation/development/optimize-agent-orchestration-reduce-model-calls) | 0.17 | 1.855e-01 | 8.678e+00 | CHE | **7.360e-01** | **+64.0%** | `███████░░░░░░░░░░░░░` |

<details><summary>Variant details</summary>

| Variant | What it does | GSF lifecycle | Priority Score | Workload metrics |
|---|---|---|---:|---|
| `ai_baseline` | llama-3-70b-fp16, 1800 prompt / 0 cached / 350 completion tokens per inference | N/A | - | 3,010,000 simulated compute units |
| `model_rightsizing` | llama-3-8b-int8-quantized, 1800 prompt / 0 cached / 250 completion | Development | 12 | 205,000 simulated compute units |
| `prompt_compression` | 70B + compressed prompt + KV-cache prefix: 320 prompt / 1100 cached / 180 completion | Development | 9 | 700,000 simulated compute units |

</details>

## SCI for AI Breakdown (`CLEER_v2`)

`BASELINE_SCI_GCO2E` is the cost per inference **without** prefix caching; `CLEER_SCI_GCO2E` is the cost **with** the 85% discount applied to `cached_tokens`.

| Variant | Model | Prompt / cached / completion tokens (total) | BASELINE_SCI_GCO2E | CLEER_SCI_GCO2E | Caching gain | |
|---|---|---|---:|---:|---:|---|
| `ai_baseline` | `llama-3-70b-fp16` | 180,000 / 0 / 35,000 | 2.046e+00 | 2.046e+00 | **0.0%** | `░░░░░░░░░░░░░░░░░░░░` |
| `model_rightsizing` | `llama-3-8b-int8-quantized` | 180,000 / 0 / 25,000 | 1.831e+00 | 1.831e+00 | **0.0%** | `░░░░░░░░░░░░░░░░░░░░` |
| `prompt_compression` | `llama-3-70b-fp16` | 32,000 / 110,000 / 18,000 | 1.407e+00 | 7.360e-01 | **47.7%** | `██████████░░░░░░░░░░` |

> **Note:** CLEER_v2 estimates energy per token with constants that do not depend on model size, so `model_rightsizing` only reflects the reduction in `completion_tokens`. The real compute saving (see *simulated compute units* in the variant details) is larger, so that figure is a lower bound.

## Quick Experimentation Guide

Each pattern is always compared with the baseline of its category using the same R:

```bash
python run_benchmark.py --list                          # flags + official GSF metadata
python run_benchmark.py --compare-all --repeats 1       # all 9 experiments (quick demo)
python run_benchmark.py --compare-all --repeats 5       # more robust results (median)

# One pattern against its baseline
python run_benchmark.py --pattern cache
python run_benchmark.py --pattern pagination
python run_benchmark.py --pattern batching
python run_benchmark.py --pattern carbon_aware
python run_benchmark.py --pattern model_rightsizing
python run_benchmark.py --pattern prompt_compression

# Change the environment assumptions
python run_benchmark.py --pattern cache --country-iso SWE    # different power grid (I)
python run_benchmark.py --pattern cache --embodied-rate 0.01 # more carbon-intensive hardware (M)
python run_benchmark.py --compare-all --no-db               # do not persist SQLite telemetry

# Rebuild this report from results/benchmark_summary.json
python report_generator.py
```

Variants included in this report: `ai_baseline`, `api_baseline`, `batch_baseline`, `batching`, `cache`, `carbon_aware`, `model_rightsizing`, `pagination`, `prompt_compression`.
