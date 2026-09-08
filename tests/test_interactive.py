import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from codex_supervisor.interactive import InteractiveSupervisor, RolloutTail
from codex_supervisor.models import JobStatus, SupervisorConfig
from codex_supervisor.state import StateStore
from codex_supervisor.state import WatchAlreadyRunning

NOW = dt.datetime(2026, 9, 7, 12, tzinfo=dt.timezone.utc)
SID = "01a00000-0000-7000-0000-000000000001"


def append(path, kind, turn="t1", error=None, timestamp=None):
    event = dict(timestamp=(timestamp or NOW).isoformat(), type="event_msg",
                 payload=dict(type=kind, turn_id=turn, error=error))
    with path.open("a") as stream:
        stream.write(json.dumps(event) + "\n")
    return event


@pytest.fixture
def monitor(tmp_path, monkeypatch):
    monkeypatch.setattr("codex_supervisor.interactive.has_active_writer", lambda sid: True)
    path = tmp_path / "rollout.jsonl"
    path.touch()
    info = dict(id=SID, cwd=str(tmp_path), rollout_path=str(path))
    config = SupervisorConfig(state_dir=tmp_path / "state", fallback_wait_minutes=1)
    store = StateStore(config.state_dir)
    sup = InteractiveSupervisor(config, store, "codex")
    return sup, info, path


def limited(path, turn="t1", reset="2026-09-07T12:01:00Z"):
    return append(path, "task_complete", turn,
                  dict(codex_error_info="usage_limit_exceeded",
                       message=f"Usage limit. Resets at {reset}" if reset else "Usage limit."))


def clock_limited(path, turn="t1", timestamp=None):
    return append(path, "task_complete", turn,
                  dict(codex_error_info="usage_limit_exceeded",
                       message="You've hit your usage limit. Try again at 06:28 AM."),
                  timestamp=timestamp)


def progress(path, turn="t1", item_type="CommandExecution", timestamp=None):
    event = dict(timestamp=(timestamp or NOW).isoformat(), type="event_msg",
                 payload=dict(type="item_completed", turn_id=turn,
                              item=dict(type=item_type, id=f"item-{turn}-{item_type}")))
    with path.open("a") as stream:
        stream.write(json.dumps(event) + "\n")
    return event


def success(*args, **kwargs):
    return subprocess.CompletedProcess(args[0], 0,
        f"Queued message {uuid.uuid4()} for thread {SID}.\n", "")


def test_wait_queue_complete_then_another_limit(monitor):
    sup, info, path = monitor
    limited(path)
    with patch("codex_supervisor.interactive.subprocess.run", side_effect=success) as queue:
        job = sup.tick(info, now=NOW)
        assert job.status == JobStatus.RATE_LIMITED
        sup.tick(info, now=NOW + dt.timedelta(seconds=59))
        queue.assert_not_called()
        job = sup.tick(info, now=NOW + dt.timedelta(seconds=60))
        assert job.status == JobStatus.SCHEDULED
        assert queue.call_args.args[0] == ["codex", "queue", "--thread", SID, "--message", "continue"]
        append(path, "task_started", "t2")
        assert sup.tick(info, now=NOW).status == JobStatus.RUNNING
        append(path, "task_complete", "t2")
        assert sup.tick(info, now=NOW).status == JobStatus.COMPLETED
        limited(path, "t3", "2026-09-07T11:00:00Z")
        sup.tick(info, now=NOW)
        assert queue.call_count == 2


def test_continuation_rate_limit_before_progress_is_explicit(monitor):
    sup, info, path = monitor
    limited(path)
    with patch("codex_supervisor.interactive.subprocess.run", side_effect=success) as queue:
        sup.tick(info, now=NOW)
        sup.tick(info, now=NOW + dt.timedelta(seconds=60))
        append(path, "task_started", "t2")
        progress(path, "t2", "UserMessage")
        limited(path, "t2", "2026-09-07T13:00:00Z")
        job = sup.tick(info, now=NOW + dt.timedelta(seconds=61))
    assert queue.call_count == 1
    assert job.status == JobStatus.RATE_LIMITED
    assert job.continuation_outcome == "rate_limited_before_progress"
    assert job.continuation_turn_id == "t2"
    assert job.progress_item_count == 0


