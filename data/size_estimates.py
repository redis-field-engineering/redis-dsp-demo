from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path

from app.models import (
    FULL_REALTIME_MODE,
    HYBRID_BITMAP_MODE,
    HYBRID_BITMAP_TAXONOMY_MODE,
    HYBRID_MODE,
    MAID_BRUTEFORCE_SINTER_MODE,
    MAID_TIGHTENED_SINTER_MODE,
    PRECOMPUTED_SEGMENT_MODE,
    Campaign,
    ScoringProfile,
    UserProfile,
)
from data.common import CARD_TIERS, DEVICE_OSES, DEVICE_TYPES, GEOS, STATES, read_jsonl


@dataclass(frozen=True)
class KeyspaceBytes:
    maid_full: int
    maid_hot: int
    identity: int
    audience: int
    campaign: int
    campaign_state: int
    fcap: int
    indexes: int
    bitmaps: int
    meta: int


@dataclass(frozen=True)
class MethodFootprint:
    small_scale_bytes: int
    scaled_up_bytes: int


# Scaled-up keyspace assumptions at 500 M MAIDs / ~5 K ads.
#
# Audience and fcap are the two estimates that swing the totals. Their
# derivations are documented in §1.1 of reports/full_scale_gcp_test_spec.md;
# the short version is:
#
#   audience ≈ 200 IDs × 9 B JSON / MAID + ~80 B Redis STRING overhead
#            ≈ 1.9 KB / MAID × 500 M ≈ ~1 TB
#     The 200-ID cap reflects the customer PDF's static-targeting reduction
#     (~900 ads pass static filters per MAID; cap at 200 keeps the top by
#     bid/freshness score and trades long-tail recall for footprint).
#
#   fcap     ≈ 18 B per (campaign_id, count) entry in listpack repr.
#     Entries per MAID scale with bid rate × win rate × 86400 / active MAIDs.
#     The constant below corresponds to the 100 K bid/s tier (~9 entries /
#     MAID at peak end-of-day, before the daily reset at midnight UTC).
#     Scales linearly with bid rate; see §1.4 for the per-tier table.
SCALED_UP_KEYSPACES = KeyspaceBytes(
    maid_full=int(500_000_000 * 11 * 1024),  # slimmed MAID hash; mostly interests_json at ~500 taxonomy labels
    maid_hot=int(500_000_000 * 9 * 1024),  # scoring-only profile used by the precomputed / hybrid paths
    identity=45 * 1024**3,  # one canonical identity->MAID entry per MAID at ~90 B/entry
    audience=1024 * 1024**3,  # precomputed aud:{maid} lists, ~1.9 KB / MAID at cap=200
    campaign=256 * 1024**2,  # 5K ads with targeting + ranking metadata
    campaign_state=256 * 1024**2,  # mutable pacing / budget state
    fcap=50 * 1024**3,  # sparse per-MAID frequency hashes (sized for 100 K bid/s; see §1.4)
    indexes=8 * 1024**2,  # ~150 K total set members across geo/state/device/segment indexes
    bitmaps=8 * 1024**2,  # bm:active / bm:pacing_ok / bm:budget_ok / bm:servable
    meta=16 * 1024**2,
)


