from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opentelemetry import metrics
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest


REQUEST_COUNT = Counter("dsp_request_total", "Total API requests", labelnames=("endpoint", "status"))
END_TO_END_LATENCY = Histogram(
    "dsp_request_latency_ms",
    "End-to-end request latency in milliseconds",
    buckets=(1, 2, 5, 10, 20, 50, 100, 200, 500),
)
CANDIDATE_LATENCY = Histogram(
    "dsp_candidate_generation_latency_ms",
    "Candidate generation latency in milliseconds",
    buckets=(0.5, 1, 2, 5, 10, 20, 50, 100),
)
RERANK_LATENCY = Histogram(
    "dsp_rerank_latency_ms",
    "Reranking latency in milliseconds",
    buckets=(0.5, 1, 2, 5, 10, 20, 50, 100),
)
CAMPAIGN_FETCH_LATENCY = Histogram(
    "dsp_campaign_fetch_latency_ms",
    "Campaign fetch latency in milliseconds",
    buckets=(0.5, 1, 2, 5, 10, 20, 50, 100),
)
REDIS_ROUND_TRIPS = Histogram(
    "dsp_redis_round_trips",
    "Redis round trips per request",
    buckets=(1, 2, 3, 4, 5, 6, 8, 10),
)
CANDIDATE_COUNT = Histogram(
    "dsp_candidate_count",
    "Candidate count per request",
    buckets=(1, 5, 10, 25, 50, 100, 200, 500),
)
TOP_SCORE = Histogram(
    "dsp_top_score",
    "Top-ranked score distribution",
    buckets=(0, 1, 2, 3, 4, 5, 6, 8, 10),
)


@dataclass
class MetricsRecorder:
    meter: Any

    def __post_init__(self) -> None:
        self.request_counter = self.meter.create_counter("dsp_request_total")
        self.candidate_latency = self.meter.create_histogram("dsp_candidate_generation_latency_ms")
        self.rerank_latency = self.meter.create_histogram("dsp_rerank_latency_ms")
        self.total_latency = self.meter.create_histogram("dsp_request_latency_ms")
        self.redis_round_trips = self.meter.create_histogram("dsp_redis_round_trips")
        self.candidate_count = self.meter.create_histogram("dsp_candidate_count")

    def record_request(self, endpoint: str, status: str, total_ms: float) -> None:
        REQUEST_COUNT.labels(endpoint=endpoint, status=status).inc()
        END_TO_END_LATENCY.observe(total_ms)
        self.request_counter.add(1, {"endpoint": endpoint, "status": status})
        self.total_latency.record(total_ms, {"endpoint": endpoint, "status": status})

    def record_rank_details(
        self,
        candidate_ms: float,
        rerank_ms: float,
        campaign_fetch_ms: float,
        redis_round_trips: int,
        candidate_count: int,
        top_score: float | None,
    ) -> None:
        CANDIDATE_LATENCY.observe(candidate_ms)
        RERANK_LATENCY.observe(rerank_ms)
        CAMPAIGN_FETCH_LATENCY.observe(campaign_fetch_ms)
        REDIS_ROUND_TRIPS.observe(redis_round_trips)
        CANDIDATE_COUNT.observe(candidate_count)
        self.candidate_latency.record(candidate_ms)
        self.rerank_latency.record(rerank_ms)
        self.redis_round_trips.record(redis_round_trips)
        self.candidate_count.record(candidate_count)
        if top_score is not None:
            TOP_SCORE.observe(top_score)


def create_metrics_recorder() -> MetricsRecorder:
    meter = metrics.get_meter("redis-dsp-demo")
    return MetricsRecorder(meter=meter)


def render_prometheus_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
