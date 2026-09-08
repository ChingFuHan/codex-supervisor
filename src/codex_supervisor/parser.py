"""
JSONL event stream parser and rate limit detector.

Primary signal: task_complete event with error.codex_error_info == "usage_limit_exceeded".
Fallback signals: stderr text pattern matching.
"""

import datetime
import json
import logging
import re
from typing import Iterator, IO

from .models import ExitClassification, RateLimitEvent

logger = logging.getLogger(__name__)

# Ordered by specificity
_RATE_LIMIT_PATTERNS = [
    re.compile(r"usage.?limit.?exceeded", re.IGNORECASE),
    re.compile(r"usage limit", re.IGNORECASE),
    re.compile(r"rate.?limit", re.IGNORECASE),
    re.compile(r"quota.?(?:exceeded|reached)", re.IGNORECASE),
    re.compile(r"limit reached", re.IGNORECASE),
    re.compile(r"you(?:'ve| have) hit your", re.IGNORECASE),
]



def parse_event(line: str) -> dict | None:
    line = line.strip()
    if not line:
        return None
    try:
        value = json.loads(line)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        logger.debug("non-JSON line: %.120s", line)
        return None


def iter_events(stream: IO[str]) -> Iterator[dict]:
    for line in stream:
        event = parse_event(line)
        if event is not None:
            yield event


def _matches_rate_limit_pattern(text: str) -> bool:
    return isinstance(text, str) and any(p.search(text) for p in _RATE_LIMIT_PATTERNS)


def parse_reset_time(
    message: str, reference_time: datetime.datetime | None = None,
) -> datetime.datetime | None:
    """Resolve displayed local clock times relative to the error, not the next scan."""
    if not isinstance(message, str):
        return None
    from zoneinfo import ZoneInfo
    import os

    try:
        if os.environ.get("TZ"):
            zone = ZoneInfo(os.environ["TZ"])
        else:
            with open("/etc/localtime", "rb") as stream:
                zone = ZoneInfo.from_file(stream)
    except (OSError, ValueError, KeyError):
        zone = datetime.datetime.now().astimezone().tzinfo
    now = (reference_time or datetime.datetime.now(datetime.timezone.utc)).astimezone(zone)
    # Codex 0.153.4: "try again at Sep 7th, 2026 2:04 AM."
    match = re.search(
        r"(?:try again|retry|resets?)\s+(?:at|after)\s+"
        r"([A-Za-z]{3})\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\s+"
        r"(\d{1,2}:\d{2}\s*[AP]M)", message, re.I,
    )
    if match:
        month, day, year, clock = match.groups()
        clock = re.sub(r"\s*([AP]M)$", r" \1", clock.upper())
        try:
            return datetime.datetime.strptime(
                f"{month} {day} {year} {clock}", "%b %d %Y %I:%M %p"
            ).replace(tzinfo=zone)
        except ValueError:
            return None
    match = re.search(
        r"(?:try again|retry|resets?)\s+(?:at|after)\s+"
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?)",
        message, re.I,
    )
    if match:
        try:
            value = datetime.datetime.fromisoformat(match[1].replace("Z", "+00:00"))
            return value if value.tzinfo else value.replace(tzinfo=zone)
        except ValueError:
            return None
    match = re.search(r"\bin\s+(\d+)\s*(seconds?|minutes?|hours?)", message, re.I)
    if match:
        multiplier = {"s": 1, "m": 60, "h": 3600}[match[2][0].lower()]
        try:
            return now + datetime.timedelta(seconds=int(match[1]) * multiplier)
        except OverflowError:
            return None
    match = re.search(
        r"(?:try again|retry|resets?)\s+(?:at|after)\s+(\d{1,2}:\d{2})(?:\s*([AP]M))?",
        message, re.I,
    )
    if match:
        clock = match[1] + (" " + match[2].upper() if match[2] else "")
        try:
            parsed = datetime.datetime.strptime(clock, "%I:%M %p" if match[2] else "%H:%M")
            result = now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
            # Rollout errors are timestamped at the reset minute. With an
            # event reference, a few seconds past that minute means the
            # reset just happened, not that it is tomorrow. Standalone
            # parsing still selects the next occurrence for past clock times.
            if result <= now and reference_time is None:
                result += datetime.timedelta(days=1)
            return result
        except ValueError:
            return None
    return None


