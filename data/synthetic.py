from __future__ import annotations

import argparse
import json
from pathlib import Path
from random import Random

import pandas as pd

from app.candidate import filter_campaigns_for_user
from app.models import Campaign, UserProfile
from data.common import (
    AGE_BUCKETS,
    DEVICES,
    FEATURES,
    GEOS,
    SyntheticConfig,
    click_label,
    click_probability,
    segment_for,
    truth_score,
    write_jsonl,
)


def generate_users(config: SyntheticConfig) -> list[UserProfile]:
    random = Random(config.seed)
    features = FEATURES[: config.feature_count]
    users: list[UserProfile] = []
    for index in range(config.num_users):
        interests = {feature: round(random.betavariate(2.2, 2.6), 4) for feature in features}
        ranked_features = sorted(interests.items(), key=lambda item: item[1], reverse=True)
        segments = [segment for feature, value in ranked_features if (segment := segment_for(feature, value))]
        if not segments:
            feature, value = ranked_features[0]
            segments = [f"{feature}_{'high' if value > 0.5 else 'medium'}"]
        min_segment_count = min(3, len(segments))
        max_segment_count = min(5, len(segments))
        users.append(
            UserProfile(
                user_id=f"u{index:05d}",
                geo=random.choice(GEOS),
                device=random.choice(DEVICES),
                age_bucket=random.choice(AGE_BUCKETS),
                interests=interests,
                segments=segments[: random.randint(min_segment_count, max_segment_count)],
                impression_count=random.randint(0, 300),
            )
        )
    return users


def generate_campaigns(config: SyntheticConfig) -> list[Campaign]:
    random = Random(config.seed + 1)
    features = FEATURES[: config.feature_count]
    segment_vocab = [f"{feature}_{band}" for feature in features for band in ("medium", "high")]
    campaigns: list[Campaign] = []
    for index in range(config.num_campaigns):
        targeted_features = random.sample(features, k=random.randint(4, min(8, len(features))))
        weights = {feature: round(random.uniform(-0.4, 1.4), 4) for feature in targeted_features}
        top_positive_features = [
            feature
            for feature, weight in sorted(weights.items(), key=lambda item: item[1], reverse=True)
            if weight > 0.35
        ]
        if not top_positive_features:
            top_positive_features = [max(weights, key=weights.get)]
        random.shuffle(top_positive_features)

        targeting_pattern = random.random()
        required_segments: list[str] = []
        any_of_segments: list[str] = []
        none_of_segments: list[str] = []

        if targeting_pattern < 0.25:
            required_segments = random.sample(segment_vocab, k=random.randint(1, 2))
        elif targeting_pattern < 0.7:
            any_of_count = random.randint(min(2, len(top_positive_features)), min(4, len(top_positive_features)))
            any_of_segments = [
                f"{feature}_{random.choice(['medium', 'high'])}"
                for feature in top_positive_features[:any_of_count]
            ]
            required_segments = random.sample(any_of_segments, k=1) if random.random() < 0.35 else []
        else:
            required_segments = [
                f"{top_positive_features[0]}_{random.choice(['medium', 'high'])}"
            ]
            remaining_positive_features = top_positive_features[1:] or top_positive_features[:1]
            any_of_count = random.randint(1, min(3, len(remaining_positive_features)))
            any_of_segments = [
                f"{feature}_{random.choice(['medium', 'high'])}"
                for feature in remaining_positive_features[:any_of_count]
            ]

        negative_features = [
            feature
            for feature, weight in sorted(weights.items(), key=lambda item: item[1])
            if weight < 0.0
        ]
        if negative_features and random.random() < 0.45:
            none_of_segments = [
                f"{feature}_{random.choice(['medium', 'high'])}"
                for feature in negative_features[: random.randint(1, min(2, len(negative_features)))]
            ]

        geo = ["*"] if random.random() < 0.18 else random.sample(GEOS, k=random.randint(1, 2))
        device = ["*"] if random.random() < 0.18 else random.sample(DEVICES, k=random.randint(1, 2))
        campaigns.append(
            Campaign(
                campaign_id=f"c{index:05d}",
                geo=geo,
                device=device,
                required_segments=required_segments,
                any_of_segments=_unique(any_of_segments, exclude=required_segments),
                none_of_segments=_unique(none_of_segments, exclude=required_segments + any_of_segments),
                weights=weights,
                bid=round(random.uniform(0.5, 4.0), 3),
                freshness_boost=round(random.uniform(0.0, 0.6), 3),
                age_in_days=random.randint(0, 90),
            )
        )
    return campaigns


