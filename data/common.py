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
STATES_BY_COUNTRY = {
    "US": ["CA", "CO", "FL", "IL", "NY", "TX"],
    "CA": ["BC", "ON", "QC"],
    "GB": ["ENG", "SCT"],
    "AU": ["NSW", "VIC"],
}
POSTAL_CODES_BY_STATE = {
    "CA": ["90001", "94105"],
    "CO": ["80014", "80202"],
    "FL": ["33101", "33602"],
    "IL": ["60601", "60611"],
    "NY": ["10001", "10018"],
    "TX": ["73301", "77001"],
    "BC": ["V5K0A1", "V6B1A1"],
    "ON": ["M5H2N2", "M4B1B3"],
    "QC": ["H2Y1C6", "G1R5M1"],
    "ENG": ["SW1A1AA", "M11AE"],
    "SCT": ["EH12NG", "G11XQ"],
    "NSW": ["2000", "2150"],
    "VIC": ["3000", "3121"],
}
DEVICE_TYPES = ["mobile", "desktop", "tablet", "ctv"]
DEVICE_OSES = ["iOS", "Android", "Windows", "macOS", "Roku", "Web"]
DEVICES = DEVICE_OSES
CARD_TIERS = ["Standard", "Gold", "Platinum", "WorldElite"]
SPEND_TIERS = ["low", "medium", "high"]
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
        _matches_dimension(user.geo, campaign.geo)
        and _matches_optional_list(user.state, campaign.geo_states)
        and _matches_optional_list(user.postal_code, campaign.geo_postal_codes)
        and _matches_dimension(user.device, campaign.device)
        and _matches_dimension(user.device_type, campaign.device_types)
        and _matches_dimension(user.card_tier, campaign.card_tiers)
        and campaign.pacing_status == "active"
        and campaign.spent_today_usd < campaign.daily_budget_usd
        and user.frequency_history.get(campaign.campaign_id, 0) < campaign.frequency_cap
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


def _matches_dimension(user_value: str, campaign_values: list[str]) -> bool:
    return user_value in campaign_values or "*" in campaign_values


def _matches_optional_list(user_value: str, campaign_values: list[str]) -> bool:
    return not campaign_values or user_value in campaign_values or "*" in campaign_values
