import copy
from pathlib import Path

import pytest
import yaml

from serverlab.experiment.spec import RunSpec
from serverlab.topology.compose import render_compose
from serverlab.topology.cpuset import assign_cpusets

DIGEST = "ghcr.io/example/sut@sha256:" + "a" * 64
K6_DIGEST = "grafana/k6@sha256:" + "b" * 64


def _spec(**overrides: object) -> RunSpec:
    raw: dict[str, object] = {
        "name": "t",
        "seed": 1,
        "target": {
            "image": DIGEST,
            "env": {"WORKERS": "2"},
            "port": 8000,
            "health_path": "/healthz",
        },
        "k6_image": K6_DIGEST,
        "load": {
            "rate": 10,
            "duration_s": 5,
            "pre_allocated_vus": 5,
            "max_vus": 10,
            "url": "http://sut:8000/work",
        },
        "cpus": {"sut": [0, 1], "load": [2]},
    }
    raw.update(overrides)
    return RunSpec.model_validate(raw)


def _render(spec: RunSpec | None = None) -> dict[str, object]:
    s = spec or _spec()
    a = assign_cpusets(4, s.cpus.sut, s.cpus.load)
    return render_compose(s, a, {"sut": DIGEST, "k6": K6_DIGEST})


def test_services_and_cpusets() -> None:
    doc = _render()
    services = doc["services"]
    assert isinstance(services, dict)
    assert services["sut"]["cpuset"] == "0,1"
    assert services["k6"]["cpuset"] == "2"
    assert services["sut"]["image"] == DIGEST
    assert services["k6"]["image"] == K6_DIGEST


def test_dedicated_network_and_env() -> None:
    doc = _render()
    services = doc["services"]
    assert isinstance(services, dict)
    assert doc["networks"] == {"lab": {"driver": "bridge"}}
    assert services["sut"]["networks"] == ["lab"]
    assert services["k6"]["networks"] == ["lab"]
    assert services["sut"]["environment"] == {"WORKERS": "2"}


def test_sut_healthcheck_uses_target_path_and_port() -> None:
    doc = _render()
    services = doc["services"]
    assert isinstance(services, dict)
    hc = services["sut"]["healthcheck"]
    assert hc["test"][:3] == ["CMD", "python", "-c"]
    assert "http://localhost:8000/healthz" in hc["test"][3]
    assert services["k6"]["depends_on"] == {"sut": {"condition": "service_healthy"}}


def test_healthz_path_must_be_absolute() -> None:
    s = _spec(target={"image": DIGEST, "health_path": "healthz"})
    with pytest.raises(ValueError, match="must start with '/'"):
        _render(s)


@pytest.mark.parametrize(
    "bad",
    [
        "ghcr.io/example/sut:latest",
        "ghcr.io/example/sut",
        "ghcr.io/example/sut@sha256:" + "A" * 64,
        "ghcr.io/example/sut@sha256:" + "a" * 63,
    ],
)
def test_tag_only_or_malformed_digest_is_rejected(bad: str) -> None:
    s = _spec(target={"image": bad})
    a = assign_cpusets(4, [0, 1], [2])
    with pytest.raises(ValueError, match="pinned by digest"):
        render_compose(s, a, {"sut": bad, "k6": K6_DIGEST})


def test_k6_tag_only_is_rejected() -> None:
    s = _spec()
    a = assign_cpusets(4, [0, 1], [2])
    with pytest.raises(ValueError, match="k6 image must be pinned"):
        render_compose(s, a, {"sut": DIGEST, "k6": "grafana/k6:latest"})


def test_missing_image_key_is_rejected() -> None:
    s = _spec()
    a = assign_cpusets(4, [0, 1], [2])
    with pytest.raises(ValueError, match="missing keys"):
        render_compose(s, a, {"sut": DIGEST})