def generate_interactions(
    users: list[UserProfile],
    campaigns: list[Campaign],
    config: SyntheticConfig,
) -> pd.DataFrame:
    random = Random(config.seed + 2)
    rows: list[dict[str, object]] = []
    for _ in range(config.num_interactions):
        user = random.choice(users)
        eligible = filter_campaigns_for_user(user, campaigns)
        if eligible and random.random() < 0.8:
            campaign = random.choice(eligible)
        else:
            campaign = random.choice(campaigns)
        rows.append(
            {
                "user_id": user.user_id,
                "campaign_id": campaign.campaign_id,
                "eligible": int(campaign in eligible),
                "truth_score": round(truth_score(user, campaign), 6),
                "click_probability": round(click_probability(user, campaign), 6),
                "label": click_label(user, campaign),
            }
        )
    return pd.DataFrame(rows)


def generate_dataset(
    dataset_dir: Path,
    *,
    num_users: int,
    num_campaigns: int,
    num_interactions: int,
    feature_count: int,
    seed: int = 17,
) -> None:
    config = SyntheticConfig(
        num_users=num_users,
        num_campaigns=num_campaigns,
        num_interactions=num_interactions,
        feature_count=feature_count,
        seed=seed,
    )
    dataset_dir.mkdir(parents=True, exist_ok=True)
    users = generate_users(config)
    campaigns = generate_campaigns(config)
    interactions = generate_interactions(users, campaigns, config)
    write_jsonl(dataset_dir / "users.jsonl", [user.model_dump() for user in users])
    write_jsonl(dataset_dir / "campaigns.jsonl", [campaign.model_dump() for campaign in campaigns])
    interactions.to_parquet(dataset_dir / "interactions.parquet", index=False)
    metadata = {
        "num_users": num_users,
        "num_campaigns": num_campaigns,
        "num_interactions": num_interactions,
        "feature_count": feature_count,
        "seed": seed,
        "wildcard_geo_campaigns": sum("*" in campaign.geo for campaign in campaigns),
        "wildcard_device_campaigns": sum("*" in campaign.device for campaign in campaigns),
        "any_of_campaigns": sum(bool(campaign.any_of_segments) for campaign in campaigns),
        "none_of_campaigns": sum(bool(campaign.none_of_segments) for campaign in campaigns),
    }
    (dataset_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def ensure_synthetic_dataset(
    dataset_dir: Path,
    *,
    num_users: int,
    num_campaigns: int,
    num_interactions: int,
    feature_count: int,
) -> None:
    required = [
        dataset_dir / "users.jsonl",
        dataset_dir / "campaigns.jsonl",
        dataset_dir / "interactions.parquet",
        dataset_dir / "metadata.json",
    ]
    if all(path.exists() for path in required):
        return
    generate_dataset(
        dataset_dir,
        num_users=num_users,
        num_campaigns=num_campaigns,
        num_interactions=num_interactions,
        feature_count=feature_count,
    )


def _unique(values: list[str], exclude: list[str] | None = None) -> list[str]:
    excluded = set(exclude or [])
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in excluded or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic DSP data")
    parser.add_argument("--output", type=Path, default=Path("data/generated/synthetic"))
    parser.add_argument("--num-users", type=int, default=4000)
    parser.add_argument("--num-campaigns", type=int, default=2500)
    parser.add_argument("--num-interactions", type=int, default=120000)
    parser.add_argument("--feature-count", type=int, default=12)
    parser.add_argument("--seed", type=int, default=17)
    args = parser.parse_args()
    generate_dataset(
        args.output,
        num_users=args.num_users,
        num_campaigns=args.num_campaigns,
        num_interactions=args.num_interactions,
        feature_count=args.feature_count,
        seed=args.seed,
    )


if __name__ == "__main__":
    main()
