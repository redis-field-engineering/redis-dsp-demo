from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from redis import Redis

from app.candidate import build_candidate_lookup_keys, _merge_probe_results
from app.models import Campaign, UserProfile
from data.load_redis import load_dataset_into_redis
from data.synthetic import ensure_synthetic_dataset


class RedisRepository:
    def __init__(self, redis_url: str):
        self.client = Redis.from_url(redis_url, decode_responses=True)
        self.campaign_cache: dict[str, Campaign] = {}

    def ping(self) -> bool:
        return bool(self.client.ping())

    def maybe_bootstrap(
        self,
        dataset_dir: Path,
        *,
        auto_generate: bool,
        generator_kwargs: dict[str, int],
        cache_campaigns_in_memory: bool,
    ) -> None:
        if auto_generate:
            ensure_synthetic_dataset(dataset_dir=dataset_dir, **generator_kwargs)
        if self.client.exists("meta:dataset_loaded"):
            if cache_campaigns_in_memory:
                self._load_campaign_cache()
            return
        load_dataset_into_redis(self.client, dataset_dir)
        if cache_campaigns_in_memory:
            self._load_campaign_cache()

    def fetch_user(self, user_id: str) -> tuple[UserProfile | None, int]:
        payload = self.client.hgetall(f"maid:{user_id}")
        if not payload:
            payload = self.client.hgetall(f"user:{user_id}")
        if not payload:
            return None, 1
        return UserProfile.from_redis_hash(payload), 1

    def resolve_identity(self, identity_token: str) -> tuple[str | None, int]:
        maid_id = self.client.get(f"identity:{identity_token}")
        return (str(maid_id) if maid_id else None), 1

    def fetch_campaigns(self, campaign_ids: Sequence[str]) -> tuple[list[Campaign], int]:
        if not campaign_ids:
            return [], 0
        if self.campaign_cache:
            return [self.campaign_cache[campaign_id] for campaign_id in campaign_ids if campaign_id in self.campaign_cache], 0
        pipeline = self.client.pipeline(transaction=False)
        for campaign_id in campaign_ids:
            pipeline.hgetall(f"campaign:{campaign_id}")
        payloads = pipeline.execute()
        campaigns = [Campaign.from_redis_hash(payload) for payload in payloads if payload]
        return campaigns, 1

    def _load_campaign_cache(self) -> None:
        campaign_ids = sorted(
            key.removeprefix("campaign:")
            for key in self.client.scan_iter(match="campaign:*", count=1000)
        )
        if not campaign_ids:
            self.campaign_cache = {}
            return
        pipeline = self.client.pipeline(transaction=False)
        for campaign_id in campaign_ids:
            pipeline.hgetall(f"campaign:{campaign_id}")
        payloads = pipeline.execute()
        self.campaign_cache = {
            campaign.campaign_id: campaign
            for payload in payloads
            if payload
            for campaign in [Campaign.from_redis_hash(payload)]
        }

    def generate_candidates(
        self,
        user: UserProfile,
        *,
        max_candidates: int,
        strong_signal_count: int,
        strategy: str = "union_probe",
    ) -> tuple[list[str], int]:
        if strategy == "union_probe":
            probe_results: list[list[str]] = []
            round_trips = 0
            for key_group in build_candidate_lookup_keys(
                user,
                strong_signal_count=strong_signal_count,
                strategy=strategy,
            ):
                result = self.client.sinter(key_group)
                round_trips += 1
                if result:
                    probe_results.append(sorted(result))
            return _merge_probe_results(probe_results, max_candidates=max_candidates), round_trips

        combined: list[str] = []
        seen: set[str] = set()
        round_trips = 0
        for key_group in build_candidate_lookup_keys(
            user,
            strong_signal_count=strong_signal_count,
            strategy=strategy,
        ):
            result = self.client.sinter(key_group)
            round_trips += 1
            for campaign_id in sorted(result):
                if campaign_id in seen:
                    continue
                seen.add(campaign_id)
                combined.append(campaign_id)
                if len(combined) >= max_candidates:
                    return combined, round_trips
        return combined, round_trips
