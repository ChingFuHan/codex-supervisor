"""Opt-in proof: synthetic rate limit -> queue -> real TUI response, same live PID."""
import datetime as dt
import fcntl
import json
import os
import pty
import select
import shutil
import struct
import subprocess
import termios
import time
from pathlib import Path

import pytest

from codex_supervisor.codex_db import find_interactive_sessions
from codex_supervisor.interactive import InteractiveSupervisor
from codex_supervisor.models import JobStatus, SupervisorConfig
from codex_supervisor.state import StateStore


@pytest.mark.smoke
@pytest.mark.skipif(os.environ.get("CODEX_SUPERVISOR_RUN_SMOKE") != "1",
                    reason="set CODEX_SUPERVISOR_RUN_SMOKE=1 to run real interactive Codex")
def test_real_tui_survives_rate_limit_continuation(tmp_path):
    codex = shutil.which("codex")
    if not codex:
        pytest.skip("Codex CLI unavailable")
    target = tmp_path / "target"
    target.mkdir()
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    proc = subprocess.Popen(
        [codex, "--no-alt-screen", "-C", str(target), "-s", "read-only", "-a", "never",
         "Reply SUPERVISOR_FIRST_OK only. When I say continue, reply SUPERVISOR_RESUMED_OK only. Do not use tools."],
        stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
        env={**os.environ, "TERM": "xterm-256color"},
    )
    os.close(slave)
    screen = bytearray()
    def drain(seconds=.1):
        end = time.monotonic() + seconds
        while time.monotonic() < end and proc.poll() is None:
            if select.select([master], [], [], .05)[0]:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                screen.extend(data)
                del screen[:-100000]
                (tmp_path / "terminal.log").write_bytes(screen)
                if b"\x1b[6n" in data:
                    os.write(master, b"\x1b[1;1R")
    def wait_for(predicate, seconds=90):
        end = time.monotonic() + seconds
        accepted = False
        while time.monotonic() < end:
            assert proc.poll() is None, "test TUI exited unexpectedly"
            drain()
            # Accept only the test directory trust prompt, never a tool approval.
            if not accepted and b"trust" in screen.lower():
                os.write(master, b"\r")
                accepted = True
            result = predicate()
            if result:
                return result
        pytest.fail("real TUI did not produce the required completed response before timeout")
    def find_info():
        return next((i for i in find_interactive_sessions() if i["cwd"] == str(target)), None)
    def completed(info, message):
        path = Path(info["rollout_path"])
        if not path.exists():
            return False
        for line in path.open():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            payload = event.get("payload", {})
            if (event.get("type") == "event_msg" and payload.get("type") == "task_complete"
                    and not payload.get("error") and payload.get("last_agent_message") == message):
                return payload["turn_id"]
        return False
    try:
        info = wait_for(find_info)
        first_turn = wait_for(lambda: completed(info, "SUPERVISOR_FIRST_OK"))
        config = SupervisorConfig(state_dir=tmp_path / "state")
        sup = InteractiveSupervisor(config, StateStore(config.state_dir), codex)
        # Fault injection lives entirely in a test fixture. Never edit Codex's rollout
        # or exhaust real quota. Queue still targets the real, isolated TUI session.
        synthetic = tmp_path / "synthetic-rate-limit.jsonl"
        now = dt.datetime.now(dt.timezone.utc)
        synthetic.write_text(json.dumps(dict(
            timestamp=now.isoformat(), type="event_msg", payload=dict(
                type="task_complete", turn_id="synthetic-limited-turn", error=dict(
                    codex_error_info="usage_limit_exceeded",
                    message="Usage limit. Resets at " + (now - dt.timedelta(seconds=1)).isoformat()
                )
            )
        )) + "\n")
        limited_info = {**info, "rollout_path": str(synthetic)}
        job = sup.tick(limited_info, now=now)
        assert job.status == JobStatus.SCHEDULED
        assert proc.poll() is None
        second_turn = wait_for(lambda: completed(info, "SUPERVISOR_RESUMED_OK"))
        assert second_turn != first_turn
        assert sup.tick(info).status == JobStatus.COMPLETED
        assert proc.poll() is None
        print(f"session={info['id']} TUI_PID={proc.pid} alive=True completed_turns=2")
    finally:
        # This handle belongs ONLY to the child created by this test. Its separate
        # session prevents cleanup from reaching a user's shell or existing Codex.
        if proc.poll() is None:
            os.write(master, b"\x03")
            drain(.5)
            os.write(master, b"\x03")
            drain(1)
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        os.close(master)
