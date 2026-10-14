# Backlog

- Synthetic target uses `Connection: close` (one TCP connection per request). Consider keep-alive before Week 3 knee validation. (from C-3 review)
- Control run at 50 rps / 8 s showed 2/400 dropped (0.5%, exactly at the threshold) during k6 start-up. Consider whether warmup must be excluded from the validity ratio. Decide in Week 2 with the warmup/cooldown work.
- env/detect: Docker Desktop on a Linux host reports the VM's OS, so `cpu_pinning_reliable` could be wrongly true. Add a second signal (e.g. `docker info` Name == "docker-desktop" or context) before the canonical Linux host is chosen. (from C-1 review)
- env/detect: record whether the git working tree is dirty alongside `git_sha`, otherwise results can claim a commit they did not run. (from C-1 review)
- knee baseline = P99 of the single lowest-rate step is noisy (s100 baseline read 122.7 ms vs ~105-112 ms elsewhere). Consider a robust baseline (e.g. median of the lowest few valid steps). Decide with more data; not changed after seeing validation results. (Week 3)
- knee estimate is consistently 1.3-2.0% below C because the step at offered rate == C already shows queueing. Document in methodology; consider reporting the bracket as the primary result. (Week 3)
- USL fitting (descriptive, >= 6 concurrency points) deferred from Week 3.
