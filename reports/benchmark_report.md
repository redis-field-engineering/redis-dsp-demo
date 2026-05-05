# Benchmark Report

## Retrieval Overview

Decision-path latency is measured as `identity_resolution_ms + profile_fetch_ms + candidate_generation_ms + campaign_fetch_ms + filtering_ms + rerank_ms`.
Every mode in this benchmark is invoked with an `identity_token`, so identity resolution is included in each row.
The metric excludes HTTP/framework overhead but includes profile fetch and reranking only when that mode actually performs them.

| Mode | Retrieval Shape | Avg SINTER Ops | Avg Redis Round Trips | Decision Path P50 (ms) | Decision Path P99 (ms) |
| --- | --- | ---: | ---: | ---: | ---: |
| `maid_bruteforce_sinter` | legacy 26-probe SINTER plan | 26 | 28 | 18.074 | 50.945 |
| `maid_tightened_sinter` | tightened pipelined SINTER plan | 3 | 3 | 4.299 | 5.379 |
| `precomputed_segment` | direct aud:{maid} + maid_hot | 0 | 3 | 2.687 | 4.484 |
| `hybrid_precompute_plus_realtime` | direct aud:{maid} + maid_hot + live gating | 0 | 3 | 2.733 | 4.812 |
| `hybrid_bitmap_gating` | direct aud:{maid} + maid_hot + bm:servable gate + live fcap hash check | 0 | 2 | 1.939 | 3.344 |

## Method Definitions

- `maid_bruteforce_sinter`
  Uses the original MAID retrieval planner. After identity resolution and full MAID fetch, it runs 26 sequential `SINTER` probes that aggressively explore combinations of card tier, geo, device, and strong user segments before fetching campaign state and filtering live.
- `maid_tightened_sinter`
  Uses the reduced MAID retrieval planner. After identity resolution and full MAID fetch, it issues a compact pipelined `SINTER` plan with only three probes: one per strong segment plus a strict base fallback.
- `precomputed_segment`
  Resolves the identity token, fetches a compact `maid_hot:{maid_id}` scoring profile, reads the precomputed `aud:{maid_id}` candidate list, then fetches campaign/state data plus a single per-MAID `fcap:{maid_id}` hash and reranks. It relies on batch-computed static targeting and only applies minimal live gating online.
- `hybrid_precompute_plus_realtime`
  Uses the same `maid_hot:{maid_id}` and `aud:{maid_id}` lookup path as precomputed mode, but preserves the live mutable gating stage for pacing, budget, and frequency before reranking. It still reads a single per-MAID `fcap:{maid_id}` hash online. This is the current production-shaped non-bitmap path.
- `hybrid_bitmap_gating`
  Uses `maid_hot:{maid_id}` and `aud:{maid_id}`, then applies a server-side bitmap gate against a single `bm:servable` bitmap before fetching campaign metadata. Frequency cap is still enforced live from the per-MAID `fcap:{maid_id}` hash before reranking.
- `full_realtime`
  Resolves the identity token, fetches the full MAID profile, materializes the entire campaign universe, filters everything live, and reranks the surviving set. It is the correctness baseline, not the preferred low-latency design.

## Synthetic Offline Mode Comparison
- users evaluated: 250

### Full Real-Time Mode
- NDCG@K: 0.9813
- Precision@K: 0.9984
- Recall@K: 0.2668
- F1@K: 0.4093
- Candidate Recall: 1.0
- Eligible Recall: 1.0
- Avg Candidates: 2500.0
- Avg Eligible: 24.468

### Precomputed Segment Mode
- NDCG@K: 0.9813
- Precision@K: 0.9984
- Recall@K: 0.2668
- F1@K: 0.4093
- Candidate Recall: 1.0
- Eligible Recall: 1.0
- Eligible Set Jaccard vs Full: 1.0
- Top Result Jaccard vs Full: 1.0
- Avg Candidates: 30.168
- Avg Eligible: 24.468

### Hybrid Precompute + Realtime Mode
- NDCG@K: 0.9813
- Precision@K: 0.9984
- Recall@K: 0.2668
- F1@K: 0.4093
- Candidate Recall: 1.0
- Eligible Recall: 1.0
- Eligible Set Jaccard vs Full: 1.0
- Top Result Jaccard vs Full: 1.0
- Avg Candidates: 30.168
- Avg Eligible: 24.468

## Serial Live Load By Mode
### full_realtime
- handler avg / p95 / p99 ms: 53.479 / 112.5 / 116.398
- identity resolution avg / p95 / p99 ms: 0.717 / 0.669 / 9.375
- profile fetch avg / p95 / p99 ms: 0.49 / 0.523 / 1.374
- candidate generation avg / p95 / p99 ms: 0.145 / 0.189 / 0.215
- avg candidates / eligible: 2500 / 24.567
- avg redis round trips: 4

### maid_bruteforce_sinter
- decision-path p50 / p99 ms: 18.074 / 50.945
- validated candidate p50 / p99 ms: 16.985 / 37.519
- candidate generation avg / p95 / p99 ms: 15.359 / 16.979 / 18.514
- avg SINTER ops / mode redis round trips: 26 / 28
- avg candidates / eligible: 50 / 2.893

### maid_tightened_sinter
- decision-path p50 / p99 ms: 4.299 / 5.379
- validated candidate p50 / p99 ms: 3.162 / 3.858
- candidate generation avg / p95 / p99 ms: 1.459 / 1.598 / 1.714
- avg SINTER ops / mode redis round trips: 3 / 3
- avg candidates / eligible: 50 / 12.57

### precomputed_segment
- handler avg / p95 / p99 ms: 3.393 / 3.534 / 4.564
- identity resolution avg / p95 / p99 ms: 0.507 / 0.594 / 0.699
- profile fetch avg / p95 / p99 ms: 0.386 / 0.491 / 0.536
- candidate generation avg / p95 / p99 ms: 0.308 / 0.405 / 0.463
- decision-path p50 / p99 ms: 2.687 / 4.484
- validated candidate p50 / p99 ms: 1.522 / 2.324
- avg SINTER ops / mode redis round trips: 0 / 3
- avg candidates / eligible: 30.083 / 24.529
- avg redis round trips: 5

### hybrid_precompute_plus_realtime
- handler avg / p95 / p99 ms: 3.0 / 3.617 / 4.899
- identity resolution avg / p95 / p99 ms: 0.598 / 0.631 / 0.717
- profile fetch avg / p95 / p99 ms: 0.478 / 0.529 / 0.606
- candidate generation avg / p95 / p99 ms: 0.329 / 0.466 / 0.511
- decision-path p50 / p99 ms: 2.733 / 4.812
- validated candidate p50 / p99 ms: 1.596 / 2.146
- avg SINTER ops / mode redis round trips: 0 / 3
- avg candidates / eligible: 30.083 / 24.529
- avg redis round trips: 5

### hybrid_bitmap_gating
- handler avg / p95 / p99 ms: 2.047 / 2.591 / 3.399
- identity resolution avg / p95 / p99 ms: 0.505 / 0.67 / 0.932
- profile fetch avg / p95 / p99 ms: 0.379 / 0.49 / 0.529
- candidate generation avg / p95 / p99 ms: 0.457 / 0.56 / 0.593
- decision-path p50 / p99 ms: 1.939 / 3.344
- validated candidate p50 / p99 ms: 0.831 / 1.095
- avg SINTER ops / mode redis round trips: 0 / 2
- avg candidates / eligible: 26.388 / 24.57
- avg redis round trips: 4