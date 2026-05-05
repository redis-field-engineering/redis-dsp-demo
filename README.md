# Redis DSP Candidate Generation Demo

This repository is a production-shaped local prototype for a Redis-based DSP retrieval and reranking workflow. The primary path is now a synthetic, identity-driven dataset that models:

- a neutral `MAID`-style profile cache
- publisher-scoped identity tokens that resolve into a profile
- an active ad cache with explicit targeting and delivery constraints
- Redis Set-based candidate generation plus in-memory reranking

The goal is to keep the request path simple and inspectable while getting closer to the real ad-tech retrieval problem.

## Core Story

The main synthetic flow is:

1. accept an `identity_token` on the request
2. resolve that token to a synthetic `MAID`
3. fetch the MAID profile from Redis
4. retrieve candidate campaigns from Redis inverted indexes
5. apply exact campaign eligibility in the app
6. rerank the survivors in memory

This keeps the current demo grounded in plain Redis primitives without introducing Redis Search.

## What The Repo Demonstrates

- Redis Hash-based storage for MAID profiles and campaign records
- Redis String-based identity resolution from `identity:<token>` to `maid:<id>`
- Redis Set-based inverted indexes for country, device OS, device type, card tier, and segments
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
- offline quality evaluation on synthetic data
- local load testing and observability
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
- `campaigns.jsonl`
- `interactions.parquet`
- `metadata.json`

For compatibility with some older utilities, the generator also writes `users.jsonl`.

## Redis Features Used

The demo intentionally relies on a narrow Redis feature set:

- `HASH`
  - `maid:<id>`
  - `campaign:<id>`
- `STRING`
  - `identity:<token> -> maid_id`
- `SET`
  - `idx:geo:<country>`
  - `idx:card_tier:<tier>`
  - `idx:device_type:<type>`
  - `idx:device:<os>`
  - `idx:segment:<segment>`
- `SINTER`
  - exact-match candidate generation across hard filters and strong segments
- `HGETALL`
  - profile and campaign reads
- pipelining
  - batch campaign fetch and bulk data load

No Redis Search or secondary indexing engine is used in the current mainline demo.

## Retrieval Flow

The hot path is now identity-driven:

1. `GET identity:<token>` resolves the synthetic identity token to a MAID.
2. `HGETALL maid:<id>` fetches the full MAID profile.
3. Candidate generation probes small inverted-index intersections built from:
   - `card_tier`
   - `country`
   - `device_type`
   - `device_os`
   - strong user segments
4. Candidate campaign hashes are fetched in one batch.
5. Exact filtering applies the broader campaign rules:
   - geo hierarchy
   - device hierarchy
   - card tier
   - pacing
   - frequency cap
   - segment logic
6. Remaining campaigns are reranked in memory.

The request model accepts either:

- `identity_token`
- `user_id`

The preferred synthetic path is `identity_token`.

## Candidate Generation Strategies

The repo still includes two retrieval planners:

- `naive`
  - intersects multiple top user-interest segments too early
- `union_probe`
  - probes strong segments separately and merges results round-robin

The synthetic comparison artifact is in [reports/retrieval_strategy_comparison.md](/Users/jeremy.plichta/work/mastercard-dsp/reports/retrieval_strategy_comparison.md).

## Current Synthetic Snapshot

Current offline evaluation on the full synthetic MAID dataset (`4000` MAIDs, `2500` campaigns, `120000` interactions):

- `NDCG@K`: `0.917`
- `Precision@K`: `0.9901`
- `Recall@K`: `0.2594`
- `F1@K`: `0.4007`
- `Candidate generation recall`: `0.4704`

The important takeaway is that the updated exact-filter model is much stricter than the older segment-only synthetic path:

- top-ranked survivors are still high precision
- candidate recall is now the limiting factor
- retrieval quality is no longer artificially near-perfect on the larger dataset

## Live Serial Latency

Current live benchmark on the rebuilt local stack, using serial requests against the identity-token path:

- client HTTP latency:
  `31.788 ms` average, `88.044 ms` p95, `194.165 ms` p99
- server process latency:
  `25.953 ms` average, `77.929 ms` p95, `184.283 ms` p99
- handler latency:
  `23.486 ms` average, `72.582 ms` p95, `174.359 ms` p99

Phase timing sampled from live `/rank` responses:

- identity + MAID fetch:
  `2.957 ms` average, `3.298 ms` p95, `24.525 ms` p99
- candidate generation:
  `15.276 ms` average, `36.878 ms` p95, `108.828 ms` p99
- campaign materialization:
  `0.029 ms` average, `0.09 ms` p95, `0.225 ms` p99
- reranking:
  `0.065 ms` average, `0.12 ms` p95, `0.335 ms` p99
- total handler time:
  `18.526 ms` average, `48.383 ms` p95, `164.494 ms` p99

The current bottleneck is clearly candidate generation, not reranking or campaign fetch.

## Repository Layout

- `app/`: FastAPI service, Redis repository, candidate generation, ranking, instrumentation
- `data/`: synthetic generator, Redis loader, public dataset adapters
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

## Notebooks

The main walkthrough sequence is now:

- [01_synthetic_data.ipynb](/Users/jeremy.plichta/work/mastercard-dsp/notebooks/01_synthetic_data.ipynb)
- [02_redis_key_schema.ipynb](/Users/jeremy.plichta/work/mastercard-dsp/notebooks/02_redis_key_schema.ipynb)
- [03_ranking_evaluation.ipynb](/Users/jeremy.plichta/work/mastercard-dsp/notebooks/03_ranking_evaluation.ipynb)
- [06_boolean_targeting_walkthrough.ipynb](/Users/jeremy.plichta/work/mastercard-dsp/notebooks/06_boolean_targeting_walkthrough.ipynb)

`MIND` and `FairJob` remain useful secondary comparisons, but the primary demo story is now the synthetic MAID + identity + explicit ad-filter path.

Fresh benchmark artifacts from the current stack are in:

- [reports/generated/evaluation.json](/Users/jeremy.plichta/work/mastercard-dsp/reports/generated/evaluation.json)
- [reports/generated/loadtest.json](/Users/jeremy.plichta/work/mastercard-dsp/reports/generated/loadtest.json)
- [reports/benchmark_report.md](/Users/jeremy.plichta/work/mastercard-dsp/reports/benchmark_report.md)
