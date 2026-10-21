# Agent CLI bundles

Agent binaries are not included in Git and are not downloaded automatically.
Provision a compatible bundle on the controller's disk and set `agent.bundle`
to its directory (absolute, or relative to the experiment YAML).

The current Codex adapter expects this entry point:

```text
bundle/
  bin/codex
  ...runtime dependencies...
```

`bin/codex` must be executable inside the selected Linux container, not merely
on the host. The recorded acceptance used a Linux x86_64 bundle reporting
version 0.147.0. The adapter invokes `--strict-config`,
`--dangerously-bypass-approvals-and-sandbox`, `exec --json`,
`--skip-git-repo-check` and `--ignore-rules`; confirm that your provisioned CLI
supports these flags. An arbitrary globally installed Codex version is not a
verified replacement. There is currently no repository command that builds or
installs this compatible bundle for a new user.

Keep the bundle self-contained and free of symbolic links: content fingerprinting
rejects links and special files. If a launcher relies on Node or another runtime,
include that runtime or install it in the container image. Do not include login
state, API credentials, user configuration or downloaded model weights.

The controller records the bundle's content fingerprint in
`agent-state/program.json`; users do not supply a hash manually. Model routing
and API credentials come from the controller's `OPENAI_BASE_URL` and
`OPENAI_API_KEY`, not a login file in this directory. See the repository
[README](../../README.md) for environment setup.
