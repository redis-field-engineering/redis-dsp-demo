# Retrieval Strategy Comparison

This compares two SINTER-based candidate-generation strategies on the standard synthetic dataset (same generator and seed as the main benchmark — the comparison is about the strategy, not the data). The dataset already contains wildcard geo/device campaigns, `any_of_segments`, `none_of_segments`, and per-user multi-segment profiles.

Strategies compared:

- `naive`
  intersects multiple top user-interest buckets together early
- `union_probe`
  probes strong user-interest buckets separately, merges candidates round-robin, and preserves more recall

## Results

### Naive

- Users evaluated: `250`
- `NDCG@K`: `0.9368`
- `Precision@K`: `1.0`
- `Recall@K`: `0.0782`
- `F1@K`: `0.1433`
- Candidate generation recall: `0.3443`

### Union Probe

- Users evaluated: `250`
- `NDCG@K`: `0.966`
- `Precision@K`: `1.0`
- `Recall@K`: `0.0782`
- `F1@K`: `0.1433`
- Candidate generation recall: `0.5344`

### Delta

- Candidate generation recall: `+0.1901`
- `NDCG@K`: `+0.0292`
- `Precision@K`: `0.0`
- `Recall@K`: `0.0`
- `F1@K`: `0.0`

## Interpretation

The `union_probe` strategy keeps substantially more relevant campaigns alive than the naive planner. The top-`K` precision and recall did not move in this run, but candidate-generation recall improved materially and `NDCG@K` improved modestly.

That means the retrieval change is real and measurable on a dataset that actually contains campaign shapes where early segment intersection is too aggressive.
