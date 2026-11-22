# agentsd command line reference

This file is generated from the command catalogue. Do not edit it by hand: run `python -m tests.support.cli_docs --write` from the repository root.

`agentsd` prints its answer as one JSON object on standard output (`mcp serve` speaks MCP instead). There is no `--json` flag: JSON is the only output format. Messages for a person go to standard error, and the exit code says how the command ended.

Every command answers `-h` and `--help` with its arguments, its output, its exit codes and an example; `agentsd --version` prints the version.

## Exit codes

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

## Commands

### `agentsd start`

Start one coding task in a supervised background run and return at once with its run id; follow it with `status`, `wait` or `report`.

Usage: `agentsd start --repo REPO --task TASK [--task-type TASK_TYPE] [--provider PROVIDER] [--hard-s HARD_S] [--memory-bytes MEMORY_BYTES]`

| Argument | Meaning |
|---|---|
| `--repo REPO` (required) | path inside a local Git working tree |
| `--task TASK` (required) | the task text; sent on standard input |
| `--task-type TASK_TYPE` | the kind of task: sets its mode (read or write) and the providers tried, in order |
| `--provider PROVIDER` | use only this provider of the task's chain, with no fallback |
| `--hard-s HARD_S` | override the hard time limit, in seconds |
| `--memory-bytes MEMORY_BYTES` | override the memory limit, in bytes |

Output: one JSON object with the members run_id and state; when no provider takes the task, a recommendation object (exit 0) or a rejection object (exit 20) instead.

```json
{
  "run_id": "0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e",
  "state": "STARTING"
}
```

Exit codes:

| Exit code | Meaning |
|---|---|
| 0 | completed (`start`, `status`, `stop` and `wait` also use 0 for "done what you asked") |
| 1 | the run could not be set up, an unexpected error occurred, or `quarantine resolve` settled the run but a warning followed |
| 2 | invalid input or settings, an unknown task type, or the directory is not a Git work tree |
| 3 | pending: `wait` timed out or was interrupted, `report` of an unfinished run, or a run that is QUARANTINED (`wait`, `agentsd run` and `quarantine resolve` return at once with it) |
| 17 | the worker never started |
| 20 | the submission was rejected: no provider of the chain could take the task |
| 24 | a write task type that `security.allow_write_tasks` does not list |

Example:

```bash
agentsd start --repo ./project --task "explain what main() does"
```

### `agentsd run`

Start one coding task, wait for the run to end and print its stored report. The exit code is the one of the run's end state.

Usage: `agentsd run --repo REPO --task TASK [--task-type TASK_TYPE] [--provider PROVIDER] [--hard-s HARD_S] [--memory-bytes MEMORY_BYTES]`

| Argument | Meaning |
|---|---|
| `--repo REPO` (required) | path inside a local Git working tree |
| `--task TASK` (required) | the task text; sent on standard input |
| `--task-type TASK_TYPE` | the kind of task: sets its mode (read or write) and the providers tried, in order |
| `--provider PROVIDER` | use only this provider of the task's chain, with no fallback |
| `--hard-s HARD_S` | override the hard time limit, in seconds |
| `--memory-bytes MEMORY_BYTES` | override the memory limit, in bytes |

Output: the stored report of the run as one JSON object; for a run that has not ended, an object with "result": "PENDING" and the status.

```json
{
  "run_id": "0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e",
  "state": "TERMINAL",
  "terminal_reason": "COMPLETED",
  "problem": null,
  "task_type": "review",
  "mode": "read",
  "provider": "anthropic",
  "repo": "/work/project",
  "base_commit": "0123456789abcdef0123456789abcdef01234567",
  "branch": null,
  "commits_on_branch": 0,
  "worker": {
    "cli_version": "2.1.304",
    "exit_status": 0,
    "model": "claude-sonnet-5-5",
    "model_source": "reported"
  },
  "route": [
    {
      "provider": "anthropic",
      "disposition": "SELECTED",
      "problems": [],
      "model": "claude-sonnet-5-5",
      "effort": "medium"
    }
  ],
  "duration_s": 12.5,
  "limits": {
    "configured": {
      "hard_s": 3600.0,
      "memory_bytes": 4294967296
    },
    "hit": null
  },
  "survivors": [],
  "result_text": "main() parses the arguments and starts the loop.",
  "notes": []
}
```