def method_keyspace_bytes(dataset_dir: Path) -> dict[str, MethodFootprint]:
    current = _measure_current_keyspaces(dataset_dir)
    return {
        FULL_REALTIME_MODE: MethodFootprint(
            small_scale_bytes=_method_total_bytes(
                current,
                maid_full=True,
                identity=True,
                campaign=True,
                campaign_state=True,
                fcap=True,
                meta=True,
            ),
            scaled_up_bytes=_method_total_bytes(
                SCALED_UP_KEYSPACES,
                maid_full=True,
                identity=True,
                campaign=True,
                campaign_state=True,
                fcap=True,
                meta=True,
            ),
        ),
        MAID_BRUTEFORCE_SINTER_MODE: MethodFootprint(
            small_scale_bytes=_method_total_bytes(
                current,
                maid_full=True,
                identity=True,
                campaign=True,
                campaign_state=True,
                fcap=True,
                indexes=True,
                meta=True,
            ),
            scaled_up_bytes=_method_total_bytes(
                SCALED_UP_KEYSPACES,
                maid_full=True,
                identity=True,
                campaign=True,
                campaign_state=True,
                fcap=True,
                indexes=True,
                meta=True,
            ),
        ),
        MAID_TIGHTENED_SINTER_MODE: MethodFootprint(
            small_scale_bytes=_method_total_bytes(
                current,
                maid_full=True,
                identity=True,
                campaign=True,
                campaign_state=True,
                fcap=True,
                indexes=True,
                meta=True,
            ),
            scaled_up_bytes=_method_total_bytes(
                SCALED_UP_KEYSPACES,
                maid_full=True,
                identity=True,
                campaign=True,
                campaign_state=True,
                fcap=True,
                indexes=True,
                meta=True,
            ),
        ),
        PRECOMPUTED_SEGMENT_MODE: MethodFootprint(
            small_scale_bytes=_method_total_bytes(
                current,
                maid_hot=True,
                identity=True,
                audience=True,
                campaign=True,
                campaign_state=True,
                fcap=True,
                meta=True,
            ),
            scaled_up_bytes=_method_total_bytes(
                SCALED_UP_KEYSPACES,
                maid_hot=True,
                identity=True,
                audience=True,
                campaign=True,
                campaign_state=True,
                fcap=True,
                meta=True,
            ),
        ),
        HYBRID_MODE: MethodFootprint(
            small_scale_bytes=_method_total_bytes(
                current,
                maid_hot=True,
                identity=True,
                audience=True,
                campaign=True,
                campaign_state=True,
                fcap=True,
                meta=True,
            ),
            scaled_up_bytes=_method_total_bytes(
                SCALED_UP_KEYSPACES,
                maid_hot=True,
                identity=True,
                audience=True,
                campaign=True,
                campaign_state=True,
                fcap=True,
                meta=True,
            ),
        ),
        HYBRID_BITMAP_MODE: MethodFootprint(
            small_scale_bytes=_method_total_bytes(
                current,
                maid_hot=True,
                identity=True,
                audience=True,
                campaign=True,
                fcap=True,
                bitmaps=True,
                meta=True,
            ),
            scaled_up_bytes=_method_total_bytes(
                SCALED_UP_KEYSPACES,
                maid_hot=True,
                identity=True,
                audience=True,
                campaign=True,
                fcap=True,
                bitmaps=True,
                meta=True,
            ),
        ),
        HYBRID_BITMAP_TAXONOMY_MODE: MethodFootprint(
            small_scale_bytes=_method_total_bytes(
                current,
                maid_hot=True,
                identity=True,
                audience=True,
                campaign=True,
                fcap=True,
                bitmaps=True,
                meta=True,
            ),
            scaled_up_bytes=_method_total_bytes(
                SCALED_UP_KEYSPACES,
                maid_hot=True,
                identity=True,
                audience=True,
                campaign=True,
                fcap=True,
                bitmaps=True,
                meta=True,
            ),
        ),
    }


