from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path
from random import Random
from typing import Any

import httpx

from data.common import read_jsonl


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    position = (len(values) - 1) * pct
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def build_user_sampler(identifiers: list[dict[str, str]], hot_fraction: float = 0.2) -> callable:
    random = Random(23)
    hot_cutoff = max(1, int(len(identifiers) * hot_fraction))
    hot_users = identifiers[:hot_cutoff]
    cold_users = identifiers[hot_cutoff:]

    def sample() -> dict[str, str]:
        if cold_users and random.random() < 0.3:
            return random.choice(cold_users)
        return random.choice(hot_users)

    return sample


async def run_load_test(
    *,
    base_url: str,
    identifiers: list[dict[str, str]],
    duration_seconds: int,
    target_rps: int,
    concurrency: int,
    warmup_seconds: int = 3,
    mode: str = "concurrent",
    rank_mode: str = "hybrid_precompute_plus_realtime",
    shadow_modes: list[str] | None = None,
) -> dict[str, float | int]:
    client = httpx.AsyncClient(
        base_url=base_url,
        timeout=10.0,
        limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency),
    )
    semaphore = asyncio.Semaphore(concurrency)
    sample_user = build_user_sampler(identifiers)
    latencies: list[float] = []
    server_process_latencies: list[float] = []
    handler_latencies: list[float] = []
    identity_resolution_latencies: list[float] = []
    profile_fetch_latencies: list[float] = []
    user_fetch_latencies: list[float] = []
    candidate_generation_latencies: list[float] = []
    campaign_fetch_latencies: list[float] = []
    filtering_latencies: list[float] = []
    validated_candidate_latencies: list[float] = []
    decision_path_latencies: list[float] = []
    rerank_latencies: list[float] = []
    redis_round_trips: list[int] = []
    mode_redis_round_trips: list[int] = []
    sinter_ops: list[int] = []
    candidate_counts: list[int] = []
    eligible_counts: list[int] = []
    overlap_buckets: dict[str, list[float]] = {}
    measured_successes = 0
    measured_failures = 0
    total_successes = 0
    total_failures = 0
    started = time.perf_counter()
    interval_seconds = 1.0 / max(target_rps, 1)
    warmup_stop_at = started + max(warmup_seconds, 0)
    measured_stop_at = warmup_stop_at + duration_seconds
    requested_shadow_modes = shadow_modes or []

    async def one_request(measure: bool) -> None:
        nonlocal measured_successes, measured_failures, total_successes, total_failures
        async with semaphore:
            request_body = {
                **sample_user(),
                "mode": rank_mode,
            }
            if requested_shadow_modes:
                request_body["shadow_modes"] = requested_shadow_modes
            begin = time.perf_counter()
            try:
                response = await client.post("/rank", json=request_body)
                latency_ms = (time.perf_counter() - begin) * 1000
                if measure:
                    latencies.append(latency_ms)
                    _record_server_timings(response, server_process_latencies, handler_latencies)
                    _record_response_details(
                        response,
                        identity_resolution_latencies=identity_resolution_latencies,
                        profile_fetch_latencies=profile_fetch_latencies,
                        user_fetch_latencies=user_fetch_latencies,
                        candidate_generation_latencies=candidate_generation_latencies,
                        campaign_fetch_latencies=campaign_fetch_latencies,
                        filtering_latencies=filtering_latencies,
                        validated_candidate_latencies=validated_candidate_latencies,
                        decision_path_latencies=decision_path_latencies,
                        rerank_latencies=rerank_latencies,
                        redis_round_trips=redis_round_trips,
                        mode_redis_round_trips=mode_redis_round_trips,
                        sinter_ops=sinter_ops,
                        candidate_counts=candidate_counts,
                        eligible_counts=eligible_counts,
                        overlap_buckets=overlap_buckets,
                    )
                if response.status_code == 200:
                    total_successes += 1
                    if measure:
                        measured_successes += 1
                else:
                    total_failures += 1
                    if measure:
                        measured_failures += 1
            except Exception:
                total_failures += 1
                if measure:
                    measured_failures += 1

    try:
        if mode == "serial":
            next_request_at = started
            while True:
                now = time.perf_counter()
                if now >= measured_stop_at:
                    break
                sleep_for = next_request_at - now
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
                now = time.perf_counter()
                measure = now >= warmup_stop_at
                await one_request(measure)
                next_request_at += interval_seconds
        else:
            tasks: list[asyncio.Task[None]] = []
            next_request_at = started
            while True:
                now = time.perf_counter()
                if now >= measured_stop_at:
                    break
                sleep_for = next_request_at - now
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
                now = time.perf_counter()
                measure = now >= warmup_stop_at
                tasks.append(asyncio.create_task(one_request(measure)))
                next_request_at += interval_seconds
            if tasks:
                await asyncio.gather(*tasks)
    finally:
        await client.aclose()

    elapsed = max(time.perf_counter() - warmup_stop_at, 1e-6)
    results: dict[str, float | int] = {
        "mode": mode,
        "rank_mode": rank_mode,
        "shadow_modes": requested_shadow_modes,
        "requests": len(latencies),
        "successes": measured_successes,
        "failures": measured_failures,
        "success_rate": round(measured_successes / max(measured_successes + measured_failures, 1), 4),
        "total_successes_including_warmup": total_successes,
        "total_failures_including_warmup": total_failures,
        "throughput_rps": round(len(latencies) / elapsed, 2),
        "avg_latency_ms": round(statistics.mean(latencies), 3) if latencies else 0.0,
        "p95_latency_ms": round(percentile(latencies, 0.95), 3),
        "p99_latency_ms": round(percentile(latencies, 0.99), 3),
    }
    if server_process_latencies:
        results.update(
            {
                "server_avg_latency_ms": round(statistics.mean(server_process_latencies), 3),
                "server_p95_latency_ms": round(percentile(server_process_latencies, 0.95), 3),
                "server_p99_latency_ms": round(percentile(server_process_latencies, 0.99), 3),
            }
        )
    if handler_latencies:
        results.update(
            {
                "handler_avg_latency_ms": round(statistics.mean(handler_latencies), 3),
                "handler_p95_latency_ms": round(percentile(handler_latencies, 0.95), 3),
                "handler_p99_latency_ms": round(percentile(handler_latencies, 0.99), 3),
            }
        )
    if user_fetch_latencies:
        results.update(
            {
                "identity_resolution_avg_latency_ms": round(statistics.mean(identity_resolution_latencies), 3),
                "identity_resolution_p95_latency_ms": round(percentile(identity_resolution_latencies, 0.95), 3),
                "identity_resolution_p99_latency_ms": round(percentile(identity_resolution_latencies, 0.99), 3),
                "profile_fetch_avg_latency_ms": round(statistics.mean(profile_fetch_latencies), 3),
                "profile_fetch_p95_latency_ms": round(percentile(profile_fetch_latencies, 0.95), 3),
                "profile_fetch_p99_latency_ms": round(percentile(profile_fetch_latencies, 0.99), 3),
                "user_fetch_avg_latency_ms": round(statistics.mean(user_fetch_latencies), 3),
                "user_fetch_p95_latency_ms": round(percentile(user_fetch_latencies, 0.95), 3),
                "user_fetch_p99_latency_ms": round(percentile(user_fetch_latencies, 0.99), 3),
                "candidate_generation_avg_latency_ms": round(statistics.mean(candidate_generation_latencies), 3),
                "candidate_generation_p95_latency_ms": round(percentile(candidate_generation_latencies, 0.95), 3),
                "candidate_generation_p99_latency_ms": round(percentile(candidate_generation_latencies, 0.99), 3),
                "campaign_fetch_avg_latency_ms": round(statistics.mean(campaign_fetch_latencies), 3),
                "campaign_fetch_p95_latency_ms": round(percentile(campaign_fetch_latencies, 0.95), 3),
                "campaign_fetch_p99_latency_ms": round(percentile(campaign_fetch_latencies, 0.99), 3),
                "filtering_avg_latency_ms": round(statistics.mean(filtering_latencies), 3),
                "filtering_p95_latency_ms": round(percentile(filtering_latencies, 0.95), 3),
                "filtering_p99_latency_ms": round(percentile(filtering_latencies, 0.99), 3),
                "validated_candidate_p50_latency_ms": round(percentile(validated_candidate_latencies, 0.50), 3),
                "validated_candidate_p95_latency_ms": round(percentile(validated_candidate_latencies, 0.95), 3),
                "validated_candidate_p99_latency_ms": round(percentile(validated_candidate_latencies, 0.99), 3),
                "decision_path_p50_latency_ms": round(percentile(decision_path_latencies, 0.50), 3),
                "decision_path_p95_latency_ms": round(percentile(decision_path_latencies, 0.95), 3),
                "decision_path_p99_latency_ms": round(percentile(decision_path_latencies, 0.99), 3),
                "rerank_avg_latency_ms": round(statistics.mean(rerank_latencies), 3),
                "rerank_p95_latency_ms": round(percentile(rerank_latencies, 0.95), 3),
                "rerank_p99_latency_ms": round(percentile(rerank_latencies, 0.99), 3),
                "avg_redis_round_trips": round(statistics.mean(redis_round_trips), 3),
                "p95_redis_round_trips": round(percentile([float(value) for value in redis_round_trips], 0.95), 3),
                "avg_mode_redis_round_trips": round(statistics.mean(mode_redis_round_trips), 3),
                "p95_mode_redis_round_trips": round(percentile([float(value) for value in mode_redis_round_trips], 0.95), 3),
                "avg_sinter_ops": round(statistics.mean(sinter_ops), 3),
                "p95_sinter_ops": round(percentile([float(value) for value in sinter_ops], 0.95), 3),
                "avg_candidate_count": round(statistics.mean(candidate_counts), 3),
                "avg_eligible_count": round(statistics.mean(eligible_counts), 3),
            }
        )
    if overlap_buckets:
        results["avg_mode_overlaps"] = {
            key: round(statistics.mean(values), 4)
            for key, values in overlap_buckets.items()
            if values
        }
    return results


