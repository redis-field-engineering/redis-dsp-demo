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

- `NDCG@K`: `0.978`
- `Precision@K`: `0.9984`
- `Recall@K`: `0.2668`
- `F1@K`: `0.4093`
- `Candidate generation recall`: `0.9372`

Interpretation:

- the exact-filter MAID path still produces very high top-of-list precision
- candidate recall is high again after reducing the probe plan and adding state-aware retrieval
- the remaining recall gap is mostly top-K truncation, not candidate-domain loss

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

- requests: `226`
- success rate: `1.0`
- throughput: `15.06 RPS`
- client latency: `21.789 ms` avg, `59.984 ms` p95, `211.566 ms` p99
- server latency: `12.201 ms` avg, `40.602 ms` p95, `92.058 ms` p99
- handler latency: `7.903 ms` avg, `24.846 ms` p95, `52.034 ms` p99

## Phase Timing Breakdown

Sampled from live `/rank` responses over `150` serial requests:

- identity + MAID fetch: `2.241 ms` avg, `5.997 ms` p95, `12.307 ms` p99
- candidate generation: `2.327 ms` avg, `5.955 ms` p95, `13.021 ms` p99
- campaign materialization: `0.393 ms` avg, `0.855 ms` p95, `7.697 ms` p99
- reranking: `0.263 ms` avg, `0.805 ms` p95, `2.227 ms` p99
- total handler time: `6.358 ms` avg, `20.229 ms` p95, `41.678 ms` p99
- Redis round trips: `3` avg, `3` p95, `3` p99

## Conclusion

What works well:

- identity-token resolution is functional and fast enough not to dominate the request
- candidate generation is now low-single-digit milliseconds on average
- campaign materialization is effectively free because campaign metadata is cached in memory
- reranking is negligible
- the demo now looks much closer to the intended MAID/ad-cache retrieval problem

What is limiting the current system:

- the handler average is now under `10 ms`, but the tail is still above that target
- remaining p95/p99 latency is mostly request-level variability, not candidate-generation cost
- the end-to-end HTTP p95/p99 still includes framework and container overhead on top of the handler

The next optimization work should focus on:

1. reducing MAID fetch and deserialization cost
2. deciding whether frequency state should move to a separate hot-path structure
3. evaluating a server-side Redis Function to collapse identity resolution and candidate retrieval into one execution
