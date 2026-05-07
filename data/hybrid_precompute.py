from __future__ import annotations

from app.models import Campaign, UserProfile


def build_hybrid_precompute_artifacts(
    *,
    users: list[UserProfile],
    campaigns: list[Campaign],
    candidate_limit: int = 150,
    version: str = "v1",
) -> dict[str, object]:
    user_candidates = [
        {
            "user_id": user.user_id,
            "candidate_ids": _precompute_user_candidates(
                user=user,
                campaigns=campaigns,
                candidate_limit=candidate_limit,
            ),
        }
        for user in users
    ]

    return {
        "version": version,
        "user_candidates": user_candidates,
    }


def _precompute_user_candidates(
    *,
    user: UserProfile,
    campaigns: list[Campaign],
    candidate_limit: int,
) -> list[str]:
    eligible = [
        campaign
        for campaign in campaigns
        if _matches_static_targeting(user, campaign)
    ]
    ranked = sorted(eligible, key=_campaign_static_score, reverse=True)
    return [campaign.campaign_id for campaign in ranked[:candidate_limit]]


def _matches_static_targeting(user: UserProfile, campaign: Campaign) -> bool:
    """Match the static targeting fields used by the precompute.

    Note: the per-campaign `taxonomy_filter` is *not* evaluated here. Taxonomy
    scores can drift between batch precompute and bid time (an online
    feedback path may rewrite individual labels between batches), so the
    filter must be evaluated online against the current `maid:{maid_id}`
    interests (read via HMGET of the scoring fields). The per-MAID candidate
    list therefore over-approximates by the taxonomy_filter pass rate; the
    online taxonomy mode (`hybrid_bitmap_taxonomy`) closes that gap.
    """
    user_segments = set(user.segments)
    return (
        _matches_dimension(user.geo, campaign.geo)
        and _matches_optional_list(user.state, campaign.geo_states)
        and _matches_optional_list(user.postal_code, campaign.geo_postal_codes)
        and _matches_dimension(user.device, campaign.device)
        and _matches_dimension(user.device_type, campaign.device_types)
        and _matches_dimension(user.card_tier, campaign.card_tiers)
        and set(campaign.required_segments).issubset(user_segments)
        and (not campaign.any_of_segments or bool(user_segments.intersection(campaign.any_of_segments)))
        and not user_segments.intersection(campaign.none_of_segments)
    )


def _campaign_static_score(campaign: Campaign) -> float:
    return float(campaign.bid) + float(campaign.freshness_boost) + max(campaign.weights.values(), default=0.0)


def _matches_dimension(user_value: str, campaign_values: list[str]) -> bool:
    values = set(campaign_values)
    return user_value in values or "*" in values


def _matches_optional_list(user_value: str, campaign_values: list[str]) -> bool:
    values = set(campaign_values)
    return not values or user_value in values or "*" in values
