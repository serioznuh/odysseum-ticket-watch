VERDICT: REVISE
FINDINGS:
1. [P1] watcher/runner.py:195 — polling order alone does not retire availability alerts disproved by the fresh snapshot — if a pending `PATHE_TARGET_DATE`, `TICKETS_AVAILABLE`, or Cinesa availability alert disappears without producing a replacement finding, recovery sends the now-stale advice.
2. [P1] watcher/delivery.py:396 — pending `RECOVERED` alerts are preserved unconditionally, with no inverse retirement after renewed source failure — if recovery delivery fails and the next Pathé check fails below the alert threshold, outbox recovery sends “Checks are running normally” while the watcher is blind again.
NOTES: The claim-save rollback and moved-reminder/healthy-outage paths are fixed. The authoritative test gate was accepted and not rerun.