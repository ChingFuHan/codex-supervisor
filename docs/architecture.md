# Architecture

## Interactive supervision (default adopt/watch)

The user's Codex TUI remains the session writer. The supervisor owns only its own
state and short-lived `codex queue` subprocesses. It neither launches a second
writer nor sends signals to Codex, its descendants, or the user's shell.

```text
existing Codex TUI ── writes lifecycle events ── rollout JSONL
       ▲                                             │ read only
       │                                             ▼
       └── codex queue <continue> ◀── deadline ── InteractiveSupervisor
                                                     │
                                                 StateStore
```

`adopt UUID` monitors a specific open session. `--continue-now` additionally
submits one explicit continuation immediately. `watch` discovers local root CLI
sessions, filters by a live writer, and advances each session independently.
Neither command interprets historical goal status as current account quota.

`codex_db.py` only reads session metadata and Linux writer-lock information.
`interactive.py` incrementally tails complete JSONL records, retaining only the
latest lifecycle event. Partial writes are deferred; replacement/truncation resets
the reader. Assistant prose mentioning quotas is not a lifecycle error.

The global `watch` owns a non-blocking flock at `watch.lock`, so cron jobs,
manual commands and the user service cannot monitor the same state directory at
the same time. The service is the supported long-running entry point; it runs in
the user's systemd session and does not require root or a system unit.

## State and submission invariants

- New task start, successful completion, or user interruption supersedes an old
  rate-limit event. Pending deadlines are invalidated by manual activity.
- Only a terminal rate-limit error schedules an automatic continuation. Unknown
  TUI errors require inspection; the monitor does not force repeated turns.
- Reset clock times use the local timezone and the original event date. Full
  dates and ISO offsets are respected. Unparseable times use bounded exponential
  backoff; repeated polls do not shift the deadline.
- Each interactive job has a stable `sv-interactive-UUID` identifier. An exclusive
  per-job flock serializes decisions and cancellation. Atomic replacement with
  fsync persists submission intent **before** queue is invoked.
- `observed_event` identifies the latest processed lifecycle event;
  `submitted_event` prevents resending that event after process restart. A missing
  job may be created, but corrupt persisted state fails closed.
- `scheduled` after submission means queued, not running or completed. New
  lifecycle records provide progress evidence; only a successful `task_complete`
  marks a completed turn. The monitor remains available for later user turns.
- Without new lifecycle evidence after 120 seconds, mark the queue outcome
  unconfirmed and do not resend. A delayed event can still advance the state.
- Queue delivery is not transactional with supervisor state. The design prefers
  at-most-once submission over duplicate execution on an ambiguous crash. A crash
  after intent but before submission may need operator recovery.
- Cancellation is serialized against submissions. It stops future queue commands,
  not an already accepted Codex message. SIGINT/SIGTERM stop the monitor without
  signalling the original TUI or changing a waiting job's status. Explicit user
  cancellation and Codex turn interruption remain cancelled. Jobs written by the
  previous watcher version with only a final `MONITOR_STOPPED` record are reattached
  once; a `USER_CANCELLED` record is never reattached. Same-state-directory
  watchers share these guarantees.

Per-job logs record lifecycle transitions, queue intent/result, queue ID, retry
count, reset and wake timestamps. They do not copy the entire conversation.
Existing JSON jobs remain readable; missing `mode` defaults to `exec`.

## Native queue and PTY experiment

Installed Codex 0.153.4 supports `codex queue --thread UUID --message TEXT`.
An isolated TUI was launched under a PTY with read-only sandbox and a tiny prompt.
After its first successful response, queue submitted another message. The same
session produced a second `task_complete` and the expected response, while the
original TUI PID stayed alive. No shared app-server daemon was running; queue
worked with the ordinary local TUI. The app-server control socket therefore is
not required by this implementation.

The production supervisor does not need a PTY: the original terminal already owns
one. PTY setup, window sizing and cursor-position responses exist only in the
opt-in integration test. It injects a synthetic rate limit into a separate test
fixture, invokes the real supervisor queue path, and verifies actual completion
in the real rollout. It does not edit Codex's rollout or deliberately exhaust quota.

## Retained batch execution and legacy scheduler

`run -- codex exec --json ...` owns a noninteractive child, captures stdout/stderr,
classifies exit, and uses the existing in-process retry policy. `resume` dispatches
according to the persisted job mode. Current execution output takes precedence;
stale goals DB state cannot turn a successful exit or Ctrl+C into a rate limit.

`examples/codex-supervisor-watch.service` is the user-level service template. Install
it under `~/.config/systemd/user/`, then run `systemctl --user daemon-reload` and
`systemctl --user enable --now codex-supervisor-watch.service`. Stop only that
identified unit with `systemctl --user disable --now codex-supervisor-watch.service`.
The template includes the locally verified NVM Codex path; update that environment
entry after a Node/Codex upgrade. `scheduler.py` remains a legacy optional backend
with its existing timer tests. Never stop unrelated units or modify system-level
systemd.

## Boundaries

Local Linux and on-disk TUI sessions only. Metadata/rollout/writer-lock formats are
version-specific read-only compatibility dependencies. The writer check reflects
an instant in time: a TUI may close immediately afterward, leaving a queued message
for later consumption. Polling does not claim to infer whether an active turn is
stuck, awaiting input, or blocked by an approval. Explicit `--continue-now` exists
for operator-directed recovery. Codex continues to own all task execution,
approvals, sandbox policy and target repository writes.
