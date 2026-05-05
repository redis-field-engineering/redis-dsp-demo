from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean

import pandas as pd

from app.candidate import build_indexes, filter_campaigns_for_user, generate_candidates_in_memory
from app.models import Campaign, UserProfile
from app.ranking import rerank_campaigns
from data.common import click_label, click_probability, read_jsonl
from data.fairjob_adapter import export_fairjob_dataset
from data.huggingface_adapter import export_mind_translation


def ndcg_at_k(predicted_relevances: list[float], ideal_relevances: list[float], k: int) -> float:
    gains = predicted_relevances[:k]
    dcg = sum((2**rel - 1) / _log2(index + 2) for index, rel in enumerate(gains))
    ideal = sorted(ideal_relevances, reverse=True)
    idcg = sum((2**rel - 1) / _log2(index + 2) for index, rel in enumerate(ideal[:k]))
    return dcg / idcg if idcg else 0.0


def precision_at_k(labels: list[int], k: int) -> float:
    top = labels[:k]
    return sum(top) / max(len(top), 1)


def recall_at_k(labels: list[int], total_positives: int, k: int) -> float:
    if total_positives == 0:
        return 0.0
    return sum(labels[:k]) / total_positives


def f1_at_k(precision: float, recall: float) -> float:
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def evaluate_synthetic(
    dataset_dir: Path,
    top_k: int = 5,
    sample_users: int = 250,
    strategy: str = "union_probe",
) -> dict[str, float]:
    users = [UserProfile.model_validate(item) for item in read_jsonl(dataset_dir / "users.jsonl")]
    campaigns = [Campaign.model_validate(item) for item in read_jsonl(dataset_dir / "campaigns.jsonl")]
    campaign_by_id = {campaign.campaign_id: campaign for campaign in campaigns}
    indexes = build_indexes(campaigns)

    ndcgs: list[float] = []
    precisions: list[float] = []
    recalls: list[float] = []
    f1s: list[float] = []
    candidate_recalls: list[float] = []
    evaluated = 0

    for user in users[:sample_users]:
        eligible = filter_campaigns_for_user(user, campaigns)
        if not eligible:
            continue
        relevant_ids = {
            campaign.campaign_id for campaign in eligible if click_label(user, campaign) == 1
        }
        if not relevant_ids:
            continue
        evaluated += 1
        candidate_ids = generate_candidates_in_memory(
            user,
            indexes,
            max_candidates=200,
            strong_signal_count=2,
            strategy=strategy,
        )
        candidate_recalls.append(len(set(candidate_ids) & relevant_ids) / len(relevant_ids))
        candidate_campaigns = filter_campaigns_for_user(
            user,
            [campaign_by_id[campaign_id] for campaign_id in candidate_ids if campaign_id in campaign_by_id],
        )
        ranked = rerank_campaigns(user, candidate_campaigns, top_k=top_k)
        labels = [int(item.campaign_id in relevant_ids) for item in ranked]
        predicted_relevances = [
            click_probability(user, campaign_by_id[item.campaign_id])
            for item in ranked
            if item.campaign_id in campaign_by_id
        ]
        ideal_relevances = [click_probability(user, campaign) for campaign in eligible]
        precision = precision_at_k(labels, top_k)
        recall = recall_at_k(labels, len(relevant_ids), top_k)
        precisions.append(precision)
        recalls.append(recall)
        f1s.append(f1_at_k(precision, recall))
        ndcgs.append(ndcg_at_k(predicted_relevances, ideal_relevances, top_k))

    return {
        "users_evaluated": evaluated,
        "strategy": strategy,
        "ndcg_at_k": round(mean(ndcgs), 4) if ndcgs else 0.0,
        "precision_at_k": round(mean(precisions), 4) if precisions else 0.0,
        "recall_at_k": round(mean(recalls), 4) if recalls else 0.0,
        "f1_at_k": round(mean(f1s), 4) if f1s else 0.0,
        "candidate_generation_recall": round(mean(candidate_recalls), 4) if candidate_recalls else 0.0,
    }


def evaluate_mind_translation(output_dir: Path, top_k: int = 5, sample_size: int = 1500) -> dict[str, float]:
    translated_path = export_mind_translation(output_dir, split="train", sample_size=sample_size)
    frame = pd.read_parquet(translated_path)
    user_rows = read_jsonl(output_dir / "mind_translated_users.jsonl")
    user_interest_lookup = {row["user_id"]: row.get("interests", {}) for row in user_rows}
    category_ctr = frame.groupby("category")["label"].mean().to_dict()
    grouped = frame.groupby("user_id")
    ndcgs: list[float] = []
    precisions: list[float] = []
    recalls: list[float] = []
    f1s: list[float] = []
    for _, group in list(grouped)[:250]:
        group = group.copy()
        interests = user_interest_lookup.get(str(group["user_id"].iloc[0]), {})
        group["predicted_score"] = group["category"].map(
            lambda category: float(interests.get(str(category).lower(), 0.0)) + float(category_ctr.get(category, 0.0))
        )
        ranked = group.sort_values("predicted_score", ascending=False).head(top_k)
        labels = ranked["label"].astype(int).tolist()
        relevances = ranked["label"].astype(float).tolist()
        ideal_relevances = sorted(group["label"].astype(float).tolist(), reverse=True)
        positives = int(group["label"].sum())
        precision = precision_at_k(labels, top_k)
        recall = recall_at_k(labels, positives, top_k)
        precisions.append(precision)
        recalls.append(recall)
        f1s.append(f1_at_k(precision, recall))
        ndcgs.append(ndcg_at_k(relevances, ideal_relevances, top_k))
    return {
        "queries_evaluated": min(len(grouped), 250),
        "ndcg_at_k": round(mean(ndcgs), 4) if ndcgs else 0.0,
        "precision_at_k": round(mean(precisions), 4) if precisions else 0.0,
        "recall_at_k": round(mean(recalls), 4) if recalls else 0.0,
        "f1_at_k": round(mean(f1s), 4) if f1s else 0.0,
    }


