from app.candidate import build_candidate_lookup_keys, build_indexes, filter_campaigns_for_user, generate_candidates_in_memory
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


def test_union_probe_uses_compact_probe_plan() -> None:
    user = UserProfile(
        user_id="u1b",
        geo="US",
        state="CO",
        device="iOS",
        device_type="mobile",
        card_tier="Gold",
        age_bucket="25-34",
        interests={"camping": 0.9, "travel": 0.7},
        segments=["camping_high", "travel_high"],
    )
    keys = build_candidate_lookup_keys(user, strong_signal_count=2, strategy="union_probe")
    assert keys == [
        [
            "idx:card_tier:Gold",
            "idx:geo:US",
            "idx:state:CO",
            "idx:device_type:mobile",
            "idx:device:iOS",
            "idx:segment:camping_high",
        ],
        [
            "idx:card_tier:Gold",
            "idx:geo:US",
            "idx:state:CO",
            "idx:device_type:mobile",
            "idx:device:iOS",
            "idx:segment:travel_high",
        ],
        [
            "idx:card_tier:Gold",
            "idx:geo:US",
            "idx:state:CO",
            "idx:device_type:mobile",
            "idx:device:iOS",
        ]
    ]


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
        state="CO",
        postal_code="80202",
        device="iOS",
        device_type="mobile",
        age_bucket="25-34",
        interests={"travel": 0.7},
        segments=["travel_high"],
        card_tier="Gold",
    )
    wildcard = Campaign(
        campaign_id="wildcard",
        geo=["*"],
        device=["*"],
        card_tiers=["*"],
        device_types=["*"],
        required_segments=[],
        any_of_segments=["travel_high"],
        weights={"travel_high": 1.0},
        bid=1.0,
    )
    filtered = filter_campaigns_for_user(user, [wildcard])
    assert [campaign.campaign_id for campaign in filtered] == ["wildcard"]


def test_filter_campaigns_applies_card_geo_device_pacing_and_frequency() -> None:
    user = UserProfile(
        user_id="u3b",
        geo="US",
        state="NY",
        postal_code="10001",
        device="iOS",
        device_type="mobile",
        card_tier="Platinum",
        age_bucket="25-34",
        interests={"travel": 0.7},
        segments=["travel_high"],
        frequency_history={"capped": 2},
    )
    matching = Campaign(
        campaign_id="match",
        geo=["US"],
        geo_states=["NY"],
        geo_postal_codes=["10001"],
        device=["iOS"],
        device_types=["mobile"],
        card_tiers=["Platinum", "WorldElite"],
        pacing_status="active",
        daily_budget_usd=100.0,
        spent_today_usd=55.0,
        frequency_cap=3,
        required_segments=[],
        weights={"travel": 1.0},
        bid=1.0,
    )
    capped = matching.model_copy(update={"campaign_id": "capped", "frequency_cap": 2})
    paused = matching.model_copy(update={"campaign_id": "paused", "pacing_status": "paused"})
    wrong_state = matching.model_copy(update={"campaign_id": "wrong_state", "geo_states": ["CA"]})
    wrong_device_type = matching.model_copy(update={"campaign_id": "wrong_device_type", "device_types": ["desktop"]})
    wrong_card = matching.model_copy(update={"campaign_id": "wrong_card", "card_tiers": ["Gold"]})
    filtered = filter_campaigns_for_user(
        user,
        [matching, capped, paused, wrong_state, wrong_device_type, wrong_card],
    )
    assert [campaign.campaign_id for campaign in filtered] == ["match"]


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
