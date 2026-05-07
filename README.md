# Redis DSP Candidate Generation Demo

This repository is a production-shaped prototype for a Redis-based DSP retrieval and reranking workflow. The primary path is a synthetic, identity-driven dataset that models:

- a neutral `MAID`-style profile cache
- publisher-scoped identity tokens that resolve into a profile
- an active ad cache with explicit targeting, delivery constraints, and per-ad `taxonomy_filter` AND/OR/NOT expressions over float interest scores
- six side-by-side candidate selection modes plus a `full_realtime` correctness baseline:
  - `maid_bruteforce_sinter`
  - `maid_tightened_sinter`
  - `precomputed_segment`
  - `hybrid_precompute_plus_realtime`
  - `hybrid_bitmap_gating`
  - `hybrid_bitmap_taxonomy`

The goal is to compare quality and latency tradeoffs using plain Redis primitives.

For the current retrieval comparison and measured latency results, see [reports/benchmark_report.md](reports/benchmark_report.md).

## MAID Scores vs Segments

One important modeling choice in this demo is that the synthetic MAID profile carries both:

- raw float interest scores, such as `travel = 0.82`
- derived segment labels, such as `travel_high`

The segment labels are produced by simple bucketing of the MAID's float scores:

- score `>= 0.70` -> `feature_high`
- score `>= 0.45` and `< 0.70` -> `feature_medium`
- score `< 0.45` -> no segment

Those derived segments are then sorted by score, and the top few become the MAID's retrieval segments. For example:

- `travel = 0.82` -> `travel_high`
- `finance = 0.74` -> `finance_high`
- `tech = 0.58` -> `tech_medium`

This distinction matters because most of the fast retrieval strategies use the bucketed segments to narrow the candidate set, rather than comparing raw float values for every ad on the hot path.

- `maid_bruteforce_sinter` and `maid_tightened_sinter` use the strongest MAID segments as Redis `SINTER` keys
- `precomputed_segment` and `hybrid_bitmap_gating` depend on batch-time candidate lists that were built from bucketed segment targeting
- `hybrid_precompute_plus_realtime` and `hybrid_bitmap_taxonomy` still use those coarse segment-driven candidate sets first, but then apply the per-ad `taxonomy_filter` against the MAID's raw float scores before final ranking
- `full_realtime` is the only mode that skips the coarse retrieval shortcut and evaluates the full campaign universe directly against the MAID's live data

That is the core tradeoff in the demo:

- bucketed segments are cheap and Redis-friendly, but lossy
- raw float-score evaluation is more expressive, but more expensive unless it is pushed late in the request path

## Core Story

The synthetic benchmark compares seven execution styles over the same MAID and campaign dataset:

1. `full_realtime`
   - fetch the MAID
   - evaluate the full campaign universe live, including the per-campaign `taxonomy_filter`
   - correctness baseline, not the preferred low-latency path
2. `maid_bruteforce_sinter`
   - resolve the MAID
   - run the original `26`-probe `SINTER` plan
   - fetch state and rerank, including `taxonomy_filter`
3. `maid_tightened_sinter`
   - resolve the MAID
   - run a compact `3`-probe `SINTER` plan
   - fetch state and rerank, including `taxonomy_filter`
4. `precomputed_segment`
   - fetch a prebuilt per-MAID candidate list
   - apply only minimal live gating (pacing, budget, frequency)
   - does **not** evaluate `taxonomy_filter`, so it admits campaigns that should be excluded
5. `hybrid_precompute_plus_realtime`
   - fetch a prebuilt candidate list
   - apply live pacing, budget, frequency, exact targeting, and `taxonomy_filter`
   - rerank the survivors
6. `hybrid_bitmap_gating`
   - fetch a prebuilt candidate list
   - apply a server-side `bm:servable` bitmap gate
   - enforce live frequency caps and rerank
   - does **not** evaluate `taxonomy_filter`
7. `hybrid_bitmap_taxonomy`
   - same retrieval and bitmap-gate path as `hybrid_bitmap_gating`
   - additionally evaluates each ad's `taxonomy_filter` AND/OR/NOT expression against the MAID's float interest scores in app memory before reranking

This makes the latency and recall tradeoffs measurable instead of theoretical, and surfaces the cost of skipping the per-ad `taxonomy_filter` rather than burying it.

