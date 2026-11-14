# agents-dispatcher

Run coding-agent CLIs as supervised background workers, with routing, quota
awareness and a watchdog.

Status: pre-release. The first milestone runs one Claude Code worker per task.

## Usage

A run is started in the background and followed by its run id:

```
agentsd start  --repo <path> --task "<text>" [--hard-s <seconds>] [--memory-bytes <bytes>]
agentsd status <run id>
agentsd wait   <run id> [--timeout-s <seconds>]
agentsd stop   <run id> --reason "<text>"
agentsd report <run id>
agentsd run    --repo <path> --task "<text>" [--hard-s <seconds>] [--memory-bytes <bytes>]
```

- `start` launches a detached supervisor and returns at once with
  `{"run_id": ..., "state": ...}`. The run does not depend on the process that started it.
- `status` prints the stored view of the run, including whether its supervisor is alive.
- `wait` blocks until the run ends and prints `{"result": "READY", "status": ...}`, or
  `{"result": "PENDING", ...}` with exit code 3 when `--timeout-s` runs out.
- `stop` asks the supervisor to end the run and prints a receipt. The run then ends as STOPPED.
- `report` prints the stored JSON report of a finished run; an unfinished run exits with 3.
- `run` is `start` followed by `wait`: it prints exactly what `report` prints for that run.

The worker runs in a new Git worktree of the repository, behind a macOS Seatbelt profile
and an allowlist proxy, under a time and memory watchdog. Your checkout is never written;
the result lands on a new branch `agentsd-run-<run id>` (or on none when the worker changed
nothing). Each command prints one JSON object on standard output; messages go to standard
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

| Exit code | Meaning |
|---|---|
| 0 | completed (`start`, `status`, `stop` and `wait` also use 0 for "done what you asked") |
| 1 | the run could not be set up or an unexpected error occurred |
| 2 | invalid input, or the directory is not a Git work tree |
| 3 | pending: `wait` timed out or was interrupted, or `report` of an unfinished run |
| 10 | the worker failed |
| 13 | stopped |
| 14 | orphaned (the supervisor died; `wait` closes the run and stops its worker) |
| 16 | a resource limit was hit |
| 17 | the worker never started |
| 21 | unknown run id |
| 25 | the report is unavailable |

`wait` and `run` exit with the code of the run's end state.

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