def human_bytes(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    value = float(num_bytes)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            if value >= 100:
                return f"{value:.0f} {unit}"
            if value >= 10:
                return f"{value:.1f} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{num_bytes} B"


def _measure_current_keyspaces(dataset_dir: Path) -> KeyspaceBytes:
    user_path = dataset_dir / "maids.jsonl"
    if not user_path.exists():
        user_path = dataset_dir / "users.jsonl"
    users = [UserProfile.model_validate(item) for item in read_jsonl(user_path)]
    campaigns = [Campaign.model_validate(item) for item in read_jsonl(dataset_dir / "campaigns.jsonl")]
    identity_rows = read_jsonl(dataset_dir / "identity_map.jsonl")
    audience_rows = read_jsonl(dataset_dir / "user_candidates.jsonl")

    maid_full = sum(_hash_bytes(f"maid:{user.user_id}", user.to_redis_hash()) for user in users)
    maid_hot = sum(
        _hash_bytes(
            f"maid_hot:{user.user_id}",
            ScoringProfile.from_user_profile(user).to_redis_hash(),
        )
        for user in users
    )
    identity = sum(_string_bytes(f"identity:{row['identity_token']}", row["user_id"]) for row in identity_rows)
    audience = sum(
        _string_bytes(f"aud:{row['user_id']}", json.dumps(row["candidate_ids"], separators=(",", ":")))
        for row in audience_rows
    )
    campaign = sum(_hash_bytes(f"campaign:{campaign.campaign_id}", campaign.to_redis_hash()) for campaign in campaigns)
    campaign_state = sum(
        _hash_bytes(
            f"campaign_state:{campaign.campaign_id}",
            {
                "campaign_id": campaign.campaign_id,
                "pacing_status": campaign.pacing_status,
                "daily_budget_usd": str(campaign.daily_budget_usd),
                "spent_today_usd": str(campaign.spent_today_usd),
                "frequency_cap": str(campaign.frequency_cap),
                "status": "active" if campaign.pacing_status == "active" else "paused",
            },
        )
        for campaign in campaigns
    )
    fcap = sum(
        _hash_bytes(
            f"fcap:{user.user_id}",
            {campaign_id: str(int(count)) for campaign_id, count in user.frequency_history.items()},
        )
        for user in users
        if user.frequency_history
    )
    indexes = _index_bytes(campaigns)
    bitmaps = _bitmap_bytes(campaigns)
    meta = sum(
        _string_bytes(key, value)
        for key, value in {
            "meta:dataset_loaded": "1",
            "meta:user_count": str(len(users)),
            "meta:campaign_count": str(len(campaigns)),
            "meta:identity_count": str(len(identity_rows)),
            "meta:precomputed_candidate_version": "v1",
        }.items()
    )
    return KeyspaceBytes(
        maid_full=maid_full,
        maid_hot=maid_hot,
        identity=identity,
        audience=audience,
        campaign=campaign,
        campaign_state=campaign_state,
        fcap=fcap,
        indexes=indexes,
        bitmaps=bitmaps,
        meta=meta,
    )


def _method_total_bytes(
    keyspaces: KeyspaceBytes,
    *,
    maid_full: bool = False,
    maid_hot: bool = False,
    identity: bool = False,
    audience: bool = False,
    campaign: bool = False,
    campaign_state: bool = False,
    fcap: bool = False,
    indexes: bool = False,
    bitmaps: bool = False,
    meta: bool = False,
) -> int:
    total = 0
    total += keyspaces.maid_full if maid_full else 0
    total += keyspaces.maid_hot if maid_hot else 0
    total += keyspaces.identity if identity else 0
    total += keyspaces.audience if audience else 0
    total += keyspaces.campaign if campaign else 0
    total += keyspaces.campaign_state if campaign_state else 0
    total += keyspaces.fcap if fcap else 0
    total += keyspaces.indexes if indexes else 0
    total += keyspaces.bitmaps if bitmaps else 0
    total += keyspaces.meta if meta else 0
    return total


def _hash_bytes(key: str, mapping: dict[str, str]) -> int:
    if not mapping:
        return 0
    return len(key) + sum(len(field) + len(str(value)) for field, value in mapping.items())


def _string_bytes(key: str, value: str) -> int:
    return len(key) + len(value)


def _index_bytes(campaigns: list[Campaign]) -> int:
    indexes: dict[str, set[str]] = {}
    for campaign in campaigns:
        for geo in _expand_dimension(campaign.geo, GEOS):
            indexes.setdefault(f"idx:geo:{geo}", set()).add(campaign.campaign_id)
        for state in _expand_dimension(campaign.geo_states or ["*"], STATES):
            indexes.setdefault(f"idx:state:{state}", set()).add(campaign.campaign_id)
        for card_tier in _expand_dimension(campaign.card_tiers, CARD_TIERS):
            indexes.setdefault(f"idx:card_tier:{card_tier}", set()).add(campaign.campaign_id)
        for device_type in _expand_dimension(campaign.device_types, DEVICE_TYPES):
            indexes.setdefault(f"idx:device_type:{device_type}", set()).add(campaign.campaign_id)
        for device in _expand_dimension(campaign.device, DEVICE_OSES):
            indexes.setdefault(f"idx:device:{device}", set()).add(campaign.campaign_id)
        for segment in [*campaign.required_segments, *campaign.any_of_segments]:
            indexes.setdefault(f"idx:segment:{segment}", set()).add(campaign.campaign_id)
    return sum(len(key) + sum(len(member) for member in members) for key, members in indexes.items())


def _bitmap_bytes(campaigns: list[Campaign]) -> int:
    if not campaigns:
        return 0
    max_bit = max(int(campaign.campaign_id.removeprefix("c")) for campaign in campaigns)
    bits_per_bitmap = max_bit + 1
    bytes_per_bitmap = math.ceil(bits_per_bitmap / 8)
    bitmap_keys = ["bm:active", "bm:pacing_ok", "bm:budget_ok", "bm:servable"]
    return sum(len(key) + bytes_per_bitmap for key in bitmap_keys)


def _expand_dimension(values: list[str], vocabulary: list[str]) -> list[str]:
    if not values or "*" in values:
        return list(vocabulary)
    return values