The benchmark report has the detailed method definitions, round trips, and p50/p99 comparisons:
- [reports/benchmark_report.md](reports/benchmark_report.md)

## What The Repo Demonstrates

- Redis Hash-based storage for MAID profiles and campaign records
- Redis String-based identity resolution from `identity:<token>` to `maid:<id>`
- Redis String-based precomputed per-MAID candidate lists for hybrid retrieval
- Redis Set-based inverted indexes for country, state, device OS, device type, card tier, and segments
- explicit ad filtering for:
  - `card_tier`
  - `country`
  - `state`
  - `postal_code`
  - `device_type`
  - `device_os`
  - `pacing_status`
  - `daily_budget_usd` vs `spent_today_usd`
  - `frequency_cap`
  - segment `required` / `any_of` / `none_of`
- lightweight in-memory reranking in Python
- offline mode comparison against a full real-time baseline
- serial live load tests by mode
- optional shadow execution for live overlap measurement
- secondary public-dataset adapters for `MIND` and `FairJob`

## Synthetic Dataset Design

The synthetic dataset is the main dataset for the demo because it lets us control both latency behavior and relevance logic.

Each synthetic MAID profile includes:

- `user_id` used as the synthetic MAID identifier
- `identity_tokens`
- `geo` as country plus `state` and `postal_code`
- `device_type` and `device` as OS
- `card_tier` and `spend_tier`
- taxonomy-like interest scores
- derived retrieval segments such as `travel_high`
- per-campaign `frequency_history`

Each campaign includes:

- `geo` for country targeting
- `geo_states`
- `geo_postal_codes`
- `device_types`
- `device` for OS targeting
- `card_tiers`
- `pacing_status`
- `daily_budget_usd`
- `spent_today_usd`
- `frequency_cap`
- segment targeting fields
- bid and freshness fields for reranking

Generated files:

- `maids.jsonl`
- `identity_map.jsonl`
- `user_candidates.jsonl`
- `campaigns.jsonl`
- `interactions.parquet`
- `metadata.json`

For compatibility with older utilities, the generator also writes `users.jsonl`.

## Redis Features Used

The demo intentionally relies on a narrow Redis feature set:

