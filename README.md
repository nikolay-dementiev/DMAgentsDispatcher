# agents-dispatcher

Run coding-agent CLIs as supervised background workers, with routing, quota
awareness and a watchdog.

Status: pre-release. A task runs on one worker, Codex or Claude Code, chosen by the provider chain.

## Command line

`agentsd` is the whole interface: a task is started, followed and reported through these commands, and
an MCP client reaches the same operations through `agentsd mcp serve`.

<!-- cli-section:start -->
`agentsd` prints its answer as one JSON object on standard output (`mcp serve` speaks MCP instead). `--format text` shows the same answer for a person; `--json` and `--format json` are the default, and when several are given the last one wins. A failure is printed as a JSON object with an `error` member that holds its `code` and `message`. Messages for a person also go to standard error, and the exit code says how the command ended.

| Command | What it does | Example |
|---|---|---|
| `agentsd start` | Start one coding task in a supervised background run and return at once with its run id; follow it with `status`, `wait` or `report`. | `agentsd start --repo ./project --task "explain what main() does"` |
| `agentsd run` | Start one coding task, wait for the run to end and print its stored report. | `agentsd run --repo ./project --task "explain what main() does"` |
| `agentsd status` | Show where a run stands now; the exit code is 0 for every run state. | `agentsd status 0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e` |
| `agentsd wait` | Wait until a run ends or the timeout passes; a signal ends the wait and leaves the run alone. | `agentsd wait 0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e --timeout-s 60` |
| `agentsd stop` | Ask a run to stop and say why; the answer says what the request did. | `agentsd stop 0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e --reason "no longer needed"` |
| `agentsd report` | Print the stored report of a run that has ended. | `agentsd report 0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e` |
| `agentsd quarantine` | Work with a run that is held (QUARANTINED) because a worker process could not be shown gone. | `agentsd quarantine resolve 0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e` |
| `agentsd quarantine resolve` | Look again at a held run. | `agentsd quarantine resolve 0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e` |
| `agentsd mcp` | Serve the agentsd tools to an MCP client; the subcommand is `serve`. | `agentsd mcp serve` |
| `agentsd mcp serve` | Serve the tools over standard input and output until the client closes the input. | `agentsd mcp serve` |

Every command answers `--help`. The full reference, with the JSON each command prints and its exit codes, is [docs/cli.md](docs/cli.md).
<!-- cli-section:end -->

## Usage

A run is started in the background and followed by its run id:

```
agentsd start  --repo <path> --task "<text>" [--task-type <name>] [--provider <id>]
               [--hard-s <seconds>] [--memory-bytes <bytes>]
agentsd status <run id>
agentsd wait   <run id> [--timeout-s <seconds>]
agentsd stop   <run id> --reason "<text>"
agentsd report <run id>
agentsd quarantine resolve <run id> [--release --confirm <run id>]
agentsd run    --repo <path> --task "<text>" [--task-type <name>] [--provider <id>]
               [--hard-s <seconds>] [--memory-bytes <bytes>]
```

- `start` launches a detached supervisor and returns at once with
  `{"run_id": ..., "state": ...}`. The run does not depend on the process that started it.
- `status` prints the stored view of the run, including whether its supervisor is alive.
- `wait` blocks until the run ends and prints `{"result": "READY", "status": ...}`, or
  `{"result": "PENDING", ...}` with exit code 3 when `--timeout-s` runs out.
- `stop` asks the supervisor to end the run and prints a receipt. The run then ends as STOPPED.
- `report` prints the stored JSON report of a finished run; an unfinished run exits with 3.
- `quarantine resolve` looks again at a run that is held back for leftover processes (see
  Quarantine below).
- `run` is `start` followed by `wait`: it prints exactly what `report` prints for that run.

