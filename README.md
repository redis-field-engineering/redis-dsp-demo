# Redis DSP Candidate Generation Demo

This repository is a production-shaped local prototype for a Redis-based DSP retrieval and reranking workflow. The primary path is a synthetic, identity-driven dataset that models:

- a neutral `MAID`-style profile cache
- publisher-scoped identity tokens that resolve into a profile
- an active ad cache with explicit targeting and delivery constraints
- three side-by-side candidate selection modes:
  - `full_realtime`
  - `precomputed_segment`
  - `hybrid_precompute_plus_realtime`

The goal is to compare quality and latency tradeoffs using plain Redis primitives, without introducing Redis Search.

## Core Story

The synthetic benchmark compares three execution styles over the same MAID and campaign dataset:

1. `full_realtime`
   - fetch the MAID
   - evaluate the full campaign cache at request time
2. `precomputed_segment`
   - fetch a prebuilt per-MAID candidate list
   - apply only minimal live gating
3. `hybrid_precompute_plus_realtime`
   - fetch a prebuilt candidate list
   - apply live pacing, budget, frequency, and exact targeting
   - rerank the survivors

This makes the latency and recall tradeoffs measurable instead of theoretical.

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
  - `maid:<id>`
  - `maid_hot:<id>`
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
2. `HMGET maid_hot:<id> user_id interests_json impression_count` to fetch only the scoring signals needed online
3. `GET aud:<maid_id>` for the precomputed candidate campaign list
4. server-side bitmap gating against `bm:servable`
5. campaign metadata fetch plus `HMGET fcap:{maid_id} <campaign_ids...>`
6. exact frequency-cap check and in-memory reranking with the `maid_hot` signals

`shadow_modes` are supported on the request so the hybrid path can be compared live against `full_realtime` and `precomputed_segment` without changing the returned mode.

## Current Synthetic Snapshot

Source artifacts:

- [reports/generated/evaluation.json](/Users/jeremy.plichta/work/mastercard-dsp/reports/generated/evaluation.json)
- [reports/generated/hybrid_benchmark.json](/Users/jeremy.plichta/work/mastercard-dsp/reports/generated/hybrid_benchmark.json)
- [reports/generated/hybrid_shadow_smoke.json](/Users/jeremy.plichta/work/mastercard-dsp/reports/generated/hybrid_shadow_smoke.json)
- [reports/benchmark_report.md](/Users/jeremy.plichta/work/mastercard-dsp/reports/benchmark_report.md)

Offline comparison on the full synthetic dataset (`4000` MAIDs, `2500` campaigns, `120000` interactions):

- `full_realtime`
  - `NDCG@K 0.9813`
  - `candidate recall 1.0`
  - `avg candidate count 2500`
- `precomputed_segment`
  - `NDCG@K 0.9813`
  - `candidate recall 1.0`
  - `top-result Jaccard vs full 1.0`
- `hybrid_precompute_plus_realtime`
  - `NDCG@K 0.9813`
  - `candidate recall 1.0`
  - `top-result Jaccard vs full 1.0`

The key result is that once batch computes the full static targeting selection per MAID, the precomputed modes preserve the full real-time ranking output exactly while only looking at about `30` candidates instead of `2500`.

## Live Serial Latency

Serial live load against the current branch app:

- `full_realtime`
  - handler `37.106 ms` avg, `64.749 ms` p95, `85.748 ms` p99
  - campaign materialization dominates because it evaluates the full campaign cache
- `precomputed_segment`
  - handler `5.643 ms` avg, `12.778 ms` p95, `23.662 ms` p99
  - decision path `4.566 ms` p50, `23.535 ms` p99
  - validated candidates `2.629 ms` p50, `14.343 ms` p99
- `hybrid_precompute_plus_realtime`
  - handler `6.022 ms` avg, `15.956 ms` p95, `43.229 ms` p99
  - decision path `4.133 ms` p50, `43.152 ms` p99
  - validated candidates `2.294 ms` p50, `18.838 ms` p99
  - average Redis round trips `5`
- `hybrid_bitmap_gating`
  - handler `3.66 ms` avg, `10.458 ms` p95, `14.939 ms` p99
  - decision path `2.9 ms` p50, `14.899 ms` p99
  - validated candidates `1.066 ms` p50, `6.248 ms` p99
  - candidate generation `0.758 ms` avg, `3.252 ms` p99
  - average Redis round trips `4`

Shadow execution smoke test for hybrid mode:

- average request latency with `full_realtime` and `precomputed_segment` in shadow: `57.196 ms` handler
- average overlap vs `full_realtime`: `1.0` top-result Jaccard
- average overlap vs `precomputed_segment`: `1.0` top-result Jaccard

With direct per-MAID candidate lists, the main tradeoff changes:

- `full_realtime` remains the baseline but is much too expensive online
- `precomputed_segment` and `hybrid` preserve the same ranking output as `full_realtime` on the synthetic dataset
- the bitmap-gated variant is currently the best live path because it eliminates campaign-state fanout and collapses frequency lookups into a single per-MAID hash
- the remaining latency now comes mostly from identity resolution, `maid_hot` fetch, and residual candidate/frequency materialization rather than candidate selection itself

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

The main walkthrough sequence is now:

- [01_synthetic_data.ipynb](/Users/jeremy.plichta/work/mastercard-dsp/notebooks/01_synthetic_data.ipynb)
- [02_redis_key_schema.ipynb](/Users/jeremy.plichta/work/mastercard-dsp/notebooks/02_redis_key_schema.ipynb)
- [03_ranking_evaluation.ipynb](/Users/jeremy.plichta/work/mastercard-dsp/notebooks/03_ranking_evaluation.ipynb)
- [06_boolean_targeting_walkthrough.ipynb](/Users/jeremy.plichta/work/mastercard-dsp/notebooks/06_boolean_targeting_walkthrough.ipynb)
- [07_hybrid_mode_comparison.ipynb](/Users/jeremy.plichta/work/mastercard-dsp/notebooks/07_hybrid_mode_comparison.ipynb)

`MIND` and `FairJob` remain useful secondary comparisons, but the main demo story is now the synthetic identity-driven benchmark and the hybrid retrieval mode.
