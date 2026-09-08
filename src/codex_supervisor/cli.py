"""
CLI: codex-supervisor run/resume/adopt/watch/cancel/jobs/status
"""

import argparse
import logging
import subprocess
import sys

from .config import load_config
from .state import StateStore
from .supervisor import CodexSupervisor


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def cmd_run(args, config, store) -> int:
    if not args.codex_args:
        print("error: no codex command specified after --", file=sys.stderr)
        return 1
    sup = CodexSupervisor(config, store)
    return sup.run(args.codex_args, work_dir=args.work_dir)


def cmd_resume(args, config, store) -> int:
    sup = CodexSupervisor(config, store)
    return sup.resume(args.job_id)


def cmd_cancel(args, config, store) -> int:
    import datetime
    import json
    from .models import JobStatus
    with store.lock_job(args.job_id):
        job = store.load_job(args.job_id)
        if job is None:
            print(f"error: job {args.job_id} not found", file=sys.stderr)
            return 1
        job.status = JobStatus.CANCELLED
        job.scheduled_resume = None
        store.save_job(job)
        with store.log_path(job.job_id).open("a") as stream:
            stream.write(json.dumps({
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "job": job.job_id,
                "session": job.session_id,
                "event": "USER_CANCELLED",
                "status": job.status.value,
            }) + "\n")
    print(f"job {args.job_id} cancelled", file=sys.stderr)
    if job.queued_at:
        print("A message already submitted to Codex may still execute; cancellation stops future submissions.",
              file=sys.stderr)
    return 0


def cmd_adopt(args, config, store) -> int:
    sup = CodexSupervisor(config, store)
    return sup.adopt(args.session_id, prompt=args.prompt, interval=args.interval,
                     continue_now=args.continue_now)


def cmd_watch(args, config, store) -> int:
    sup = CodexSupervisor(config, store)
    return sup.watch(poll_interval=args.interval, prompt=args.prompt)


def cmd_jobs(args, config, store) -> int:
    jobs = store.load_all_jobs()
    if not jobs:
        print("no jobs found")
        return 0
    fmt = "{:<26} {:<12} {:<12} {}"
    print(fmt.format("JOB_ID", "STATUS", "SESSION", "COMMAND"))
    print("-" * 80)
    for job in sorted(jobs, key=lambda j: j.created_at, reverse=True):
        session = (job.session_id or "")[:12]
        cmd = " ".join(job.codex_command)[:40]
        print(fmt.format(job.job_id, job.status.value, session, cmd))
    return 0


def cmd_status(args, config, store) -> int:
    jobs = store.load_all_jobs()
    if not jobs:
        print("no jobs")
        return 0
    # Show most recent job detail
    job = sorted(jobs, key=lambda j: j.created_at, reverse=True)[0]
    print(f"job_id:            {job.job_id}")
    print(f"status:            {job.status.value}")
    print(f"session_id:        {job.session_id or '(none)'}")
    print(f"mode:              {job.mode}")
    print(f"exit_class:        {job.exit_classification or '(none)'}")
    print(f"retry_count:       {job.retry_count}")
    print(f"rate_limit_retries:{job.rate_limit_retries}")
    print(f"last_error:        {job.last_error or '(none)'}")
    print(f"scheduled_resume:  {job.scheduled_resume or '(none)'}")
    print(f"parsed_reset:      {job.parsed_reset or '(none)'}")
    print(f"queue_id:          {job.queue_id or '(none)'}")
    print(f"queued_at:         {job.queued_at or '(none)'}")
    print(f"continuation:      {job.continuation_outcome or '(none)'}")
    print(f"continuation_turn: {job.continuation_turn_id or '(none)'}")
    print(f"continuation_start:{job.continuation_started_at or '(none)'}")
    print(f"last_progress_at:  {job.last_progress_at or '(none)'}")
    print(f"progress_items:    {job.progress_item_count}")
    print(f"progress_types:    {', '.join(job.progress_summary) or '(none)'}")
    print(f"command:           {' '.join(job.codex_command)}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="codex-supervisor",
        description="Supervisor for Codex CLI — auto-resumes after usage/rate limits",
    )
    parser.add_argument("-v", "--verbose", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Start a supervised Codex job")
    run_p.add_argument("-C", "--work-dir", dest="work_dir", default=None,
                       help="Working directory for Codex")
    run_p.add_argument("codex_args", nargs=argparse.REMAINDER,
                       help="Codex command (everything after --)")

    resume_p = sub.add_parser("resume", help="Resume a scheduled job")
    resume_p.add_argument("job_id")

    cancel_p = sub.add_parser("cancel", help="Cancel a job")
    cancel_p.add_argument("job_id")

    adopt_p = sub.add_parser("adopt", help="Monitor an open Codex TUI; queue continuation after rate limits")
    adopt_p.add_argument("session_id", help="Codex session UUID (thread_id)")
    adopt_p.add_argument("--prompt", default="continue",
                         help="Prompt to send when resuming (default: continue)")
    adopt_p.add_argument("--interval", type=int, default=5,
                         help="Seconds between lifecycle checks (default: 5)")
    adopt_p.add_argument("--continue-now", action="store_true",
                         help="Also queue one continuation immediately (explicit manual recovery)")

    watch_p = sub.add_parser("watch", help="Monitor local interactive sessions; queue after rate limits")
    watch_p.add_argument("--interval", type=int, default=5,
                         help="Seconds between scans (default: 5)")
    watch_p.add_argument("--prompt", default="continue",
                         help="Prompt to send when resuming (default: continue)")

    sub.add_parser("jobs", help="List all jobs")
    sub.add_parser("status", help="Show most recent job status")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    # Strip leading '--' from codex_args if present
    if hasattr(args, "codex_args") and args.codex_args and args.codex_args[0] == "--":
        args.codex_args = args.codex_args[1:]

    dispatch = {
        "run": cmd_run,
        "resume": cmd_resume,
        "adopt": cmd_adopt,
        "watch": cmd_watch,
        "cancel": cmd_cancel,
        "jobs": cmd_jobs,
        "status": cmd_status,
    }
    try:
        config = load_config()
        store = StateStore(config.state_dir)
        return dispatch[args.command](args, config, store)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
