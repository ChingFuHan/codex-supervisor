#!/usr/bin/env python3
"""Fake codex: rate limit, no parseable reset time. Exit 1."""
import json, sys

SESSION_ID = "01a00000-0000-7000-0000-000000000003"

def emit(obj):
    print(json.dumps(obj), flush=True)

emit({"timestamp": "2026-09-06T00:00:00Z", "ordinal": 0, "type": "session_meta",
      "payload": {"session_id": SESSION_ID, "cwd": "/tmp"}})
emit({"timestamp": "2026-09-06T00:00:01Z", "ordinal": 1, "type": "event_msg",
      "payload": {"type": "task_started", "turn_id": "turn-1"}})
emit({"timestamp": "2026-09-06T00:00:02Z", "ordinal": 2, "type": "event_msg",
      "payload": {
          "type": "task_complete",
          "turn_id": "turn-1",
          "error": {
              "codex_error_info": "usage_limit_exceeded",
              "message": "You have hit your usage limit."
          }
      }})
sys.exit(1)
