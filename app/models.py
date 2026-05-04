from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field


class UserProfile(BaseModel):
    user_id: str
    geo: str
    device: str
    age_bucket: str
    interests: dict[str, float]
    segments: list[str]
    impression_count: int = 0

    def to_redis_hash(self) -> dict[str, str]:
        return {
            "user_id": self.user_id,
            "geo": self.geo,
            "device": self.device,
            "age_bucket": self.age_bucket,
            "interests_json": json.dumps(self.interests, sort_keys=True),
            "segments_json": json.dumps(self.segments),
            "impression_count": str(self.impression_count),
        }

    @classmethod
    def from_redis_hash(cls, values: dict[str, Any]) -> "UserProfile":
        return cls(
            user_id=str(values["user_id"]),
            geo=str(values["geo"]),
            device=str(values["device"]),
            age_bucket=str(values["age_bucket"]),
            interests=json.loads(values["interests_json"]),
            segments=list(json.loads(values["segments_json"])),
            impression_count=int(values.get("impression_count", 0)),
        )


class Campaign(BaseModel):
    campaign_id: str
    geo: list[str]
    device: list[str]
    required_segments: list[str]
    any_of_segments: list[str] = Field(default_factory=list)
    none_of_segments: list[str] = Field(default_factory=list)
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
            weights=json.loads(values["weights_json"]),
            bid=float(values["bid"]),
            freshness_boost=float(values.get("freshness_boost", 0.0)),
            age_in_days=int(values.get("age_in_days", 0)),
        )


class RankRequest(BaseModel):
    user_id: str
    top_k: int | None = Field(default=None, ge=1, le=25)
    max_candidates: int | None = Field(default=None, ge=10, le=1000)


class ScoredCandidate(BaseModel):
    campaign_id: str
    score: float
    score_components: dict[str, float]


class TimingBreakdown(BaseModel):
    user_fetch_ms: float
    candidate_generation_ms: float
    campaign_fetch_ms: float
    rerank_ms: float
    total_ms: float


class RankResponse(BaseModel):
    user_id: str
    candidate_ids: list[str]
    top_results: list[ScoredCandidate]
    timing: TimingBreakdown
    redis_round_trips: int


class BatchScoreRequest(BaseModel):
    user_id: str
    candidate_ids: list[str] = Field(default_factory=list)


class BatchScoreResponse(BaseModel):
    user_id: str
    scored_candidates: list[ScoredCandidate]


class HealthResponse(BaseModel):
    status: str
    dataset_dir: str
