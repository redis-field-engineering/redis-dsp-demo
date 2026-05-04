from app.models import Campaign, UserProfile
from app.ranking import rerank_campaigns


def test_rerank_campaigns_orders_by_linear_score() -> None:
    user = UserProfile(
        user_id="u1",
        geo="US",
        device="iOS",
        age_bucket="25-34",
        interests={"camping": 0.8, "gaming": 0.2},
        segments=["camping_high"],
    )
    high = Campaign(
        campaign_id="high",
        geo=["US"],
        device=["iOS"],
        required_segments=["camping_high"],
        weights={"camping": 1.0},
        bid=1.0,
    )
    low = Campaign(
        campaign_id="low",
        geo=["US"],
        device=["iOS"],
        required_segments=["camping_high"],
        weights={"gaming": 0.1},
        bid=0.5,
    )
    ranked = rerank_campaigns(user, [low, high], top_k=2)
    assert [item.campaign_id for item in ranked] == ["high", "low"]
