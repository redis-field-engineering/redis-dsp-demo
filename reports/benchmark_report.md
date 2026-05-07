# Benchmark Report

## Retrieval Overview

Decision-path latency is measured as `identity_resolution_ms + profile_fetch_ms + candidate_generation_ms + campaign_fetch_ms + filtering_ms + rerank_ms`.
Every mode in this benchmark is invoked with an `identity_token`, so identity resolution is included in each row.
The metric excludes HTTP/framework overhead but includes profile fetch and reranking only when that mode actually performs them.

### Methodology

- **Workload shape:** serial requests at 12 RPS, concurrency = 1, 10s measured + 2s warmup per mode → **N ≈ 120 measured requests per mode**.
- **Sample sizing:** p99 from N = 120 is essentially the second-largest observation. Treat the p99 column as indicative; small absolute differences at p99 are within noise. p50 is the more reliable comparison point at this sample size.
- **Sampling:** the load driver picks a hot 20% of MAIDs on 70% of requests and a cold 80% of MAIDs on 30%, so cache locality is intentionally favorable.
- **Concurrency:** these numbers measure the serial latency floor of each path. They do **not** measure behavior under realistic concurrent bid traffic. A concurrent load test against the same cluster shape is out of scope for this report and is called out in the GCP scaling spec.
- **Redis cache:** the FastAPI service runs with `cache_campaigns_in_memory=False` so every campaign-metadata fetch goes through Redis. The avg / p99 numbers therefore include the actual round-trip cost of reading campaign hashes from `redis-server`.
- **Data-size columns:** `Small Test Data` is the logical payload size of the current 4K-MAID / 2.5K-campaign synthetic dataset for that method's required keyspaces. `Scaled-Up Data` is the corresponding logical footprint at 500 M MAIDs / 5 K active ads at the **100 K bid/s** tier (`fcap:` is the only bid-rate-sensitive keyspace; see `reports/full_scale_gcp_test_spec.md` §1.4 for the per-tier deltas). The two tiers in this column are within ~60 GB at production scale because the `maid → maid_hot` saving (~1 TB) is offset by the `aud:` precompute (~1 TB).

| Mode | Retrieval Shape | Small Test Data | Scaled-Up Data | Avg SINTER Ops | Avg Total Redis Round Trips | Decision Path P50 (ms) | Decision Path P99 (ms) |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `full_realtime` | full campaign materialization + live exact targeting + taxonomy_filter | 4.18 MB | 5.22 TB | 0 | 5 | 236.653 | 303.059 |
| `maid_bruteforce_sinter` | legacy 26-probe SINTER plan | 4.46 MB | 5.22 TB | 26 | 31 | 19.79 | 52.6 |
| `maid_tightened_sinter` | tightened pipelined SINTER plan | 4.46 MB | 5.22 TB | 3 | 6 | 7.254 | 29.691 |
| `precomputed_segment` | direct aud:{maid} + maid_hot | 4.62 MB | 5.28 TB | 0 | 6 | 4.537 | 18.18 |
| `hybrid_precompute_plus_realtime` | direct aud:{maid} + maid_hot + live gating | 4.62 MB | 5.28 TB | 0 | 6 | 4.691 | 42.455 |
| `hybrid_bitmap_gating` | direct aud:{maid} + maid_hot + bm:servable gate + live fcap hash check | 4.32 MB | 5.28 TB | 0 | 5 | 3.991 | 5.332 |
| `hybrid_bitmap_taxonomy` | bm:servable gate + live fcap + app-side taxonomy_filter on float scores | 4.32 MB | 5.28 TB | 0 | 5 | 3.723 | 9.916 |

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
- `hybrid_bitmap_taxonomy`
  Same retrieval and bitmap-gating path as `hybrid_bitmap_gating`, but additionally evaluates each surviving campaign's `taxonomy_filter` AND/OR/NOT expression against the MAID's float interest scores in app memory before reranking. This is the path that closes the gap between batch precompute and per-ad threshold-based targeting on continuous taxonomy scores.
- `full_realtime`
  Resolves the identity token, fetches the full MAID profile, materializes the entire campaign universe, filters everything live, and reranks the surviving set. It is the correctness baseline, not the preferred low-latency design.

## Synthetic Offline Mode Comparison
- users evaluated: 234

### Full Real-Time Mode
- NDCG@K: 0.9818
- Precision@K: 0.9421
- Recall@K: 0.5811
- F1@K: 0.6603
- Candidate Recall: 1.0
- Eligible Recall: 1.0
- Avg Candidates: 2500.0
- Avg Eligible: 12.3803

### Precomputed Segment Mode
- NDCG@K: 0.5584
- Precision@K: 0.5248
- Recall@K: 0.2992
- F1@K: 0.3477
- Candidate Recall: 1.0
- Eligible Recall: 1.0
- Eligible Set Jaccard vs Full: 0.5214
- Top Result Jaccard vs Full: 0.3995
- Avg Candidates: 29.9786
- Avg Eligible: 23.094

