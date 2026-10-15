# Local UI JSON API (contract)

Base: `http://127.0.0.1:<port>`. All bodies are JSON. Errors: `{"error": "<message>"}` with 400/403/404/409.
Every `POST` needs header `X-Serverlab-Token: <token>`; the token is in the page: `<meta name="serverlab-token" content="...">`. `GET`s need no token. Requests whose `Host` is not `127.0.0.1:<port>` / `localhost:<port>` get 403.
Names in paths (`experiment`, `timestamp`, `run_id`) match `^[A-Za-z0-9_.-]+$`; anything else is 400.

## GET /api/specs
```json
[{"file": "week2_demo.yaml", "name": "week2_demo", "repetitions": 3, "conditions": ["limit5", "limit10"],
  "kind": "experiment", "sweep": null, "error": null}]
```
`kind` is `"sweep"` when the spec has a `sweep:` section (then `sweep` is `{"rate_lo":20,"rate_hi":800,"coarse_points":8,"max_bisect":6,"step_duration_s":10}`). A spec that fails validation has `error` set (string) and null `name`/`conditions`.

## GET /api/results
Newest first.
```json
[{"experiment": "week2_demo", "timestamp": "20261014T144256Z", "kind": "run", "comparability": "not-cross-machine-comparable",
  "warnings": ["host swap in use: 16613 MiB ..."], "runs": 6, "invalid": 0, "errors": 0, "sweeps": 0}]
```
`kind` = `"sweep"` if any `sweep_*.json` exists, else `"run"`.

## GET /api/results/{experiment}/{timestamp}
```json
{"experiment": "...", "timestamp": "...", "kind": "run|sweep",
 "spec": {"name": "...", "seed": 1, "repetitions": 3, "conditions": [{"name": "a", "target_env": {}, "load_rate": null}],
          "load": {"rate": 40, "duration_s": 15}, "cpus": {"sut": [0,1], "load": [2]}},
 "environment": {"os_name": "macOS", "os_version": "...", "cpu_model": "...", "cpu_cores": 8, "docker_vm_cpus": 4,
   "host_memory_gib": 8.0, "memory_free_pct": 32, "swap_used_mib": 16613.8, "power_source": "AC Power", "low_power_mode": false,
   "other_running_containers": [], "comparability": "not-cross-machine-comparable", "cpu_pinning_reliable": false,
   "warnings": [], "reasons": {}},
 "schedule": {"method": "randomized-blocks", "seed": 1, "order": [{"index": 0, "condition": "a", "repetition": 1}]},
 "runs": [{"run_id": "run_a_1", "condition": "a", "repetition": 1, "index": 0, "offered_rate": 40, "valid": true,
   "invalid_reasons": [], "planned": 600, "completed": 600, "dropped": 0, "peak_vus": 6, "p50_ms": 101.5, "p90_ms": 104.0,
   "p99_ms": 109.3, "error_rate": 0.0, "sut_cpu_usec": 1234567, "k6_cpu_usec": 2345678,
   "metrics": ["docker", "app", "pg"]}],
 "errors": [{"run_id": "run_a_2", "message": "..."}],
 "sweeps": [{"condition": "a", "repetition": 1, "seed": 5, "coarse_order": [100, 20], "bisect_rates": [44],
   "steps": [{"rate": 20, "status": "ok|saturated|invalid-other", "note": "", "achieved": 20.1, "p99_ms": 105.1,
              "k6_cpu_pct": 3.0, "error_rate": 0.0, "valid": true, "invalid_reasons": []}],
   "knee": {"status": "found|above_range|below_range|no_baseline", "lo": 48, "hi": 50, "estimate": 49.0,
            "width_pct": 4.2, "baseline_p99_ms": 105.1, "warnings": []}}]}
```
Any field may be `null` when it was not measured; the UI must show "n/a", never 0. `schedule` is `null` for sweep-only results.

## GET /api/results/{experiment}/{timestamp}/metrics/{run_id}/{kind}
`kind` in `docker | app | pg`. Returns `{"kind": "app", "lines": [ ... parsed JSON lines, at most 2000 ... ], "truncated": false}`; 404 if the file does not exist. Line shapes: docker `{t_host_utc, container, cpu_percent, mem_bytes, ...}`; app `{t_host_utc, pool_size, pool_available, requests_waiting, in_flight, delta_ok, delta_requests_queued, mean_wait_ms, interval_s_actual, counter_reset}`; pg `{t_host_utc, rows: [{state, wait_event_type, wait_event, count}]}`.

## GET /api/predictions
`{"markdown": "<contents of docs/predictions.md>"}` (empty string if the file does not exist).

## POST /api/jobs
Body: `{"action": "schedule|run|sweep|validate-offline", "spec": "week2_demo.yaml"}` or `{"action": "...", "yaml": "<pasted spec text>"}` (saved as `experiments/ui-<hash>.yaml` after validation; `validate-offline` needs neither).
`202 {"id": "...", "state": "running"}`; `409` if a job is already running; `400` if the spec is missing, outside `experiments/`, or fails validation (message included).
Actions: `schedule` = `serverlab run <spec> --dry-run`, `run` = `serverlab run <spec>`, `sweep` = `serverlab sweep <spec>`, `validate-offline` = `serverlab validate --offline`.

## GET /api/jobs/current
```json
{"id": "abc", "action": "run", "spec": "week2_demo.yaml", "state": "idle|running|finished|failed|cancelled",
 "exit_code": null, "started_at": "2026-10-15T12:00:00Z", "elapsed_s": 12.3, "log_tail": ["line", "..."],
 "result_dir": {"experiment": "week2_demo", "timestamp": "20261014T144256Z"}}
```
`state: "idle"` with everything else null before any job. `log_tail` is the last 200 lines. `result_dir` is parsed from the CLI's `results:` line, else null. Exit code 3 = finished but a run was invalid (state `finished`, show the reasons from the log/results).

## POST /api/jobs/cancel
`{"cancelled": true|false}`. Sends SIGTERM so the CLI tears its containers down; SIGKILL after 60 s.