- `HASH`
  - `maid:<id>` (single per-MAID hash; the bid path's hybrid modes `HMGET` only the scoring subset — `user_id`, `interests_json`, `impression_count`)
  - `campaign:<id>`
  - `campaign_state:<id>`
  - `fcap:{maid_id} -> {campaign_id: delivery_count}`
- `STRING`
  - `identity:<token> -> maid_id`
  - `aud:<maid_id> -> [campaign_ids]`
- `BITMAP`
  - `bm:active`
  - `bm:pacing_ok`
  - `bm:budget_ok`
  - `bm:servable`
- `SET`
  - `idx:geo:<country>`
  - `idx:state:<state>`
  - `idx:card_tier:<tier>`
  - `idx:device_type:<type>`
  - `idx:device:<os>`
  - `idx:segment:<segment>`
- `SINTER`
  - exact-match candidate generation across hard filters and strong segments
- `HMGET`
  - compact scoring profile fields, per-MAID frequency counters, and mutable campaign state fields
- pipelining
  - batch candidate probes, metadata reads, and bulk data load

No Redis Search or secondary indexing engine is used in the current mainline demo.

## Hybrid Mode Design

### Batch Layer

The batch step evaluates the full static targeting expression for each MAID against the active campaign set. In this branch, “static” means:

- geo, state, and postal targeting
- device type and OS targeting
- card tier targeting
- segment `required` / `any_of` / `none_of`

It writes:

- `aud:{maid_id}` with the ordered campaign IDs that match that MAID’s static targeting
- `meta:precomputed_candidate_version` as provenance for the current batch snapshot

For the current local workflow, versioning is informational rather than operational. We fully reload the Redis dataset, so old versions do not need online cleanup. If we later switch to live swaps, the right pattern is versioned prefixes plus TTL or `UNLINK` after the active pointer moves.

### Incremental Layer

Mutable delivery state stays live in Redis:

- `campaign_state:{id}` for pacing, budget, and status
- `fcap:{maid_id}` hash for per-user delivery counters
- `bm:servable` bitmap for global active+pacing+budget eligibility

That lets the hybrid mode push the expensive static matching into batch while still enforcing live delivery constraints online.

### Request Flow

At request time, the hybrid mode does:

1. `GET identity:<token>` to resolve the incoming identifier into a `maid_id`
2. `HMGET maid:<id> user_id interests_json impression_count` to fetch only the scoring signals needed online
3. `GET aud:<maid_id>` for the precomputed candidate campaign list
4. server-side bitmap gating against `bm:servable`
5. campaign metadata fetch plus `HMGET fcap:{maid_id} <campaign_ids...>`
6. exact frequency-cap check and in-memory reranking with the scoring signals from the `maid:` HMGET

`shadow_modes` are supported on the request so the hybrid path can be compared live against `full_realtime` and `precomputed_segment` without changing the returned mode.

## Current Synthetic Snapshot

Source artifacts:

- [reports/generated/evaluation.json](/Users/jeremy.plichta/work/mastercard-dsp/reports/generated/evaluation.json)
- [reports/generated/hybrid_benchmark.json](/Users/jeremy.plichta/work/mastercard-dsp/reports/generated/hybrid_benchmark.json)
- [reports/generated/hybrid_shadow_smoke.json](/Users/jeremy.plichta/work/mastercard-dsp/reports/generated/hybrid_shadow_smoke.json)
- [reports/benchmark_report.md](reports/benchmark_report.md)

Offline comparison on the full synthetic dataset (`4000` MAIDs, `2500` campaigns, `120000` interactions, `~80%` of campaigns carry a `taxonomy_filter` AND/OR/NOT expression):

- `full_realtime`
  - `NDCG@K 0.9818`
  - `candidate recall 1.0`
  - `avg candidate count 2500`
- `precomputed_segment` (no `taxonomy_filter`)
  - `NDCG@K 0.5584`
  - `top-result Jaccard vs full 0.40`
  - admits campaigns the `taxonomy_filter` would reject
- `hybrid_precompute_plus_realtime` (full live gating, including `taxonomy_filter`)
  - `NDCG@K 0.9818`
  - `top-result Jaccard vs full 1.0`
- `hybrid_bitmap_gating` (no `taxonomy_filter`)
  - `NDCG@K 0.5584`
  - `top-result Jaccard vs full 0.40`
- `hybrid_bitmap_taxonomy` (bitmap gate plus app-side `taxonomy_filter`)
  - `NDCG@K 0.8751`
  - `top-result Jaccard vs full 0.80`

Two independent properties show up in these numbers, both of which are structural rather than empirical:

1. The precomputed candidate list is built from the full static targeting expression, so by construction `hybrid_precompute_plus_realtime` reaches the same eligible set as `full_realtime` while only fetching `~30` candidates instead of `2500`. This verifies the precompute does not lose information; it does not measure a ranking improvement.
2. Modes that skip `taxonomy_filter` (`precomputed_segment`, `hybrid_bitmap_gating`) admit campaigns the per-ad rule would reject, and their ranking quality drops accordingly. The `hybrid_bitmap_taxonomy` mode closes most of that gap by evaluating the AND/OR/NOT expression in app memory after the bitmap gate; the residual gap vs `full_realtime` comes from the bitmap path skipping the live `campaign_state` fanout, which is the same tradeoff already documented for `hybrid_bitmap_gating`.

## Native VM Latency

Serial live load on a dedicated GCP VM with native `redis-server` and native `uvicorn`:

- VM shape: `n2-standard-8`
- Redis: native `redis-server 7.0.15`
- app host: native Python 3.11 process

Methodology:

- 12 RPS, concurrency = 1, 10 s measured + 2 s warmup per mode → **N ≈ 120 samples per mode**.
- p99 from N = 120 is essentially the second-largest observation. Read the p99 column as indicative, not as a tight bound. p50 is the reliable comparison point.
- Hot/cold sampler: 70% of requests target a hot 20% of MAIDs.
- These are serial-latency numbers. They do **not** characterize behavior under realistic concurrent bid traffic. A concurrent test at production scale is described in [`reports/full_scale_gcp_test_spec.md`](/Users/jeremy.plichta/work/mastercard-dsp/reports/full_scale_gcp_test_spec.md).
- The numbers below are from the current native-VM benchmark run with `cache_campaigns_in_memory=False`, so campaign metadata fetch cost is included.

Decision-path latency from the current native VM benchmark:

- `full_realtime`
  - decision path `236.653 ms` p50, `303.059 ms` p99
  - validated candidates `235.704 ms` p50, `302.076 ms` p99
  - average total Redis round trips `5`
- `maid_bruteforce_sinter`
  - decision path `19.790 ms` p50, `52.600 ms` p99
  - validated candidates `18.814 ms` p50, `49.568 ms` p99
  - average total Redis round trips `31`
- `maid_tightened_sinter`
  - decision path `7.254 ms` p50, `29.691 ms` p99
  - validated candidates `6.222 ms` p50, `23.606 ms` p99
  - average total Redis round trips `6`
- `precomputed_segment`
  - decision path `4.537 ms` p50, `18.180 ms` p99
  - validated candidates `3.468 ms` p50, `16.415 ms` p99
  - average total Redis round trips `6`
- `hybrid_precompute_plus_realtime`
  - decision path `4.691 ms` p50, `42.455 ms` p99
  - validated candidates `3.531 ms` p50, `24.616 ms` p99
  - average total Redis round trips `6`
- `hybrid_bitmap_gating`
  - decision path `3.991 ms` p50, `5.332 ms` p99
  - validated candidates `2.816 ms` p50, `3.991 ms` p99
  - candidate generation `0.525 ms` avg, `0.685 ms` p99
  - average total Redis round trips `5`
- `hybrid_bitmap_taxonomy`
  - decision path `3.723 ms` p50, `9.916 ms` p99
  - validated candidates `2.702 ms` p50, `8.684 ms` p99
  - candidate generation `0.464 ms` avg, `0.616 ms` p99
  - average total Redis round trips `5`

Shadow execution smoke test for hybrid mode:

- average overlap vs `full_realtime`: `1.0` top-result Jaccard
- average overlap vs `precomputed_segment`: `1.0` top-result Jaccard

With direct per-MAID candidate lists, the main tradeoff changes:

- `full_realtime` remains the baseline but is much too expensive online
- `hybrid_precompute_plus_realtime` preserves the same ranking output as `full_realtime` on the synthetic dataset while cutting the candidate set from `2500` ads to about `30`
- `precomputed_segment` and `hybrid_bitmap_gating` are faster, but both intentionally skip `taxonomy_filter` and lose ranking quality as a result
- `hybrid_bitmap_taxonomy` is the best low-latency path that still enforces the per-ad float-score `taxonomy_filter`, and on the native VM it stays under the `< 10 ms` decision-path p99 target
- on a native VM, identity resolution, `maid:` HMGET, candidate lookup, and campaign fetch all fall into sub-millisecond or low-single-digit-millisecond behavior instead of the noisier local-container tails

## Reproducing The VM Benchmark

The tested GCP Terraform scaffold is in [terraform/gcp/README.md](/Users/jeremy.plichta/work/mastercard-dsp/terraform/gcp/README.md).

The benchmark flow on the VM is:

1. Provision the VM with Terraform from `terraform/gcp`.
2. Transfer the repo snapshot or clone the repo onto the VM.
3. Create a venv and install the package with `pip install -e .`.
4. Generate the synthetic dataset with `data.synthetic.generate_dataset(...)`.
5. Load Redis with `python3 data/load_redis.py --redis-url redis://127.0.0.1:6379/0 --dataset-dir data/generated/synthetic`.
6. Run the app with `REDIS_URL=redis://127.0.0.1:6379/0 python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000`.
7. Run `python3 experiments/benchmark.py --base-url http://127.0.0.1:8000 --dataset-dir data/generated/synthetic --output reports/benchmark_report.md > reports/generated/hybrid_benchmark.json`.

## Repository Layout

- `app/`: FastAPI service, Redis repository, candidate generation, hybrid execution, ranking, instrumentation
- `data/`: synthetic generator, hybrid precompute builder, Redis loader, public dataset adapters
- `experiments/`: offline evaluation and benchmark helpers
- `loadtest/`: configurable load driver
- `observability/`: Prometheus, Grafana, and OTel collector configs
- `notebooks/`: walkthrough notebooks
- `tests/`: unit coverage

## Local Stack

Prerequisites:

- Python 3.11+
- Docker + Docker Compose

Install dependencies:

```bash
make install
```

Start the stack:

```bash
make up
```

Endpoints:

- API: `http://localhost:8000`
- Health: `http://localhost:8000/health`
- Metrics: `http://localhost:8000/metrics`
- Redis: `localhost:6381`
- Prometheus: `http://localhost:9091`
- Grafana: `http://localhost:3002` with `admin/admin`

Stop the stack:

```bash
make down
```

## Synthetic Dataset Commands

Generate a local MAID-style dataset:

```bash
python3 -m data.synthetic \
  --output data/generated/synthetic \
  --num-users 4000 \
  --num-campaigns 2500 \
  --num-interactions 120000 \
  --feature-count 12
```

Load Redis manually:

```bash
python3 -m data.load_redis \
  --redis-url redis://localhost:6381/0 \
  --dataset-dir data/generated/synthetic
```

Run the offline evaluator:

```bash
python3 -m experiments.evaluate --dataset-dir data/generated/synthetic
```

Run the mode benchmark:

```bash
python3 -m experiments.benchmark \
  --base-url http://127.0.0.1:8001 \
  --dataset-dir data/generated/synthetic \
  --output reports/benchmark_report.md
```

## Notebooks

The notebook sequence is a **runnable demo** that walks the bid path from naive to fast. Each notebook executes real Redis commands against a live cluster — no simulation — so the timings and key contents are live.

**Run in Colab** (zero local setup): each notebook has an *Open in Colab* badge at the top. The setup cells clone the repo, install dependencies, start a Redis Stack server, and load the synthetic dataset. First run takes ~60–90 seconds; everything after that is near-instant.

| Notebook | Open in Colab |
| --- | --- |
| 01 · Bid request and data shape | [![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/redis-field-engineering/redis-dsp-demo/blob/main/notebooks/01_bid_request_and_data.ipynb) |
| 02 · `full_realtime` baseline | [![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/redis-field-engineering/redis-dsp-demo/blob/main/notebooks/02_full_realtime_baseline.ipynb) |
| 03 · SINTER tightening | [![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/redis-field-engineering/redis-dsp-demo/blob/main/notebooks/03_sinter_tightening.ipynb) |
| 04 · Precomputed candidates | [![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/redis-field-engineering/redis-dsp-demo/blob/main/notebooks/04_precomputed_candidates.ipynb) |
| 05 · Bitmap gate Lua script | [![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/redis-field-engineering/redis-dsp-demo/blob/main/notebooks/05_bitmap_gate_lua.ipynb) |
| 06 · Taxonomy filter + headline comparison | [![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/redis-field-engineering/redis-dsp-demo/blob/main/notebooks/06_taxonomy_filter_and_comparison.ipynb) |

**Run locally** against the docker-compose stack:

```bash
make up
python3 -m data.load_redis --redis-url redis://localhost:6381/0 \
  --dataset-dir data/generated/synthetic
jupyter lab notebooks/
```

The notebook setup cells auto-detect a local environment and skip the Colab-specific steps.

What each notebook demonstrates:

1. [01_bid_request_and_data.ipynb](notebooks/01_bid_request_and_data.ipynb) — the workload: one bid request, one MAID profile, one ad cache. What Redis is holding.
2. [02_full_realtime_baseline.ipynb](notebooks/02_full_realtime_baseline.ipynb) — the naive path: fetch every campaign, filter in app. The number every other mode beats.
3. [03_sinter_tightening.ipynb](notebooks/03_sinter_tightening.ipynb) — inverted indexes, the bruteforce 26-probe plan vs the tightened 3-probe pipelined plan.
4. [04_precomputed_candidates.ipynb](notebooks/04_precomputed_candidates.ipynb) — the central pattern: per-MAID `aud:` STRING built offline, bid path collapses to one `GET`.
5. [05_bitmap_gate_lua.ipynb](notebooks/05_bitmap_gate_lua.ipynb) — server-side gating: the actual Lua script that joins `aud:` against `bm:servable` in one round trip.
6. [06_taxonomy_filter_and_comparison.ipynb](notebooks/06_taxonomy_filter_and_comparison.ipynb) — app-side AND/OR/NOT on float interest scores, plus the headline mode comparison.

Two appendix notebooks cover related but off-path material — public-dataset adapters and segment-bucket boolean targeting — under [notebooks/appendix/](notebooks/appendix/).
