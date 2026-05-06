from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter

from app.candidate import evaluate_taxonomy_filter
from app.models import (
    FULL_REALTIME_MODE,
    HYBRID_MODE,
    HYBRID_BITMAP_MODE,
    HYBRID_BITMAP_TAXONOMY_MODE,
    MAID_BRUTEFORCE_SINTER_MODE,
    MAID_TIGHTENED_SINTER_MODE,
    PRECOMPUTED_SEGMENT_MODE,
    Campaign,
    ModeDiagnostics,
    ModeOverlap,
    ScoringProfile,
    ScoredCandidate,
    TimingBreakdown,
    UserProfile,
)
from app.ranking import rerank_campaigns
from app.repository import RedisRepository


@dataclass
class ModeExecution:
    diagnostics: ModeDiagnostics
    top_results: list[ScoredCandidate]


FILTERED_SINTER_MODES = {MAID_BRUTEFORCE_SINTER_MODE, MAID_TIGHTENED_SINTER_MODE}


def execute_mode(
    *,
    repository: RedisRepository,
    maid_id: str,
    mode: str,
    top_k: int,
    max_candidates: int,
    strong_signal_count: int,
    user: UserProfile | None = None,
    scoring_profile: ScoringProfile | None = None,
) -> ModeExecution:
    started = perf_counter()
    candidate_started = perf_counter()
    candidate_ids, candidate_round_trips, sinter_ops = _retrieve_candidate_ids(
        repository=repository,
        maid_id=maid_id,
        mode=mode,
        max_candidates=max_candidates,
        strong_signal_count=strong_signal_count,
        user=user,
    )
    candidate_ms = (perf_counter() - candidate_started) * 1000

    data_started = perf_counter()
    campaigns, campaign_round_trips = _fetch_mode_campaigns(
        repository=repository,
        mode=mode,
        candidate_ids=candidate_ids,
    )
    if mode in {HYBRID_BITMAP_MODE, HYBRID_BITMAP_TAXONOMY_MODE}:
        states = {}
        state_round_trips = 0
    else:
        states, state_round_trips = repository.fetch_campaign_states(candidate_ids)
    frequency_user_id = user.user_id if user is not None else maid_id
    fcap_counts, fcap_round_trips = repository.fetch_frequency_caps(frequency_user_id, candidate_ids)
    data_fetch_ms = (perf_counter() - data_started) * 1000

    filtering_started = perf_counter()
    runtime_campaigns = _apply_campaign_states(campaigns, states) if states else campaigns
    if mode == FULL_REALTIME_MODE or mode in FILTERED_SINTER_MODES:
        if user is None:
            raise ValueError(f"{mode} mode requires a user profile")
        from app.candidate import filter_campaigns_for_user

        runtime_user = user.model_copy(update={"frequency_history": fcap_counts})
        eligible = filter_campaigns_for_user(runtime_user, runtime_campaigns)
    else:
        runtime_user = scoring_profile
        if mode == HYBRID_BITMAP_TAXONOMY_MODE:
            if scoring_profile is None:
                raise ValueError(f"{mode} mode requires a scoring profile for taxonomy evaluation")
            eligible = _frequency_and_taxonomy_filter(
                runtime_campaigns,
                fcap_counts,
                scoring_profile.interests,
            )
        elif mode == HYBRID_BITMAP_MODE:
            eligible = _frequency_only_filter(runtime_campaigns, fcap_counts)
        else:
            eligible = _minimal_live_filter(runtime_campaigns, fcap_counts)
    filtering_ms = (perf_counter() - filtering_started) * 1000

    rerank_started = perf_counter()
    if runtime_user is None:
        raise ValueError(f"{mode} mode requires scoring signals")
    top_results = rerank_campaigns(runtime_user, eligible, top_k=top_k)
    rerank_ms = (perf_counter() - rerank_started) * 1000
    total_ms = (perf_counter() - started) * 1000
    validated_candidate_ms = candidate_ms + data_fetch_ms + filtering_ms

    diagnostics = ModeDiagnostics(
        mode=mode,
        candidate_ids=candidate_ids,
        final_candidate_count=len(candidate_ids),
        eligible_count=len(eligible),
        redis_round_trips=candidate_round_trips + campaign_round_trips + state_round_trips + fcap_round_trips,
        sinter_ops=sinter_ops,
        timing=TimingBreakdown(
            identity_resolution_ms=0.0,
            profile_fetch_ms=0.0,
            user_fetch_ms=0.0,
            candidate_generation_ms=round(candidate_ms, 3),
            campaign_fetch_ms=round(data_fetch_ms, 3),
            filtering_ms=round(filtering_ms, 3),
            validated_candidate_ms=round(validated_candidate_ms, 3),
            rerank_ms=round(rerank_ms, 3),
            total_ms=round(total_ms, 3),
        ),
        top_campaign_ids=[item.campaign_id for item in top_results],
    )
    return ModeExecution(diagnostics=diagnostics, top_results=top_results)


