"""Render a compose document for one run: SUT + k6 on disjoint cpusets (plan.md s3.3).

Pure function. No Docker calls; the runner (L-3) writes the file.
"""

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from serverlab.experiment.spec import RunSpec
from serverlab.topology.cpuset import CpuAssignment

SUT_SERVICE = "sut"
K6_SERVICE = "k6"
NETWORK = "lab"

# Immutable references only: a registry digest, or a local content-addressed image id.
_DIGEST_REF = re.compile(
    r"^([a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9._-]+)?@)?sha256:[0-9a-f]{64}$"
)


def _check_digest_ref(label: str, ref: str) -> None:
    if not _DIGEST_REF.match(ref):
        raise ValueError(
            f"{label} image must be pinned by digest ('name@sha256:<64 hex>' or local id "
            f"'sha256:<64 hex>'), got {ref!r}"
        )


def _healthcheck(port: int, health_path: str) -> dict[str, Any]:
    if not health_path.startswith("/"):
        raise ValueError(f"health_path must start with '/', got {health_path!r}")
    url = f"http://localhost:{port}{health_path}"
    # Python base image (see targets/synthetic/Dockerfile); no curl/wget assumed.
    script = f"import urllib.request; urllib.request.urlopen({json.dumps(url)}, timeout=2)"
    return {
        "test": ["CMD", "python", "-c", script],
        "interval": "1s",
        "timeout": "3s",
        "retries": 30,
        "start_period": "2s",
    }


def render_compose(
    spec: RunSpec,
    assignment: CpuAssignment,
    image_digests: Mapping[str, str],
    work_dir: Path | None = None,
) -> dict[str, Any]:
    """Build a compose document (as a dict) with services `sut` and `k6`.

    `image_digests` must contain keys "sut" and "k6", each a `name@sha256:<64 hex>` reference.
    The "sut" image must equal `spec.target.image`. `assignment` must match `spec.cpus`.
    """
    missing = {"sut", "k6"} - set(image_digests)
    if missing:
        raise ValueError(f"image_digests missing keys: {sorted(missing)}")
    sut_image = image_digests["sut"]
    k6_image = image_digests["k6"]
    _check_digest_ref("sut", sut_image)
    _check_digest_ref("k6", k6_image)
    if sut_image != spec.target.image:
        raise ValueError(
            f"sut image {sut_image!r} does not match spec.target.image {spec.target.image!r}"
        )
    if list(assignment.sut) != spec.cpus.sut or list(assignment.load) != spec.cpus.load:
        raise ValueError(
            f"cpu assignment sut={list(assignment.sut)} load={list(assignment.load)} "
            f"does not match spec.cpus sut={spec.cpus.sut} load={spec.cpus.load}"
        )

    sut_service: dict[str, Any] = {
        "image": sut_image,
        "cpuset": assignment.sut_cpuset(),
        "environment": dict(spec.target.env),
        "expose": [str(spec.target.port)],
        "networks": [NETWORK],
        "healthcheck": _healthcheck(spec.target.port, spec.target.health_path),
    }
    k6_service: dict[str, Any] = {
        "image": k6_image,
        "cpuset": assignment.load_cpuset(),
        "networks": [NETWORK],
        "depends_on": {SUT_SERVICE: {"condition": "service_healthy"}},
    }
    if work_dir is not None:
        k6_service["volumes"] = [f"{work_dir.resolve()}:/work"]
    return {
        "services": {SUT_SERVICE: sut_service, K6_SERVICE: k6_service},
        "networks": {NETWORK: {"driver": "bridge"}},
    }