def detect_rate_limit(event: dict) -> RateLimitEvent | None:
    """Check a single parsed JSONL event for rate limit indicators.

    Handles two formats:
    - Internal rollout format: {"type": "event_msg", "payload": {"type": "task_complete", ...}}
    - Public exec --json format: {"type": "error", "message": "..."} /
                                  {"type": "turn.failed", "error": {"message": "..."}}
    """
    etype = event.get("type", "")
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        return None

    # --- Public exec --json format ---

    # {"type": "error", "message": "You've hit your usage limit. try again at 7:05 AM."}
    if etype == "error":
        msg = event.get("message", "")
        if _matches_rate_limit_pattern(msg):
            return RateLimitEvent(
                detected=True,
                reset_at=parse_reset_time(msg),
                raw_message=msg,
                confidence=1.0,
                source="jsonl_event",
            )

    # {"type": "turn.failed", "error": {"message": "..."}}
    if etype == "turn.failed":
        error = event.get("error") or {}
        if not isinstance(error, dict):
            return None
        msg = error.get("message", "")
        if _matches_rate_limit_pattern(msg):
            return RateLimitEvent(
                detected=True,
                reset_at=parse_reset_time(msg),
                raw_message=msg,
                confidence=1.0,
                source="jsonl_event",
            )

    # --- Internal rollout format ---

    # Primary: structured error in task_complete
    if payload.get("type") == "task_complete":
        error = payload.get("error")
        if error:
            if not isinstance(error, dict):
                return None
            if error.get("codex_error_info") == "usage_limit_exceeded":
                msg = error.get("message", "")
                return RateLimitEvent(
                    detected=True,
                    reset_at=parse_reset_time(msg),
                    raw_message=msg,
                    confidence=1.0,
                    source="jsonl_event",
                )
            # Fallback: error message text matching
            msg = error.get("message", "")
            if _matches_rate_limit_pattern(msg):
                return RateLimitEvent(
                    detected=True,
                    reset_at=parse_reset_time(msg),
                    raw_message=msg,
                    confidence=0.7,
                    source="jsonl_event",
                )

    return None


def detect_rate_limit_stderr(text: str) -> RateLimitEvent | None:
    """Scan accumulated stderr text for rate limit patterns."""
    if not text:
        return None
    if _matches_rate_limit_pattern(text):
        return RateLimitEvent(
            detected=True,
            reset_at=parse_reset_time(text),
            raw_message=text[:500],
            confidence=0.7,
            source="stderr",
        )
    return None


def extract_session_id(event: dict) -> str | None:
    """Extract session/thread ID from either event format."""
    etype = event.get("type", "")
    # Public exec --json format
    if etype == "thread.started":
        return event.get("thread_id")
    # Internal rollout format
    if etype == "session_meta":
        return (event.get("payload") or {}).get("session_id")
    return None


def classify_exit(
    events: list[dict],
    exit_code: int,
    interrupted: bool,
) -> ExitClassification:
    if interrupted:
        return ExitClassification.USER_INTERRUPT

    for event in events:
        etype = event.get("type", "")
        payload = event.get("payload") or {}

        # Public exec --json format
        if etype == "turn.completed":
            return ExitClassification.NORMAL_COMPLETION
        if etype == "turn.failed":
            error = event.get("error") or {}
            msg = error.get("message", "")
            if _matches_rate_limit_pattern(msg):
                return ExitClassification.RATE_LIMIT
            return ExitClassification.UNKNOWN_FAILURE
        if etype == "error":
            msg = event.get("message", "")
            if _matches_rate_limit_pattern(msg):
                return ExitClassification.RATE_LIMIT

        # Internal rollout format
        payload_type = payload.get("type")
        if payload_type == "task_complete":
            error = payload.get("error")
            if error:
                info = error.get("codex_error_info", "")
                if info == "usage_limit_exceeded" or _matches_rate_limit_pattern(
                    error.get("message", "")
                ):
                    return ExitClassification.RATE_LIMIT
                return ExitClassification.UNKNOWN_FAILURE
            return ExitClassification.NORMAL_COMPLETION
        if payload_type == "turn_aborted":
            reason = payload.get("reason", "")
            if "interrupt" in reason.lower() or "cancel" in reason.lower():
                return ExitClassification.USER_INTERRUPT

    if exit_code == 0:
        return ExitClassification.NORMAL_COMPLETION
    return ExitClassification.TRANSIENT_ERROR
