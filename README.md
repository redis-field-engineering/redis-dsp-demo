# Redis DSP Candidate Generation Demo

This repository is a production-shaped local prototype for a Redis-based DSP candidate-generation and reranking workflow. The core demo is built around a synthetic dataset so the full retrieval path, ranking logic, and latency behavior are easy to inspect and reproduce locally.

What the repo demonstrates:

- Redis Set-based candidate generation with explicit inverted indexes
- Redis Hash-based storage for user and campaign metadata
- Lightweight in-memory reranking in Python
- Offline quality evaluation on synthetic data with candidate-recall measurement
- Serial load testing with average, p95, and p99 latency reporting
- Optional secondary dataset paths for `MIND` and `FairJob`
- Local observability with Prometheus, Grafana, and OpenTelemetry

## Headline Results

Synthetic offline quality:

- `NDCG@K`: `0.9946`
- `Precision@K`: `0.9001`
- `Recall@K`: `0.9606`
- `F1@K`: `0.9113`
- `Candidate generation recall`: `1.0`

Synthetic retrieval and reranking latency:

- Redis `HGETALL user`:
  `0.278 ms` average, `0.476 ms` p95, `0.945 ms` p99
- Redis candidate generation (`SINTER` ladder):
  `0.568 ms` average, `0.884 ms` p95, `4.128 ms` p99
- In-memory reranking:
  `0.023 ms` average, `0.046 ms` p95, `0.073 ms` p99

These numbers are the phase-level timings from the synthetic serving path. They focus on Redis retrieval and reranking rather than full HTTP application latency. The current quality and benchmark summaries live in [reports/generated/evaluation.json](/Users/jeremy.plichta/work/mastercard-dsp/reports/generated/evaluation.json) and [reports/benchmark_report.md](/Users/jeremy.plichta/work/mastercard-dsp/reports/benchmark_report.md).

## Core Story

The main question in this demo is:

Can Redis Sets retrieve a small, relevant campaign pool fast enough that the hot path stays in low single-digit milliseconds?

The answer from this prototype is yes for the synthetic workload:

- retrieval avoids scanning the full campaign universe
- the hot path uses a few exact-match Redis operations
- reranking happens in memory over a small candidate pool
- quality is strong because the synthetic generator and ranking model are intentionally aligned

## Redis Features Used

The demo relies on a small, explicit subset of Redis functionality:

- `HASH`
  Used for `user:<id>` and `campaign:<id>` records
- `SET`
  Used for inverted indexes such as `idx:geo:*`, `idx:device:*`, and `idx:segment:*`
- `SINTER`
  Used for exact-match candidate generation across hard filters and strong user segments
- `HGETALL`
  Used to fetch user profiles and campaign metadata
- pipelining
  Used when loading data and when campaign hashes are fetched without the in-process cache

This is intentionally simple. The point of the prototype is to show how far a plain set-based inverted index can go before introducing more specialized retrieval layers.

## Key Schema

The synthetic path uses three Redis object families:

- `user:<id>`
  Redis Hash with user features, interest map, and segment list
- `campaign:<id>`
  Redis Hash with targeting metadata, reranking weights, and bid
- `idx:*`
  Redis Sets for exact-match retrieval

Example keys:

- `user:u00042`
- `campaign:c00123`
- `idx:geo:US`
- `idx:device:iOS`
- `idx:segment:camping_high`

## Retrieval Flow

The hot path stays intentionally small:

1. The API fetches `user:<id>` from Redis.
2. Candidate generation intersects a small number of exact-match index sets such as:
   - `idx:geo:US`
   - `idx:device:iOS`
   - `idx:segment:camping_high`
3. Candidate campaign hashes are fetched in one Redis pipeline batch.
4. Candidates are reranked in memory with an interpretable linear model:
   - weighted interest affinity
   - bid contribution
   - freshness adjustment
   - light frequency penalty

The retrieval path avoids scanning the full campaign universe in the request hot path.

The lookup ladder is intentionally progressive:

1. `geo + device + strongest segments`
2. `geo + device + strongest single segment`
3. broader fallback intersections
4. finally `geo + device`

That preserves recall without turning the request path into a full query engine.

## Synthetic Dataset Design

The synthetic dataset is the primary dataset for this demo because it gives a controlled environment for both latency and ranking quality.

It includes:

- `4000` users by default
- `2500` campaigns by default
- `120000` synthetic interactions by default
- `12` interest features by default

Each synthetic user has:

- coarse exact-match attributes such as `geo` and `device`
- continuous interest scores
- derived segment memberships such as `camping_high` or `travel_medium`

