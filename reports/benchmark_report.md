# Benchmark Report

## Scope

This report reflects the rebuilt MAID-style synthetic demo path now running on the local Docker stack:

- identity token resolution
- MAID profile lookup
- set-based candidate generation
- exact ad filtering for card tier, geo hierarchy, device hierarchy, pacing, and frequency
- in-memory reranking

The benchmark uses the current full synthetic dataset:

- `4000` MAIDs
- `2500` campaigns
- `120000` synthetic interactions

## Offline Quality

Source: [reports/generated/evaluation.json](/Users/jeremy.plichta/work/mastercard-dsp/reports/generated/evaluation.json)

- `NDCG@K`: `0.917`
- `Precision@K`: `0.9901`
- `Recall@K`: `0.2594`
- `F1@K`: `0.4007`
- `Candidate generation recall`: `0.4704`

Interpretation:

- the exact-filter MAID path still produces very high top-of-list precision
- recall is now meaningfully constrained by retrieval
- this is a more realistic outcome than the earlier near-perfect synthetic path

## Serial Live Load Test

Source: [reports/generated/loadtest.json](/Users/jeremy.plichta/work/mastercard-dsp/reports/generated/loadtest.json)

Method:

- serial request mode
- `15` target RPS
- `15` measured seconds
- `2` warmup seconds
- live HTTP calls to `POST /rank`
- request body uses `identity_token`

Results:

- requests: `232`
- success rate: `1.0`
- throughput: `15.45 RPS`
- client latency: `31.788 ms` avg, `88.044 ms` p95, `194.165 ms` p99
- server latency: `25.953 ms` avg, `77.929 ms` p95, `184.283 ms` p99
- handler latency: `23.486 ms` avg, `72.582 ms` p95, `174.359 ms` p99

## Phase Timing Breakdown

Sampled from live `/rank` responses over `150` serial requests:

- identity + MAID fetch: `2.957 ms` avg, `3.298 ms` p95, `24.525 ms` p99
- candidate generation: `15.276 ms` avg, `36.878 ms` p95, `108.828 ms` p99
- campaign materialization: `0.029 ms` avg, `0.09 ms` p95, `0.225 ms` p99
- reranking: `0.065 ms` avg, `0.12 ms` p95, `0.335 ms` p99
- total handler time: `18.526 ms` avg, `48.383 ms` p95, `164.494 ms` p99
- Redis round trips: `28` avg, `28` p95, `28` p99

## Conclusion

What works well:

- identity-token resolution is functional and fast enough not to dominate the request
- campaign materialization is effectively free because campaign metadata is cached in memory
- reranking is negligible
- the demo now looks much closer to the intended MAID/ad-cache retrieval problem

What is limiting the current system:

- candidate generation is now the dominant latency cost
- candidate recall is materially lower than the earlier synthetic story
- the current set-probing plan is still too expensive and too lossy for the stricter exact-filter model

The next optimization work should focus on:

1. reducing candidate-generation round trips
2. improving candidate recall before reranking
3. deciding which exact filters should move earlier into retrieval without exploding index cost