The worker runs in a new Git worktree of the repository, behind a macOS Seatbelt profile
and an allowlist proxy, under a time and memory watchdog. Your checkout is never written.
A write task (`--task-type implement`) delivers its result on a new branch
`agentsd-run-<run id>` (or on none when the worker changed nothing); a read task delivers
none (see below). Each command prints one JSON object on standard output; messages go to standard
error. The worker starts with no Claude Code setting sources, so your user, project and local
settings (language, model, effort) do not steer a run; its login is still used.

The worker's model and effort are the settings `providers.anthropic.model` (default
`claude-sonnet-5-5`, one token without whitespace) and `providers._template.effort` (default
`medium`; `low`, `medium`, `high`, `xhigh` or `max`), passed as `--model` and `--effort`. The
task text is at most `security.input_bytes` bytes of UTF-8 (default 1048576); a longer one is
refused with exit code 2 before anything is created. The report names the model in
`worker.model`, read from the `modelUsage` object of the CLI's result; when several models
answered, the one with the most tokens (input, output, cache read and cache creation) is
named, and the field is `null` when the result has none.

Run `agentsd <command> --help` to see a command's options, the output it prints and its exit
codes: every command answers `--help` and exits 0. The exit codes are:

| Exit code | Meaning |
|---|---|
| 0 | completed (`start`, `status`, `stop` and `wait` also use 0 for "done what you asked") |
| 1 | the run could not be set up, an unexpected error occurred, or `quarantine resolve` settled the run but a warning followed |
| 2 | invalid input or settings, an unknown task type, or the directory is not a Git work tree |
| 3 | pending: `wait` timed out or was interrupted, `report` of an unfinished run, or a run that is QUARANTINED (`wait`, `agentsd run` and `quarantine resolve` return at once with it) |
| 10 | the worker failed |
| 13 | stopped |
| 14 | orphaned (the supervisor died; `wait` closes the run and stops its worker) |
| 16 | a resource limit was hit |
| 17 | the worker never started |
| 20 | the submission was rejected: no provider of the chain could take the task |
| 21 | unknown run id |
| 24 | a write task type that `security.allow_write_tasks` does not list |
| 25 | the report is unavailable |

`wait` and `run` exit with the code of the run's end state.

### Task types, modes and routing

`--task-type <name>` picks a task type from the settings (`tasks.<name>`); it sets the mode
and the providers tried. The names are lowercase letters, digits and hyphens, starting with
a letter, at most 32 characters. Shipped: `audit` (read) and `implement` (write).

- **Without `--task-type` a task is a read task**, resolved from `[defaults]` alone and
  reported with `task_type: null`. Earlier versions made every task a write task: a result
  branch now needs `--task-type implement`.
- A **read task** runs in the same detached worktree at `HEAD`, but the Seatbelt profile
  denies every write to it, whatever the worker CLI is told. The worker may write only its
  temporary area inside the run sandbox, which is not preserved. The worker CLI is also
  started with its file-editing tools denied; a shell command still works and is still
  confined. A read task has no result branch (`branch: null`, `commits_on_branch: 0`); the
  worker's final text in `result_text` is the output. Uncommitted changes in the repository
  are not included.
- A **write task** behaves as before. A write task type must be listed in
  `security.allow_write_tasks`, or the submission ends with exit code 24 before anything is
  created.

The task's chain (`tasks.<name>.chain`, else `defaults.chain`) is walked in order: each
provider either takes the task or refuses it with a problem, and the first that takes it runs
it. `--provider <id>` evaluates only that provider of the chain, with no fallback and no
handoff; a provider that is not in the chain is invalid input (exit 2). Three outcomes:

1. **Started**: the run starts with the selected provider's model and effort (a link's own
   `model` and `effort` win over the provider's). `status` and `report` show `task_type`,
   `mode`, `provider` and `route` (every evaluated link, in order, with its disposition
   `SELECTED` or `REFUSED` and its problems).