Each synthetic campaign has:

- hard filters for `geo` and `device`
- required segments
- optional positive and negative segment criteria
- linear reranking weights
- bid and freshness attributes

The label generation process is also explicit:

- eligibility is enforced first
- a latent truth score combines user interests, campaign weights, bid, freshness, and deterministic noise
- click labels come from the resulting click probability

That is why the synthetic metrics are strong: the dataset is designed to validate the retrieval-plus-rerank architecture, not to act as an adversarial public benchmark.

## Repository Layout

- `app/`: FastAPI service, Redis repository, ranking logic, instrumentation
- `data/`: synthetic generator, Redis loader, Hugging Face dataset adapters for MIND and FairJob
- `experiments/`: offline evaluation and benchmark report generation
- `loadtest/`: configurable async load driver
- `observability/`: Prometheus, Grafana, and OTel collector configs
- `notebooks/`: walkthrough notebooks
- `tests/`: unit coverage for candidate generation, reranking, and metrics

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

The `app` container will auto-generate a bounded synthetic dataset and load Redis on startup.

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

## Synthetic Dataset

Generate a local dataset explicitly:

```bash
python3 -m data.synthetic \
  --output data/generated/synthetic \
  --num-users 4000 \
  --num-campaigns 2500 \
  --num-interactions 120000 \
  --feature-count 12
```

Files produced:

- `users.jsonl`
- `campaigns.jsonl`
- `interactions.parquet`
- `metadata.json`

Load Redis manually:

```bash
python3 -m data.load_redis --dataset-dir data/generated/synthetic
```

The generated files are intentionally simple:

- `users.jsonl`
  request-time user profiles
- `campaigns.jsonl`
  campaign metadata and targeting criteria
- `interactions.parquet`
  offline evaluation table
- `metadata.json`
  dataset size and generator parameters

## Measured Results

The current synthetic benchmark snapshot is:

- offline quality:
  `NDCG@K 0.9946`, `Precision@K 0.9001`, `Recall@K 0.9606`, `F1@K 0.9113`
- candidate-generation recall:
  `1.0`
- Redis `HGETALL user`:
  `0.278 ms` average, `0.476 ms` p95, `0.945 ms` p99
- Redis candidate generation:
  `0.568 ms` average, `0.884 ms` p95, `4.128 ms` p99
- in-memory reranking:
  `0.023 ms` average, `0.046 ms` p95, `0.073 ms` p99

The app also records a full per-request timing breakdown internally, but the primary README latency story is now focused on Redis retrieval and reranking rather than HTTP/framework overhead.

## Retrieval Strategy Comparison

The repo now includes a direct comparison between two candidate-generation strategies on a richer synthetic dataset that includes:

- wildcard geo/device campaigns
- `any_of_segments`
- `none_of_segments`
- more multi-interest users

Strategies compared:

- `naive`
  intersects multiple top user-interest buckets together early
- `union_probe`
  probes strong user-interest buckets separately, merges candidates round-robin, and preserves more recall

Current comparison snapshot from [reports/retrieval_strategy_comparison.md](/Users/jeremy.plichta/work/mastercard-dsp/reports/retrieval_strategy_comparison.md):

- `naive`
  `NDCG@K 0.9368`, `Candidate generation recall 0.3443`
- `union_probe`
  `NDCG@K 0.966`, `Candidate generation recall 0.5344`
- delta
  `+0.1901` candidate-generation recall and `+0.0292` NDCG@K for `union_probe`

Run the comparison locally:

```bash
python3 -m experiments.compare_retrieval_strategies \
  --dataset-dir data/generated/synthetic_retrieval_compare \
  --output reports/generated/retrieval_strategy_comparison.json
```

## Ranking API

Example request:

```bash
curl -s http://localhost:8000/rank \
  -H 'content-type: application/json' \
  -d '{"user_id":"u00000","top_k":5,"max_candidates":50}' | jq
```

Response includes:

- raw candidate IDs
- reranked top results with component scores
- timing breakdown
- Redis round trip count

Batch scoring endpoint:

```bash
curl -s http://localhost:8000/batch-score \
  -H 'content-type: application/json' \
  -d '{"user_id":"u00000","candidate_ids":["c00001","c00002"]}' | jq
```

## Load Testing

Run a configurable load test:

```bash
python3 -m loadtest.run \
  --mode serial \
  --base-url http://localhost:8000 \
  --dataset-dir data/generated/synthetic \
  --duration-seconds 20 \
  --target-rps 25 \
  --concurrency 20
```

