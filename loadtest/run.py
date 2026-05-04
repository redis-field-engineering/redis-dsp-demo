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


def build_user_sampler(user_ids: list[str], hot_fraction: float = 0.2) -> callable:
    random = Random(23)
    hot_cutoff = max(1, int(len(user_ids) * hot_fraction))
    hot_users = user_ids[:hot_cutoff]
    cold_users = user_ids[hot_cutoff:]

    def sample() -> str:
        if cold_users and random.random() < 0.3:
            return random.choice(cold_users)
        return random.choice(hot_users)

    return sample


async def run_load_test(
    *,
    base_url: str,
    user_ids: list[str],
    duration_seconds: int,
    target_rps: int,
    concurrency: int,
    warmup_seconds: int = 3,
    mode: str = "concurrent",
) -> dict[str, float | int]:
    client = httpx.AsyncClient(
        base_url=base_url,
        timeout=10.0,
        limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency),
    )
    semaphore = asyncio.Semaphore(concurrency)
    sample_user = build_user_sampler(user_ids)
    latencies: list[float] = []
    server_process_latencies: list[float] = []
    handler_latencies: list[float] = []
    measured_successes = 0
    measured_failures = 0
    total_successes = 0
    total_failures = 0
    started = time.perf_counter()
    interval_seconds = 1.0 / max(target_rps, 1)
    warmup_stop_at = started + max(warmup_seconds, 0)
    measured_stop_at = warmup_stop_at + duration_seconds

    async def one_request(measure: bool) -> None:
        nonlocal measured_successes, measured_failures, total_successes, total_failures
        async with semaphore:
            user_id = sample_user()
            begin = time.perf_counter()
            try:
                response = await client.post("/rank", json={"user_id": user_id})
                latency_ms = (time.perf_counter() - begin) * 1000
                if measure:
                    latencies.append(latency_ms)
                    _record_server_timings(response, server_process_latencies, handler_latencies)
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a configurable DSP load test")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--dataset-dir", type=Path, default=Path("data/generated/synthetic"))
    parser.add_argument("--duration-seconds", type=int, default=20)
    parser.add_argument("--target-rps", type=int, default=25)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--warmup-seconds", type=int, default=3)
    parser.add_argument("--mode", choices=["serial", "concurrent"], default="concurrent")
    parser.add_argument("--output", type=Path, default=Path("reports/generated/loadtest.json"))
    args = parser.parse_args()

    users = read_jsonl(args.dataset_dir / "users.jsonl")
    results = asyncio.run(
        run_load_test(
            base_url=args.base_url,
            user_ids=[user["user_id"] for user in users],
            duration_seconds=args.duration_seconds,
            target_rps=args.target_rps,
            concurrency=args.concurrency,
            warmup_seconds=args.warmup_seconds,
            mode=args.mode,
        )
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