def test_continuation_progress_is_recorded_before_completion(monitor):
    sup, info, path = monitor
    limited(path)
    with patch("codex_supervisor.interactive.subprocess.run", side_effect=success) as queue:
        sup.tick(info, now=NOW)
        sup.tick(info, now=NOW + dt.timedelta(seconds=60))
        append(path, "task_started", "t2")
        progress(path, "t2", "CommandExecution", NOW + dt.timedelta(seconds=61))
        job = sup.tick(info, now=NOW + dt.timedelta(seconds=61))
    assert queue.call_count == 1
    assert job.status == JobStatus.RUNNING
    assert job.continuation_outcome == "working"
    assert job.progress_item_count == 1
    assert job.progress_summary == ["CommandExecution"]


def test_continuation_rate_limit_after_progress_is_distinguished(monitor):
    sup, info, path = monitor
    limited(path)
    with patch("codex_supervisor.interactive.subprocess.run", side_effect=success) as queue:
        sup.tick(info, now=NOW)
        sup.tick(info, now=NOW + dt.timedelta(seconds=60))
        append(path, "task_started", "t2")
        progress(path, "t2", "Reasoning")
        limited(path, "t2", "2026-09-07T13:00:00Z")
        job = sup.tick(info, now=NOW + dt.timedelta(seconds=61))
    assert queue.call_count == 1
    assert job.status == JobStatus.RATE_LIMITED
    assert job.continuation_outcome == "rate_limited_after_progress"
    assert job.progress_item_count == 1
    assert job.progress_summary == ["Reasoning"]


def test_progress_after_terminal_event_is_not_counted(monitor):
    sup, info, path = monitor
    limited(path)
    with patch("codex_supervisor.interactive.subprocess.run", side_effect=success) as queue:
        sup.tick(info, now=NOW)
        sup.tick(info, now=NOW + dt.timedelta(seconds=60))
        append(path, "task_started", "t2")
        limited(path, "t2", "2026-09-07T13:00:00Z")
        progress(path, "t2", "CommandExecution")
        job = sup.tick(info, now=NOW + dt.timedelta(seconds=61))
    assert queue.call_count == 1
    assert job.continuation_outcome == "rate_limited_before_progress"
    assert job.progress_item_count == 0


def test_legacy_queued_terminal_is_backfilled(monitor):
    sup, info, path = monitor
    limited(path)
    with patch("codex_supervisor.interactive.subprocess.run", side_effect=success):
        sup.tick(info, now=NOW)
        sup.tick(info, now=NOW + dt.timedelta(seconds=60))
        append(path, "task_started", "t2")
        progress(path, "t2", "UserMessage")
        limited(path, "t2", "2026-09-07T13:00:00Z")
        job = sup.tick(info, now=NOW + dt.timedelta(seconds=61))
    job.continuation_turn_id = None
    job.continuation_started_at = None
    job.last_progress_at = None
    job.progress_item_count = 0
    job.progress_summary = []
    job.continuation_outcome = None
    sup.store.save_job(job)
    recovered = InteractiveSupervisor(sup.config, sup.store, "codex").tick(
        info, now=NOW + dt.timedelta(seconds=62),
    )
    assert recovered.continuation_outcome == "rate_limited_before_progress"
    assert recovered.continuation_turn_id == "t2"


def test_continuation_progress_survives_watcher_restart(monitor):
    sup, info, path = monitor
    limited(path)
    with patch("codex_supervisor.interactive.subprocess.run", side_effect=success) as queue:
        sup.tick(info, now=NOW)
        sup.tick(info, now=NOW + dt.timedelta(seconds=60))
        append(path, "task_started", "t2")
        progress(path, "t2", "Extension")
        job = sup.tick(info, now=NOW + dt.timedelta(seconds=61))
        restarted = InteractiveSupervisor(sup.config, sup.store, "codex")
        recovered = restarted.tick(info, now=NOW + dt.timedelta(seconds=62))
    assert queue.call_count == 1
    assert job.continuation_outcome == "working"
    assert recovered.continuation_outcome == "working"
    assert recovered.continuation_turn_id == "t2"
    assert recovered.progress_item_count == 1
    assert recovered.progress_summary == ["Extension"]


def test_queue_without_new_turn_becomes_unconfirmed(monitor):
    sup, info, path = monitor
    limited(path)
    with patch("codex_supervisor.interactive.subprocess.run", side_effect=success) as queue:
        sup.tick(info, now=NOW)
        sup.tick(info, now=NOW + dt.timedelta(seconds=60))
        job = sup.tick(info, now=NOW + dt.timedelta(seconds=181))
    assert queue.call_count == 1
    assert job.status == JobStatus.FAILED
    assert job.continuation_outcome == "unconfirmed"


