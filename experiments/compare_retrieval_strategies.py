from __future__ import annotations

import argparse
import json
from pathlib import Path

from data.synthetic import generate_dataset
from experiments.evaluate import evaluate_synthetic


def compare_strategies(
    dataset_dir: Path,
    *,
    num_users: int,
    num_campaigns: int,
    num_interactions: int,
    feature_count: int,
    seed: int,
    top_k: int,
    sample_users: int,
) -> dict[str, object]:
    generate_dataset(
        dataset_dir,
        num_users=num_users,
        num_campaigns=num_campaigns,
        num_interactions=num_interactions,
        feature_count=feature_count,
        seed=seed,
    )
    naive = evaluate_synthetic(
        dataset_dir,
        top_k=top_k,
        sample_users=sample_users,
        strategy="naive",
    )
    union_probe = evaluate_synthetic(
        dataset_dir,
        top_k=top_k,
        sample_users=sample_users,
        strategy="union_probe",
    )
    return {
        "dataset_dir": str(dataset_dir),
        "naive": naive,
        "union_probe": union_probe,
        "deltas": {
            "candidate_generation_recall": round(
                union_probe["candidate_generation_recall"] - naive["candidate_generation_recall"],
                4,
            ),
            "ndcg_at_k": round(union_probe["ndcg_at_k"] - naive["ndcg_at_k"], 4),
            "precision_at_k": round(union_probe["precision_at_k"] - naive["precision_at_k"], 4),
            "recall_at_k": round(union_probe["recall_at_k"] - naive["recall_at_k"], 4),
            "f1_at_k": round(union_probe["f1_at_k"] - naive["f1_at_k"], 4),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare naive and union-probe retrieval strategies")
    parser.add_argument("--dataset-dir", type=Path, default=Path("data/generated/synthetic_retrieval_compare"))
    parser.add_argument("--num-users", type=int, default=4000)
    parser.add_argument("--num-campaigns", type=int, default=2500)
    parser.add_argument("--num-interactions", type=int, default=120000)
    parser.add_argument("--feature-count", type=int, default=12)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--sample-users", type=int, default=250)
    parser.add_argument("--output", type=Path, default=Path("reports/generated/retrieval_strategy_comparison.json"))
    args = parser.parse_args()

    results = compare_strategies(
        args.dataset_dir,
        num_users=args.num_users,
        num_campaigns=args.num_campaigns,
        num_interactions=args.num_interactions,
        feature_count=args.feature_count,
        seed=args.seed,
        top_k=args.top_k,
        sample_users=args.sample_users,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
