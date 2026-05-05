from app.candidate import build_indexes, filter_campaigns_for_user, generate_candidates_in_memory
from app.models import Campaign, UserProfile


def test_generate_candidates_uses_strong_segments() -> None:
    user = UserProfile(
        user_id="u1",
        geo="US",
        device="iOS",
        age_bucket="25-34",
        interests={"camping": 0.9, "gaming": 0.6},
        segments=["camping_high", "gaming_medium"],
    )
    campaigns = [
        Campaign(
            campaign_id="c1",
            geo=["US"],
            device=["iOS"],
            required_segments=["camping_high"],
            weights={"camping": 1.0},
            bid=2.0,
        ),
        Campaign(
            campaign_id="c2",
            geo=["US"],
            device=["iOS"],
            required_segments=["gaming_medium"],
            weights={"gaming": 1.0},
            bid=1.0,
        ),
    ]
    indexes = build_indexes(campaigns)
    results = generate_candidates_in_memory(user, indexes, max_candidates=10, strong_signal_count=2)
    assert results[:2] == ["c1", "c2"]


def test_filter_campaigns_supports_any_of_and_none_of() -> None:
    user = UserProfile(
        user_id="u2",
        geo="US",
        device="iOS",
        age_bucket="25-34",
        interests={"camping": 0.8},
        segments=["camping_high", "travel_high"],
    )
    matching = Campaign(
        campaign_id="match",
        geo=["US"],
        device=["iOS"],
        required_segments=[],
        any_of_segments=["travel_high", "family_high"],
        none_of_segments=["gaming_high"],
        weights={"travel_high": 0.8},
        bid=1.0,
    )
    blocked = Campaign(
        campaign_id="blocked",
        geo=["US"],
        device=["iOS"],
        required_segments=[],
        any_of_segments=["travel_high"],
        none_of_segments=["camping_high"],
        weights={"camping_high": 0.8},
        bid=1.0,
    )
    filtered = filter_campaigns_for_user(user, [matching, blocked])
    assert [campaign.campaign_id for campaign in filtered] == ["match"]


def test_filter_campaigns_supports_wildcard_geo_and_device() -> None:
    user = UserProfile(
        user_id="u3",
        geo="US",
        device="iOS",
        age_bucket="25-34",
        interests={"travel": 0.7},
        segments=["travel_high"],
    )
    wildcard = Campaign(
        campaign_id="wildcard",
        geo=["*"],
        device=["*"],
        required_segments=[],
        any_of_segments=["travel_high"],
        weights={"travel_high": 1.0},
        bid=1.0,
    )
    filtered = filter_campaigns_for_user(user, [wildcard])
    assert [campaign.campaign_id for campaign in filtered] == ["wildcard"]


def test_union_probe_recovers_second_segment_but_naive_drops_it() -> None:
    user = UserProfile(
        user_id="u4",
        geo="US",
        device="iOS",
        age_bucket="25-34",
        interests={"camping": 0.9, "travel": 0.85},
        segments=["camping_high", "travel_high"],
    )
    campaigns = [
        Campaign(
            campaign_id=f"camping_{index}",
            geo=["US"],
            device=["iOS"],
            required_segments=[],
            any_of_segments=["camping_high"],
            weights={"camping": 1.0},
            bid=1.0,
        )
        for index in range(4)
    ] + [
        Campaign(
            campaign_id="travel",
            geo=["US"],
            device=["iOS"],
            required_segments=[],
            any_of_segments=["travel_high"],
            weights={"travel": 1.0},
            bid=1.0,
        ),
    ]
    indexes = build_indexes(campaigns)
    naive = generate_candidates_in_memory(
        user,
        indexes,
        max_candidates=4,
        strong_signal_count=2,
        strategy="naive",
    )
    union_probe = generate_candidates_in_memory(
        user,
        indexes,
        max_candidates=4,
        strong_signal_count=2,
        strategy="union_probe",
    )
    assert naive == ["camping_0", "camping_1", "camping_2", "camping_3"]
    assert "travel" in union_probe