def _record_server_timings(
    response: httpx.Response,
    server_process_latencies: list[float],
    handler_latencies: list[float],
) -> None:
    process_header = response.headers.get("X-Process-Time-Ms")
    if process_header is not None:
        try:
            server_process_latencies.append(float(process_header))
        except ValueError:
            pass

    try:
        payload: dict[str, Any] = response.json()
    except Exception:
        return
    timing = payload.get("timing")
    if isinstance(timing, dict) and "total_ms" in timing:
        try:
            handler_latencies.append(float(timing["total_ms"]))
        except (TypeError, ValueError):
            pass


def _record_response_details(
    response: httpx.Response,
    *,
    identity_resolution_latencies: list[float],
    profile_fetch_latencies: list[float],
    user_fetch_latencies: list[float],
    candidate_generation_latencies: list[float],
    campaign_fetch_latencies: list[float],
    filtering_latencies: list[float],
    validated_candidate_latencies: list[float],
    decision_path_latencies: list[float],
    rerank_latencies: list[float],
    redis_round_trips: list[int],
    mode_redis_round_trips: list[int],
    sinter_ops: list[int],
    candidate_counts: list[int],
    eligible_counts: list[int],
    overlap_buckets: dict[str, list[float]],
) -> None:
    try:
        payload: dict[str, Any] = response.json()
    except Exception:
        return
    timing = payload.get("timing")
    if isinstance(timing, dict):
        identity_ms = _append_float(timing.get("identity_resolution_ms"), identity_resolution_latencies)
        profile_ms = _append_float(timing.get("profile_fetch_ms"), profile_fetch_latencies)
        _append_float(timing.get("user_fetch_ms"), user_fetch_latencies)
        candidate_ms = _append_float(timing.get("candidate_generation_ms"), candidate_generation_latencies)
        campaign_ms = _append_float(timing.get("campaign_fetch_ms"), campaign_fetch_latencies)
        filtering_ms = _append_float(timing.get("filtering_ms"), filtering_latencies)
        validated_ms = _append_float(timing.get("validated_candidate_ms"), validated_candidate_latencies)
        rerank_ms = _append_float(timing.get("rerank_ms"), rerank_latencies)
        if None not in (identity_ms, profile_ms, candidate_ms, campaign_ms, filtering_ms, rerank_ms):
            decision_path_latencies.append(
                identity_ms + profile_ms + candidate_ms + campaign_ms + filtering_ms + rerank_ms
            )
    _append_int(payload.get("redis_round_trips"), redis_round_trips)

    diagnostics = payload.get("diagnostics")
    if isinstance(diagnostics, dict):
        _append_int(diagnostics.get("redis_round_trips"), mode_redis_round_trips)
        _append_int(diagnostics.get("sinter_ops"), sinter_ops)
        _append_int(diagnostics.get("final_candidate_count"), candidate_counts)
        _append_int(diagnostics.get("eligible_count"), eligible_counts)

    overlaps = payload.get("mode_overlaps")
    if not isinstance(overlaps, list):
        return
    for overlap in overlaps:
        if not isinstance(overlap, dict):
            continue
        mode_name = overlap.get("mode")
        if not isinstance(mode_name, str):
            continue
        candidate_key = f"{mode_name}.candidate_jaccard"
        top_key = f"{mode_name}.top_result_jaccard"
        overlap_buckets.setdefault(candidate_key, [])
        overlap_buckets.setdefault(top_key, [])
        _append_float(overlap.get("candidate_jaccard"), overlap_buckets[candidate_key])
        _append_float(overlap.get("top_result_jaccard"), overlap_buckets[top_key])


