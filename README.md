# agents-dispatcher

Run coding-agent CLIs as supervised background workers, with routing, quota
awareness and a watchdog.

Status: pre-release. The first milestone runs one Claude Code worker per task.

## Usage

```
agentsd run --repo <path> --task "<text>" [--hard-s <seconds>] [--memory-bytes <bytes>]
```

The worker runs in a new Git worktree of the repository, behind a macOS Seatbelt profile
and an allowlist proxy, under a time and memory watchdog. Your checkout is never written;
the result lands on a new branch `agentsd-run-<run id>` (or on none when the worker changed
nothing). A JSON report is printed on standard output; messages go to standard error.

| Exit code | Meaning |
|---|---|
| 0 | completed |
| 1 | the run could not be set up or an unexpected error occurred |
| 2 | invalid input, or the directory is not a Git work tree |
| 10 | the worker failed |
| 13 | stopped |
| 14 | orphaned (a supervisor died; closed by a later start) |
| 16 | a resource limit was hit |
| 17 | the worker never started |

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
