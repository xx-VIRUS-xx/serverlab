import json
from collections import Counter

import pytest
from pydantic import ValidationError

from serverlab.experiment.schedule import build_schedule
from serverlab.experiment.spec import ExperimentSpec

CONDS = ["A", "B", "C"]


def test_same_seed_same_schedule_byte_identical() -> None:
    a = build_schedule(CONDS, 5, seed=42).model_dump_json()
    b = build_schedule(CONDS, 5, seed=42).model_dump_json()
    assert a == b


def test_different_seeds_give_different_orders() -> None:
    orders = {build_schedule(CONDS, 5, seed=s).order() for s in range(20)}
    assert len(orders) > 1


def test_every_block_has_each_condition_once() -> None:
    sched = build_schedule(CONDS, 6, seed=7)
    for block in range(6):
        names = [r.condition for r in sched.runs if r.block == block]
        assert sorted(names) == sorted(CONDS)
    assert Counter(r.condition for r in sched.runs) == {c: 6 for c in CONDS}


def test_repetition_numbers_and_indexes() -> None:
    sched = build_schedule(CONDS, 4, seed=1)
    assert [r.index for r in sched.runs] == list(range(12))
    for c in CONDS:
        assert [r.repetition for r in sched.runs if r.condition == c] == [1, 2, 3, 4]


def test_order_is_actually_randomized_not_fixed() -> None:
    first_blocks = {
        tuple(r.condition for r in build_schedule(CONDS, 1, s).runs) for s in range(50)
    }
    assert len(first_blocks) == 6  # all 3! permutations occur across seeds


def test_json_roundtrip_records_seed_and_order() -> None:
    sched = build_schedule(CONDS, 2, seed=9)
    data = json.loads(sched.model_dump_json())
    assert data["seed"] == 9 and data["method"] == "randomized-blocks"
    assert [r["condition"] for r in data["runs"]] == [r.condition for r in sched.runs]


@pytest.mark.parametrize(("conds", "reps"), [([], 1), (["A", "A"], 1), (["A"], 0)])
def test_invalid_inputs_rejected(conds: list[str], reps: int) -> None:
    with pytest.raises(ValueError):
        build_schedule(conds, reps, seed=1)


def _exp(**over: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "name": "e", "seed": 1, "repetitions": 2,
        "target": {"image": "sha256:" + "a" * 64, "env": {"X": "1"}},
        "k6_image": "grafana/k6@sha256:" + "b" * 64,
        "load": {"rate": 10, "duration_s": 3, "pre_allocated_vus": 2, "max_vus": 5,
                 "url": "http://sut:8000/w"},
        "cpus": {"sut": [0], "load": [1]},
        "conditions": [
            {"name": "A"},
            {"name": "B", "target_env": {"X": "2", "Y": "3"}, "load_rate": 20},
        ],
    }
    raw.update(over)
    return raw


def test_resolve_applies_overrides_only_to_that_condition() -> None:
    exp = ExperimentSpec.model_validate(_exp())
    a, b = exp.resolve("A"), exp.resolve("B")
    assert a.target.env == {"X": "1"} and a.load.rate == 10
    assert b.target.env == {"X": "2", "Y": "3"} and b.load.rate == 20
    assert exp.target.env == {"X": "1"}  # base untouched


def test_spec_validation() -> None:
    with pytest.raises(ValidationError, match="duplicate condition"):
        ExperimentSpec.model_validate(_exp(conditions=[{"name": "A"}, {"name": "A"}]))
    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(_exp(conditions=[{"name": "has_underscore"}]))
    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(_exp(repetitions=0))
    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(_exp(metrics={"interval_s": 0.5}))
    with pytest.raises(KeyError):
        ExperimentSpec.model_validate(_exp()).resolve("Z")
