"""
Read-only Codex session discovery and writer inspection.
Legacy goal queries remain available but are not quota triggers.
"""

import logging
import os
import sqlite3
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_CODEX_HOME = Path.home() / ".codex"


def _codex_home() -> Path:
    val = os.environ.get("CODEX_HOME", "")
    return Path(val) if val else _DEFAULT_CODEX_HOME


def check_usage_limited(session_id: str, db_path: Path | None = None) -> bool:
    """Return True if goals_1.sqlite shows status='usage_limited' for session."""
    path = db_path or (_codex_home() / "goals_1.sqlite")
    if not path.exists():
        return False
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            cur = con.execute(
                "SELECT 1 FROM thread_goals WHERE thread_id=? AND status='usage_limited'",
                (session_id,),
            )
            return cur.fetchone() is not None
        finally:
            con.close()
    except sqlite3.Error as exc:
        logger.debug("goals_1.sqlite query failed: %s", exc)
        return False


def get_thread_title(session_id: str, db_path: Path | None = None) -> str | None:
    """Return thread title from state_5.sqlite, or None."""
    path = db_path or (_codex_home() / "state_5.sqlite")
    if not path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            cur = con.execute(
                "SELECT title FROM threads WHERE id=?", (session_id,)
            )
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            con.close()
    except sqlite3.Error as exc:
        logger.debug("state_5.sqlite query failed: %s", exc)
        return None


def find_usage_limited_sessions() -> list[dict]:
    """Find all sessions with status='usage_limited' in goals_1.sqlite."""
    path = _codex_home() / "goals_1.sqlite"
    if not path.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT thread_id, objective, status, tokens_used, time_used_seconds "
                "FROM thread_goals WHERE status='usage_limited' "
                "ORDER BY updated_at_ms DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()
    except sqlite3.Error as exc:
        logger.debug("goals_1.sqlite query failed: %s", exc)
        return []


def get_session_info(session_id: str) -> dict | None:
    """Get session cwd and title from state_5.sqlite."""
    path = _codex_home() / "state_5.sqlite"
    if not path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        try:
            cur = con.execute(
                "SELECT id, cwd, title, rollout_path FROM threads WHERE id=?",
                (session_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            con.close()
    except sqlite3.Error as exc:
        logger.debug("state_5.sqlite query failed: %s", exc)
        return None


def find_interactive_sessions() -> list[dict]:
    """Discover local root TUI sessions, without treating goal status as quota truth."""
    path = _codex_home() / "state_5.sqlite"
    if not path.exists():
        return []
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    con.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in con.execute(
            "SELECT id, cwd, title, rollout_path FROM threads "
            "WHERE source='cli' AND archived=0 ORDER BY updated_at DESC"
        )]
    finally:
        con.close()


def has_active_writer(session_id: str) -> bool:
    """Linux read-only lock inspection; never acquire or remove Codex's lock."""
    sid = str(uuid.UUID(session_id))
    path = _codex_home() / "thread-writer-locks" / f"{sid}.lock"
    try:
        stat = path.stat()
    except FileNotFoundError:
        return False
    device = (os.major(stat.st_dev), os.minor(stat.st_dev), stat.st_ino)
    for line in Path("/proc/locks").read_text().splitlines():
        fields = line.split()
        if len(fields) < 6 or fields[1:4] != ["FLOCK", "ADVISORY", "WRITE"]:
            continue
        try:
            major, minor, inode = fields[5].split(":")
            if (int(major, 16), int(minor, 16), int(inode)) == device:
                return True
        except ValueError:
            continue
    return False
