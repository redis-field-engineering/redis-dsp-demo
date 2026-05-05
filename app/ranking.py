from __future__ import annotations

from math import exp

from app.models import Campaign, ScoredCandidate, ScoringProfile, UserProfile


def score_campaign(user: UserProfile | ScoringProfile, campaign: Campaign) -> ScoredCandidate:
    interest_score = sum(
        user.interests.get(feature, 0.0) * weight for feature, weight in campaign.weights.items()
    )
    freshness_score = campaign.freshness_boost * exp(-campaign.age_in_days / 30.0)
    frequency_penalty = min(user.impression_count / 1000.0, 0.2)
    final_score = interest_score + campaign.bid + freshness_score - frequency_penalty
    return ScoredCandidate(
        campaign_id=campaign.campaign_id,
        score=round(final_score, 6),
        score_components={
            "interest": round(interest_score, 6),
            "bid": round(campaign.bid, 6),
            "freshness": round(freshness_score, 6),
            "frequency_penalty": round(frequency_penalty, 6),
        },
    )


def rerank_campaigns(user: UserProfile | ScoringProfile, campaigns: list[Campaign], top_k: int) -> list[ScoredCandidate]:
    ranked = [score_campaign(user, campaign) for campaign in campaigns]
    ranked.sort(key=lambda candidate: candidate.score, reverse=True)
    return ranked[:top_k]
