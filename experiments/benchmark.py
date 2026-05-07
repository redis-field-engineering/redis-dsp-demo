from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.models import (
    FULL_REALTIME_MODE,
    HYBRID_MODE,
    HYBRID_BITMAP_MODE,
    HYBRID_BITMAP_TAXONOMY_MODE,
    MAID_BRUTEFORCE_SINTER_MODE,
    MAID_TIGHTENED_SINTER_MODE,
    PRECOMPUTED_SEGMENT_MODE,
)
from data.size_estimates import human_bytes, method_keyspace_bytes
from experiments.evaluate import evaluate_synthetic_modes
from loadtest.run import run_load_test
from data.common import read_jsonl


def build_report(
    synthetic_mode_results: dict[str, object],
    loadtest_results: dict[str, dict[str, float | int]],
    method_sizes: dict[str, object],
) -> str:
    modes = synthetic_mode_results["modes"]
    retrieval_overview_rows = [
        (
            FULL_REALTIME_MODE,
            "full campaign materialization + live exact targeting + taxonomy_filter",
            loadtest_results[FULL_REALTIME_MODE],
        ),
        (
            MAID_BRUTEFORCE_SINTER_MODE,
            "legacy 26-probe SINTER plan",
            loadtest_results[MAID_BRUTEFORCE_SINTER_MODE],
        ),
        (
            MAID_TIGHTENED_SINTER_MODE,
            "tightened pipelined SINTER plan",
            loadtest_results[MAID_TIGHTENED_SINTER_MODE],
        ),
        (
            PRECOMPUTED_SEGMENT_MODE,
            "direct aud:{maid} + maid_hot",
            loadtest_results[PRECOMPUTED_SEGMENT_MODE],
        ),
        (
            HYBRID_MODE,
            "direct aud:{maid} + maid_hot + live gating",
            loadtest_results[HYBRID_MODE],
        ),
        (
            HYBRID_BITMAP_MODE,
            "direct aud:{maid} + maid_hot + bm:servable gate + live fcap hash check",
            loadtest_results[HYBRID_BITMAP_MODE],
        ),
        (
            HYBRID_BITMAP_TAXONOMY_MODE,
            "bm:servable gate + live fcap + app-side taxonomy_filter on float scores",
            loadtest_results[HYBRID_BITMAP_TAXONOMY_MODE],
        ),
    ]
    overview_table = [
        "## Retrieval Overview",
        "",
        "Decision-path latency is measured as `identity_resolution_ms + profile_fetch_ms + candidate_generation_ms + campaign_fetch_ms + filtering_ms + rerank_ms`.",
        "Every mode in this benchmark is invoked with an `identity_token`, so identity resolution is included in each row.",
        "The metric excludes HTTP/framework overhead but includes profile fetch and reranking only when that mode actually performs them.",
        "",
        "### Methodology",
        "",
        "- **Workload shape:** serial requests at 12 RPS, concurrency = 1, 10s measured + 2s warmup per mode → **N ≈ 120 measured requests per mode**.",
        "- **Sample sizing:** p99 from N = 120 is essentially the second-largest observation. Treat the p99 column as indicative; small absolute differences at p99 are within noise. p50 is the more reliable comparison point at this sample size.",
        "- **Sampling:** the load driver picks a hot 20% of MAIDs on 70% of requests and a cold 80% of MAIDs on 30%, so cache locality is intentionally favorable.",
        "- **Concurrency:** these numbers measure the serial latency floor of each path. They do **not** measure behavior under realistic concurrent bid traffic. A concurrent load test against the same cluster shape is out of scope for this report and is called out in the GCP scaling spec.",
        "- **Redis cache:** the FastAPI service runs with `cache_campaigns_in_memory=False` so every campaign-metadata fetch goes through Redis. The avg / p99 numbers therefore include the actual round-trip cost of reading campaign hashes from `redis-server`.",
        "- **Data-size columns:** `Small Test Data` is the logical payload size of the current 4K-MAID / 2.5K-campaign synthetic dataset for that method's required keyspaces. `Scaled-Up Data` is the corresponding logical footprint at 500 M MAIDs / 5 K active ads, using the assumptions in `reports/full_scale_gcp_test_spec.md`.",
        "",
        "| Mode | Retrieval Shape | Small Test Data | Scaled-Up Data | Avg SINTER Ops | Avg Total Redis Round Trips | Decision Path P50 (ms) | Decision Path P99 (ms) |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for mode_name, retrieval_shape, stats in retrieval_overview_rows:
        footprint = method_sizes[mode_name]
        overview_table.append(
            f"| `{mode_name}` | {retrieval_shape} | {human_bytes(footprint.small_scale_bytes)} | {human_bytes(footprint.scaled_up_bytes)} | {stats['avg_sinter_ops']} | {stats['avg_redis_round_trips']} | {stats['decision_path_p50_latency_ms']} | {stats['decision_path_p99_latency_ms']} |"
        )
    method_definitions = [
        "## Method Definitions",
        "",
        f"- `{MAID_BRUTEFORCE_SINTER_MODE}`",
        "  Uses the original MAID retrieval planner. After identity resolution and full MAID fetch, it runs 26 sequential `SINTER` probes that aggressively explore combinations of card tier, geo, device, and strong user segments before fetching campaign state and filtering live.",
        f"- `{MAID_TIGHTENED_SINTER_MODE}`",
        "  Uses the reduced MAID retrieval planner. After identity resolution and full MAID fetch, it issues a compact pipelined `SINTER` plan with only three probes: one per strong segment plus a strict base fallback.",
        f"- `{PRECOMPUTED_SEGMENT_MODE}`",
        "  Resolves the identity token, fetches a compact `maid_hot:{maid_id}` scoring profile, reads the precomputed `aud:{maid_id}` candidate list, then fetches campaign/state data plus a single per-MAID `fcap:{maid_id}` hash and reranks. It relies on batch-computed static targeting and only applies minimal live gating online.",
        f"- `{HYBRID_MODE}`",
        "  Uses the same `maid_hot:{maid_id}` and `aud:{maid_id}` lookup path as precomputed mode, but preserves the live mutable gating stage for pacing, budget, and frequency before reranking. It still reads a single per-MAID `fcap:{maid_id}` hash online. This is the current production-shaped non-bitmap path.",
        f"- `{HYBRID_BITMAP_MODE}`",
        "  Uses `maid_hot:{maid_id}` and `aud:{maid_id}`, then applies a server-side bitmap gate against a single `bm:servable` bitmap before fetching campaign metadata. Frequency cap is still enforced live from the per-MAID `fcap:{maid_id}` hash before reranking.",
        f"- `{HYBRID_BITMAP_TAXONOMY_MODE}`",
        "  Same retrieval and bitmap-gating path as `hybrid_bitmap_gating`, but additionally evaluates each surviving campaign's `taxonomy_filter` AND/OR/NOT expression against the MAID's float interest scores in app memory before reranking. This is the path that closes the gap between batch precompute and per-ad threshold-based targeting on continuous taxonomy scores.",
        f"- `{FULL_REALTIME_MODE}`",
        "  Resolves the identity token, fetches the full MAID profile, materializes the entire campaign universe, filters everything live, and reranks the surviving set. It is the correctness baseline, not the preferred low-latency design.",
    ]
    return "\n".join(
        [
            "# Benchmark Report",
            "",
            *overview_table,
            "",
            *method_definitions,
            "",
            "## Synthetic Offline Mode Comparison",
            f"- users evaluated: {synthetic_mode_results['users_evaluated']}",
            "",
            "### Full Real-Time Mode",
            f"- NDCG@K: {modes[FULL_REALTIME_MODE]['ndcg_at_k']}",
            f"- Precision@K: {modes[FULL_REALTIME_MODE]['precision_at_k']}",
            f"- Recall@K: {modes[FULL_REALTIME_MODE]['recall_at_k']}",
            f"- F1@K: {modes[FULL_REALTIME_MODE]['f1_at_k']}",
            f"- Candidate Recall: {modes[FULL_REALTIME_MODE]['candidate_generation_recall']}",
            f"- Eligible Recall: {modes[FULL_REALTIME_MODE]['eligible_recall']}",
            f"- Avg Candidates: {modes[FULL_REALTIME_MODE]['candidate_count']}",
            f"- Avg Eligible: {modes[FULL_REALTIME_MODE]['eligible_count']}",
            "",
            "### Precomputed Segment Mode",
            f"- NDCG@K: {modes[PRECOMPUTED_SEGMENT_MODE]['ndcg_at_k']}",
            f"- Precision@K: {modes[PRECOMPUTED_SEGMENT_MODE]['precision_at_k']}",
            f"- Recall@K: {modes[PRECOMPUTED_SEGMENT_MODE]['recall_at_k']}",
            f"- F1@K: {modes[PRECOMPUTED_SEGMENT_MODE]['f1_at_k']}",
            f"- Candidate Recall: {modes[PRECOMPUTED_SEGMENT_MODE]['candidate_generation_recall']}",
            f"- Eligible Recall: {modes[PRECOMPUTED_SEGMENT_MODE]['eligible_recall']}",
            f"- Eligible Set Jaccard vs Full: {modes[PRECOMPUTED_SEGMENT_MODE]['eligible_set_jaccard_vs_full_realtime']}",
            f"- Top Result Jaccard vs Full: {modes[PRECOMPUTED_SEGMENT_MODE]['top_result_jaccard_vs_full_realtime']}",
            f"- Avg Candidates: {modes[PRECOMPUTED_SEGMENT_MODE]['candidate_count']}",
            f"- Avg Eligible: {modes[PRECOMPUTED_SEGMENT_MODE]['eligible_count']}",
            "",
            "### Hybrid Precompute + Realtime Mode",
            f"- NDCG@K: {modes[HYBRID_MODE]['ndcg_at_k']}",
            f"- Precision@K: {modes[HYBRID_MODE]['precision_at_k']}",
            f"- Recall@K: {modes[HYBRID_MODE]['recall_at_k']}",
            f"- F1@K: {modes[HYBRID_MODE]['f1_at_k']}",
            f"- Candidate Recall: {modes[HYBRID_MODE]['candidate_generation_recall']}",
            f"- Eligible Recall: {modes[HYBRID_MODE]['eligible_recall']}",
            f"- Eligible Set Jaccard vs Full: {modes[HYBRID_MODE]['eligible_set_jaccard_vs_full_realtime']}",
            f"- Top Result Jaccard vs Full: {modes[HYBRID_MODE]['top_result_jaccard_vs_full_realtime']}",
            f"- Avg Candidates: {modes[HYBRID_MODE]['candidate_count']}",
            f"- Avg Eligible: {modes[HYBRID_MODE]['eligible_count']}",
            "",
            "### Hybrid Bitmap + Taxonomy Mode",
            f"- NDCG@K: {modes[HYBRID_BITMAP_TAXONOMY_MODE]['ndcg_at_k']}",
            f"- Precision@K: {modes[HYBRID_BITMAP_TAXONOMY_MODE]['precision_at_k']}",
            f"- Recall@K: {modes[HYBRID_BITMAP_TAXONOMY_MODE]['recall_at_k']}",
            f"- F1@K: {modes[HYBRID_BITMAP_TAXONOMY_MODE]['f1_at_k']}",
            f"- Candidate Recall: {modes[HYBRID_BITMAP_TAXONOMY_MODE]['candidate_generation_recall']}",
            f"- Eligible Recall: {modes[HYBRID_BITMAP_TAXONOMY_MODE]['eligible_recall']}",
            f"- Eligible Set Jaccard vs Full: {modes[HYBRID_BITMAP_TAXONOMY_MODE]['eligible_set_jaccard_vs_full_realtime']}",
            f"- Top Result Jaccard vs Full: {modes[HYBRID_BITMAP_TAXONOMY_MODE]['top_result_jaccard_vs_full_realtime']}",
            f"- Avg Candidates: {modes[HYBRID_BITMAP_TAXONOMY_MODE]['candidate_count']}",
            f"- Avg Eligible: {modes[HYBRID_BITMAP_TAXONOMY_MODE]['eligible_count']}",
            "",
            "## Serial Live Load By Mode",
            f"### {FULL_REALTIME_MODE}",
            f"- handler avg / p95 / p99 ms: {loadtest_results[FULL_REALTIME_MODE]['handler_avg_latency_ms']} / {loadtest_results[FULL_REALTIME_MODE]['handler_p95_latency_ms']} / {loadtest_results[FULL_REALTIME_MODE]['handler_p99_latency_ms']}",
            f"- identity resolution avg / p95 / p99 ms: {loadtest_results[FULL_REALTIME_MODE]['identity_resolution_avg_latency_ms']} / {loadtest_results[FULL_REALTIME_MODE]['identity_resolution_p95_latency_ms']} / {loadtest_results[FULL_REALTIME_MODE]['identity_resolution_p99_latency_ms']}",
            f"- profile fetch avg / p95 / p99 ms: {loadtest_results[FULL_REALTIME_MODE]['profile_fetch_avg_latency_ms']} / {loadtest_results[FULL_REALTIME_MODE]['profile_fetch_p95_latency_ms']} / {loadtest_results[FULL_REALTIME_MODE]['profile_fetch_p99_latency_ms']}",
            f"- candidate generation avg / p95 / p99 ms: {loadtest_results[FULL_REALTIME_MODE]['candidate_generation_avg_latency_ms']} / {loadtest_results[FULL_REALTIME_MODE]['candidate_generation_p95_latency_ms']} / {loadtest_results[FULL_REALTIME_MODE]['candidate_generation_p99_latency_ms']}",
            f"- avg candidates / eligible: {loadtest_results[FULL_REALTIME_MODE]['avg_candidate_count']} / {loadtest_results[FULL_REALTIME_MODE]['avg_eligible_count']}",
            f"- avg redis round trips: {loadtest_results[FULL_REALTIME_MODE]['avg_redis_round_trips']}",
            "",
            f"### {MAID_BRUTEFORCE_SINTER_MODE}",
            f"- decision-path p50 / p99 ms: {loadtest_results[MAID_BRUTEFORCE_SINTER_MODE]['decision_path_p50_latency_ms']} / {loadtest_results[MAID_BRUTEFORCE_SINTER_MODE]['decision_path_p99_latency_ms']}",
            f"- validated candidate p50 / p99 ms: {loadtest_results[MAID_BRUTEFORCE_SINTER_MODE]['validated_candidate_p50_latency_ms']} / {loadtest_results[MAID_BRUTEFORCE_SINTER_MODE]['validated_candidate_p99_latency_ms']}",
            f"- candidate generation avg / p95 / p99 ms: {loadtest_results[MAID_BRUTEFORCE_SINTER_MODE]['candidate_generation_avg_latency_ms']} / {loadtest_results[MAID_BRUTEFORCE_SINTER_MODE]['candidate_generation_p95_latency_ms']} / {loadtest_results[MAID_BRUTEFORCE_SINTER_MODE]['candidate_generation_p99_latency_ms']}",
            f"- avg SINTER ops / mode redis round trips: {loadtest_results[MAID_BRUTEFORCE_SINTER_MODE]['avg_sinter_ops']} / {loadtest_results[MAID_BRUTEFORCE_SINTER_MODE]['avg_mode_redis_round_trips']}",
            f"- avg candidates / eligible: {loadtest_results[MAID_BRUTEFORCE_SINTER_MODE]['avg_candidate_count']} / {loadtest_results[MAID_BRUTEFORCE_SINTER_MODE]['avg_eligible_count']}",
            "",
            f"### {MAID_TIGHTENED_SINTER_MODE}",
            f"- decision-path p50 / p99 ms: {loadtest_results[MAID_TIGHTENED_SINTER_MODE]['decision_path_p50_latency_ms']} / {loadtest_results[MAID_TIGHTENED_SINTER_MODE]['decision_path_p99_latency_ms']}",
            f"- validated candidate p50 / p99 ms: {loadtest_results[MAID_TIGHTENED_SINTER_MODE]['validated_candidate_p50_latency_ms']} / {loadtest_results[MAID_TIGHTENED_SINTER_MODE]['validated_candidate_p99_latency_ms']}",
            f"- candidate generation avg / p95 / p99 ms: {loadtest_results[MAID_TIGHTENED_SINTER_MODE]['candidate_generation_avg_latency_ms']} / {loadtest_results[MAID_TIGHTENED_SINTER_MODE]['candidate_generation_p95_latency_ms']} / {loadtest_results[MAID_TIGHTENED_SINTER_MODE]['candidate_generation_p99_latency_ms']}",
            f"- avg SINTER ops / mode redis round trips: {loadtest_results[MAID_TIGHTENED_SINTER_MODE]['avg_sinter_ops']} / {loadtest_results[MAID_TIGHTENED_SINTER_MODE]['avg_mode_redis_round_trips']}",
            f"- avg candidates / eligible: {loadtest_results[MAID_TIGHTENED_SINTER_MODE]['avg_candidate_count']} / {loadtest_results[MAID_TIGHTENED_SINTER_MODE]['avg_eligible_count']}",
            "",
            f"### {PRECOMPUTED_SEGMENT_MODE}",
            f"- handler avg / p95 / p99 ms: {loadtest_results[PRECOMPUTED_SEGMENT_MODE]['handler_avg_latency_ms']} / {loadtest_results[PRECOMPUTED_SEGMENT_MODE]['handler_p95_latency_ms']} / {loadtest_results[PRECOMPUTED_SEGMENT_MODE]['handler_p99_latency_ms']}",
            f"- identity resolution avg / p95 / p99 ms: {loadtest_results[PRECOMPUTED_SEGMENT_MODE]['identity_resolution_avg_latency_ms']} / {loadtest_results[PRECOMPUTED_SEGMENT_MODE]['identity_resolution_p95_latency_ms']} / {loadtest_results[PRECOMPUTED_SEGMENT_MODE]['identity_resolution_p99_latency_ms']}",
            f"- profile fetch avg / p95 / p99 ms: {loadtest_results[PRECOMPUTED_SEGMENT_MODE]['profile_fetch_avg_latency_ms']} / {loadtest_results[PRECOMPUTED_SEGMENT_MODE]['profile_fetch_p95_latency_ms']} / {loadtest_results[PRECOMPUTED_SEGMENT_MODE]['profile_fetch_p99_latency_ms']}",
            f"- candidate generation avg / p95 / p99 ms: {loadtest_results[PRECOMPUTED_SEGMENT_MODE]['candidate_generation_avg_latency_ms']} / {loadtest_results[PRECOMPUTED_SEGMENT_MODE]['candidate_generation_p95_latency_ms']} / {loadtest_results[PRECOMPUTED_SEGMENT_MODE]['candidate_generation_p99_latency_ms']}",
            f"- decision-path p50 / p99 ms: {loadtest_results[PRECOMPUTED_SEGMENT_MODE]['decision_path_p50_latency_ms']} / {loadtest_results[PRECOMPUTED_SEGMENT_MODE]['decision_path_p99_latency_ms']}",
            f"- validated candidate p50 / p99 ms: {loadtest_results[PRECOMPUTED_SEGMENT_MODE]['validated_candidate_p50_latency_ms']} / {loadtest_results[PRECOMPUTED_SEGMENT_MODE]['validated_candidate_p99_latency_ms']}",
            f"- avg SINTER ops / mode redis round trips: {loadtest_results[PRECOMPUTED_SEGMENT_MODE]['avg_sinter_ops']} / {loadtest_results[PRECOMPUTED_SEGMENT_MODE]['avg_mode_redis_round_trips']}",
            f"- avg candidates / eligible: {loadtest_results[PRECOMPUTED_SEGMENT_MODE]['avg_candidate_count']} / {loadtest_results[PRECOMPUTED_SEGMENT_MODE]['avg_eligible_count']}",
            f"- avg redis round trips: {loadtest_results[PRECOMPUTED_SEGMENT_MODE]['avg_redis_round_trips']}",
            "",
            f"### {HYBRID_MODE}",
            f"- handler avg / p95 / p99 ms: {loadtest_results[HYBRID_MODE]['handler_avg_latency_ms']} / {loadtest_results[HYBRID_MODE]['handler_p95_latency_ms']} / {loadtest_results[HYBRID_MODE]['handler_p99_latency_ms']}",
            f"- identity resolution avg / p95 / p99 ms: {loadtest_results[HYBRID_MODE]['identity_resolution_avg_latency_ms']} / {loadtest_results[HYBRID_MODE]['identity_resolution_p95_latency_ms']} / {loadtest_results[HYBRID_MODE]['identity_resolution_p99_latency_ms']}",
            f"- profile fetch avg / p95 / p99 ms: {loadtest_results[HYBRID_MODE]['profile_fetch_avg_latency_ms']} / {loadtest_results[HYBRID_MODE]['profile_fetch_p95_latency_ms']} / {loadtest_results[HYBRID_MODE]['profile_fetch_p99_latency_ms']}",
            f"- candidate generation avg / p95 / p99 ms: {loadtest_results[HYBRID_MODE]['candidate_generation_avg_latency_ms']} / {loadtest_results[HYBRID_MODE]['candidate_generation_p95_latency_ms']} / {loadtest_results[HYBRID_MODE]['candidate_generation_p99_latency_ms']}",
            f"- decision-path p50 / p99 ms: {loadtest_results[HYBRID_MODE]['decision_path_p50_latency_ms']} / {loadtest_results[HYBRID_MODE]['decision_path_p99_latency_ms']}",
            f"- validated candidate p50 / p99 ms: {loadtest_results[HYBRID_MODE]['validated_candidate_p50_latency_ms']} / {loadtest_results[HYBRID_MODE]['validated_candidate_p99_latency_ms']}",
            f"- avg SINTER ops / mode redis round trips: {loadtest_results[HYBRID_MODE]['avg_sinter_ops']} / {loadtest_results[HYBRID_MODE]['avg_mode_redis_round_trips']}",
            f"- avg candidates / eligible: {loadtest_results[HYBRID_MODE]['avg_candidate_count']} / {loadtest_results[HYBRID_MODE]['avg_eligible_count']}",
            f"- avg redis round trips: {loadtest_results[HYBRID_MODE]['avg_redis_round_trips']}",
            "",
            f"### {HYBRID_BITMAP_MODE}",
            f"- handler avg / p95 / p99 ms: {loadtest_results[HYBRID_BITMAP_MODE]['handler_avg_latency_ms']} / {loadtest_results[HYBRID_BITMAP_MODE]['handler_p95_latency_ms']} / {loadtest_results[HYBRID_BITMAP_MODE]['handler_p99_latency_ms']}",
            f"- identity resolution avg / p95 / p99 ms: {loadtest_results[HYBRID_BITMAP_MODE]['identity_resolution_avg_latency_ms']} / {loadtest_results[HYBRID_BITMAP_MODE]['identity_resolution_p95_latency_ms']} / {loadtest_results[HYBRID_BITMAP_MODE]['identity_resolution_p99_latency_ms']}",
            f"- profile fetch avg / p95 / p99 ms: {loadtest_results[HYBRID_BITMAP_MODE]['profile_fetch_avg_latency_ms']} / {loadtest_results[HYBRID_BITMAP_MODE]['profile_fetch_p95_latency_ms']} / {loadtest_results[HYBRID_BITMAP_MODE]['profile_fetch_p99_latency_ms']}",
            f"- candidate generation avg / p95 / p99 ms: {loadtest_results[HYBRID_BITMAP_MODE]['candidate_generation_avg_latency_ms']} / {loadtest_results[HYBRID_BITMAP_MODE]['candidate_generation_p95_latency_ms']} / {loadtest_results[HYBRID_BITMAP_MODE]['candidate_generation_p99_latency_ms']}",
            f"- decision-path p50 / p99 ms: {loadtest_results[HYBRID_BITMAP_MODE]['decision_path_p50_latency_ms']} / {loadtest_results[HYBRID_BITMAP_MODE]['decision_path_p99_latency_ms']}",
            f"- validated candidate p50 / p99 ms: {loadtest_results[HYBRID_BITMAP_MODE]['validated_candidate_p50_latency_ms']} / {loadtest_results[HYBRID_BITMAP_MODE]['validated_candidate_p99_latency_ms']}",
            f"- avg SINTER ops / mode redis round trips: {loadtest_results[HYBRID_BITMAP_MODE]['avg_sinter_ops']} / {loadtest_results[HYBRID_BITMAP_MODE]['avg_mode_redis_round_trips']}",
            f"- avg candidates / eligible: {loadtest_results[HYBRID_BITMAP_MODE]['avg_candidate_count']} / {loadtest_results[HYBRID_BITMAP_MODE]['avg_eligible_count']}",
            f"- avg redis round trips: {loadtest_results[HYBRID_BITMAP_MODE]['avg_redis_round_trips']}",
            "",
            f"### {HYBRID_BITMAP_TAXONOMY_MODE}",
            f"- handler avg / p95 / p99 ms: {loadtest_results[HYBRID_BITMAP_TAXONOMY_MODE]['handler_avg_latency_ms']} / {loadtest_results[HYBRID_BITMAP_TAXONOMY_MODE]['handler_p95_latency_ms']} / {loadtest_results[HYBRID_BITMAP_TAXONOMY_MODE]['handler_p99_latency_ms']}",
            f"- identity resolution avg / p95 / p99 ms: {loadtest_results[HYBRID_BITMAP_TAXONOMY_MODE]['identity_resolution_avg_latency_ms']} / {loadtest_results[HYBRID_BITMAP_TAXONOMY_MODE]['identity_resolution_p95_latency_ms']} / {loadtest_results[HYBRID_BITMAP_TAXONOMY_MODE]['identity_resolution_p99_latency_ms']}",
            f"- profile fetch avg / p95 / p99 ms: {loadtest_results[HYBRID_BITMAP_TAXONOMY_MODE]['profile_fetch_avg_latency_ms']} / {loadtest_results[HYBRID_BITMAP_TAXONOMY_MODE]['profile_fetch_p95_latency_ms']} / {loadtest_results[HYBRID_BITMAP_TAXONOMY_MODE]['profile_fetch_p99_latency_ms']}",
            f"- candidate generation avg / p95 / p99 ms: {loadtest_results[HYBRID_BITMAP_TAXONOMY_MODE]['candidate_generation_avg_latency_ms']} / {loadtest_results[HYBRID_BITMAP_TAXONOMY_MODE]['candidate_generation_p95_latency_ms']} / {loadtest_results[HYBRID_BITMAP_TAXONOMY_MODE]['candidate_generation_p99_latency_ms']}",
            f"- filtering avg / p95 / p99 ms: {loadtest_results[HYBRID_BITMAP_TAXONOMY_MODE]['filtering_avg_latency_ms']} / {loadtest_results[HYBRID_BITMAP_TAXONOMY_MODE]['filtering_p95_latency_ms']} / {loadtest_results[HYBRID_BITMAP_TAXONOMY_MODE]['filtering_p99_latency_ms']}",
            f"- decision-path p50 / p99 ms: {loadtest_results[HYBRID_BITMAP_TAXONOMY_MODE]['decision_path_p50_latency_ms']} / {loadtest_results[HYBRID_BITMAP_TAXONOMY_MODE]['decision_path_p99_latency_ms']}",
            f"- validated candidate p50 / p99 ms: {loadtest_results[HYBRID_BITMAP_TAXONOMY_MODE]['validated_candidate_p50_latency_ms']} / {loadtest_results[HYBRID_BITMAP_TAXONOMY_MODE]['validated_candidate_p99_latency_ms']}",
            f"- avg SINTER ops / mode redis round trips: {loadtest_results[HYBRID_BITMAP_TAXONOMY_MODE]['avg_sinter_ops']} / {loadtest_results[HYBRID_BITMAP_TAXONOMY_MODE]['avg_mode_redis_round_trips']}",
            f"- avg candidates / eligible: {loadtest_results[HYBRID_BITMAP_TAXONOMY_MODE]['avg_candidate_count']} / {loadtest_results[HYBRID_BITMAP_TAXONOMY_MODE]['avg_eligible_count']}",
            f"- avg redis round trips: {loadtest_results[HYBRID_BITMAP_TAXONOMY_MODE]['avg_redis_round_trips']}",
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a combined benchmark report")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--dataset-dir", type=Path, default=Path("data/generated/synthetic"))
    parser.add_argument("--output", type=Path, default=Path("reports/benchmark_report.md"))
    parser.add_argument("--mind-output-dir", type=Path, default=Path("data/generated/mind"))
    parser.add_argument("--fairjob-output-dir", type=Path, default=Path("data/generated/fairjob"))
    args = parser.parse_args()

    synthetic_modes = evaluate_synthetic_modes(args.dataset_dir)
    method_sizes = method_keyspace_bytes(args.dataset_dir)
    user_path = args.dataset_dir / "maids.jsonl"
    if not user_path.exists():
        user_path = args.dataset_dir / "users.jsonl"
    users = read_jsonl(user_path)
    identifiers = [
        {"identity_token": user["identity_tokens"][0]}
        if user.get("identity_tokens")
        else {"user_id": user["user_id"]}
        for user in users
    ]

    loadtests = {
        mode_name: asyncio.run(
            run_load_test(
                base_url=args.base_url,
                identifiers=identifiers,
                duration_seconds=10,
                target_rps=12,
                concurrency=1,
                warmup_seconds=2,
                mode="serial",
                rank_mode=mode_name,
            )
        )
        for mode_name in (
            FULL_REALTIME_MODE,
            MAID_BRUTEFORCE_SINTER_MODE,
            MAID_TIGHTENED_SINTER_MODE,
            PRECOMPUTED_SEGMENT_MODE,
            HYBRID_MODE,
            HYBRID_BITMAP_MODE,
            HYBRID_BITMAP_TAXONOMY_MODE,
        )
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(build_report(synthetic_modes, loadtests, method_sizes), encoding="utf-8")
    print(
        json.dumps(
            {
                "synthetic_modes": synthetic_modes,
                "loadtests": loadtests,
                "method_sizes": {
                    mode_name: {
                        "small_scale_bytes": footprint.small_scale_bytes,
                        "scaled_up_bytes": footprint.scaled_up_bytes,
                    }
                    for mode_name, footprint in method_sizes.items()
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
