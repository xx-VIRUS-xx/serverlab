from serverlab.experiment.spec import RunSpec


def test_runspec_roundtrip() -> None:
    raw = {
        "name": "t", "seed": 1,
        "target": {"image": "x@sha256:" + "0" * 64},
        "load": {"rate": 10, "duration_s": 5, "pre_allocated_vus": 5, "max_vus": 10,
                 "url": "http://sut:8000/work"},
        "cpus": {"sut": [0, 1], "load": [2]},
    }
    assert RunSpec.model_validate(raw).load.rate == 10
