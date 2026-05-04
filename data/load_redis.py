from __future__ import annotations

import argparse
from pathlib import Path

from redis import Redis

from app.models import Campaign, UserProfile
from data.common import read_jsonl


def load_dataset_into_redis(client: Redis, dataset_dir: Path) -> None:
    users = [UserProfile.model_validate(item) for item in read_jsonl(dataset_dir / "users.jsonl")]
    campaigns = [Campaign.model_validate(item) for item in read_jsonl(dataset_dir / "campaigns.jsonl")]

    client.flushdb()
    pipeline = client.pipeline(transaction=False)
    pending_commands = 0
    for user in users:
        pipeline.hset(f"user:{user.user_id}", mapping=user.to_redis_hash())
        pending_commands += 1
        if pending_commands >= 1000:
            pipeline.execute()
            pipeline = client.pipeline(transaction=False)
            pending_commands = 0
    for campaign in campaigns:
        pipeline.hset(f"campaign:{campaign.campaign_id}", mapping=campaign.to_redis_hash())
        pending_commands += 1
        for geo in campaign.geo:
            pipeline.sadd(f"idx:geo:{geo}", campaign.campaign_id)
            pending_commands += 1
        for device in campaign.device:
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
    pipeline.execute()


def main() -> None:
    parser = argparse.ArgumentParser(description="Load generated DSP data into Redis")
    parser.add_argument("--redis-url", default="redis://localhost:6379/0")
    parser.add_argument("--dataset-dir", type=Path, default=Path("data/generated/synthetic"))
    args = parser.parse_args()
    client = Redis.from_url(args.redis_url, decode_responses=True)
    load_dataset_into_redis(client, args.dataset_dir)


if __name__ == "__main__":
    main()
