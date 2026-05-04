from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.models import Campaign, UserProfile

FEATURES = [
    "camping",
    "home_improvement",
    "gaming",
    "travel",
    "luxury",
    "fitness",
    "foodie",
    "family",
    "pet_care",
    "streaming",
    "finance",
    "tech",
    "auto",
    "beauty",
]
GEOS = ["US", "CA", "GB", "AU"]
DEVICES = ["iOS", "Android", "Web", "CTV"]
AGE_BUCKETS = ["18-24", "25-34", "35-44", "45-54", "55+"]


@dataclass
class SyntheticConfig:
    num_users: int = 4000
    num_campaigns: int = 2500
    num_interactions: int = 120000
    feature_count: int = 12
    seed: int = 17


def segment_for(feature: str, value: float) -> str | None:
    if value >= 0.7:
        return f"{feature}_high"
    if value >= 0.45:
        return f"{feature}_medium"
    return None


def stable_noise(*parts: str) -> float:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return (int(digest[:8], 16) / 0xFFFFFFFF) - 0.5


def sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-value))


def truth_score(user: UserProfile, campaign: Campaign) -> float:
    if not campaign_is_eligible(user, campaign):
        return -4.0
    affinity = sum(user.interests.get(feature, 0.0) * weight for feature, weight in campaign.weights.items())
    freshness = campaign.freshness_boost * math.exp(-campaign.age_in_days / 45.0)
    noise = stable_noise(user.user_id, campaign.campaign_id)
    return affinity + (0.4 * campaign.bid) + freshness + (0.3 * noise)


def click_probability(user: UserProfile, campaign: Campaign) -> float:
    return sigmoid(truth_score(user, campaign) - 1.6)


def click_label(user: UserProfile, campaign: Campaign) -> int:
    return int(click_probability(user, campaign) >= 0.5)


def campaign_is_eligible(user: UserProfile, campaign: Campaign) -> bool:
    user_segments = set(user.segments)
    return (
        user.geo in campaign.geo
        and user.device in campaign.device
        and set(campaign.required_segments).issubset(user_segments)
        and (not campaign.any_of_segments or bool(user_segments.intersection(campaign.any_of_segments)))
        and not user_segments.intersection(campaign.none_of_segments)
    )


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True))
            handle.write("\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]