Exit codes:

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
| 24 | a write task type that `security.allow_write_tasks` does not list |
| 25 | the report is unavailable |

Example:

```bash
agentsd run --repo ./project --task "explain what main() does"
```

### `agentsd status`

Show where a run stands now; the exit code is 0 for every run state.

Usage: `agentsd status RUN_ID`

| Argument | Meaning |
|---|---|
| `RUN_ID` (required) | the run id that `start` printed |

Output: one JSON object with the members target, revision, run, route, owner_alive and stop, and a quarantine member while the run is held.

```json
{
  "target": {
    "run_id": "0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e"
  },
  "revision": 3,
  "run": {
    "run_id": "0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e",
    "task": "explain what main() does",
    "task_type": "review",
    "mode": "read",
    "provider": "anthropic",
    "state": "RUNNING",
    "reason": null,
    "branch": null
  },
  "route": [
    {
      "provider": "anthropic",
      "disposition": "SELECTED",
      "problems": [],
      "model": "claude-sonnet-5-5",
      "effort": "medium"
    }
  ],
  "owner_alive": true,
  "stop": null
}
```

Exit codes:

| Exit code | Meaning |
|---|---|
| 0 | completed (`start`, `status`, `stop` and `wait` also use 0 for "done what you asked") |
| 1 | the run could not be set up, an unexpected error occurred, or `quarantine resolve` settled the run but a warning followed |
| 2 | invalid input or settings, an unknown task type, or the directory is not a Git work tree |
| 21 | unknown run id |

Example:

```bash
agentsd status 0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e
```

### `agentsd wait`

Wait until a run ends or the timeout passes; a signal ends the wait and leaves the run alone. The exit code is the one of the run's end state.

Usage: `agentsd wait RUN_ID [--timeout-s TIMEOUT_S]`

| Argument | Meaning |
|---|---|
| `RUN_ID` (required) | the run id that `start` printed |
| `--timeout-s TIMEOUT_S` | give up waiting after this long |

Output: one JSON object with the members result ("READY" or "PENDING") and status.

```json
{
  "result": "PENDING",
  "status": {
    "target": {
      "run_id": "0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e"
    },
    "revision": 3,
    "run": {
      "run_id": "0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e",
      "task": "explain what main() does",
      "task_type": "review",
      "mode": "read",
      "provider": "anthropic",
      "state": "RUNNING",
      "reason": null,
      "branch": null
    },
    "route": [
      {
        "provider": "anthropic",
        "disposition": "SELECTED",
        "problems": [],
        "model": "claude-sonnet-5-5",
        "effort": "medium"
      }
    ],
    "owner_alive": true,
    "stop": null
  }
}
```

Exit codes:

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
| 21 | unknown run id |

Example:

```bash
agentsd wait 0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e --timeout-s 60
```

### `agentsd stop`

Ask a run to stop and say why; the answer says what the request did.

Usage: `agentsd stop RUN_ID --reason REASON`

| Argument | Meaning |
|---|---|
| `RUN_ID` (required) | the run id that `start` printed |
| `--reason REASON` (required) | why the run is stopped |

Output: one JSON object with the members run_id, disposition and primary_cause.

```json
{
  "run_id": "0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e",
  "disposition": "EFFECTIVE",
  "primary_cause": null
}
```

Exit codes:

| Exit code | Meaning |
|---|---|
| 0 | completed (`start`, `status`, `stop` and `wait` also use 0 for "done what you asked") |
| 1 | the run could not be set up, an unexpected error occurred, or `quarantine resolve` settled the run but a warning followed |
| 2 | invalid input or settings, an unknown task type, or the directory is not a Git work tree |
| 21 | unknown run id |

Example:

```bash
agentsd stop 0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e --reason "no longer needed"
```

### `agentsd report`

Print the stored report of a run that has ended.

Usage: `agentsd report RUN_ID`

| Argument | Meaning |
|---|---|
| `RUN_ID` (required) | the run id that `start` printed |

Output: the stored report of the run as one JSON object; for a run that has not ended, an object with "result": "PENDING" and the status.