The driver uses a hot/cold user mix to expose locality and repeated-user behavior. It writes a JSON report to `reports/generated/loadtest.json`.

Recommended benchmarking modes:

- `--mode serial` for honest single-shard latency measurement
- `--mode concurrent` for application-layer concurrency testing

For this repo, `serial` is the primary benchmark mode because the local demo uses a single Redis instance and a single serving process by default.

## Ranking Evaluation

Run offline evaluation:

```bash
python3 -m experiments.evaluate \
  --dataset-dir data/generated/synthetic \
  --mind-output-dir data/generated/mind
```

Metrics reported:

- `NDCG@K`
- `Precision@K`
- `Recall@K`
- `F1@K`
- candidate-generation recall before reranking

Synthetic evaluation is the primary quality readout for the demo. The MIND and FairJob paths are useful secondary checks, but they are not the main performance story.

## Hugging Face MIND Translation

This is an auxiliary path, not the main benchmark.

The translated public dataset path uses `Recommenders/MIND` from Hugging Face.

Why this dataset:

- impression and click behavior are much closer to ad serving than rating-only datasets
- it preserves query-like recommendation sessions
- it is relatable to retrieval + rerank evaluation

Local constraints:

- the source dataset is materially larger than the synthetic path
- translation is sampled by default to keep the demo lightweight
- if article metadata is unavailable from the current loader schema, a deterministic topic bucketing fallback is used so the evaluation flow still runs locally

Export a sampled translation:

```bash
python3 -m data.huggingface_adapter \
  --output-dir data/generated/mind \
  --split train \
  --sample-size 1500
```

## FairJob Targeting Translation

This is an auxiliary path that demonstrates how ad-like public click data can be mapped into the same Redis retrieval abstraction.

The FairJob path uses `criteo/FairJob` from Hugging Face and maps it into the demo schema like this:

- `impression_id` becomes a request-scoped pseudo-user
- `product_id` becomes `campaign_id`
- user categorical and numeric fields become Redis segment buckets
- campaign targeting criteria are inferred from segment lift in historical click data
- `required_segments`, `any_of_segments`, and `none_of_segments` are enforced in the app after coarse retrieval

Export a FairJob-derived dataset:

```bash
python3 -m data.fairjob_adapter \
  --output-dir data/generated/fairjob \
  --max-impressions 12000 \
  --max-campaigns 3000 \
  --min-campaign-impressions 10 \
  --min-segment-support 8
```

Load it into Redis:

```bash
python3 -m data.load_redis \
  --dataset-dir data/generated/fairjob \
  --redis-url redis://localhost:6381/0
```

Run the app locally against the FairJob export:

```bash
REDIS_URL=redis://localhost:6381/0 \
DATASET_DIR=data/generated/fairjob \
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8011
```

Run a serial FairJob load test:

```bash
python3 -m loadtest.run \
  --mode serial \
  --base-url http://127.0.0.1:8011 \
  --dataset-dir data/generated/fairjob \
  --duration-seconds 10 \
  --target-rps 100
```

## Benchmark Report

Generate a combined markdown report:

```bash
python3 -m experiments.benchmark \
  --base-url http://localhost:8000 \
  --dataset-dir data/generated/synthetic
```

This writes `reports/benchmark_report.md`.

## Notebooks

Launch Jupyter:

```bash
make notebooks
```

Included notebooks:

- `01_synthetic_data.ipynb`
- `02_redis_key_schema.ipynb`
- `03_ranking_evaluation.ipynb`
- `04_mind_translation.ipynb`
- `05_fairjob_targeting.ipynb`
- `06_boolean_targeting_walkthrough.ipynb`

## Observability

Prometheus scrapes:

- app-level Prometheus metrics at `/metrics`
- OTel collector metrics export at `otel-collector:8889`

Grafana dashboard includes:

- request throughput
- median / p95 / p99 request latency
- candidate pool size
- Redis round trips
- candidate generation and rerank latency breakdown

## Extension Points

- Swap local Redis for Redis Enterprise by changing `REDIS_URL` and scaling the index footprint.
- Replace the linear reranker with a richer feature store or model server.
- Extend hard filters with additional set indexes for supply-side targeting constraints.
- Add richer public datasets or internal event logs through additional adapters in `data/`.

## Current Verification

- `python3 -m pytest`
- `python3 -m data.synthetic --output data/generated/synthetic --num-users 500 --num-campaigns 400 --num-interactions 5000 --feature-count 10`

The MIND adapter path is implemented and sampled, but its full download/runtime depends on local network and available time because the upstream dataset is large.