2. **Recommendation**: every provider refused and the chain ends in `{ handoff = true }`.
   Nothing is created and the exit code is 0. Fields: `decision` (`"RECOMMENDATION"`),
   `task_type`, `model`, `effort` (from `defaults.recommendation_model` and
   `defaults.recommendation_effort`), `requirements` (`{"mode": ...}`), `refusals` (the
   evaluated links) and `boundary` (`"OUTSIDE_SDK_GUARANTEES"`: the SDK does not run, confine
   or watch whatever acts on the advice).
3. **Rejected**: every provider refused and there is no handoff. Nothing is created and the
   exit code is 20. The command prints the same failure object as the MCP tools:
   `error` (`code` `NO_PROVIDER_ELIGIBLE` and a message) and, beside it, `rejection`, with the
   members `decision` (`"REJECTED"`), `task_type`, `problems` (one `NO_PROVIDER_ELIGIBLE`) and
   `context` (the evaluated links).

The shipped chain tries `openai` first (the Codex CLI), then `anthropic` (Claude Code), then
hands the task back as advice. A refusal carries a code: `PROVIDER_NOT_LOADED` (the provider's
implementation is not part of this version), `PROVIDER_DISABLED`
(`providers.<id>.enabled = false`) or `EXECUTABLE_UNAVAILABLE` (the worker CLI cannot be
found), and, from the account checks below, `AUTH_REQUIRED`, `LOGIN_LOST`, `QUOTA_RESERVE`,
`QUOTA_STALE`, `QUOTA_UNAVAILABLE`, `QUOTA_UNSUPPORTED`, `QUOTA_CONFLICT`, `ACCOUNT_CHANGED`
and `CAPACITY_CONTENDED`.

### Accounts, quota and capacity

Every provider link is also checked against the provider's account before it takes a task.
The checks run in a confined helper (the vendor's own `login status` command, and for Codex
its app-server), never with your credentials in the SDK: each CLI keeps its own login.

- **Login.** `AUTH_REQUIRED`: the CLI reports that nobody is logged in. `LOGIN_LOST`: a run of
  that provider saw the login go from in to out, and no later look has seen it in again. A look
  that cannot be made (the command fails, prints nothing usable) is "unknown" and refuses
  nothing. The login is looked at again just before the launch and once more after the run;
  a login lost during a run is noted in the report.
- **Quota** (Codex only; Claude Code has no quota interface, so its quota is "unsupported" and
  only warns in `best_effort`). `defaults.quota_mode` is `required`, `best_effort` (default) or
  `off`. With evidence, a window with less than `defaults.quota_reserve_percent` percent left
  (default 15) refuses with `QUOTA_RESERVE`; evidence older than `defaults.quota_max_age_s`
  (default 30) is `QUOTA_STALE`; two replies that disagree are `QUOTA_CONFLICT`. Without
  evidence, `required` refuses (`QUOTA_UNAVAILABLE`, `QUOTA_UNSUPPORTED`) and `best_effort`
  admits with a warning. `profiles.openai.main.bound_bucket` names the quota bucket read
  (default `codex`).
