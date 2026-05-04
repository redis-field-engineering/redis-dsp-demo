from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd
from huggingface_hub import hf_hub_download

from app.models import Campaign, UserProfile
from data.common import write_jsonl

FAIRJOB_USER_CAT_COLUMNS = [f"cat{index}" for index in range(6)]
FAIRJOB_PRODUCT_CAT_COLUMNS = [f"cat{index}" for index in range(6, 13)]
FAIRJOB_NUMERIC_COLUMNS = [f"num{index}" for index in range(16, 51)]


def export_fairjob_dataset(
    output_dir: Path,
    *,
    max_impressions: int = 30000,
    max_campaigns: int = 5000,
    min_campaign_impressions: int = 20,
    min_segment_support: int = 12,
    positive_lift_threshold: float = 1.15,
    negative_lift_threshold: float = 0.85,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    required = [
        output_dir / "users.jsonl",
        output_dir / "campaigns.jsonl",
        output_dir / "interactions.parquet",
        output_dir / "metadata.json",
    ]
    if all(path.exists() for path in required):
        return output_dir / "interactions.parquet"
    csv_path = hf_hub_download(repo_id="criteo/FairJob", filename="fairjob.csv.gz", repo_type="dataset")
    frame = pd.read_csv(csv_path)
    frame = _sample_impressions(frame, max_impressions=max_impressions)

    product_counts = frame["product_id"].value_counts()
    kept_products = product_counts[product_counts >= min_campaign_impressions].head(max_campaigns).index
    frame = frame[frame["product_id"].isin(kept_products)].copy()

    if frame.empty:
        raise ValueError("No FairJob rows left after sampling and campaign filtering")

    quantiles = _numeric_quantiles(frame)
    frame["request_user_id"] = frame["impression_id"].map(lambda value: f"fj_imp_{value}")
    frame["campaign_id"] = frame["product_id"].map(lambda value: f"fj_prod_{value}")
    frame["label"] = frame["click"].astype(int)
    frame["segments"] = frame.apply(lambda row: _user_segments(row, quantiles), axis=1)

    impression_users = (
        frame.sort_values(["impression_id", "rank"])
        .groupby("request_user_id", as_index=False)
        .first()
        .copy()
    )

    segment_prevalence = Counter(segment for segments in impression_users["segments"] for segment in segments)
    users = [
        UserProfile(
            user_id=row["request_user_id"],
            geo=f"cat0_{int(row['cat0'])}",
            device=f"cat1_{int(row['cat1'])}",
            age_bucket=f"protected_{int(row['protected_attribute'])}",
            interests=_segment_interests(row["segments"]),
            segments=_ordered_segments(row["segments"], segment_prevalence),
            impression_count=int(frame.loc[frame["request_user_id"] == row["request_user_id"]].shape[0]),
        )
        for _, row in impression_users.iterrows()
    ]

    campaign_frame = frame.copy()
    campaign_frame["train_split"] = campaign_frame["impression_id"].map(_is_training_impression)
    campaigns = _derive_campaigns(
        campaign_frame,
        min_segment_support=min_segment_support,
        positive_lift_threshold=positive_lift_threshold,
        negative_lift_threshold=negative_lift_threshold,
    )

    interactions = frame[
        [
            "request_user_id",
            "campaign_id",
            "impression_id",
            "product_id",
            "displayrandom",
            "rank",
            "click",
        ]
    ].rename(columns={"request_user_id": "user_id", "click": "label"})
    interactions.to_parquet(output_dir / "interactions.parquet", index=False)
    write_jsonl(output_dir / "users.jsonl", [user.model_dump() for user in users])
    write_jsonl(output_dir / "campaigns.jsonl", [campaign.model_dump() for campaign in campaigns])

    metadata = {
        "source_dataset": "criteo/FairJob",
        "rows_exported": int(len(interactions)),
        "users_exported": len(users),
        "campaigns_exported": len(campaigns),
        "max_impressions": max_impressions,
        "max_campaigns": max_campaigns,
        "min_campaign_impressions": min_campaign_impressions,
        "min_segment_support": min_segment_support,
        "positive_lift_threshold": positive_lift_threshold,
        "negative_lift_threshold": negative_lift_threshold,
    }
    (output_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return output_dir / "interactions.parquet"


def _sample_impressions(frame: pd.DataFrame, *, max_impressions: int) -> pd.DataFrame:
    unique_impressions = pd.Series(frame["impression_id"].drop_duplicates().tolist())
    selected = unique_impressions.iloc[:max_impressions]
    return frame[frame["impression_id"].isin(selected)].copy()


def _numeric_quantiles(frame: pd.DataFrame) -> dict[str, tuple[float, float]]:
    quantiles: dict[str, tuple[float, float]] = {}
    for column in FAIRJOB_NUMERIC_COLUMNS:
        quantiles[column] = (
            float(frame[column].quantile(0.25)),
            float(frame[column].quantile(0.75)),
        )
    return quantiles


def _user_segments(row: pd.Series, quantiles: dict[str, tuple[float, float]]) -> list[str]:
    segments = [f"{column}_{int(row[column])}" for column in FAIRJOB_USER_CAT_COLUMNS[2:]]
    for column in FAIRJOB_NUMERIC_COLUMNS:
        low, high = quantiles[column]
        value = float(row[column])
        if value >= high:
            segments.append(f"{column}_high")
        elif value <= low:
            segments.append(f"{column}_low")
    return segments


def _ordered_segments(segments: list[str], prevalence: Counter[str]) -> list[str]:
    del prevalence
    return list(dict.fromkeys(segments))


def _segment_interests(segments: list[str]) -> dict[str, float]:
    interests: dict[str, float] = {}
    for segment in segments:
        if segment.endswith("_high"):
            interests[segment] = 1.0
        elif segment.endswith("_low"):
            interests[segment] = 0.7
        else:
            interests[segment] = 0.9
    return interests


def _derive_campaigns(
    frame: pd.DataFrame,
    *,
    min_segment_support: int,
    positive_lift_threshold: float,
    negative_lift_threshold: float,
) -> list[Campaign]:
    train = frame[frame["train_split"]].copy()
    if train.empty:
        train = frame.copy()

    campaigns: list[Campaign] = []
    for campaign_id, group in train.groupby("campaign_id"):
        product_row = group.iloc[0]
        baseline_ctr = _smoothed_ctr(int(group["label"].sum()), int(len(group)))

        segment_impressions: Counter[str] = Counter()
        segment_clicks: Counter[str] = Counter()
        for _, row in group.iterrows():
            clicked = int(row["label"])
            for segment in row["segments"]:
                segment_impressions[segment] += 1
                segment_clicks[segment] += clicked

        positive_candidates: list[tuple[str, float, int]] = []
        negative_candidates: list[tuple[str, float, int]] = []
        for segment, impressions in segment_impressions.items():
            if impressions < min_segment_support:
                continue
            ctr = _smoothed_ctr(segment_clicks[segment], impressions)
            lift = ctr / baseline_ctr if baseline_ctr else 1.0
            score = math.log(lift) * math.log1p(impressions)
            if lift >= positive_lift_threshold:
                positive_candidates.append((segment, score, impressions))
            elif lift <= negative_lift_threshold:
                negative_candidates.append((segment, -score, impressions))

        positive_candidates.sort(key=lambda item: (item[1], item[2], item[0]), reverse=True)
        negative_candidates.sort(key=lambda item: (item[1], item[2], item[0]), reverse=True)

        required_segments = [segment for segment, _, _ in positive_candidates[:1]]
        any_of_segments = [segment for segment, _, _ in positive_candidates[1:4]]
        none_of_segments = [segment for segment, _, _ in negative_candidates[:2]]

        if not required_segments and positive_candidates:
            any_of_segments = [segment for segment, _, _ in positive_candidates[:3]]

        weights = {
            segment: round(min(max(score, 0.15), 2.5), 4)
            for segment, score, _ in positive_candidates[:8]
        }
        for segment, score, _ in negative_candidates[:4]:
            weights[segment] = round(-min(max(score, 0.15), 2.0), 4)

        if not weights:
            for segment in required_segments or any_of_segments or [f"{FAIRJOB_PRODUCT_CAT_COLUMNS[0]}_{int(product_row[FAIRJOB_PRODUCT_CAT_COLUMNS[0]])}"]:
                weights[segment] = 0.4

        geo_values = _top_matching_segments(positive_candidates, prefix="cat0_") or _top_exposure_values(group, column="cat0", prefix="cat0_")
        device_values = _top_matching_segments(positive_candidates, prefix="cat1_") or _top_exposure_values(group, column="cat1", prefix="cat1_")

        campaigns.append(
            Campaign(
                campaign_id=campaign_id,
                geo=geo_values[:2] or [f"cat0_{int(product_row['cat0'])}"],
                device=device_values[:2] or [f"cat1_{int(product_row['cat1'])}"],
                required_segments=required_segments,
                any_of_segments=any_of_segments,
                none_of_segments=none_of_segments,
                weights=weights,
                bid=round(0.5 + (3.5 * baseline_ctr), 4),
                freshness_boost=0.0,
                age_in_days=0,
            )
        )
    return campaigns


def _smoothed_ctr(clicks: int, impressions: int, alpha: float = 1.0, beta: float = 20.0) -> float:
    return (clicks + alpha) / (impressions + alpha + beta)


def _top_matching_segments(
    candidates: list[tuple[str, float, int]],
    *,
    prefix: str,
    limit: int = 3,
) -> list[str]:
    return [segment for segment, _, _ in candidates if segment.startswith(prefix)][:limit]


def _top_exposure_values(group: pd.DataFrame, *, column: str, prefix: str, limit: int = 3) -> list[str]:
    counts = group[column].value_counts().head(limit).index.tolist()
    return [f"{prefix}{int(value)}" for value in counts]


def _is_training_impression(impression_id: int) -> bool:
    digest = hashlib.sha256(str(impression_id).encode("utf-8")).hexdigest()
    return (int(digest[:8], 16) % 10) < 8


def main() -> None:
    parser = argparse.ArgumentParser(description="Translate FairJob into the DSP domain")
    parser.add_argument("--output-dir", type=Path, default=Path("data/generated/fairjob"))
    parser.add_argument("--max-impressions", type=int, default=30000)
    parser.add_argument("--max-campaigns", type=int, default=5000)
    parser.add_argument("--min-campaign-impressions", type=int, default=20)
    parser.add_argument("--min-segment-support", type=int, default=12)
    parser.add_argument("--positive-lift-threshold", type=float, default=1.15)
    parser.add_argument("--negative-lift-threshold", type=float, default=0.85)
    args = parser.parse_args()
    export_fairjob_dataset(
        args.output_dir,
        max_impressions=args.max_impressions,
        max_campaigns=args.max_campaigns,
        min_campaign_impressions=args.min_campaign_impressions,
        min_segment_support=args.min_segment_support,
        positive_lift_threshold=args.positive_lift_threshold,
        negative_lift_threshold=args.negative_lift_threshold,
    )


if __name__ == "__main__":
    main()
