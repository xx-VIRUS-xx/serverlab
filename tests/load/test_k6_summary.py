"""Tests for k6 summary parsing and validity checks (plan.md s3.2)."""

import http.server
import threading
from pathlib import Path

import pytest

from serverlab.load.base import LoadSpec
from serverlab.load.k6 import K6Adapter, check_validity, parse_k6_summary

CLEAN_FIXTURE = """{
  "metrics": {
    "vus": {
      "value": 0,
      "min": 0,
      "max": 0
    },
    "http_req_tls_handshaking": {
      "med": 0,
      "p(90)": 0,
      "p(99)": 0,
      "max": 0
    },
    "iteration_duration": {
      "max": 18.895625,
      "med": 1.4216665,
      "p(90)": 5.103950300000003,
      "p(99)": 17.516457529999997
    },
    "vus_max": {
      "value": 5,
      "min": 5,
      "max": 5
    },
    "data_received": {
      "count": 1120,
      "rate": 559.5987676835708
    },
    "http_req_sending": {
      "med": 0.057999999999999996,
      "p(90)": 0.7781000000000001,
      "p(99)": 1.16771,
      "max": 1.211
    },
    "http_req_waiting": {
      "med": 0.5465,
      "p(90)": 0.8595000000000003,
      "p(99)": 2.0947500000000003,
      "max": 2.232
    },
    "http_req_connecting": {
      "p(99)": 0.6931299999999999,
      "max": 0.706,
      "med": 0.329,
      "p(90)": 0.5772999999999999
    },
    "http_req_duration": {
      "med": 0.7945,
      "p(90)": 2.2531000000000003,
      "p(99)": 2.39161,
      "max": 2.407
    },
    "http_req_duration{expected_response:true}": {
      "med": 0.7945,
      "p(90)": 2.2531000000000003,
      "p(99)": 2.39161,
      "max": 2.407
    },
    "iterations": {
      "count": 10,
      "rate": 4.996417568603311
    },
    "http_req_blocked": {
      "med": 0.4525,
      "p(90)": 1.1185000000000007,
      "p(99)": 4.14385,
      "max": 4.48
    },
    "http_req_failed": {
      "passes": 0,
      "fails": 10,
      "value": 0
    },
    "data_sent": {
      "rate": 369.73490007664503,
      "count": 740
    },
    "http_req_receiving": {
      "p(90)": 0.14700000000000005,
      "p(99)": 0.3738,
      "max": 0.399,
      "med": 0.0755
    },
    "http_reqs": {
      "count": 10,
      "rate": 4.996417568603311
    }
  },
  "root_group": {
    "checks": {},
    "name": "",
    "path": "",
    "id": "d41d8cd98f00b204e9800998ecf8427e",
    "groups": {}
  }
}"""

