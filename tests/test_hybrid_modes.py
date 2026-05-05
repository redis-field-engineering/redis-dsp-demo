from pathlib import Path

from experiments.evaluate import evaluate_synthetic_modes
from data.synthetic import generate_dataset


def test_evaluate_synthetic_modes_smoke(tmp_path: Path) -> None:
    dataset_dir = tmp_path / "synthetic_hybrid"
    generate_dataset(
        dataset_dir,
        num_users=80,
        num_campaigns=120,
        num_interactions=2000,
        feature_count=8,
        seed=19,
    )

    results = evaluate_synthetic_modes(dataset_dir, sample_users=40, top_k=5, precomputed_limit=50)

    assert results["users_evaluated"] > 0
    modes = results["modes"]
    assert modes["full_realtime"]["candidate_count"] >= 120
    assert modes["precomputed_segment"]["candidate_count"] > 0
    assert modes["hybrid_precompute_plus_realtime"]["candidate_count"] > 0
