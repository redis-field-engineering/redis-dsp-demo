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
    results = generate_candidates_in_memory(user, indexes, max_candidates=10, strong_signal_count=1)
    assert results[0] == "c1"
    assert "c2" in results


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
