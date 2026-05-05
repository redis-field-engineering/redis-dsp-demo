from __future__ import annotations

import argparse
from pathlib import Path

from redis import Redis

from app.models import Campaign, UserProfile
from data.common import CARD_TIERS, DEVICE_OSES, DEVICE_TYPES, GEOS, STATES, read_jsonl


def load_dataset_into_redis(client: Redis, dataset_dir: Path) -> None:
    user_path = dataset_dir / "maids.jsonl"
    if not user_path.exists():
        user_path = dataset_dir / "users.jsonl"
    users = [UserProfile.model_validate(item) for item in read_jsonl(user_path)]
    campaigns = [Campaign.model_validate(item) for item in read_jsonl(dataset_dir / "campaigns.jsonl")]
    identity_rows = read_jsonl(dataset_dir / "identity_map.jsonl") if (dataset_dir / "identity_map.jsonl").exists() else []

    client.flushdb()
    pipeline = client.pipeline(transaction=False)
    pending_commands = 0
    for user in users:
        pipeline.hset(f"maid:{user.user_id}", mapping=user.to_redis_hash())
        pending_commands += 1
        if pending_commands >= 1000:
            pipeline.execute()
            pipeline = client.pipeline(transaction=False)
            pending_commands = 0
    for row in identity_rows:
        pipeline.set(f"identity:{row['identity_token']}", row["user_id"])
        pending_commands += 1
        if pending_commands >= 1000:
            pipeline.execute()
            pipeline = client.pipeline(transaction=False)
            pending_commands = 0
    for campaign in campaigns:
        pipeline.hset(f"campaign:{campaign.campaign_id}", mapping=campaign.to_redis_hash())
        pending_commands += 1
        for geo in _expand_dimension(campaign.geo, GEOS):
            pipeline.sadd(f"idx:geo:{geo}", campaign.campaign_id)
            pending_commands += 1
        for state in _expand_dimension(campaign.geo_states or ["*"], STATES):
            pipeline.sadd(f"idx:state:{state}", campaign.campaign_id)
            pending_commands += 1
        for card_tier in _expand_dimension(campaign.card_tiers, CARD_TIERS):
            pipeline.sadd(f"idx:card_tier:{card_tier}", campaign.campaign_id)
            pending_commands += 1
        for device_type in _expand_dimension(campaign.device_types, DEVICE_TYPES):
            pipeline.sadd(f"idx:device_type:{device_type}", campaign.campaign_id)
            pending_commands += 1
        for device in _expand_dimension(campaign.device, DEVICE_OSES):
            pipeline.sadd(f"idx:device:{device}", campaign.campaign_id)
            pending_commands += 1
        for segment in [*campaign.required_segments, *campaign.any_of_segments]:
            pipeline.sadd(f"idx:segment:{segment}", campaign.campaign_id)
            pending_commands += 1
        if pending_commands >= 1000:
            pipeline.execute()
            pipeline = client.pipeline(transaction=False)
            pending_commands = 0
    pipeline.set("meta:dataset_loaded", "1")
    pipeline.set("meta:user_count", len(users))
    pipeline.set("meta:campaign_count", len(campaigns))
    pipeline.set("meta:identity_count", len(identity_rows))
    pipeline.execute()


def _expand_dimension(values: list[str], vocabulary: list[str]) -> list[str]:
    if not values or "*" in values:
        return list(vocabulary)
    return values


def main() -> None:
    parser = argparse.ArgumentParser(description="Load generated DSP data into Redis")
    parser.add_argument("--redis-url", default="redis://localhost:6379/0")
    parser.add_argument("--dataset-dir", type=Path, default=Path("data/generated/synthetic"))
    args = parser.parse_args()
    client = Redis.from_url(args.redis_url, decode_responses=True)
    load_dataset_into_redis(client, args.dataset_dir)


if __name__ == "__main__":
    main()
