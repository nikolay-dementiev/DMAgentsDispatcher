# Claude Code output fixtures

Output of `claude auth status` (Claude Code 2.1.304 and later) captured on 2026-11-15 and reduced
to the keys the login probe reads (`loggedIn`) plus the account keys the real output also carries
(`email`, `orgId`, `orgName`), whose values are scrubbed placeholders. The real output has more
keys; the probe reads `loggedIn` only and never keeps the rest. Exit status: 0 logged in, 1 not.

| File | Command | What it shows |
|---|---|---|
| `auth_status_in.json` | `claude auth status` with a login | `loggedIn: true` next to account fields that must not be kept |
| `auth_status_out.json` | the same with an empty home | `loggedIn: false`, exit 1 |
