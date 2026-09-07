#!/usr/bin/env python3
"""Fake codex: real exec --json format rate limit. Exit 1."""
import json, sys

def emit(obj):
    print(json.dumps(obj), flush=True)

emit({"type": "thread.started", "thread_id": "01a00000-0000-7000-0000-000000000006"})
emit({"type": "turn.started"})
emit({"type": "error", "message": "You've hit your usage limit. Upgrade to Pro or try again at 9:00 AM."})
emit({"type": "turn.failed", "error": {"message": "You've hit your usage limit. Upgrade to Pro or try again at 9:00 AM."}})
sys.exit(1)
