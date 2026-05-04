from data.fairjob_adapter import _segment_interests, _smoothed_ctr


def test_smoothed_ctr_is_bounded() -> None:
    assert 0.0 < _smoothed_ctr(0, 10) < 1.0
    assert _smoothed_ctr(9, 10) > _smoothed_ctr(1, 10)


def test_segment_interests_prioritize_high_segments() -> None:
    interests = _segment_interests(["cat0_17", "num16_high", "num18_low"])
    assert interests["num16_high"] > interests["num18_low"]
    assert interests["cat0_17"] > 0.0
