# agents-dispatcher

Run coding-agent CLIs as supervised background workers, with routing, quota
awareness and a watchdog.

Status: pre-release. A task runs on one worker, Codex or Claude Code, chosen by the provider chain.

## Usage

A run is started in the background and followed by its run id:

```
agentsd start  --repo <path> --task "<text>" [--task-type <name>] [--provider <id>]
               [--hard-s <seconds>] [--memory-bytes <bytes>]
agentsd status <run id>
agentsd wait   <run id> [--timeout-s <seconds>]
agentsd stop   <run id> --reason "<text>"
agentsd report <run id>
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

| Exit code | Meaning |
|---|---|
| 0 | completed (`start`, `status`, `stop` and `wait` also use 0 for "done what you asked") |
| 1 | the run could not be set up or an unexpected error occurred |
| 2 | invalid input or settings, an unknown task type, or the directory is not a Git work tree |
| 3 | pending: `wait` timed out or was interrupted, or `report` of an unfinished run |
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
   exit code is 20. Fields: `decision` (`"REJECTED"`), `task_type`, `problems` (one
   `NO_PROVIDER_ELIGIBLE`) and `context` (the evaluated links).

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
(`login`, `quota` windows, `account` fingerprint) for each link.

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
own run log.

Settings in this version: `defaults.mode`, `defaults.chain`, `defaults.recommendation_model`,
`defaults.recommendation_effort`, `tasks.<name>.mode` and `.chain`, `security.allow_write_tasks`,
`defaults.quota_mode`, `.quota_reserve_percent` and `.quota_max_age_s`,
`providers.<id>.enabled`, `.implementation`, `.model`, `.effort` and `.capacity`,
`profiles.<id>.main.bound_bucket`, `inspector.timeout_s`, and
`profiles._template.invocation_read` and `.invocation_write` (only `auto`). A settings file
that is malformed or holds an invalid value exits with code 2 and names the key. Not
supported yet: `tasks.<name>.write_roots` and the other `defaults` policy rows.

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
