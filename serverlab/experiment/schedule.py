"""Seeded, randomized, interleaved run schedule (plan.md s3.5).

Method: randomized blocks. Every block contains each condition exactly once in a shuffled
order, so conditions stay balanced over time (guards against drift) while the order within a
block is random, e.g. `A B | B A | A B`. The shuffle sorts by keys drawn from
`random.Random(seed).random()`, whose stream is stable across Python versions, so the same
seed gives the same schedule everywhere.
"""

import random

from pydantic import BaseModel

METHOD = "randomized-blocks"


class ScheduledRun(BaseModel):
    index: int  # 0-based position in the realized order
    condition: str
    repetition: int  # 1-based, per condition
    block: int  # 0-based


class Schedule(BaseModel):
    method: str = METHOD
    seed: int
    repetitions: int
    conditions: list[str]
    runs: list[ScheduledRun]

    def order(self) -> str:
        return " ".join(r.condition for r in self.runs)


def build_schedule(conditions: list[str], repetitions: int, seed: int) -> Schedule:
    if repetitions < 1:
        raise ValueError(f"repetitions must be >= 1, got {repetitions}")
    if not conditions:
        raise ValueError("at least one condition is required")
    if len(set(conditions)) != len(conditions):
        raise ValueError(f"duplicate conditions: {conditions}")
    rng = random.Random(seed)
    runs: list[ScheduledRun] = []
    for block in range(repetitions):
        keyed = sorted((rng.random(), name) for name in conditions)
        for _, name in keyed:
            runs.append(
                ScheduledRun(index=len(runs), condition=name, repetition=block + 1, block=block)
            )
    return Schedule(seed=seed, repetitions=repetitions, conditions=list(conditions), runs=runs)
