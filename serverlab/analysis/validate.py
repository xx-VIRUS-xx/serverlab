"""Tool validation checks behind `serverlab validate` (plan.md s3.12).

Pass rules are fixed in docs/predictions.md (committed before the first run) and are not
retuned here. Every number is returned, pass or fail.
"""

import numpy as np
from pydantic import BaseModel

from serverlab.analysis.stats import DEFAULT_CONFIDENCE, DEFAULT_RESAMPLES, bootstrap_median_ci

COVERAGE_TRIALS = 2000
COVERAGE_BAND = (0.90, 0.99)  # docs/predictions.md rule 3
COVERAGE_PASS_N = (10, 20)  # n = 5 is reported only
COVERAGE_REPORT_N = (5, 10, 20)
POPULATIONS = ("normal", "lognormal")
# True medians of the simulated populations: N(0,1) -> 0; lognormal(mu=0, sigma=1) -> 1.
TRUE_MEDIAN = {"normal": 0.0, "lognormal": 1.0}


class CoverageResult(BaseModel):
    population: str
    n: int
    trials: int
    resamples: int
    coverage: float
    mc_standard_error: float
    graded: bool
    passed: bool | None  # None when not graded


class CheckResult(BaseModel):
    name: str
    passed: bool
    detail: str


def _draw(rng: np.random.Generator, population: str, n: int) -> list[float]:
    x = rng.standard_normal(n)
    return [float(v) for v in (x if population == "normal" else np.exp(x))]


def simulate_coverage(
    population: str,
    n: int,
    seed: int,
    trials: int = COVERAGE_TRIALS,
    resamples: int = DEFAULT_RESAMPLES,
) -> CoverageResult:
    """Fraction of simulated experiments whose 95% bootstrap CI contains the true median.

    Uses the production `bootstrap_median_ci` code path, with a derived seed per trial.
    """
    truth = TRUE_MEDIAN[population]
    rng = np.random.default_rng(seed)
    hits = 0
    for t in range(trials):
        sample = _draw(rng, population, n)
        ci = bootstrap_median_ci(sample, seed=seed * 1_000_003 + t, resamples=resamples)
        hits += ci.contains(truth)
    cov = hits / trials
    graded = n in COVERAGE_PASS_N
    return CoverageResult(
        population=population, n=n, trials=trials, resamples=resamples, coverage=cov,
        mc_standard_error=float(np.sqrt(cov * (1 - cov) / trials)), graded=graded,
        passed=(COVERAGE_BAND[0] <= cov <= COVERAGE_BAND[1]) if graded else None,
    )


def coverage_checks(seed: int = 20261014, trials: int = COVERAGE_TRIALS) -> list[CheckResult]:
    out: list[CheckResult] = []
    for population in POPULATIONS:
        for n in COVERAGE_REPORT_N:
            r = simulate_coverage(population, n, seed + n, trials)
            verdict = "reported only" if not r.graded else ("PASS" if r.passed else "FAIL")
            out.append(CheckResult(
                name=f"ci-coverage {population} n={n}",
                passed=True if r.passed is None else r.passed,
                detail=(
                    f"coverage={r.coverage:.4f} (nominal {DEFAULT_CONFIDENCE}, "
                    f"MC s.e. {r.mc_standard_error:.4f}, {r.trials} trials x {r.resamples} "
                    f"resamples; band [{COVERAGE_BAND[0]}, {COVERAGE_BAND[1]}]) {verdict}"
                ),
            ))
    return out