def evaluate_fairjob_translation(
    output_dir: Path,
    top_k: int = 5,
    max_impressions: int = 30000,
    sample_users: int = 250,
    randomized_only: bool = True,
) -> dict[str, float]:
    export_fairjob_dataset(output_dir, max_impressions=max_impressions)
    return evaluate_interaction_dataset(
        output_dir,
        top_k=top_k,
        sample_users=sample_users,
        randomized_only=randomized_only,
    )


def evaluate_interaction_dataset(
    dataset_dir: Path,
    *,
    top_k: int,
    sample_users: int,
    randomized_only: bool,
    strategy: str = "union_probe",
) -> dict[str, float]:
    users = {
        user.user_id: user
        for user in [UserProfile.model_validate(item) for item in read_jsonl(dataset_dir / "users.jsonl")]
    }
    campaigns = [Campaign.model_validate(item) for item in read_jsonl(dataset_dir / "campaigns.jsonl")]
    campaign_by_id = {campaign.campaign_id: campaign for campaign in campaigns}
    indexes = build_indexes(campaigns)
    interactions = pd.read_parquet(dataset_dir / "interactions.parquet")
    if randomized_only and "displayrandom" in interactions.columns:
        interactions = interactions[interactions["displayrandom"] == 1].copy()

    ndcgs: list[float] = []
    precisions: list[float] = []
    recalls: list[float] = []
    f1s: list[float] = []
    candidate_recalls: list[float] = []
    displayed_coverages: list[float] = []
    evaluated = 0

    grouped = interactions.groupby("user_id")
    for user_id, group in grouped:
        user = users.get(str(user_id))
        if user is None:
            continue
        relevant_ids = set(group.loc[group["label"] == 1, "campaign_id"].astype(str))
        displayed_ids = set(group["campaign_id"].astype(str))
        if not relevant_ids or not displayed_ids:
            continue
        evaluated += 1
        if evaluated > sample_users:
            break
        candidate_ids = generate_candidates_in_memory(
            user,
            indexes,
            max_candidates=200,
            strong_signal_count=2,
            strategy=strategy,
        )
        candidate_recalls.append(len(set(candidate_ids) & relevant_ids) / len(relevant_ids))

        displayed_candidate_ids = [campaign_id for campaign_id in candidate_ids if campaign_id in displayed_ids]
        displayed_coverages.append(len(displayed_candidate_ids) / len(displayed_ids))
        candidate_campaigns = filter_campaigns_for_user(
            user,
            [campaign_by_id[campaign_id] for campaign_id in displayed_candidate_ids if campaign_id in campaign_by_id],
        )
        ranked = rerank_campaigns(user, candidate_campaigns, top_k=top_k)
        labels = [int(item.campaign_id in relevant_ids) for item in ranked]
        precision = precision_at_k(labels, top_k)
        recall = recall_at_k(labels, len(relevant_ids), top_k)
        precisions.append(precision)
        recalls.append(recall)
        f1s.append(f1_at_k(precision, recall))
        ndcgs.append(ndcg_at_k([float(label) for label in labels], group["label"].astype(float).tolist(), top_k))

    return {
        "queries_evaluated": evaluated,
        "strategy": strategy,
        "ndcg_at_k": round(mean(ndcgs), 4) if ndcgs else 0.0,
        "precision_at_k": round(mean(precisions), 4) if precisions else 0.0,
        "recall_at_k": round(mean(recalls), 4) if recalls else 0.0,
        "f1_at_k": round(mean(f1s), 4) if f1s else 0.0,
        "candidate_generation_recall": round(mean(candidate_recalls), 4) if candidate_recalls else 0.0,
        "displayed_candidate_coverage": round(mean(displayed_coverages), 4) if displayed_coverages else 0.0,
    }


def _log2(value: int) -> float:
    from math import log2

    return log2(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate ranking quality offline")
    parser.add_argument("--dataset-dir", type=Path, default=Path("data/generated/synthetic"))
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--output", type=Path, default=Path("reports/generated/evaluation.json"))
    parser.add_argument("--mind-output-dir", type=Path, default=Path("data/generated/mind"))
    parser.add_argument("--fairjob-output-dir", type=Path, default=Path("data/generated/fairjob"))
    args = parser.parse_args()

    results = {
        "synthetic": evaluate_synthetic(args.dataset_dir, top_k=args.top_k),
        "mind": evaluate_mind_translation(args.mind_output_dir, top_k=args.top_k),
        "fairjob": evaluate_fairjob_translation(args.fairjob_output_dir, top_k=args.top_k),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