def compare_modes(primary: ModeExecution, shadows: list[ModeExecution]) -> list[ModeOverlap]:
    primary_candidate_set = set(primary.diagnostics.candidate_ids)
    primary_top_set = set(primary.diagnostics.top_campaign_ids)
    overlaps: list[ModeOverlap] = []
    for shadow in shadows:
        overlaps.append(
            ModeOverlap(
                mode=shadow.diagnostics.mode,
                candidate_jaccard=round(_jaccard(primary_candidate_set, set(shadow.diagnostics.candidate_ids)), 4),
                top_result_jaccard=round(_jaccard(primary_top_set, set(shadow.diagnostics.top_campaign_ids)), 4),
            )
        )
    return overlaps


def _retrieve_candidate_ids(
    *,
    repository: RedisRepository,
    maid_id: str,
    mode: str,
    max_candidates: int,
    strong_signal_count: int,
    user: UserProfile | None,
) -> tuple[list[str], int, int]:
    if mode == FULL_REALTIME_MODE:
        return repository.all_campaign_ids(), 0, 0
    if mode in {PRECOMPUTED_SEGMENT_MODE, HYBRID_MODE}:
        candidate_ids, candidate_round_trips = repository.fetch_user_candidates(
            maid_id,
            limit=max_candidates,
        )
        return candidate_ids, candidate_round_trips, 0
    if mode in {HYBRID_BITMAP_MODE, HYBRID_BITMAP_TAXONOMY_MODE}:
        candidate_ids, candidate_round_trips = repository.fetch_bitmap_gated_user_candidates(
            maid_id,
            limit=max_candidates,
        )
        return candidate_ids, candidate_round_trips, 0

    if user is None:
        raise ValueError(f"{mode} mode requires a user profile")
    strategy = "union_probe"
    if mode == MAID_BRUTEFORCE_SINTER_MODE:
        strategy = "legacy_union_probe"
    elif mode == MAID_TIGHTENED_SINTER_MODE:
        strategy = "union_probe"
    candidate_ids, round_trips, sinter_ops = repository.generate_candidates(
        user,
        max_candidates=max_candidates,
        strong_signal_count=strong_signal_count,
        strategy=strategy,
    )
    return candidate_ids, round_trips, sinter_ops


def _fetch_mode_campaigns(
    *,
    repository: RedisRepository,
    mode: str,
    candidate_ids: list[str],
) -> tuple[list[Campaign], int]:
    if mode == FULL_REALTIME_MODE:
        campaigns = list(repository.campaign_cache.values()) if repository.campaign_cache else []
        if campaigns:
            return campaigns, 0
        return repository.fetch_campaigns(candidate_ids)
    return repository.fetch_campaigns(candidate_ids)


def _apply_campaign_states(
    campaigns: list[Campaign],
    states: dict[str, dict[str, str]],
) -> list[Campaign]:
    runtime_campaigns: list[Campaign] = []
    for campaign in campaigns:
        state = states.get(campaign.campaign_id)
        if not state:
            runtime_campaigns.append(campaign)
            continue
        runtime_campaigns.append(
            campaign.model_copy(
                update={
                    "pacing_status": state.get("pacing_status", campaign.pacing_status),
                    "daily_budget_usd": float(state.get("daily_budget_usd", campaign.daily_budget_usd)),
                    "spent_today_usd": float(state.get("spent_today_usd", campaign.spent_today_usd)),
                    "frequency_cap": int(state.get("frequency_cap", campaign.frequency_cap)),
                }
            )
        )
    return runtime_campaigns


def _minimal_live_filter(
    campaigns: list[Campaign],
    fcap_counts: dict[str, int],
) -> list[Campaign]:
    eligible: list[Campaign] = []
    for campaign in campaigns:
        if campaign.pacing_status != "active":
            continue
        if campaign.spent_today_usd >= campaign.daily_budget_usd:
            continue
        if fcap_counts.get(campaign.campaign_id, 0) >= campaign.frequency_cap:
            continue
        eligible.append(campaign)
    return eligible


def _frequency_only_filter(
    campaigns: list[Campaign],
    fcap_counts: dict[str, int],
) -> list[Campaign]:
    eligible: list[Campaign] = []
    for campaign in campaigns:
        if fcap_counts.get(campaign.campaign_id, 0) >= campaign.frequency_cap:
            continue
        eligible.append(campaign)
    return eligible


def _frequency_and_taxonomy_filter(
    campaigns: list[Campaign],
    fcap_counts: dict[str, int],
    interests: dict[str, float],
) -> list[Campaign]:
    eligible: list[Campaign] = []
    for campaign in campaigns:
        if fcap_counts.get(campaign.campaign_id, 0) >= campaign.frequency_cap:
            continue
        if not evaluate_taxonomy_filter(campaign.taxonomy_filter, interests):
            continue
        eligible.append(campaign)
    return eligible


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)
