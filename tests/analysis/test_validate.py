from serverlab.analysis.validate import simulate_coverage


def test_coverage_simulation_is_seeded_and_bounded() -> None:
    a = simulate_coverage("normal", 10, seed=1, trials=60, resamples=400)
    assert a == simulate_coverage("normal", 10, seed=1, trials=60, resamples=400)
    assert 0.0 <= a.coverage <= 1.0 and a.graded and a.passed is not None


def test_n5_is_reported_not_graded() -> None:
    r = simulate_coverage("normal", 5, seed=1, trials=20, resamples=200)
    assert not r.graded and r.passed is None


def test_median_interval_is_rank_based_so_populations_agree() -> None:
    # exp() preserves ranks, so the median-CI coverage of normal and lognormal is identical
    n = simulate_coverage("normal", 7, seed=3, trials=80, resamples=300)
    ln = simulate_coverage("lognormal", 7, seed=3, trials=80, resamples=300)
    assert n.coverage == ln.coverage
