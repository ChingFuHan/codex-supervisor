#!/usr/bin/env python3
"""Fake codex: fatal failure with garbage output. Exit 2."""
import sys

print("fatal: internal error: assertion failed at core.rs:42", file=sys.stderr)
sys.exit(2)
