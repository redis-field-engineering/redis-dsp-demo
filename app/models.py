from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field


FULL_REALTIME_MODE = "full_realtime"
PRECOMPUTED_SEGMENT_MODE = "precomputed_segment"
HYBRID_MODE = "hybrid_precompute_plus_realtime"
HYBRID_BITMAP_MODE = "hybrid_bitmap_gating"
MAID_BRUTEFORCE_SINTER_MODE = "maid_bruteforce_sinter"
MAID_TIGHTENED_SINTER_MODE = "maid_tightened_sinter"
CANDIDATE_MODES = [
    FULL_REALTIME_MODE,
    PRECOMPUTED_SEGMENT_MODE,
    HYBRID_MODE,
    HYBRID_BITMAP_MODE,
    MAID_BRUTEFORCE_SINTER_MODE,
    MAID_TIGHTENED_SINTER_MODE,
]


class UserProfile(BaseModel):
    user_id: str
    geo: str
    device: str
    age_bucket: str
    interests: dict[str, float]
    segments: list[str]
    identity_tokens: list[str] = Field(default_factory=list)
    state: str = ""
    postal_code: str = ""
    device_type: str = "mobile"
    card_tier: str = "Standard"
    spend_tier: str = "medium"
    frequency_history: dict[str, int] = Field(default_factory=dict)
    impression_count: int = 0

    def to_redis_hash(self) -> dict[str, str]:
        return {
            "user_id": self.user_id,
            "geo": self.geo,
            "state": self.state,
            "postal_code": self.postal_code,
            "device": self.device,
            "device_type": self.device_type,
            "age_bucket": self.age_bucket,
            "card_tier": self.card_tier,
            "spend_tier": self.spend_tier,
            "interests_json": json.dumps(self.interests, sort_keys=True),
            "segments_json": json.dumps(self.segments),
            "identity_tokens_json": json.dumps(self.identity_tokens),
            "frequency_history_json": json.dumps(self.frequency_history, sort_keys=True),
            "impression_count": str(self.impression_count),
        }

    @classmethod
    def from_redis_hash(cls, values: dict[str, Any]) -> "UserProfile":
        return cls(
            user_id=str(values["user_id"]),
            geo=str(values["geo"]),
            state=str(values.get("state", "")),
            postal_code=str(values.get("postal_code", "")),
            device=str(values["device"]),
            device_type=str(values.get("device_type", "mobile")),
            age_bucket=str(values["age_bucket"]),
            card_tier=str(values.get("card_tier", "Standard")),
            spend_tier=str(values.get("spend_tier", "medium")),
            interests=json.loads(values["interests_json"]),
            segments=list(json.loads(values["segments_json"])),
            identity_tokens=list(json.loads(values.get("identity_tokens_json", "[]"))),
            frequency_history={
                str(key): int(value)
                for key, value in json.loads(values.get("frequency_history_json", "{}")).items()
            },
            impression_count=int(values.get("impression_count", 0)),
        )


class ScoringProfile(BaseModel):
    user_id: str
    interests: dict[str, float]
    impression_count: int = 0

    def to_redis_hash(self) -> dict[str, str]:
        return {
            "user_id": self.user_id,
            "interests_json": json.dumps(self.interests, sort_keys=True),
            "impression_count": str(self.impression_count),
        }

    @classmethod
    def from_user_profile(cls, user: "UserProfile") -> "ScoringProfile":
        return cls(
            user_id=user.user_id,
            interests=dict(user.interests),
            impression_count=user.impression_count,
        )

    @classmethod
    def from_redis_hash(cls, values: dict[str, Any]) -> "ScoringProfile":
        return cls(
            user_id=str(values["user_id"]),
            interests=json.loads(values["interests_json"]),
            impression_count=int(values.get("impression_count", 0)),
        )


