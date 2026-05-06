from pathlib import Path

from experiments.evaluate import evaluate_synthetic_modes
from data.synthetic import generate_dataset


def test_evaluate_synthetic_modes_smoke(tmp_path: Path) -> None:
    dataset_dir = tmp_path / "synthetic_hybrid"
    generate_dataset(
        dataset_dir,
        num_users=400,
        num_campaigns=400,
        num_interactions=8000,
        feature_count=10,
        seed=19,
    )

    results = evaluate_synthetic_modes(dataset_dir, sample_users=200, top_k=5, precomputed_limit=80)

    assert results["users_evaluated"] > 0
    modes = results["modes"]
    assert modes["full_realtime"]["candidate_count"] >= 400
    assert modes["precomputed_segment"]["candidate_count"] > 0
    assert modes["hybrid_precompute_plus_realtime"]["candidate_count"] > 0
    assert modes["hybrid_bitmap_taxonomy"]["candidate_count"] > 0
    # The new mode is at least as strict as hybrid_bitmap_gating: it adds the
    # taxonomy_filter check on top of the same retrieval path, so its eligible
    # set must be a subset.
    assert modes["hybrid_bitmap_taxonomy"]["eligible_count"] <= modes["hybrid_bitmap_gating"]["eligible_count"]
