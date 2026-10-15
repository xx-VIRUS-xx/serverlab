import pytest
from pydantic import ValidationError

from serverlab.topology.cpuset import CpuAssignment, assign_cpusets


def test_disjoint_sets_are_accepted_and_formatted() -> None:
    a = assign_cpusets(8, [0, 1, 2, 3], [4, 5])
    assert a.sut_cpuset() == "0,1,2,3"
    assert a.load_cpuset() == "4,5"
    assert a.unassigned == (6, 7)


def test_unsorted_input_formats_sorted() -> None:
    a = assign_cpusets(4, [3, 1], [0])
    assert a.sut_cpuset() == "1,3"


def test_overlap_is_rejected() -> None:
    with pytest.raises(ValidationError, match="overlap on cpu ids \\[2\\]"):
        assign_cpusets(8, [0, 1, 2], [2, 3])


def test_out_of_range_is_rejected() -> None:
    with pytest.raises(ValidationError, match="out of range"):
        assign_cpusets(4, [0, 1], [4])


def test_negative_id_is_rejected() -> None:
    with pytest.raises(ValidationError, match="out of range"):
        assign_cpusets(4, [-1], [1])


def test_duplicate_within_set_is_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        assign_cpusets(4, [0, 0], [1])


def test_oversubscription_is_rejected() -> None:
    # 3 cpus requested on a 2-cpu host: the third id is out of range.
    with pytest.raises(ValidationError, match="out of range"):
        assign_cpusets(2, [0, 1], [2])


def test_empty_sets_are_rejected() -> None:
    with pytest.raises(ValidationError, match="must not be empty"):
        assign_cpusets(4, [], [1])
    with pytest.raises(ValidationError, match="must not be empty"):
        assign_cpusets(4, [0], [])


def test_non_positive_total_is_rejected() -> None:
    with pytest.raises(ValidationError, match="positive"):
        CpuAssignment(total=0, sut=(0,), load=(1,))


def test_assignment_is_frozen() -> None:
    a = assign_cpusets(4, [0], [1])
    with pytest.raises(ValidationError):
        a.total = 8  # type: ignore[misc]


def test_extra_service_sets_are_disjoint_and_listed() -> None:
    a = assign_cpusets(8, [0, 1], [2], extra={"db": [3, 4]})
    assert a.extra_cpuset("db") == "3,4" and a.unassigned == (5, 6, 7)


def test_extra_set_overlap_and_range_rejected() -> None:
    with pytest.raises(ValueError, match="sut and db cpusets overlap"):
        assign_cpusets(8, [0, 1], [2], extra={"db": [1, 3]})
    with pytest.raises(ValueError, match="out of range"):
        assign_cpusets(4, [0], [1], extra={"db": [4]})
    with pytest.raises(ValueError, match="must not be empty"):
        assign_cpusets(4, [0], [1], extra={"db": []})
