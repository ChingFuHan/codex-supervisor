#!/usr/bin/env python3
"""Fake codex: sleeps until interrupted, then exits 130."""
import json, signal, sys, time

SESSION_ID = "01a00000-0000-7000-0000-000000000005"

def emit(obj):
    print(json.dumps(obj), flush=True)

interrupted = False

def _handler(sig, frame):
    global interrupted
    interrupted = True

signal.signal(signal.SIGINT, _handler)
signal.signal(signal.SIGTERM, _handler)

emit({"timestamp": "2026-09-06T00:00:00Z", "ordinal": 0, "type": "session_meta",
      "payload": {"session_id": SESSION_ID, "cwd": "/tmp"}})
emit({"timestamp": "2026-09-06T00:00:01Z", "ordinal": 1, "type": "event_msg",
      "payload": {"type": "task_started", "turn_id": "turn-1"}})

# Wait up to 30s for interrupt
for _ in range(300):
    if interrupted:
        break
    time.sleep(0.1)

emit({"timestamp": "2026-09-06T00:00:02Z", "ordinal": 2, "type": "event_msg",
      "payload": {"type": "turn_aborted", "turn_id": "turn-1", "reason": "interrupted"}})
sys.exit(130)