def _append_float(value: Any, bucket: list[float]) -> float | None:
    try:
        parsed = float(value)
        bucket.append(parsed)
        return parsed
    except (TypeError, ValueError):
        return None


def _append_int(value: Any, bucket: list[int]) -> None:
    try:
        bucket.append(int(value))
    except (TypeError, ValueError):
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a configurable DSP load test")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--dataset-dir", type=Path, default=Path("data/generated/synthetic"))
    parser.add_argument("--duration-seconds", type=int, default=20)
    parser.add_argument("--target-rps", type=int, default=25)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--warmup-seconds", type=int, default=3)
    parser.add_argument("--mode", choices=["serial", "concurrent"], default="concurrent")
    parser.add_argument("--rank-mode", default="hybrid_precompute_plus_realtime")
    parser.add_argument("--shadow-mode", action="append", default=[])
    parser.add_argument("--output", type=Path, default=Path("reports/generated/loadtest.json"))
    args = parser.parse_args()

    user_path = args.dataset_dir / "maids.jsonl"
    if not user_path.exists():
        user_path = args.dataset_dir / "users.jsonl"
    users = read_jsonl(user_path)
    identifiers = [
        {"identity_token": user["identity_tokens"][0]}
        if user.get("identity_tokens")
        else {"user_id": user["user_id"]}
        for user in users
    ]
    results = asyncio.run(
        run_load_test(
            base_url=args.base_url,
            identifiers=identifiers,
            duration_seconds=args.duration_seconds,
            target_rps=args.target_rps,
            concurrency=args.concurrency,
            warmup_seconds=args.warmup_seconds,
            mode=args.mode,
            rank_mode=args.rank_mode,
            shadow_modes=args.shadow_mode,
        )
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
