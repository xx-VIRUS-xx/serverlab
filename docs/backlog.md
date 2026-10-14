# Backlog

- Synthetic target uses `Connection: close` (one TCP connection per request). Consider keep-alive before Week 3 knee validation. (from C-3 review)
- Control run at 50 rps / 8 s showed 2/400 dropped (0.5%, exactly at the threshold) during k6 start-up. Consider whether warmup must be excluded from the validity ratio. Decide in Week 2 with the warmup/cooldown work.
