# Framework and task packages

The framework runs packages; it does not maintain a built-in scientific task
registry. `sie check PATH` and the YAML `task` field accept independently
supplied directories. Relative paths resolve from the YAML file, not the shell.

For example, if the operator's configuration is `/srv/experiments/run.yaml`:

```yaml
task: /srv/benchmark-tasks/my_task
output: /srv/experiments/runs
# agent, resources and other settings follow the README example.
```

Keep the task's five-part structure unchanged. See the
[package contract](../contributor/build-sie-task-package/task-contract.md).
The controller needs the whole package; the agent receives only its public
instruction, input and submission templates. An external container backend may
add storage accessibility requirements; configure those in that backend.

The public [examples](../examples/README.md) contain an integer-copy check and
a GPU computation check, with no hidden scientific answers. They are useful for
installation acceptance, not evidence of scientific ability.

The former protein package, its scripted acceptance producer and operator CPU
configuration were moved to a separate local task repository on 2026-10-19.
Package bytes and historical run artifacts were preserved. No runtime loader
changes were needed. Formal task data is governed by its own provenance and
distribution terms; the framework license does not relicense external data.

The public framework starts from a reviewed snapshot with fresh Git history
(2026-10-21). Earlier implementation history contains operator scoring data and
is retained in a separate private archive. Historical commit IDs in acceptance
notes refer to that archive, not public Git ancestors. Scientific task packages,
credentials, private adapters, agent bundles and experiment outputs are absent
from the public snapshot.

Removing files from a current tree does not remove them from old Git commits.
Never merge or mirror the private archive into the public repository. Future
releases must continue reviewing the tracked tree and reachable history for
operator-only data; keep original experiment evidence privately.