class Campaign(BaseModel):
    campaign_id: str
    geo: list[str]
    device: list[str]
    required_segments: list[str]
    any_of_segments: list[str] = Field(default_factory=list)
    none_of_segments: list[str] = Field(default_factory=list)
    card_tiers: list[str] = Field(default_factory=lambda: ["*"])
    geo_states: list[str] = Field(default_factory=list)
    geo_postal_codes: list[str] = Field(default_factory=list)
    device_types: list[str] = Field(default_factory=lambda: ["*"])
    pacing_status: str = "active"
    daily_budget_usd: float = 10000.0
    spent_today_usd: float = 0.0
    frequency_cap: int = 3
    weights: dict[str, float]
    bid: float
    freshness_boost: float = 0.0
    age_in_days: int = 0

    def to_redis_hash(self) -> dict[str, str]:
        return {
            "campaign_id": self.campaign_id,
            "geo_json": json.dumps(self.geo),
            "device_json": json.dumps(self.device),
            "required_segments_json": json.dumps(self.required_segments),
            "any_of_segments_json": json.dumps(self.any_of_segments),
            "none_of_segments_json": json.dumps(self.none_of_segments),
            "card_tiers_json": json.dumps(self.card_tiers),
            "geo_states_json": json.dumps(self.geo_states),
            "geo_postal_codes_json": json.dumps(self.geo_postal_codes),
            "device_types_json": json.dumps(self.device_types),
            "pacing_status": self.pacing_status,
            "daily_budget_usd": str(self.daily_budget_usd),
            "spent_today_usd": str(self.spent_today_usd),
            "frequency_cap": str(self.frequency_cap),
            "weights_json": json.dumps(self.weights, sort_keys=True),
            "bid": str(self.bid),
            "freshness_boost": str(self.freshness_boost),
            "age_in_days": str(self.age_in_days),
        }

    @classmethod
    def from_redis_hash(cls, values: dict[str, Any]) -> "Campaign":
        return cls(
            campaign_id=str(values["campaign_id"]),
            geo=list(json.loads(values["geo_json"])),
            device=list(json.loads(values["device_json"])),
            required_segments=list(json.loads(values["required_segments_json"])),
            any_of_segments=list(json.loads(values.get("any_of_segments_json", "[]"))),
            none_of_segments=list(json.loads(values.get("none_of_segments_json", "[]"))),
            card_tiers=list(json.loads(values.get("card_tiers_json", '["*"]'))),
            geo_states=list(json.loads(values.get("geo_states_json", "[]"))),
            geo_postal_codes=list(json.loads(values.get("geo_postal_codes_json", "[]"))),
            device_types=list(json.loads(values.get("device_types_json", '["*"]'))),
            pacing_status=str(values.get("pacing_status", "active")),
            daily_budget_usd=float(values.get("daily_budget_usd", 10000.0)),
            spent_today_usd=float(values.get("spent_today_usd", 0.0)),
            frequency_cap=int(values.get("frequency_cap", 3)),
            weights=json.loads(values["weights_json"]),
            bid=float(values["bid"]),
            freshness_boost=float(values.get("freshness_boost", 0.0)),
            age_in_days=int(values.get("age_in_days", 0)),
        )


class RankRequest(BaseModel):
    user_id: str | None = None
    identity_token: str | None = None
    mode: str = Field(default=HYBRID_MODE)
    shadow_modes: list[str] = Field(default_factory=list)
    top_k: int | None = Field(default=None, ge=1, le=25)
    max_candidates: int | None = Field(default=None, ge=10, le=1000)


class ScoredCandidate(BaseModel):
    campaign_id: str
    score: float
    score_components: dict[str, float]


class TimingBreakdown(BaseModel):
    identity_resolution_ms: float = 0.0
    profile_fetch_ms: float = 0.0
    user_fetch_ms: float
    candidate_generation_ms: float
    campaign_fetch_ms: float
    filtering_ms: float = 0.0
    validated_candidate_ms: float = 0.0
    rerank_ms: float
    total_ms: float


class ModeDiagnostics(BaseModel):
    mode: str
    candidate_ids: list[str]
    final_candidate_count: int
    eligible_count: int
    redis_round_trips: int
    sinter_ops: int = 0
    timing: TimingBreakdown
    top_campaign_ids: list[str]


class ModeOverlap(BaseModel):
    mode: str
    candidate_jaccard: float
    top_result_jaccard: float


class RankResponse(BaseModel):
    mode: str
    user_id: str
    candidate_ids: list[str]
    top_results: list[ScoredCandidate]
    timing: TimingBreakdown
    redis_round_trips: int
    diagnostics: ModeDiagnostics | None = None
    shadow_results: list[ModeDiagnostics] = Field(default_factory=list)
    mode_overlaps: list[ModeOverlap] = Field(default_factory=list)


class BatchScoreRequest(BaseModel):
    user_id: str
    candidate_ids: list[str] = Field(default_factory=list)


class BatchScoreResponse(BaseModel):
    user_id: str
    scored_candidates: list[ScoredCandidate]


class HealthResponse(BaseModel):
    status: str
    dataset_dir: str
