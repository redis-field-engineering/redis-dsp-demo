# Benchmark Report

## Retrieval Overview

Decision-path latency is measured as `identity_resolution_ms + profile_fetch_ms + candidate_generation_ms + campaign_fetch_ms + filtering_ms + rerank_ms`.
Every mode in this benchmark is invoked with an `identity_token`, so identity resolution is included in each row.
The metric excludes HTTP/framework overhead but includes profile fetch and reranking only when that mode actually performs them.

| Mode | Retrieval Shape | Avg SINTER Ops | Avg Redis Round Trips | Decision Path P50 (ms) | Decision Path P99 (ms) |
| --- | --- | ---: | ---: | ---: | ---: |
| `maid_bruteforce_sinter` | legacy 26-probe SINTER plan | 26 | 28 | 28.701 | 124.332 |
| `maid_tightened_sinter` | tightened pipelined SINTER plan | 3 | 3 | 5.102 | 31.929 |
| `precomputed_segment` | direct aud:{maid} + maid_hot | 0 | 3 | 4.566 | 23.535 |
| `hybrid_precompute_plus_realtime` | direct aud:{maid} + maid_hot + live gating | 0 | 3 | 4.133 | 43.152 |
| `hybrid_bitmap_gating` | direct aud:{maid} + maid_hot + bm:servable gate + live fcap hash check | 0 | 2 | 2.9 | 14.899 |

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
- handler avg / p95 / p99 ms: 40.029 / 79.84 / 140.11
- identity resolution avg / p95 / p99 ms: 2.346 / 3.355 / 56.064
- profile fetch avg / p95 / p99 ms: 1.387 / 3.508 / 16.624
- candidate generation avg / p95 / p99 ms: 0.094 / 0.217 / 0.27
- avg candidates / eligible: 2500 / 24.579
- avg redis round trips: 4

### maid_bruteforce_sinter
- decision-path p50 / p99 ms: 28.701 / 124.332
- validated candidate p50 / p99 ms: 25.536 / 120.943
- candidate generation avg / p95 / p99 ms: 32.834 / 74.259 / 105.826
- avg SINTER ops / mode redis round trips: 26 / 28
- avg candidates / eligible: 50 / 2.893

### maid_tightened_sinter
- decision-path p50 / p99 ms: 5.102 / 31.929
- validated candidate p50 / p99 ms: 3.297 / 21.52
- candidate generation avg / p95 / p99 ms: 1.305 / 3.236 / 6.405
- avg SINTER ops / mode redis round trips: 3 / 3
- avg candidates / eligible: 50 / 12.57

### precomputed_segment
- handler avg / p95 / p99 ms: 5.643 / 12.778 / 23.662
- identity resolution avg / p95 / p99 ms: 1.151 / 3.014 / 5.226
- profile fetch avg / p95 / p99 ms: 1.011 / 3.042 / 3.99
- candidate generation avg / p95 / p99 ms: 0.819 / 2.025 / 3.714
- decision-path p50 / p99 ms: 4.566 / 23.535
- validated candidate p50 / p99 ms: 2.629 / 14.343
- avg SINTER ops / mode redis round trips: 0 / 3
- avg candidates / eligible: 30.083 / 24.529
- avg redis round trips: 5

### hybrid_precompute_plus_realtime
- handler avg / p95 / p99 ms: 6.022 / 15.956 / 43.229
- identity resolution avg / p95 / p99 ms: 1.125 / 2.717 / 5.98
- profile fetch avg / p95 / p99 ms: 0.86 / 2.573 / 3.03
- candidate generation avg / p95 / p99 ms: 0.87 / 2.552 / 6.509
- decision-path p50 / p99 ms: 4.133 / 43.152
- validated candidate p50 / p99 ms: 2.294 / 18.838
- avg SINTER ops / mode redis round trips: 0 / 3
- avg candidates / eligible: 30.083 / 24.529
- avg redis round trips: 5

### hybrid_bitmap_gating
- handler avg / p95 / p99 ms: 3.66 / 10.458 / 14.939
- identity resolution avg / p95 / p99 ms: 1.09 / 2.073 / 4.132
- profile fetch avg / p95 / p99 ms: 0.795 / 2.281 / 3.801
- candidate generation avg / p95 / p99 ms: 0.758 / 1.953 / 3.252
- decision-path p50 / p99 ms: 2.9 / 14.899
- validated candidate p50 / p99 ms: 1.066 / 6.248
- avg SINTER ops / mode redis round trips: 0 / 2
- avg candidates / eligible: 26.388 / 24.57
- avg redis round trips: 4