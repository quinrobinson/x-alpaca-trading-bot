"""Independent signal-source scanners.

Each scanner watches the market for a specific setup (failed breakout,
unusual options volume, etc.) and writes detections to scanner_events
in Phase A.

Wiring into the orchestrator's signal queue is deliberately deferred —
Phase A is log-only so the operator can validate detections in real
time before any capital is at risk.
"""
