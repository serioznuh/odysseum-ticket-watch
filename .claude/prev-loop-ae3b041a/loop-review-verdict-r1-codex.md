VERDICT: REVISE
FINDINGS:
1. [P1] watcher/notify.py:271 — `ReadError` and `RemoteProtocolError` are treated as definite failures and retried, although Telegram may already have accepted the POST — a dropped response can produce an immediate duplicate with only the second attempt recorded.
2. [P1] watcher/delivery.py:245 — quarantined sale alerts never separate current observations from delivery acknowledgement — one uncertain send leaves `sales` and `sale_target` frozen; subsequent checks refuse the same outbox item, so the reminder ladder may never arm.
3. [P1] watcher/runner.py:153 — pending work is recovered before Pathé/Cinesa polling can supersede it — after a failed send, a moved opening or changed availability can cause the stale message to be delivered first, followed by the corrected alert.
NOTES: The authoritative test gate was accepted and not rerun.