### Hybrid Precompute + Realtime Mode
- NDCG@K: 0.9818
- Precision@K: 0.9421
- Recall@K: 0.5811
- F1@K: 0.6603
- Candidate Recall: 1.0
- Eligible Recall: 1.0
- Eligible Set Jaccard vs Full: 1.0
- Top Result Jaccard vs Full: 1.0
- Avg Candidates: 29.9786
- Avg Eligible: 12.3803

### Hybrid Bitmap + Taxonomy Mode
- NDCG@K: 0.8751
- Precision@K: 0.8314
- Recall@K: 0.523
- F1@K: 0.5807
- Candidate Recall: 1.0
- Eligible Recall: 1.0
- Eligible Set Jaccard vs Full: 0.8451
- Top Result Jaccard vs Full: 0.8043
- Avg Candidates: 29.9786
- Avg Eligible: 14.5214

## Serial Live Load By Mode
### full_realtime
- handler avg / p95 / p99 ms: 237.906 / 287.37 / 311.347
- identity resolution avg / p95 / p99 ms: 0.453 / 0.56 / 0.561
- profile fetch avg / p95 / p99 ms: 0.367 / 0.438 / 0.44
- candidate generation avg / p95 / p99 ms: 22.392 / 23.699 / 43.551
- avg candidates / eligible: 2500 / 9.732
- avg redis round trips: 5

### maid_bruteforce_sinter
- decision-path p50 / p99 ms: 19.79 / 52.6
- validated candidate p50 / p99 ms: 18.814 / 49.568
- candidate generation avg / p95 / p99 ms: 14.404 / 16.346 / 26.009
- avg SINTER ops / mode redis round trips: 26 / 29
- avg candidates / eligible: 50 / 1.273

### maid_tightened_sinter
- decision-path p50 / p99 ms: 7.254 / 29.691
- validated candidate p50 / p99 ms: 6.222 / 23.606
- candidate generation avg / p95 / p99 ms: 1.499 / 1.644 / 1.802
- avg SINTER ops / mode redis round trips: 3 / 4
- avg candidates / eligible: 50 / 5.521

### precomputed_segment
- handler avg / p95 / p99 ms: 5.368 / 6.158 / 18.314
- identity resolution avg / p95 / p99 ms: 0.522 / 0.63 / 0.676
- profile fetch avg / p95 / p99 ms: 0.379 / 0.491 / 0.533
- candidate generation avg / p95 / p99 ms: 0.307 / 0.421 / 0.471
- decision-path p50 / p99 ms: 4.537 / 18.18
- validated candidate p50 / p99 ms: 3.468 / 16.415
- avg SINTER ops / mode redis round trips: 0 / 4
- avg candidates / eligible: 28.694 / 21.934
- avg redis round trips: 6

### hybrid_precompute_plus_realtime
- handler avg / p95 / p99 ms: 6.002 / 6.834 / 42.633
- identity resolution avg / p95 / p99 ms: 0.674 / 0.701 / 4.799
- profile fetch avg / p95 / p99 ms: 0.499 / 0.562 / 0.673
- candidate generation avg / p95 / p99 ms: 0.426 / 0.487 / 0.535
- decision-path p50 / p99 ms: 4.691 / 42.455
- validated candidate p50 / p99 ms: 3.531 / 24.616
- avg SINTER ops / mode redis round trips: 0 / 4
- avg candidates / eligible: 28.694 / 21.934
- avg redis round trips: 6

### hybrid_bitmap_gating
- handler avg / p95 / p99 ms: 4.21 / 5.283 / 5.517
- identity resolution avg / p95 / p99 ms: 0.546 / 0.659 / 0.704
- profile fetch avg / p95 / p99 ms: 0.41 / 0.532 / 0.594
- candidate generation avg / p95 / p99 ms: 0.525 / 0.609 / 0.685
- decision-path p50 / p99 ms: 3.991 / 5.332
- validated candidate p50 / p99 ms: 2.816 / 3.991
- avg SINTER ops / mode redis round trips: 0 / 3
- avg candidates / eligible: 25.298 / 21.942
- avg redis round trips: 5

### hybrid_bitmap_taxonomy
- handler avg / p95 / p99 ms: 4.455 / 4.934 / 10.106
- identity resolution avg / p95 / p99 ms: 0.504 / 0.61 / 0.649
- profile fetch avg / p95 / p99 ms: 0.382 / 0.528 / 0.578
- candidate generation avg / p95 / p99 ms: 0.464 / 0.593 / 0.616
- filtering avg / p95 / p99 ms: 0.07 / 0.114 / 0.131
- decision-path p50 / p99 ms: 3.723 / 9.916
- validated candidate p50 / p99 ms: 2.702 / 8.684
- avg SINTER ops / mode redis round trips: 0 / 3
- avg candidates / eligible: 25.298 / 11.248
- avg redis round trips: 5