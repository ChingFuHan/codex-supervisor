# Codex CLI findings

Investigated locally on 2026-09-07: `codex-cli 0.153.4`, npm package `@openai/codex`.
Resolve the executable through PATH (`command -v codex`); no user's NVM path is hardcoded.

## Commands verified with installed help

| Command | Behavior |
| --- | --- |
| `codex [PROMPT]` | Interactive TUI |
| `codex resume [SESSION_ID] [PROMPT]` | Reopen an interactive session; prompt is optional |
| `codex queue --thread <UUID> --message <TEXT>` | Enqueue input for an existing session |
| `codex exec --json [PROMPT]` | Noninteractive JSONL output |
| `codex exec resume --json <UUID> <PROMPT>` | Noninteractive continuation; pass the prompt explicitly |
| `codex app-server proxy` | Connect to an existing app-server control socket |
| `codex app-server generate-json-schema --experimental --out <DIR>` | Generate protocol schemas matching the installed binary |

`exec resume` without a prompt can attempt to read stdin, which fails with DEVNULL.
`resume` without a prompt is not evidence that a fresh turn was requested.
A second writer for the same session can fail with `already has an active writer`.
The supervisor must not solve this by killing an existing interactive writer.

## Native queue experiment

A real local TUI in an isolated temporary directory completed an initial tiny prompt.
While that same process stayed alive:

```bash
codex queue --thread <test-session-uuid> --message "Print SUPERVISOR_QUEUE_OK only. Do not use tools."
```

Returned exit 0 and `Queued message <queue-uuid> for thread <session-uuid>.`
The existing TUI consumed the message, displayed `SUPERVISOR_QUEUE_OK`, and recorded
a second successful `task_complete` with a different turn ID. The original PID
remained alive. No app-server control socket/daemon was present or needed.

The repeatable `tests/test_interactive_smoke.py` additionally drives the actual
supervisor path: a synthetic limit fixture expires, the supervisor queues
`continue`, and the real TUI completes `SUPERVISOR_RESUMED_OK` in the same session.
This tests recovery plumbing without exhausting real quota or editing Codex state.

## Session metadata and lifecycle

The local `state_5.sqlite` registry provides `id`, `cwd`, `rollout_path`, `source`
and `archived`. Root local interactive sessions have `source='cli'`. The tested
paginated session also appended lifecycle records to its registered JSONL path.
All supervisor access to these files is read-only.

```jsonl
{"timestamp":"2026-09-07T12:15:19Z","type":"event_msg","payload":{"type":"task_started","turn_id":"<turn-id>"}}
{"timestamp":"2026-09-07T12:15:22Z","type":"event_msg","payload":{"type":"task_complete","turn_id":"<turn-id>","last_agent_message":"SUPERVISOR_QUEUE_OK"}}
```

Rate limit errors observed in real historical lifecycle records:

```json
{"type":"task_complete","turn_id":"<turn-id>","error":{"codex_error_info":"usage_limit_exceeded","message":"You've hit your usage limit. ... try again at Sep 7th, 2026 2:04 AM."}}
```

Also observed: `try again at 12:10 PM.` These displayed clocks use local time.
The parser anchors them to the error timestamp, rather than advancing an old
reset date on each new scan. Old `goals_1.sqlite` status is not current quota proof.

An active local writer holds an advisory exclusive FLOCK on
`$CODEX_HOME/thread-writer-locks/<UUID>.lock`. The supervisor matches its inode and
device against `/proc/locks`, without acquiring, removing or changing the lock.
A lock-file's mere existence is insufficient. A suspended writer still owns the
lock and may not consume queued messages; lack of progress is reported separately.

## Batch mode

Public `exec --json` output is distinct from internal rollout records:

```jsonl
{"type":"thread.started","thread_id":"<UUID>"}
{"type":"turn.started"}
{"type":"error","message":"You've hit your usage limit. ..."}
{"type":"turn.failed","error":{"message":"You've hit your usage limit. ..."}}
```

The batch supervisor captures session ID from `thread.started.thread_id` and uses
its existing exit/retry policy. Starting a thread or turn is not proof of completed
work. `--ephemeral` cannot support persisted session continuation.

## Sources and compatibility

- [Official CLI reference](https://developers.openai.com/codex/cli/reference)
- [Official app-server protocol](https://developers.openai.com/codex/app-server)
- Installed CLI help and schema output for **0.153.4**, plus the isolated experiments above.

The fetched official CLI reference did not document the `codex queue` subcommand;
its exact syntax and behavior are established here by local help and execution.
App-server `thread/read` is available as a read-only interface, but a normal local
TUI need not expose the control socket, so the monitor uses the observed local
rollout path instead. Codex metadata, rollout and lock layout remain version-specific
compatibility dependencies; remote and ephemeral sessions are outside this mode.