def test_no_reset_backoff_does_not_move_on_every_poll(monitor):
    sup, info, path = monitor
    limited(path, reset=None)
    job = sup.tick(info, now=NOW)
    assert dt.datetime.fromisoformat(job.scheduled_resume) == NOW + dt.timedelta(minutes=1)
    job = sup.tick(info, now=NOW + dt.timedelta(seconds=5))
    assert dt.datetime.fromisoformat(job.scheduled_resume) == NOW + dt.timedelta(minutes=1)


def test_stale_clock_deadline_is_refreshed_and_queued(monitor, monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Taipei")
    sup, info, path = monitor
    event_time = dt.datetime(2026, 9, 7, 22, 28, 5, tzinfo=dt.timezone.utc)
    clock_limited(path, timestamp=event_time)
    with patch("codex_supervisor.interactive.subprocess.run", side_effect=success) as queue:
        job = sup.tick(info, now=dt.datetime(2026, 9, 7, 22, 0, tzinfo=dt.timezone.utc))
        assert job.status == JobStatus.RATE_LIMITED
        job.scheduled_resume = "2026-09-09T06:28:00+08:00"
        sup.store.save_job(job)
        job = sup.tick(info, now=dt.datetime(2026, 9, 7, 22, 30, tzinfo=dt.timezone.utc))
    assert queue.call_count == 1
    assert job.status == JobStatus.SCHEDULED
    assert job.continuation_outcome == "queued"


def test_restart_and_other_watcher_do_not_duplicate(monitor):
    sup, info, path = monitor
    limited(path, reset="2026-09-07T11:00:00Z")
    with patch("codex_supervisor.interactive.subprocess.run", side_effect=success) as queue:
        sup.tick(info, now=NOW)
        other = InteractiveSupervisor(sup.config, sup.store, "codex")
        other.tick(info, now=NOW)
        job = other.tick(info, now=NOW + dt.timedelta(minutes=3))
        assert queue.call_count == 1
        assert job.status == JobStatus.FAILED
        assert "not resending" in job.last_error
        other.tick(info, now=NOW + dt.timedelta(hours=1))
        assert queue.call_count == 1


@pytest.mark.parametrize("last", ["task_started", "task_complete", "turn_aborted"])
def test_latest_event_supersedes_old_limit(monitor, last):
    sup, info, path = monitor
    limited(path, reset="2026-09-07T11:00:00Z")
    append(path, last, "new-turn")
    with patch("codex_supervisor.interactive.subprocess.run") as queue:
        sup.tick(info, adopt=True, now=NOW)
        queue.assert_not_called()


def test_manual_resume_during_wait_cancels_submission(monitor):
    sup, info, path = monitor
    limited(path)
    sup.tick(info, now=NOW)
    append(path, "task_started", "manual")
    with patch("codex_supervisor.interactive.subprocess.run") as queue:
        job = sup.tick(info, now=NOW + dt.timedelta(minutes=5))
        assert job.scheduled_resume is None
        queue.assert_not_called()


def test_cancel_persisted_before_deadline(monitor):
    from codex_supervisor.cli import cmd_cancel
    from argparse import Namespace
    sup, info, path = monitor
    limited(path)
    job = sup.tick(info, now=NOW)
    cmd_cancel(Namespace(job_id=job.job_id), sup.config, sup.store)
    with patch("codex_supervisor.interactive.subprocess.run") as queue:
        job = sup.tick(info, now=NOW + dt.timedelta(minutes=5))
        assert job.status == JobStatus.CANCELLED
        queue.assert_not_called()


def test_explicit_continue_now_queues_once_for_idle_session(monitor):
    sup, info, path = monitor
    append(path, "task_complete")
    with patch("codex_supervisor.interactive.subprocess.run", side_effect=success) as queue:
        sup.tick(info, prompt="keep working", adopt=True, continue_now=True, now=NOW)
        with pytest.raises(ValueError, match="already pending"):
            sup.tick(info, adopt=True, continue_now=True, now=NOW)
        assert queue.call_count == 1
        assert queue.call_args.args[0][-1] == "keep working"


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("codex", 30), OSError("unavailable")])
def test_uncertain_queue_outcome_is_never_retried(monitor, error):
    sup, info, path = monitor
    limited(path, reset="2026-09-07T11:00:00Z")
    with patch("codex_supervisor.interactive.subprocess.run", side_effect=error) as queue:
        assert sup.tick(info, now=NOW).status == JobStatus.FAILED
        sup.tick(info, now=NOW + dt.timedelta(hours=2))
        assert queue.call_count == 1