- **Account.** The account is compared by a fingerprint (12 hex characters of a SHA-256 of
  the vendor's account id, never the id itself). A run admitted with one account and launched
  with another known one is refused with `ACCOUNT_CHANGED`.
- **Capacity.** `providers.<id>.capacity` (default 5) bounds the live runs of one provider; the
  check and the acceptance of the run are one store transaction, so concurrent submissions
  cannot exceed it. A full provider is `CAPACITY_CONTENDED`.

The evaluations of `route` (in `status` and `report`) carry `warnings` and `evidence`
(`login`, `quota` windows, and `account`: the login the account dialogue saw, and the
fingerprint when it names an account) for each link.

The inspector that runs the status commands has its own limits: `inspector.timeout_s` (default
20) and `inspector.memory_bytes` (default 1073741824). For Codex it needs write access to the Codex state files of your real
Codex home (SQLite files), which the worker itself never gets.

Limits of this version: there is **no quota cache**, every admission asks the vendor anew; the
Codex worker's `auth.json` stays readable to it and to the commands the model runs, and what
is read there can only reach `chatgpt.com`, the one host the proxy allows for `openai`; the
Codex shell has **no `apply_patch` alias** because the worker gets no `tmp` grant; Claude
Code has **no quota interface**; and a login look can be "unknown".

Every decision, started or not, is one line in `<logging.directory>/sdk.log` (a private file
next to the run logs), written before anything is created; the line never holds the task text.
The file is not rotated or purged in this version. A started run also logs its route in its
own run log. Every look of the account inspector (the login status command, the Codex account
dialogue) is one more `INSPECTION` line in the same file: the provider, the kind of look, its
outcome and each proxy decision as `host:port=allow` or `=refuse`; never the account itself.

Settings in this version: `defaults.mode`, `defaults.chain`, `defaults.recommendation_model`,
`defaults.recommendation_effort`, `tasks.<name>.mode` and `.chain`, `security.allow_write_tasks`,
`defaults.quota_mode`, `.quota_reserve_percent` and `.quota_max_age_s`,
`providers.<id>.enabled`, `.implementation`, `.model`, `.effort` and `.capacity`,
`profiles.<id>.main.bound_bucket`, `inspector.timeout_s`, and
`profiles._template.invocation_read` and `.invocation_write` (only `auto`). A settings file
that is malformed or holds an invalid value exits with code 2 and names the key. Not
supported yet: `tasks.<name>.write_roots` and the other `defaults` policy rows.

### MCP server

```
agentsd mcp serve
```

serves the tools below to an MCP client over standard input and output (newline-delimited
JSON-RPC, protocol versions 2025-06-18 and 2025-11-25). The client starts the process and ends it
by closing the input. Standard output carries only protocol frames: anything else that writes to
descriptor 1, a child process included, is sent to standard error instead.

| Tool | Does | Inputs |
|---|---|---|
| `route` | submit a task; the provider chain chooses the worker | `repo`, `task`, optional `task_type`, `hard_s`, `memory_bytes` |
| `start` | submit a task to one provider, with no fallback | the same, and `provider` |
| `status` | show a run | `run_id` |
| `wait` | wait for a run, at most `timeout_s` (default and maximum `mcp.wait_max_s`) | `run_id`, optional `timeout_s` |
| `stop` | ask a run to stop | `run_id`, `reason` |
| `report` | the stored report of a finished run, or `PENDING` | `run_id` |

Hand registration (there is no installer yet). Both commands only edit the client's own
configuration; give an absolute path to `agentsd` when it is not on the `PATH` of the client:

```
claude mcp add --scope user agents-dispatcher -- agentsd mcp serve
codex mcp add agents-dispatcher -- agentsd mcp serve
```

`claude mcp add` takes `--scope local|user|project` (default `local`). Check the registration with
`claude mcp list` or `codex mcp list`, and remove it with `claude mcp remove agents-dispatcher`
or `codex mcp remove agents-dispatcher`. A client may enforce its own request timeout: keep the `timeout_s` of `wait`
below it (`mcp.wait_max_s`, default 20, is the largest value the server accepts).

Every tool answers with the object the command of the same name prints, as `structuredContent`
and as one text block. A run that failed, was stopped or is still pending is data, not an error.
A call that did not do what was asked is a tool result with `isError: true` and
`{"error": {"code": ..., "message": ...}}`: `INVALID_ARGUMENT`, `UNKNOWN_RUN`,
`REPORT_UNAVAILABLE`, `AUTHORITY_VIOLATION`, `NO_PROVIDER_ELIGIBLE` (with the evaluated links in
`rejection`), `BUSY`, `SHUTTING_DOWN` (a call that was still queued when the server ended),
`RESULT_TOO_LARGE`, `INTERNAL` and the other codes of the command line. A
`timeout_s` above `mcp.wait_max_s` is refused, never clamped. An unknown tool or a malformed
message is a protocol error.

`status` and `stop` run on their own lane, so they answer while `wait` calls hold every worker of
the other lane. When a lane or the table of requests in flight is full, the call is answered at
once with `BUSY` and may be retried. At most `mcp.normal.workers` (default 6) `wait` calls can be
blocked at the same time, and `mcp.normal.queue` more can queue behind them. Cancelling a call
(`notifications/cancelled`) suppresses its answer; a `route` or `start` cancelled before its run is
committed creates none, one that already created a run is not undone, and the SDK log names
that run (`CANCELLED_AFTER_COMMIT`).

The settings under `[mcp]` (frame and input bounds, lane sizes, the timeouts, `wait_max_s`) are
read once, when the server starts. Change them, then let the client restart the server. The
server ends with code 0 when the input ends or on SIGTERM, SIGINT or SIGHUP, within
`mcp.shutdown_total_s`; runs that are in progress keep running and can be read with `agentsd
report`. It ends with code 1 when the client stops reading its output, or when no `initialize`
arrives within `mcp.initialize_timeout_s`. `mcp.transport = "sdk"` is reserved for an optional
extra that this build does not ship and exits with code 2. Events are logged, without arguments or
task text, as `MCP <EVENT> ...` lines in the SDK log (`sdk.log`). When the log cannot be written,
the first failure is shown on standard error, and a `MCP LOG_GAP lost=<n>` line follows once
writing works again.

### Containment

When a worker ends, every process that belongs to its run is found and ended before the run
is reported. A process is attributed to the run by its process group, by being remembered as a
descendant of the worker (seen while its parent was alive), by the `AGENTSD_RUN_ID` marker in
its environment, and by a working directory or open file inside the run folder. The folder rule
applies only to a process that started after the worker did, so a shell you opened in the folder
earlier is never signalled. Cleanup stops the processes first, then ends them, and revalidates
each process's identity (pid and start time) before every signal. A run is reported empty
only after consecutive complete looks find nothing; an ended process that has not been reaped (a
zombie) counts as gone.

Limits of a run, by how they are held:

| Limit | Setting | Held by |
|---|---|---|
| time | `watchdog.hard_s` | monitored by the watchdog |
| memory, summed over the tracked tree | `resources.run.memory_bytes` | monitored |
| captured output | `resources.capture.output_bytes` | monitored |
| open descriptors, summed | `resources.run.aggregate_descriptors` | monitored |
| run folder size and file count | `resources.run.sandbox_bytes`, `resources.run.sandbox_inodes` | monitored by a walk |
| size of one file | `resources.run.file_bytes` | enforced by the system (`RLIMIT_FSIZE`) |
| descriptors of one process | `resources.run.descriptors_per_process` | enforced by the system (`RLIMIT_NOFILE`) |
| free space at admission | `storage.free_floor_bytes` | checked when a run is accepted |

An enforced limit stops the offending call at once. A monitored limit is read at intervals
(`process.observation.*`, `resources.walk.*`), so a burst between two looks can pass it before
the run is stopped; the run then ends with exit code 16 and the setting named in its report.
When a look cannot be completed within its maximum age the run is stopped with the problem
`RESOURCE_OBSERVATION_LOST`. `watchdog.scan_s` is how often the watch loop wakes; the process
look has its own interval and runs only when the loop is awake.

Known gaps, stated as residual risk: descendants are found by polling the process table, with
no kqueue, so a process that starts and ends between two looks is not seen. A process that
leaves the group, clears its environment, never enters the run folder and is never observed
while its parent lives can escape attribution. A protected system daemon of your account that
refuses the listing of its open files is not attributed through them. The folder size and file
count cover files under the run folder; Git objects stored in your repository, including the
result commit written at the end, are not counted.

### Quarantine

If a process of a run cannot be ended or cannot be shown gone, the run is not reported as over.
It enters the state QUARANTINED with the problem `CLEANUP_UNCERTAIN`: it keeps its provider slot,
its sandbox stays, and `status` lists the survivors (pid, start time and why each is attributed)
and the action to take. The outcome the run would have had (COMPLETED stays COMPLETED) is kept
and applied when the run ends for real. A run whose supervisor died with leftovers is held the
same way.

When a supervisor dies, the sweep that closes its run applies the folder rule only to processes
that started no later than the run's last sign of life: the supervisor touches its owner lock
every few seconds while it lives, and that touch is the bound (a live worker group moves it to
the moment of the sweep, and a run with no sign of life attributes nothing by folder). A process
that started outside the run's life, before the run or after its last sign of life (for example a
shell you opened there to look at what the run left), is never signalled. A process that entered the folder in the last few seconds before a crash is
held, not killed. While such a process holds a path in the sandbox the run stays QUARANTINED and
the sandbox is kept; the process is named in the report notes and in the SDK log
(`HELD_RESIDUAL`), and a run with no sign of life is noted as `NO_SIGN_OF_LIFE`.

