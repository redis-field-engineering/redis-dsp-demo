# Benchmark Report

## Synthetic Offline Metrics
These metrics come from the repo-generated synthetic dataset and measure the full candidate-generation plus reranking pipeline offline.
- NDCG@K: 0.9946
- Precision@K: 0.9001
- Recall@K: 0.9606
- F1@K: 0.9113
- Candidate Recall: 1.0

## MIND Translation Metrics
These metrics come from the translated Hugging Face `Recommenders/MIND` sample and provide a more realistic, weaker baseline than the synthetic generator.
- NDCG@K: 0.2618
- Precision@K: 0.1138
- Recall@K: 0.3631
- F1@K: 0.1656

## FairJob Translation Metrics
These metrics come from the derived `criteo/FairJob` path. The adapter infers targeting buckets from historical click lift, then evaluates reranking against observed impression slates. The conservative numbers below use `displayrandom = 1` rows to reduce position bias.
- NDCG@K: 0.0938
- Precision@K: 0.0938
- Recall@K: 0.0938
- F1@K: 0.0938
- Candidate Recall: 0.125
- Displayed Candidate Coverage: 0.1377

## Serial Load Test
This is the current single-shard serial measurement against the running local container app. It is the most representative non-concurrent latency snapshot for the prototype.
- Requests: 1001
- Success Rate: 1.0
- Throughput RPS: 100.05
- Client Avg Latency ms: 4.502
- Client p95 Latency ms: 6.951
- Client p99 Latency ms: 14.295
- Server Avg Latency ms: 2.465
- Server p95 Latency ms: 3.621
- Server p99 Latency ms: 7.732
- Handler Avg Latency ms: 1.755
- Handler p95 Latency ms: 2.425
- Handler p99 Latency ms: 5.875

## FairJob App Serial Load Test
This is the same serial benchmark shape run against a local app instance backed by the FairJob-derived dataset on `http://127.0.0.1:8011`.
- Requests: 1001
- Success Rate: 1.0
- Throughput RPS: 100.03
- Client Avg Latency ms: 6.328
- Client p95 Latency ms: 13.856
- Client p99 Latency ms: 25.516
- Server Avg Latency ms: 4.741
- Server p95 Latency ms: 11.015
- Server p99 Latency ms: 22.413
- Handler Avg Latency ms: 3.919
- Handler p95 Latency ms: 8.758
- Handler p99 Latency ms: 21.193