DROPPED_FIXTURE = """{
  "metrics": {
    "http_req_duration": {
      "med": 960.306,
      "p(90)": 968.2388000000001,
      "p(99)": 970.51508,
      "max": 970.768
    },
    "http_req_tls_handshaking": {
      "med": 0,
      "p(90)": 0,
      "p(99)": 0,
      "max": 0
    },
    "http_req_receiving": {
      "med": 0.298,
      "p(90)": 0.3766,
      "p(99)": 0.40396,
      "max": 0.407
    },
    "iterations": {
      "count": 5,
      "rate": 1.9825472401356379
    },
    "vus_max": {
      "min": 2,
      "max": 2,
      "value": 2
    },
    "data_received": {
      "count": 560,
      "rate": 222.04529089519144
    },
    "http_req_duration{expected_response:true}": {
      "med": 960.306,
      "p(90)": 968.2388000000001,
      "p(99)": 970.51508,
      "max": 970.768
    },
    "http_req_sending": {
      "max": 0.07,
      "med": 0.047,
      "p(90)": 0.066,
      "p(99)": 0.06960000000000001
    },
    "http_req_waiting": {
      "p(90)": 967.9218,
      "p(99)": 970.23408,
      "max": 970.491,
      "med": 959.938
    },
    "http_req_failed": {
      "passes": 0,
      "fails": 5,
      "value": 0
    },
    "data_sent": {
      "count": 375,
      "rate": 148.69104301017285
    },
    "vus": {
      "max": 2,
      "value": 2,
      "min": 2
    },
    "http_req_blocked": {
      "p(90)": 0.5668000000000001,
      "p(99)": 0.64528,
      "max": 0.654,
      "med": 0.407
    },
    "http_req_connecting": {
      "med": 0.309,
      "p(90)": 0.3266,
      "p(99)": 0.33056,
      "max": 0.331
    },
    "dropped_iterations": {
      "count": 36,
      "rate": 14.274340128976593
    },
    "iteration_duration": {
      "max": 971.425584,
      "med": 960.918709,
      "p(90)": 968.9062504,
      "p(99)": 971.17365064
    },
    "http_reqs": {
      "count": 5,
      "rate": 1.9825472401356379
    }
  },
  "root_group": {
    "id": "d41d8cd98f00b204e9800998ecf8427e",
    "groups": {},
    "checks": {},
    "name": "",
    "path": ""
  }
}"""

MAXVUS_FIXTURE = """{
  "root_group": {
    "name": "",
    "path": "",
    "id": "d41d8cd98f00b204e9800998ecf8427e",
    "groups": {},
    "checks": {}
  },
  "metrics": {
    "http_req_blocked": {
      "med": 0.351,
      "p(90)": 0.5588,
      "p(99)": 0.56258,
      "max": 0.563
    },
    "http_req_tls_handshaking": {
      "med": 0,
      "p(90)": 0,
      "p(99)": 0,
      "max": 0
    },
    "http_req_sending": {
      "p(99)": 0.07239999999999999,
      "max": 0.073,
      "med": 0.047,
      "p(90)": 0.067
    },
    "data_received": {
      "rate": 221.73770522404416,
      "count": 784
    },
    "dropped_iterations": {
      "count": 14,
      "rate": 3.9596018790007887
    },
    "http_req_duration{expected_response:true}": {
      "p(99)": 1933.75412,
      "max": 1934.246,
      "med": 1516.282,
      "p(90)": 1929.3272
    },
    "http_req_receiving": {
      "med": 0.169,
      "p(90)": 0.35119999999999996,
      "p(99)": 0.35552,
      "max": 0.356
    },
    "http_req_duration": {
      "med": 1516.282,
      "p(90)": 1929.3272,
      "p(99)": 1933.75412,
      "max": 1934.246
    },
    "iterations": {
      "count": 7,
      "rate": 1.9798009395003944
    },
    "http_req_waiting": {
      "med": 1516.071,
      "p(90)": 1928.9137999999998,
      "p(99)": 1933.34828,
      "max": 1933.841
    },
    "http_req_connecting": {
      "max": 0.419,
      "med": 0.267,
      "p(90)": 0.4052,
      "p(99)": 0.41762
    },
    "http_req_failed": {
      "passes": 0,
      "fails": 7,
      "value": 0
    },
    "vus_max": {
      "value": 4,
      "min": 4,
      "max": 4
    },
    "data_sent": {
      "count": 525,
      "rate": 148.48507046252956
    },
    "vus": {
      "value": 2,
      "min": 2,
      "max": 4
    },
    "iteration_duration": {
      "p(90)": 1930.2415416,
      "p(99)": 1934.3880660599998,
      "max": 1934.848791,
      "med": 1516.950917
    },
    "http_reqs": {
      "count": 7,
      "rate": 1.9798009395003944
    }
  }
}"""