def test_sut_image_must_match_spec() -> None:
    s = _spec()
    a = assign_cpusets(4, [0, 1], [2])
    other = "ghcr.io/example/other@sha256:" + "c" * 64
    with pytest.raises(ValueError, match="does not match spec.target.image"):
        render_compose(s, a, {"sut": other, "k6": K6_DIGEST})


def test_assignment_must_match_spec_cpus() -> None:
    s = _spec()
    a = assign_cpusets(4, [0], [1])
    with pytest.raises(ValueError, match="does not match spec.cpus"):
        render_compose(s, a, {"sut": DIGEST, "k6": K6_DIGEST})


def test_rendered_document_is_plain_yaml_serialisable() -> None:
    doc = copy.deepcopy(_render())
    assert yaml.safe_load(yaml.safe_dump(doc)) == doc


def test_local_image_id_is_accepted_and_work_dir_mounted(tmp_path: Path) -> None:
    local = "sha256:" + "c" * 64
    s = _spec(target={"image": local})
    a = assign_cpusets(4, [0, 1], [2])
    doc = render_compose(s, a, {"sut": local, "k6": K6_DIGEST}, work_dir=tmp_path)
    assert doc["services"]["sut"]["image"] == local
    assert doc["services"]["k6"]["volumes"] == [f"{tmp_path.resolve()}:/work"]


PG = "postgres@sha256:" + "d" * 64


def _spec_with_db(**svc: object) -> RunSpec:
    service: dict[str, object] = {
        "name": "db", "image": PG, "env": {"POSTGRES_PASSWORD": "x"},
        "command": ["postgres", "-c", "max_connections=300"], "tmpfs": ["/var/lib/postgresql/data"],
        "healthcheck": ["CMD-SHELL", "pg_isready -U postgres"], "container_port": 5432,
        "publish": 55432,
    }
    service.update(svc)
    return _spec(
        target={"image": DIGEST, "publish": 58000, "services": [service]},
        cpus={"sut": [0], "load": [3], "services": {"db": [1, 2]}},
    )


def test_extra_service_gets_own_cpuset_healthcheck_and_published_port() -> None:
    s = _spec_with_db()
    a = assign_cpusets(4, s.cpus.sut, s.cpus.load, extra=s.cpus.services)
    doc = render_compose(s, a, {"sut": DIGEST, "k6": K6_DIGEST})
    db, sut = doc["services"]["db"], doc["services"]["sut"]
    assert db["cpuset"] == "1,2" and db["tmpfs"] == ["/var/lib/postgresql/data"]
    assert db["ports"] == ["127.0.0.1:55432:5432"] and sut["ports"] == ["127.0.0.1:58000:8000"]
    assert db["healthcheck"]["test"] == ["CMD-SHELL", "pg_isready -U postgres"]
    assert sut["depends_on"] == {"db": {"condition": "service_healthy"}}
    assert doc["services"]["k6"]["cpuset"] == "3"


def test_extra_service_image_must_be_pinned_and_cpuset_matched() -> None:
    s = _spec_with_db(image="postgres:16")
    a = assign_cpusets(4, s.cpus.sut, s.cpus.load, extra=s.cpus.services)
    with pytest.raises(ValueError, match="pinned by digest"):
        render_compose(s, a, {"sut": DIGEST, "k6": K6_DIGEST})
    s2 = _spec_with_db()
    wrong = assign_cpusets(4, s2.cpus.sut, s2.cpus.load, extra={"db": [1]})
    with pytest.raises(ValueError, match="does not match"):
        render_compose(s2, wrong, {"sut": DIGEST, "k6": K6_DIGEST})


def test_spec_requires_cpuset_for_every_service() -> None:
    with pytest.raises(ValueError, match="must match target.services"):
        from serverlab.experiment.spec import ExperimentSpec
        base = _spec_with_db().model_dump(mode="json")
        base["cpus"]["services"] = {}
        base.update({"repetitions": 1, "conditions": [{"name": "a"}]})
        ExperimentSpec.model_validate(base)