def test_partial_line_and_rotation(tmp_path):
    path = tmp_path / "rollout"
    path.write_text('{"type":"event_msg",')
    tail = RolloutTail(path)
    assert tail.poll() is None
    with path.open("a") as stream:
        stream.write('"payload":{"type":"task_started"}}\n')
    assert tail.poll()["payload"]["type"] == "task_started"
    replacement = tmp_path / "replacement"
    replacement.write_text("")
    append(replacement, "task_complete")
    replacement.replace(path)
    assert tail.poll()["payload"]["type"] == "task_complete"


def test_model_text_is_not_a_rate_limit(monitor):
    sup, info, path = monitor
    path.write_text(json.dumps(dict(type="response_item", payload=dict(
        type="message", role="assistant", content=[dict(type="output_text", text="usage limit")]
    ))) + "\n")
    with patch("codex_supervisor.interactive.subprocess.run") as queue:
        assert sup.tick(info, now=NOW) is None
        queue.assert_not_called()


def test_fake_queue_process_drives_real_rollout_observation(monitor, tmp_path):
    sup, info, path = monitor
    fake = tmp_path / "fake-codex"
    fake.write_text("#!/usr/bin/env python3\n" +
        "import json,sys\n" +
        f"assert sys.argv[1:4] == ['queue', '--thread', {SID!r}]\n" +
        f"with open({str(path)!r}, 'a') as f:\n" +
        " f.write(json.dumps({'type':'event_msg','payload':{'type':'task_complete', 'turn_id':'resumed'}})+'\\n')\n" +
        f"print('Queued message 01a00000-0000-7000-0000-000000000002 for thread {SID}.')\n")
    fake.chmod(0o700)
    sup.codex = str(fake)
    limited(path, reset="2026-09-07T11:00:00Z")
    assert sup.tick(info, now=NOW).status == JobStatus.SCHEDULED
    assert sup.tick(info, now=NOW).status == JobStatus.COMPLETED


def test_sigint_only_stops_monitor(tmp_path):
    """Separate fake TUI survives; the monitor never sends it or the shell a signal."""
    import sqlite3
    home = tmp_path / "codex"
    home.mkdir()
    import fcntl
    locks = home / "thread-writer-locks"
    locks.mkdir()
    writer = (locks / f"{SID}.lock").open("w")
    fcntl.flock(writer, fcntl.LOCK_EX)
    path = home / "rollout.jsonl"
    limited(path, reset="2099-01-01T00:00:00Z")
    with sqlite3.connect(home / "state_5.sqlite") as con:
        con.execute('CREATE TABLE threads (id TEXT, cwd TEXT, title TEXT, rollout_path TEXT)')
        con.execute('INSERT INTO threads VALUES (?, ?, ?, ?)', (SID, str(tmp_path), "test", str(path)))
    fake = tmp_path / "codex-cli"
    fake.write_text('#!/usr/bin/env python3\nprint("--thread")\n')
    fake.chmod(0o700)
    env = {**os.environ, "CODEX_HOME": str(home), "CODEX_SUPERVISOR_STATE_DIR": str(tmp_path / "state"),
           "CODEX_SUPERVISOR_CODEX_PATH": str(fake), "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    tui = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    proc = subprocess.Popen([sys.executable, "-m", "codex_supervisor", "adopt", SID], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        store = StateStore(tmp_path / "state")
        deadline = time.monotonic() + 5
        while not store.load_all_jobs() and time.monotonic() < deadline:
            time.sleep(.02)
        assert store.load_all_jobs()
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=5) == 0
        assert tui.poll() is None
        assert store.load_all_jobs()[0].status == JobStatus.RATE_LIMITED
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        tui.terminate()
        tui.wait(timeout=5)
        writer.close()


def test_no_writer_means_no_queue(monitor, monkeypatch):
    sup, info, path = monitor
    monkeypatch.setattr("codex_supervisor.interactive.has_active_writer", lambda sid: False)
    limited(path, reset="2026-09-07T11:00:00Z")
    with patch("codex_supervisor.interactive.subprocess.run") as queue:
        with pytest.raises(ValueError, match="no live Codex writer"):
            sup.tick(info, now=NOW)
        queue.assert_not_called()
        assert sup.store.load_all_jobs()[0].submitted_event is None