def test_clean_summary_is_valid() -> None:
    spec = LoadSpec(
        rate=5,
        duration_s=2,
        pre_allocated_vus=5,
        max_vus=10,
        url="http://127.0.0.1:8000/fast",
    )
    result = parse_k6_summary(CLEAN_FIXTURE, spec=spec, run_id="run_clean")

    assert result.run_id == "run_clean"
    assert result.planned == 10
    assert result.completed == 10
    assert result.dropped == 0
    assert result.peak_vus < spec.max_vus
    assert result.latency_ms.p50 == pytest.approx(0.7945)
    assert result.latency_ms.p90 == pytest.approx(2.2531)
    assert result.latency_ms.p99 == pytest.approx(2.39161)
    assert result.latency_ms.max == pytest.approx(2.407)
    assert result.error_rate == 0.0
    assert result.valid is True
    assert result.invalid_reasons == []


def test_dropped_iterations_exceeding_threshold_is_invalid() -> None:
    spec = LoadSpec(
        rate=20,
        duration_s=2,
        pre_allocated_vus=1,
        max_vus=2,
        url="http://127.0.0.1:8000/delay",
    )
    result = parse_k6_summary(DROPPED_FIXTURE, spec=spec, run_id="run_dropped")

    assert result.run_id == "run_dropped"
    assert result.planned == 40
    assert result.completed == 5
    assert result.dropped == 36
    assert result.valid is False

    # Acceptance: Invalid reasons are specific strings naming the threshold and counts
    dropped_reasons = [r for r in result.invalid_reasons if "dropped iterations ratio" in r]
    assert len(dropped_reasons) == 1
    assert "0.005 (0.5%) threshold" in dropped_reasons[0]
    assert "36/40" in dropped_reasons[0]


def test_maxvus_hit_is_invalid() -> None:
    spec = LoadSpec(
        rate=10,
        duration_s=2,
        pre_allocated_vus=2,
        max_vus=4,
        url="http://127.0.0.1:8000/delay",
    )
    result = parse_k6_summary(MAXVUS_FIXTURE, spec=spec, run_id="run_maxvus")

    assert result.run_id == "run_maxvus"
    assert result.peak_vus == 4
    assert result.valid is False

    # Acceptance: Specific reason naming peak VUs and max_vus
    vus_reasons = [r for r in result.invalid_reasons if "peak VUs" in r]
    assert len(vus_reasons) == 1
    assert "peak VUs (4) reached or exceeded max_vus (4)" in vus_reasons[0]


def test_invalid_reasons_are_specific_strings_not_booleans() -> None:
    valid, reasons = check_validity(dropped=10, planned=100, peak_vus=15, max_vus=15)
    assert valid is False
    assert len(reasons) == 2
    assert isinstance(reasons[0], str)
    assert isinstance(reasons[1], str)
    assert "dropped iterations ratio 0.1000 (10/100) exceeded 0.005 (0.5%) threshold" in reasons[0]
    assert "peak VUs (15) reached or exceeded max_vus (15)" in reasons[1]


def test_parse_summary_from_path(tmp_path: Path) -> None:
    summary_file = tmp_path / "summary.json"
    summary_file.write_text(CLEAN_FIXTURE, encoding="utf-8")

    result = parse_k6_summary(summary_file, planned=10, max_vus=10, run_id="from_path")
    assert result.run_id == "from_path"
    assert result.valid is True
    assert result.completed == 10
    assert result.summary_path == summary_file


def test_adapter_live_run(tmp_path: Path) -> None:
    class DummyHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), DummyHandler)
    port = server.server_address[1]
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        adapter = K6Adapter()
        spec = LoadSpec(
            rate=5,
            duration_s=1,
            pre_allocated_vus=2,
            max_vus=5,
            url=f"http://127.0.0.1:{port}/work",
            method="GET",
        )
        run_result = adapter.run(spec, out_dir=tmp_path, run_id="run_live")

        assert run_result.run_id == "run_live"
        assert run_result.valid is True
        assert run_result.completed > 0
        assert run_result.dropped == 0
        assert run_result.summary_path.is_file()
        assert (tmp_path / "run_live.js").is_file()
    finally:
        server.shutdown()
