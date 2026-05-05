from __future__ import annotations

from time import perf_counter

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import ORJSONResponse

from app.config import Settings, get_settings
from app.execution import compare_modes, execute_mode
from app.metrics import create_metrics_recorder, render_prometheus_metrics
from app.models import (
    CANDIDATE_MODES,
    BatchScoreRequest,
    BatchScoreResponse,
    HealthResponse,
    MAID_BRUTEFORCE_SINTER_MODE,
    MAID_TIGHTENED_SINTER_MODE,
    RankRequest,
    RankResponse,
)
from app.ranking import score_campaign
from app.repository import RedisRepository
from app.telemetry import configure_telemetry, instrument_fastapi

settings = get_settings()
configure_telemetry(settings)
app = FastAPI(title=settings.app_name)
instrument_fastapi(app)
metrics = create_metrics_recorder()
repository = RedisRepository(settings.redis_url)


@app.middleware("http")
async def add_process_time_header(request, call_next):
    started = perf_counter()
    response = await call_next(request)
    response.headers["X-Process-Time-Ms"] = f"{((perf_counter() - started) * 1000):.3f}"
    return response


@app.on_event("startup")
def startup() -> None:
    repository.maybe_bootstrap(
        settings.dataset_dir,
        auto_generate=settings.auto_bootstrap_data,
        generator_kwargs={
            "num_users": settings.synthetic_users,
            "num_campaigns": settings.synthetic_campaigns,
            "num_interactions": settings.synthetic_interactions,
            "feature_count": settings.synthetic_feature_count,
        },
        cache_campaigns_in_memory=settings.cache_campaigns_in_memory,
    )


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok" if repository.ping() else "degraded", dataset_dir=str(settings.dataset_dir))


@app.get("/metrics")
def metrics_endpoint() -> Response:
    payload, content_type = render_prometheus_metrics()
    return Response(content=payload, media_type=content_type)


@app.post("/rank")
def rank(request: RankRequest) -> ORJSONResponse:
    request_started = perf_counter()
    resolved_id = request.user_id
    round_trips = 0
    identity_started = perf_counter()
    if request.identity_token:
        resolved_id, identity_round_trips = repository.resolve_identity(request.identity_token)
        round_trips += identity_round_trips
        if resolved_id is None:
            metrics.record_request("/rank", "not_found", (perf_counter() - request_started) * 1000)
            raise HTTPException(status_code=404, detail=f"Unknown identity_token {request.identity_token}")
    if resolved_id is None:
        raise HTTPException(status_code=422, detail="Provide either user_id or identity_token")
    identity_resolution_ms = (perf_counter() - identity_started) * 1000

    mode = request.mode or settings.default_mode
    if mode not in CANDIDATE_MODES:
        raise HTTPException(status_code=422, detail=f"Unsupported mode {mode}")
    shadow_modes = [shadow_mode for shadow_mode in request.shadow_modes if shadow_mode != mode]
    invalid_shadow = [shadow_mode for shadow_mode in shadow_modes if shadow_mode not in CANDIDATE_MODES]
    if invalid_shadow:
        raise HTTPException(status_code=422, detail=f"Unsupported shadow modes {invalid_shadow}")

    full_user_modes = {"full_realtime", MAID_BRUTEFORCE_SINTER_MODE, MAID_TIGHTENED_SINTER_MODE}
    requires_full_user = mode in full_user_modes or any(shadow_mode in full_user_modes for shadow_mode in shadow_modes)
    user = None
    scoring_profile = None
    profile_fetch_ms = 0.0
    if requires_full_user:
        profile_started = perf_counter()
        user, user_round_trips = repository.fetch_user(resolved_id)
        round_trips += user_round_trips
        if user is None:
            metrics.record_request("/rank", "not_found", (perf_counter() - request_started) * 1000)
            raise HTTPException(status_code=404, detail=f"Unknown user_id {resolved_id}")
        profile_fetch_ms = (perf_counter() - profile_started) * 1000
        scoring_profile = user
    elif mode != "full_realtime" or shadow_modes:
        profile_started = perf_counter()
        scoring_profile, profile_round_trips = repository.fetch_scoring_profile(resolved_id)
        round_trips += profile_round_trips
        if scoring_profile is None:
            metrics.record_request("/rank", "not_found", (perf_counter() - request_started) * 1000)
            raise HTTPException(status_code=404, detail=f"Unknown user_id {resolved_id}")
        profile_fetch_ms = (perf_counter() - profile_started) * 1000
    user_fetch_ms = identity_resolution_ms + profile_fetch_ms

    max_candidates = request.max_candidates or settings.max_candidates
    top_k = request.top_k or settings.top_k

    mode_execution = execute_mode(
        repository=repository,
        maid_id=resolved_id,
        mode=mode,
        top_k=top_k,
        max_candidates=max_candidates,
        strong_signal_count=settings.strong_signal_count,
        user=user,
        scoring_profile=scoring_profile,
    )
    shadow_executions = [
        execute_mode(
            repository=repository,
            maid_id=resolved_id,
            mode=shadow_mode,
            top_k=top_k,
            max_candidates=max_candidates,
            strong_signal_count=settings.strong_signal_count,
            user=user,
            scoring_profile=scoring_profile,
        )
        for shadow_mode in shadow_modes
    ]
    round_trips += mode_execution.diagnostics.redis_round_trips + sum(
        shadow.diagnostics.redis_round_trips for shadow in shadow_executions
    )
    total_ms = (perf_counter() - request_started) * 1000

    metrics.record_request("/rank", "ok", total_ms)
    metrics.record_rank_details(
        candidate_ms=mode_execution.diagnostics.timing.candidate_generation_ms,
        rerank_ms=mode_execution.diagnostics.timing.rerank_ms,
        campaign_fetch_ms=mode_execution.diagnostics.timing.campaign_fetch_ms,
        redis_round_trips=round_trips,
        candidate_count=len(mode_execution.diagnostics.candidate_ids),
        top_score=mode_execution.top_results[0].score if mode_execution.top_results else None,
    )

    primary_timing = mode_execution.diagnostics.timing.model_copy(
        update={
            "identity_resolution_ms": round(identity_resolution_ms, 3),
            "profile_fetch_ms": round(profile_fetch_ms, 3),
            "user_fetch_ms": round(user_fetch_ms, 3),
        }
    )
    payload = RankResponse(
        mode=mode,
        user_id=resolved_id,
        candidate_ids=mode_execution.diagnostics.candidate_ids,
        top_results=mode_execution.top_results,
        timing=primary_timing.model_copy(update={"total_ms": round(total_ms, 3)}),
        redis_round_trips=round_trips,
        diagnostics=mode_execution.diagnostics.model_copy(update={"timing": primary_timing}),
        shadow_results=[
            shadow.diagnostics
            for shadow in shadow_executions
        ],
        mode_overlaps=compare_modes(mode_execution, shadow_executions),
    )
    return ORJSONResponse(content=payload.model_dump())


@app.post("/batch-score")
def batch_score(request: BatchScoreRequest) -> ORJSONResponse:
    user, _ = repository.fetch_user(request.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail=f"Unknown user_id {request.user_id}")
    campaigns, _ = repository.fetch_campaigns(request.candidate_ids)
    from app.candidate import filter_campaigns_for_user

    scored = [score_campaign(user, campaign) for campaign in filter_campaigns_for_user(user, campaigns)]
    scored.sort(key=lambda candidate: candidate.score, reverse=True)
    payload = BatchScoreResponse(user_id=user.user_id, scored_candidates=scored)
    return ORJSONResponse(content=payload.model_dump())
