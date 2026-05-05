import json

from app.main import rank
from app.models import Campaign, RankRequest, ScoringProfile
import pytest


class StubRepository:
    def __init__(self) -> None:
        self.fetch_user_calls = 0

    def resolve_identity(self, identity_token: str) -> tuple[str | None, int]:
        assert identity_token == "tok-1"
        return "maid-1", 1

    def fetch_user(self, user_id: str):
        self.fetch_user_calls += 1
        raise AssertionError("hybrid mode should not fetch the full MAID profile")

    def fetch_scoring_profile(self, user_id: str):
        assert user_id == "maid-1"
        return ScoringProfile(user_id="maid-1", interests={"travel": 0.8}, impression_count=12), 1

    def fetch_user_candidates(self, user_id: str, *, limit: int) -> tuple[list[str], int]:
        assert user_id == "maid-1"
        return ["camp-1", "camp-2"][:limit], 1

    def fetch_bitmap_gated_user_candidates(self, user_id: str, *, limit: int) -> tuple[list[str], int]:
        assert user_id == "maid-1"
        return ["camp-1"][:limit], 1

    def fetch_campaigns(self, campaign_ids):
        campaigns = [
            Campaign(
                campaign_id="camp-1",
                geo=["US"],
                device=["iOS"],
                required_segments=[],
                weights={"travel": 0.6},
                bid=1.2,
                freshness_boost=0.1,
                age_in_days=4,
            ),
            Campaign(
                campaign_id="camp-2",
                geo=["US"],
                device=["iOS"],
                required_segments=[],
                pacing_status="paused",
                weights={"travel": 0.3},
                bid=0.8,
                freshness_boost=0.0,
                age_in_days=7,
            ),
        ]
        return [campaign for campaign in campaigns if campaign.campaign_id in campaign_ids], 1

    def fetch_campaign_states(self, campaign_ids):
        return (
            {
                "camp-1": {
                    "pacing_status": "active",
                    "daily_budget_usd": "1000",
                    "spent_today_usd": "10",
                    "frequency_cap": "3",
                },
                "camp-2": {
                    "pacing_status": "paused",
                    "daily_budget_usd": "1000",
                    "spent_today_usd": "10",
                    "frequency_cap": "3",
                },
            },
            1,
        )

    def fetch_frequency_caps(self, user_id: str, campaign_ids):
        assert user_id == "maid-1"
        return {"camp-1": 0, "camp-2": 0}, 1


@pytest.mark.parametrize("mode", ["hybrid_precompute_plus_realtime", "hybrid_bitmap_gating"])
def test_hybrid_rank_skips_full_profile_fetch(monkeypatch, mode: str) -> None:
    stub_repository = StubRepository()
    monkeypatch.setattr("app.main.repository", stub_repository)

    response = rank(
        RankRequest(
            identity_token="tok-1",
            mode=mode,
            top_k=5,
            max_candidates=10,
        )
    )
    payload = json.loads(response.body)

    assert stub_repository.fetch_user_calls == 0
    assert payload["user_id"] == "maid-1"
    if mode == "hybrid_bitmap_gating":
        assert payload["candidate_ids"] == ["camp-1"]
    else:
        assert payload["candidate_ids"] == ["camp-1", "camp-2"]
    assert payload["diagnostics"]["eligible_count"] == 1
    assert payload["timing"]["identity_resolution_ms"] >= 0.0
    assert payload["timing"]["profile_fetch_ms"] >= 0.0