`agentsd quarantine resolve <run id>` runs the cleanup protocol again against the survivors
recorded when the run was quarantined, the marker and the run folder (a process that started
after the quarantine, or for a run the orphan sweep held, after the run's last sign of life, is
never attributed by the folder rule). When nothing is left the result
branch of a write run is sealed, the run ends with its own outcome, the sandbox is removed and
the slot is freed; a note records that absence was verified and that files may have changed after
the worker's exit. When a process is still alive the command exits with code 3 and the run stays
held. `agentsd start` also re-checks held runs after its sweep; a failed look is written to the
SDK log and never blocks the start.
The children that a surviving process starts later are followed and ended with it. A process
outside the run (for example a shell the owner opened in the folder) is never signalled, but while
one holds a path in the sandbox the run stays held and the sandbox is kept; the command names the
process, and a later look removes the sandbox once the folder is free. A process whose paths cannot
be read is named in the report notes and in the SDK log.

Releasing is the owner's decision and proves nothing about the processes:

```
agentsd quarantine resolve <run id> --release --confirm <run id>
```

`--confirm` must repeat the run id exactly, and `--release` is refused without it. The run ends
as TERMINAL with the reason FAILED and the note `released by operator`, the slot is freed, the
sandbox stays and the release is written to the SDK log with the run id and the time. A later
`quarantine resolve` or `start` that shows the run empty removes the sandbox. The command exists
only on the command line, not as an MCP tool.

