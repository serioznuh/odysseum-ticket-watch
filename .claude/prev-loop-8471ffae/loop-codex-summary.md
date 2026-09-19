All final-round findings are resolved. No Git commands were run.

1. **FIXED** — Replaced per-case retirement logic with condition-based observation reconciliation in [delivery.py](/Users/serhii.afanasiev/Documents/Projects/odysseum-ticket-watch/watcher/delivery.py). Pending Pathé date, generic ticket, and Cinesa availability alerts are retired whenever an authoritative snapshot contradicts their triggering condition—even when no replacement finding is generated. Unknown or degraded evidence never retires work.

2. **FIXED** — Health conditions are symmetric. A healthy observation retires pending BLIND/degraded alerts; a later unhealthy observation retires pending RECOVERED alerts, including failures below the next alert threshold.

Claude’s note is also addressed: `sale_target=None` is treated as unknown rather than moved, preserving a pending `open` ping. Only a different non-null target contradicts the old ladder.

No `Finding.key` formats changed. Updated [current-state.md](/Users/serhii.afanasiev/Documents/Projects/odysseum-ticket-watch/docs/current-state.md), remaining at its 180-line limit.

Verification:

- Targeted tests: 84 passed
- Full suite: 325 passed
- Ruff: passed
- Check-mode dry-run completed read-only; unavailable external DNS was handled and state was not saved.