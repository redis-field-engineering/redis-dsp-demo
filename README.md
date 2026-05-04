# Redis DSP Candidate Generation Demo

This repository is a production-shaped local prototype for a Redis-based DSP retrieval and reranking workflow. It demonstrates:

- Redis Set-based candidate generation with explicit inverted indexes
- Lightweight in-memory reranking in Python
- Offline ranking evaluation on synthetic data, a translated Hugging Face `MIND` path, and a derived `FairJob` targeting path
- Configurable load testing with average, p95, and p99 latency reporting
- Local observability with Prometheus, Grafana, and OpenTelemetry

## Architecture

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

Synthetic evaluation uses the repo’s noisy latent click model. The MIND path translates recommendation impressions into the same abstract user/campaign/label domain. The FairJob path derives Redis-friendly targeting criteria from historical response lift and evaluates retrieval plus reranking against observed impression slates.

## Hugging Face MIND Translation

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
