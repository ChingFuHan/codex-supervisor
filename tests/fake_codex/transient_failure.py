#!/usr/bin/env python3
"""Fake codex: transient error (no task_complete). Exit 1."""
import json, sys

SESSION_ID = "01a00000-0000-7000-0000-000000000004"

def emit(obj):
    print(json.dumps(obj), flush=True)

emit({"timestamp": "2026-09-06T00:00:00Z", "ordinal": 0, "type": "session_meta",
      "payload": {"session_id": SESSION_ID, "cwd": "/tmp"}})
emit({"timestamp": "2026-09-06T00:00:01Z", "ordinal": 1, "type": "event_msg",
      "payload": {"type": "task_started", "turn_id": "turn-1"}})
print("network error: connection reset", file=sys.stderr)
sys.exit(1)