```json
{
  "run_id": "0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e",
  "state": "TERMINAL",
  "terminal_reason": "COMPLETED",
  "problem": null,
  "task_type": "review",
  "mode": "read",
  "provider": "anthropic",
  "repo": "/work/project",
  "base_commit": "0123456789abcdef0123456789abcdef01234567",
  "branch": null,
  "commits_on_branch": 0,
  "worker": {
    "cli_version": "2.1.304",
    "exit_status": 0,
    "model": "claude-sonnet-5-5",
    "model_source": "reported"
  },
  "route": [
    {
      "provider": "anthropic",
      "disposition": "SELECTED",
      "problems": [],
      "model": "claude-sonnet-5-5",
      "effort": "medium"
    }
  ],
  "duration_s": 12.5,
  "limits": {
    "configured": {
      "hard_s": 3600.0,
      "memory_bytes": 4294967296
    },
    "hit": null
  },
  "survivors": [],
  "result_text": "main() parses the arguments and starts the loop.",
  "notes": []
}
```

Exit codes:

| Exit code | Meaning |
|---|---|
| 0 | completed (`start`, `status`, `stop` and `wait` also use 0 for "done what you asked") |
| 1 | the run could not be set up, an unexpected error occurred, or `quarantine resolve` settled the run but a warning followed |
| 2 | invalid input or settings, an unknown task type, or the directory is not a Git work tree |
| 3 | pending: `wait` timed out or was interrupted, `report` of an unfinished run, or a run that is QUARANTINED (`wait`, `agentsd run` and `quarantine resolve` return at once with it) |
| 21 | unknown run id |
| 25 | the report is unavailable |

Example:

```bash
agentsd report 0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e
```

### `agentsd quarantine`

Work with a run that is held (QUARANTINED) because a worker process could not be shown gone. The subcommand `resolve` looks at it again.

Usage: `agentsd quarantine`

Output: nothing; the command needs a subcommand.

Exit codes:

| Exit code | Meaning |
|---|---|
| 0 | completed (`start`, `status`, `stop` and `wait` also use 0 for "done what you asked") |
| 2 | invalid input or settings, an unknown task type, or the directory is not a Git work tree |

Example:

```bash
agentsd quarantine resolve 0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e
```

### `agentsd quarantine resolve`

Look again at a held run. A process that started outside the run's life, before the run or after its last sign of life, is never signalled. With --release and --confirm (the run id repeated) the run is released at your risk.

Usage: `agentsd quarantine resolve RUN_ID [--release] [--confirm CONFIRM]`

| Argument | Meaning |
|---|---|
| `RUN_ID` (required) | the run id that `start` printed |
| `--release` | accept the risk of leftover processes and end the run (needs --confirm) |
| `--confirm CONFIRM` | repeat the run id to confirm a release |

Output: one JSON object with the members run_id, outcome, survivors and warnings.

```json
{
  "run_id": "0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e",
  "outcome": "SETTLED",
  "survivors": [],
  "warnings": []
}
```

Exit codes:

| Exit code | Meaning |
|---|---|
| 0 | completed (`start`, `status`, `stop` and `wait` also use 0 for "done what you asked") |
| 1 | the run could not be set up, an unexpected error occurred, or `quarantine resolve` settled the run but a warning followed |
| 2 | invalid input or settings, an unknown task type, or the directory is not a Git work tree |
| 3 | pending: `wait` timed out or was interrupted, `report` of an unfinished run, or a run that is QUARANTINED (`wait`, `agentsd run` and `quarantine resolve` return at once with it) |
| 21 | unknown run id |

Example:

```bash
agentsd quarantine resolve 0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e
```

### `agentsd mcp`

Serve the agentsd tools to an MCP client; the subcommand is `serve`.

Usage: `agentsd mcp`

Output: nothing; the command needs a subcommand.

Exit codes:

| Exit code | Meaning |
|---|---|
| 0 | completed (`start`, `status`, `stop` and `wait` also use 0 for "done what you asked") |
| 2 | invalid input or settings, an unknown task type, or the directory is not a Git work tree |

Example:

```bash
agentsd mcp serve
```

### `agentsd mcp serve`

Serve the tools over standard input and output until the client closes the input.

Usage: `agentsd mcp serve`

Output: MCP messages on standard output, not JSON for a person.

Exit codes:

| Exit code | Meaning |
|---|---|
| 0 | completed (`start`, `status`, `stop` and `wait` also use 0 for "done what you asked") |
| 1 | the run could not be set up, an unexpected error occurred, or `quarantine resolve` settled the run but a warning followed |
| 2 | invalid input or settings, an unknown task type, or the directory is not a Git work tree |

Example:

```bash
agentsd mcp serve
```