### Interrupts

SIGINT or SIGTERM during `wait` ends the wait with exit code 3 and leaves the run alone.
During `run` the first one requests one stop with the reason "interrupted" and keeps waiting
for the run to end; a second one ends the waiting with exit code 3 (the stop request stays).
An interrupt while `run` or `start` is handing the run to its supervisor exits with code 3 and
names the run id, which keeps starting in the background. SIGTERM or SIGHUP sent to a
supervisor stops its worker, seals the work done so far on the result branch and ends the run
as STOPPED, with the signal named in the report notes.

### Limit of `stop`

`stop` is mediated by the run store: the request is a row in the SQLite store, which is
created with mode 0700 in your account. A process that can write that store can stop any run;
a confined worker cannot reach it and so cannot stop or inspect runs.

## Known limitation

A confined worker can still read the command line and environment of other processes of
the same user: a Seatbelt profile cannot deny that read. Keep secrets out of the environment
of long-lived processes of your account while a worker runs, and start the supervisor with a
minimal environment, for example:

```
env -i HOME="$HOME" PATH="$PATH" agentsd run --repo <path> --task "<text>"
```

## Development

The project uses `uv` and Python 3.12 or newer. The test suite is run on
Python 3.12, 3.13 and 3.14; 3.14 is the default interpreter of the project
environment. Run these from the repository root:

```
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked python -W error -m unittest discover -s tests -t . -v
```

## License

Apache License 2.0. See `LICENSE`.
