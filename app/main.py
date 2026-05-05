from __future__ import annotations

from time import perf_counter

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import ORJSONResponse

from app.candidate import filter_campaigns_for_user
from app.config import Settings, get_settings
from app.metrics import create_metrics_recorder, render_prometheus_metrics
from app.models import BatchScoreRequest, BatchScoreResponse, HealthResponse, RankRequest, RankResponse, TimingBreakdown
from app.ranking import score_campaign, rerank_campaigns
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
    user_started = perf_counter()
    resolved_id = request.user_id
    round_trips = 0
    if request.identity_token:
        resolved_id, identity_round_trips = repository.resolve_identity(request.identity_token)
        round_trips += identity_round_trips
        if resolved_id is None:
            metrics.record_request("/rank", "not_found", (perf_counter() - request_started) * 1000)
            raise HTTPException(status_code=404, detail=f"Unknown identity_token {request.identity_token}")
    if resolved_id is None:
        raise HTTPException(status_code=422, detail="Provide either user_id or identity_token")
    user, user_round_trips = repository.fetch_user(resolved_id)
    round_trips += user_round_trips
    user_fetch_ms = (perf_counter() - user_started) * 1000
    if user is None:
        metrics.record_request("/rank", "not_found", (perf_counter() - request_started) * 1000)
        raise HTTPException(status_code=404, detail=f"Unknown user_id {resolved_id}")

    max_candidates = request.max_candidates or settings.max_candidates
    top_k = request.top_k or settings.top_k

    candidate_started = perf_counter()
    candidate_ids, candidate_round_trips = repository.generate_candidates(
        user,
        max_candidates=max_candidates,
        strong_signal_count=settings.strong_signal_count,
    )
    candidate_ms = (perf_counter() - candidate_started) * 1000
    round_trips += candidate_round_trips

    campaign_started = perf_counter()
    campaigns, campaign_round_trips = repository.fetch_campaigns(candidate_ids)
    campaign_fetch_ms = (perf_counter() - campaign_started) * 1000
    round_trips += campaign_round_trips
    eligible_campaigns = filter_campaigns_for_user(user, campaigns)

    rerank_started = perf_counter()
    top_results = rerank_campaigns(user, eligible_campaigns, top_k=top_k)
    rerank_ms = (perf_counter() - rerank_started) * 1000
    total_ms = (perf_counter() - request_started) * 1000

    metrics.record_request("/rank", "ok", total_ms)
    metrics.record_rank_details(
        candidate_ms=candidate_ms,
        rerank_ms=rerank_ms,
        campaign_fetch_ms=campaign_fetch_ms,
        redis_round_trips=round_trips,
        candidate_count=len(candidate_ids),
        top_score=top_results[0].score if top_results else None,
    )

    payload = RankResponse(
        user_id=user.user_id,
        candidate_ids=candidate_ids,
        top_results=top_results,
        timing=TimingBreakdown(
            user_fetch_ms=round(user_fetch_ms, 3),
            candidate_generation_ms=round(candidate_ms, 3),
            campaign_fetch_ms=round(campaign_fetch_ms, 3),
            rerank_ms=round(rerank_ms, 3),
            total_ms=round(total_ms, 3),
        ),
        redis_round_trips=round_trips,
    )
    return ORJSONResponse(content=payload.model_dump())


@app.post("/batch-score")
def batch_score(request: BatchScoreRequest) -> ORJSONResponse:
    user, _ = repository.fetch_user(request.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail=f"Unknown user_id {request.user_id}")
    campaigns, _ = repository.fetch_campaigns(request.candidate_ids)
    scored = [score_campaign(user, campaign) for campaign in filter_campaigns_for_user(user, campaigns)]
    scored.sort(key=lambda candidate: candidate.score, reverse=True)
    payload = BatchScoreResponse(user_id=user.user_id, scored_candidates=scored)
    return ORJSONResponse(content=payload.model_dump())
