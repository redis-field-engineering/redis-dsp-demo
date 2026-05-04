from experiments.evaluate import f1_at_k, ndcg_at_k, precision_at_k, recall_at_k


def test_metric_helpers() -> None:
    labels = [1, 0, 1]
    assert round(precision_at_k(labels, 2), 3) == 0.5
    assert round(recall_at_k(labels, 3, 3), 3) == 0.667
    assert round(f1_at_k(0.5, 0.3333333333), 3) == 0.4
    assert ndcg_at_k([3.0, 2.0, 0.0], [3.0, 2.0, 1.0], 3) > 0.0
