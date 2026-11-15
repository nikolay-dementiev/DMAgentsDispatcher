# Codex output fixtures

Real output of `codex-cli 0.160.0` on macOS (arm64), captured on 2026-11-15 under the SDK sandbox
and proxy on throwaway repositories. Account fields are scrubbed: e-mail addresses become
`user@example.invalid`, every UUID is replaced by a fixed fake one, the host name and home paths
are generic.

| File | Command | What it shows |
|---|---|---|
| `exec_read_ok.jsonl` | `codex exec --json ... -m <model> -c model_reasoning_effort="low" -` with the brief "Reply with the single word ok." | the event stream of a completed turn with a final agent message |
| `exec_write_command.jsonl` | the same with a brief that asks for a new file | a command execution item between the messages |
| `appserver_dialogue.jsonl` | `codex app-server --listen stdio://`, then `initialize`, `initialized`, and `account/read`, `account/rateLimits/read` alternating | the replies with a logged-in account, including unsolicited notifications |
| `appserver_unauthenticated.jsonl` | the same dialogue with an empty Codex home | `account` null and the JSON-RPC error of the rate limit read |

Fake executables and fake servers in the tests return only shapes found in these files.

| `login_status_in.txt`, `login_status_out.txt` | `codex login status`, logged in (exit 0) and with an empty Codex home (exit 1) | the one-line texts the login probe reads (the CLI prints them on standard error); the command works with the network denied |