def test_monitor_stop_rearms_legacy_cancelled_job(monitor):
    sup, info, path = monitor
    limited(path, reset="2026-09-07T12:01:00Z")
    job = sup.tick(info, now=NOW)
    job.status = JobStatus.CANCELLED
    job.scheduled_resume = None
    sup.store.save_job(job)
    sup._record(job, "MONITOR_STOPPED", NOW)
    recovered = sup.tick(info, now=NOW)
    assert recovered.status == JobStatus.RATE_LIMITED
    assert recovered.scheduled_resume is not None


def test_user_cancel_is_not_rearmed(monitor):
    from argparse import Namespace
    from codex_supervisor.cli import cmd_cancel
    sup, info, path = monitor
    limited(path, reset="2026-09-07T12:01:00Z")
    job = sup.tick(info, now=NOW)
    cmd_cancel(Namespace(job_id=job.job_id), sup.config, sup.store)
    assert sup.tick(info, now=NOW + dt.timedelta(minutes=2)).status == JobStatus.CANCELLED


def test_watch_lock_allows_one_owner(tmp_path):
    store = StateStore(tmp_path)
    with store.watch_lock():
        with pytest.raises(WatchAlreadyRunning):
            with store.watch_lock():
                pass


def test_corrupt_submission_history_fails_closed(monitor):
    sup, info, path = monitor
    limited(path, reset="2026-09-07T11:00:00Z")
    (sup.config.state_dir / "jobs" / f"{sup.job_id(SID)}.json").write_text("{")
    with patch("codex_supervisor.interactive.subprocess.run") as queue:
        with pytest.raises(ValueError, match="corrupt job"):
            sup.tick(info, now=NOW)
        queue.assert_not_called()


def test_custom_prompt_persists_without_new_events(monitor):
    sup, info, path = monitor
    append(path, "task_started")
    sup.tick(info, adopt=True, now=NOW)
    sup.tick(info, adopt=True, prompt="finish the task", now=NOW)
    assert sup.store.load_all_jobs()[0].resume_prompt == "finish the task"


def test_concurrent_watchers_submit_only_once(monitor, tmp_path):
    import fcntl
    sup, info, path = monitor
    home = tmp_path / "codex-home"
    locks = home / "thread-writer-locks"
    locks.mkdir(parents=True)
    writer = (locks / f"{SID}.lock").open("w")
    fcntl.flock(writer, fcntl.LOCK_EX)
    calls = tmp_path / "calls"
    fake = tmp_path / "fake-queue"
    fake.write_text("#!/usr/bin/env python3\nimport time\n" +
                   f"with open({str(calls)!r}, 'a') as f: f.write('queued\\n')\n" +
                   "time.sleep(.1)\n")
    fake.chmod(0o700)
    limited(path, reset="2026-09-07T11:00:00Z")
    code = (
        "import json,sys; from pathlib import Path; "
        "from codex_supervisor.interactive import InteractiveSupervisor; "
        "from codex_supervisor.models import SupervisorConfig; "
        "from codex_supervisor.state import StateStore; "
        "config=SupervisorConfig(state_dir=Path(sys.argv[1])); "
        "InteractiveSupervisor(config,StateStore(config.state_dir),sys.argv[2]).tick(json.loads(sys.argv[3]))"
    )
    env = {**os.environ, "CODEX_HOME": str(home), "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    processes = []
    try:
        for _ in range(2):
            processes.append(subprocess.Popen([sys.executable, "-c", code, str(sup.config.state_dir),
                                               str(fake), json.dumps(info)], env=env))
        assert [p.wait(timeout=5) for p in processes] == [0, 0]
        assert calls.read_text().splitlines() == ["queued"]
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
        writer.close()


def test_shutdown_before_submission_never_queues(monitor):
    sup, info, path = monitor
    limited(path, reset="2026-09-07T11:00:00Z")
    sup.stopped = True
    with patch("codex_supervisor.interactive.subprocess.run") as queue:
        sup.tick(info, now=NOW)
        queue.assert_not_called()


@pytest.mark.parametrize("field", ["fallback_wait_minutes", "backoff_base_seconds", "max_wait_seconds"])
def test_invalid_wait_settings_fail_early(tmp_path, field):
    with pytest.raises(ValueError, match="must be positive"):
        SupervisorConfig(state_dir=tmp_path, **{field: 0})
