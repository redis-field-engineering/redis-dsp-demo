"""Shared boilerplate for the demo notebook sequence (notebooks 1–6).

Each notebook does roughly the same setup: locate the repo root, put it on
`sys.path` so `app.*` and `data.*` imports work, connect to the local Redis,
and verify the dataset is loaded. Centralising it here keeps each notebook's
focus on the *demonstration* rather than the boilerplate.

Default Redis URL points at the docker-compose port (`6381`). Override with
`DEMO_REDIS_URL` if you have Redis running elsewhere.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from time import perf_counter

DEFAULT_REDIS_URL = os.environ.get("DEMO_REDIS_URL", "redis://localhost:6381/0")


def find_repo_root(start: Path | None = None) -> Path:
    start = (start or Path.cwd()).resolve()
    for path in [start, *start.parents]:
        if (path / "pyproject.toml").exists():
            return path
    raise RuntimeError("Could not locate repo root (no pyproject.toml found)")


def add_repo_to_path() -> Path:
    repo_root = find_repo_root()
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    return repo_root


def connect_redis(url: str = DEFAULT_REDIS_URL):
    """Open a Redis connection and verify the prototype dataset is loaded.

    Returns the Redis client. Prints a one-line status banner so the SE knows
    they are pointed at the right cluster and can see the dataset metadata.
    """
    add_repo_to_path()
    from redis import Redis

    client = Redis.from_url(url, decode_responses=True)
    if not client.ping():
        raise RuntimeError(f"Redis at {url} did not answer PING")
    if not client.exists("meta:dataset_loaded"):
        raise RuntimeError(
            f"Redis at {url} has no dataset loaded. From the repo root run:\n"
            f"  make up && python3 -m data.load_redis "
            f"--redis-url {url} --dataset-dir data/generated/synthetic"
        )
    user_count = client.get("meta:user_count")
    campaign_count = client.get("meta:campaign_count")
    candidate_version = client.get("meta:precomputed_candidate_version")
    print(
        f"connected to {url}\n"
        f"  users={user_count}  campaigns={campaign_count}  "
        f"precompute_version={candidate_version}"
    )
    return client


class StepTimer:
    """Tiny context manager that measures per-step latency for the bid path.

    Usage:
        timer = StepTimer()
        with timer.step('identity_resolution'):
            ...
        print(timer.summary())
    """

    def __init__(self) -> None:
        self.steps: list[tuple[str, float]] = []

    class _Span:
        def __init__(self, owner: "StepTimer", name: str) -> None:
            self._owner = owner
            self._name = name

        def __enter__(self) -> "StepTimer._Span":
            self._t0 = perf_counter()
            return self

        def __exit__(self, *exc) -> None:
            elapsed_ms = (perf_counter() - self._t0) * 1000
            self._owner.steps.append((self._name, elapsed_ms))

    def step(self, name: str) -> "_Span":
        return StepTimer._Span(self, name)

    def total_ms(self) -> float:
        return sum(ms for _, ms in self.steps)

    def summary(self) -> str:
        lines = [f"{name:>32}  {ms:7.3f} ms" for name, ms in self.steps]
        lines.append("-" * 44)
        lines.append(f"{'TOTAL':>32}  {self.total_ms():7.3f} ms")
        return "\n".join(lines)
