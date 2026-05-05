from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from app.models import Campaign, UserProfile
from data.common import CARD_TIERS, DEVICE_OSES, DEVICE_TYPES, GEOS, STATES


def build_candidate_lookup_keys(
    user: UserProfile,
    strong_signal_count: int = 2,
    strategy: str = "union_probe",
) -> list[list[str]]:
    if strategy == "naive":
        return build_naive_candidate_lookup_keys(user, strong_signal_count=strong_signal_count)
    if strategy == "union_probe":
        return build_union_probe_candidate_lookup_keys(user, strong_signal_count=strong_signal_count)
    raise ValueError(f"Unsupported candidate lookup strategy: {strategy}")


def build_union_probe_candidate_lookup_keys(
    user: UserProfile,
    strong_signal_count: int = 2,
) -> list[list[str]]:
    strong_segments = user.segments[:strong_signal_count]
    strict_base = _base_lookup_keys(user)
    keys: list[list[str]] = []
    for segment_key in segment_keys(strong_segments):
        keys.append([*strict_base, segment_key])
    keys.append(strict_base)
    return _dedupe_key_groups(keys)


def build_naive_candidate_lookup_keys(
    user: UserProfile,
    strong_signal_count: int = 2,
) -> list[list[str]]:
    strong_segments = user.segments[:strong_signal_count]
    strict_base = _base_lookup_keys(user)
    keys: list[list[str]] = []
    if strong_segments:
        segment_group = segment_keys(strong_segments)
        first_segment_key = segment_group[0]
        keys.extend(
            [
                [*strict_base, *segment_group],
                [*strict_base, first_segment_key],
            ]
        )
    keys.append(strict_base)
    return _dedupe_key_groups(keys)


def generate_candidates_in_memory(
    user: UserProfile,
    indexes: Mapping[str, set[str]],
    max_candidates: int,
    strong_signal_count: int = 2,
    strategy: str = "union_probe",
) -> list[str]:
    if strategy == "union_probe":
        probe_results: list[list[str]] = []
        for key_group in build_candidate_lookup_keys(
            user,
            strong_signal_count=strong_signal_count,
            strategy=strategy,
        ):
            groups = [indexes.get(key, set()) for key in key_group]
            if not groups or any(not group for group in groups):
                continue
            probe_results.append(sorted(set.intersection(*groups)))
        return _merge_probe_results(probe_results, max_candidates=max_candidates)

    combined: list[str] = []
    seen: set[str] = set()
    for key_group in build_candidate_lookup_keys(
        user,
        strong_signal_count=strong_signal_count,
        strategy=strategy,
    ):
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
        if _matches_dimension(user.geo, campaign.geo)
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
    ]


def build_indexes(campaigns: Sequence[Campaign]) -> dict[str, set[str]]:
    indexes: dict[str, set[str]] = {}
    for campaign in campaigns:
        for geo in _expand_dimension(campaign.geo, GEOS):
            indexes.setdefault(f"idx:geo:{geo}", set()).add(campaign.campaign_id)
        for state in _expand_dimension(campaign.geo_states or ["*"], STATES):
            indexes.setdefault(f"idx:state:{state}", set()).add(campaign.campaign_id)
        for card_tier in _expand_dimension(campaign.card_tiers, CARD_TIERS):
            indexes.setdefault(f"idx:card_tier:{card_tier}", set()).add(campaign.campaign_id)
        for device in _expand_dimension(campaign.device, DEVICE_OSES):
            indexes.setdefault(f"idx:device:{device}", set()).add(campaign.campaign_id)
        for device_type in _expand_dimension(campaign.device_types, DEVICE_TYPES):
            indexes.setdefault(f"idx:device_type:{device_type}", set()).add(campaign.campaign_id)
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


def _matches_dimension(user_value: str, campaign_values: Iterable[str]) -> bool:
    values = set(campaign_values)
    return user_value in values or "*" in values


def _matches_optional_list(user_value: str, campaign_values: Iterable[str]) -> bool:
    values = set(campaign_values)
    return not values or user_value in values or "*" in values


def _merge_probe_results(probe_results: list[list[str]], *, max_candidates: int) -> list[str]:
    combined: list[str] = []
    seen: set[str] = set()
    indexes = [0 for _ in probe_results]
    while len(combined) < max_candidates:
        progressed = False
        for probe_index, candidates in enumerate(probe_results):
            while indexes[probe_index] < len(candidates):
                campaign_id = candidates[indexes[probe_index]]
                indexes[probe_index] += 1
                if campaign_id in seen:
                    continue
                seen.add(campaign_id)
                combined.append(campaign_id)
                progressed = True
                break
            if len(combined) >= max_candidates:
                return combined
        if not progressed:
            return combined
    return combined


def _expand_dimension(values: Iterable[str], vocabulary: Sequence[str]) -> list[str]:
    concrete_values = list(values)
    if not concrete_values or "*" in concrete_values:
        return list(vocabulary)
    return concrete_values


def _base_lookup_keys(user: UserProfile) -> list[str]:
    keys = [
        f"idx:card_tier:{user.card_tier}",
        f"idx:geo:{user.geo}",
        f"idx:device_type:{user.device_type}",
        f"idx:device:{user.device}",
    ]
    if user.state:
        keys.insert(2, f"idx:state:{user.state}")
    return keys
