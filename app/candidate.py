from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from app.models import Campaign, UserProfile


def build_candidate_lookup_keys(user: UserProfile, strong_signal_count: int = 2) -> list[list[str]]:
    strong_segments = user.segments[:strong_signal_count]
    keys: list[list[str]] = []
    if strong_segments:
        first_segment_key = segment_keys(strong_segments)[0]
        keys.append([f"idx:geo:{user.geo}", f"idx:device:{user.device}", *segment_keys(strong_segments)])
        keys.append([f"idx:geo:{user.geo}", f"idx:device:{user.device}", first_segment_key])
        keys.append([f"idx:geo:{user.geo}", first_segment_key])
        keys.append([f"idx:device:{user.device}", first_segment_key])
        keys.append([first_segment_key])
    keys.append([f"idx:geo:{user.geo}", f"idx:device:{user.device}"])
    return _dedupe_key_groups(keys)


def generate_candidates_in_memory(
    user: UserProfile,
    indexes: Mapping[str, set[str]],
    max_candidates: int,
    strong_signal_count: int = 2,
) -> list[str]:
    combined: list[str] = []
    seen: set[str] = set()
    for key_group in build_candidate_lookup_keys(user, strong_signal_count=strong_signal_count):
        groups = [indexes.get(key, set()) for key in key_group]
        if not groups or any(not group for group in groups):
            continue
        for campaign_id in sorted(set.intersection(*groups)):
            if campaign_id in seen:
                continue
            combined.append(campaign_id)
            seen.add(campaign_id)
            if len(combined) >= max_candidates:
                return combined
    return combined


def filter_campaigns_for_user(user: UserProfile, campaigns: Iterable[Campaign]) -> list[Campaign]:
    user_segments = set(user.segments)
    return [
        campaign
        for campaign in campaigns
        if user.geo in campaign.geo
        and user.device in campaign.device
        and set(campaign.required_segments).issubset(user_segments)
        and (not campaign.any_of_segments or bool(user_segments.intersection(campaign.any_of_segments)))
        and not user_segments.intersection(campaign.none_of_segments)
    ]


def build_indexes(campaigns: Sequence[Campaign]) -> dict[str, set[str]]:
    indexes: dict[str, set[str]] = {}
    for campaign in campaigns:
        for geo in campaign.geo:
            indexes.setdefault(f"idx:geo:{geo}", set()).add(campaign.campaign_id)
        for device in campaign.device:
            indexes.setdefault(f"idx:device:{device}", set()).add(campaign.campaign_id)
        for segment in [*campaign.required_segments, *campaign.any_of_segments]:
            indexes.setdefault(f"idx:segment:{segment}", set()).add(campaign.campaign_id)
    return indexes


def segment_keys(segments: Iterable[str]) -> list[str]:
    return [f"idx:segment:{segment}" for segment in segments]


def _dedupe_key_groups(groups: list[list[str]]) -> list[list[str]]:
    unique: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for group in groups:
        normalized = tuple(group)
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(group)
    return unique
