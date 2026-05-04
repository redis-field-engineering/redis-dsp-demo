from __future__ import annotations

import argparse
import json
from pathlib import Path

from experiments.evaluate import evaluate_fairjob_translation, evaluate_mind_translation, evaluate_synthetic
from loadtest.run import run_load_test
from data.common import read_jsonl


def build_report(
    synthetic_results: dict[str, float],
    mind_results: dict[str, float],
    fairjob_results: dict[str, float],
    loadtest_results: dict[str, float | int],
) -> str:
    return "\n".join(
        [
            "# Benchmark Report",
            "",
            "## Synthetic Offline Metrics",
            f"- NDCG@K: {synthetic_results['ndcg_at_k']}",
            f"- Precision@K: {synthetic_results['precision_at_k']}",
            f"- Recall@K: {synthetic_results['recall_at_k']}",
            f"- F1@K: {synthetic_results['f1_at_k']}",
            f"- Candidate Recall: {synthetic_results['candidate_generation_recall']}",
            "",
            "## MIND Translation Metrics",
            f"- NDCG@K: {mind_results['ndcg_at_k']}",
            f"- Precision@K: {mind_results['precision_at_k']}",
            f"- Recall@K: {mind_results['recall_at_k']}",
            f"- F1@K: {mind_results['f1_at_k']}",
            "",
            "## FairJob Translation Metrics",
            f"- NDCG@K: {fairjob_results['ndcg_at_k']}",
            f"- Precision@K: {fairjob_results['precision_at_k']}",
            f"- Recall@K: {fairjob_results['recall_at_k']}",
            f"- F1@K: {fairjob_results['f1_at_k']}",
            f"- Candidate Recall: {fairjob_results['candidate_generation_recall']}",
            f"- Displayed Candidate Coverage: {fairjob_results['displayed_candidate_coverage']}",
            "",
            "## Load Test",
            f"- Requests: {loadtest_results['requests']}",
            f"- Success Rate: {loadtest_results['success_rate']}",
            f"- Throughput RPS: {loadtest_results['throughput_rps']}",
            f"- Avg Latency ms: {loadtest_results['avg_latency_ms']}",
            f"- p95 Latency ms: {loadtest_results['p95_latency_ms']}",
            f"- p99 Latency ms: {loadtest_results['p99_latency_ms']}",
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

    synthetic = evaluate_synthetic(args.dataset_dir)
    mind = evaluate_mind_translation(args.mind_output_dir)
    fairjob = evaluate_fairjob_translation(args.fairjob_output_dir)
    users = read_jsonl(args.dataset_dir / "users.jsonl")

    import asyncio

    loadtest = asyncio.run(
        run_load_test(
            base_url=args.base_url,
            user_ids=[user["user_id"] for user in users],
            duration_seconds=15,
            target_rps=20,
            concurrency=20,
        )
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(build_report(synthetic, mind, fairjob, loadtest), encoding="utf-8")
    print(json.dumps({"synthetic": synthetic, "mind": mind, "fairjob": fairjob, "loadtest": loadtest}, indent=2))


if __name__ == "__main__":
    main()
