# AI guide for framework maintainers

This file is for coding assistants working on this repository. It is not a system prompt for the benchmarked agent. Start with [README.md](README.md) for operator setup and [AGENTS.md](AGENTS.md) for repository instructions.

## Establish the execution context

- Work from this repository root. Inspect `git status --short` and preserve unrelated edits.
- The controller is the host Python process launched by `sie run` or `python -m science_innovation_exam run`. Agent and verifier processes run in separate containers.
- Use Linux and a disk-backed checkout. Check `findmnt -T .` before creating environments, caches or build artifacts. `/tmp`, `/dev/shm` and `/run` may be RAM-backed. Use `storage/` for local artifacts, never bulk-delete shared temporary directories.
- New installations should use Python 3.11 and the editable installation described in README. An existing `storage/controller-runtime/` is optional local infrastructure, not a prerequisite shipped by Git.
- The controller needs Docker access and permission to assign output ownership to UID/GID 65532. Docker group membership alone is insufficient. If using sudo, invoke the same virtual environment's Python explicitly.

## Credentials and configuration

- Scripted CPU checks require no model credentials, agent bundle, GPU or cluster account.
- Scientific task packages are supplied separately through YAML `task`; there is no built-in registry. `examples/` contains public infrastructure fixtures only. Keep hidden scientific data out of the framework, including any public Git history. See [task distribution](docs/task-packages.md).
- Public history begins with the reviewed 2026-10-21 snapshot. Older implementation history is a separate private archive containing operator data. Never merge, mirror or push that archive into this public repo; historical acceptance commit IDs are provenance references, not public ancestors.
- Codex runs require `OPENAI_BASE_URL` and `OPENAI_API_KEY` in the controller's launch environment. The endpoint must end in `/v1` and support the Responses API. `agent.model` and `agent.bundle` belong in YAML.
- The framework does not load `.env`. If an operator chooses that file, it must be loaded and exported explicitly before launching the controller. Normal sudo may discard those variables; README shows the narrow preserve-env form.
- Never print environment dumps, read private keys to diagnose connectivity, include secrets in shell arguments, or write credentials into YAML, images or tracked files. Check whether a variable is set without displaying its value.
- User-run configurations belong in ignored `configs/*.local.yaml`. Agent binaries, caches and run outputs are also excluded from Git. The tracked `.env.example` contains placeholders only.
- `SIE_ACTION_*`, `SIE_MAILBOX`, `SIE_VERIFY_SOCKET`, `SIE_VERIFY_CONTEXT` and `SIE_MODEL_GATEWAY_CREDENTIAL` are runtime details set by the framework, not user setup requirements.
- Task/output and agent file paths, plus `container_service.module_path`, resolve relative to the experiment YAML. Plugin-specific path semantics belong to that plugin.
- An external backend is loaded through `module` plus optional `module_path` and `create(config, run_dir)`. The main framework must run without private cluster code. Plugins depend on the framework interface and need compatible versions; a plugin is not a second controller.

## Read the relevant implementation before editing

| Concern | Source of truth |
|---|---|
| Defaults, allowed fields, path resolution | `src/science_innovation_exam/config.py` |
| Lifecycle, timing, final result and cleanup | `src/science_innovation_exam/runner.py` |
| Submission collection and package structure | `src/science_innovation_exam/task.py` |
| Backend contract and loading | `src/science_innovation_exam/container_service/interface.py`, `container_service/__init__.py` |
| Docker runtime and cleanup | `src/science_innovation_exam/container_service/docker.py` |
| Prompt composition, agent bundle and environment | `src/science_innovation_exam/agents.py`, `prompts/general.json` |
| Actions and retry identity | `src/science_innovation_exam/actions.py`, `action_client.py` |
| Offline scoring and per-run state | `src/science_innovation_exam/verifier.py`, `verify_context.py` |
| Model transport and request compatibility | `src/science_innovation_exam/model_gateway.py`, `codex_adapter.py` |
| Model activity, operational limits and external supervision | `src/science_innovation_exam/model_activity.py`, `model_monitor_config.py`, `model_monitor.py`, [supervision contract](docs/run-supervision.md) |
| Sandbox observation, sustained recovery and platform/exit attribution | `src/science_innovation_exam/sandbox_monitor.py`, backend `Handle.observe(timeout_seconds)` |
| Durable resource intents, live ownership, post-crash cleanup | `src/science_innovation_exam/resources.py`, `recovery.py`, `owned_command.py`, backend `recover_resources()` |
| Task author contract | `contributor/build-sie-task-package/task-contract.md` and `contracts/schemas/` |

Paths in the last column are relative to this repository; abbreviated source paths remain under `src/science_innovation_exam/`.

## Contracts to preserve

- Agent mounts contain public input and writable submission/state/control only. Never expose verifier code, hidden scoring input, the private verification context, host Docker socket or the entire run directory to the agent.
- `validation` is serialized and replayable by request ID. `test` and `abort` are terminal. Abort takes a nonempty free-text reason, with no predefined reason categories.
- Stop agent writers before freezing a submission. Copy declared regular files and complete directory trees (including empty directories) into an independent scoring directory mounted read-only. Ignore paths outside the declarations; reject missing paths, symlinks in declared paths or trees, and special files. File/directory shape requirements belong to the task verifier.
- Validation and final scoring run offline in separate containers. A failed invocation restores its preceding verification state. Do not change task data, scoring logic or reported historical outcomes as part of a framework-only fix.
- Preparation does not consume the agent's research budget. Keep preparation, execution and total wall time distinct; do not fabricate scores for infrastructure failures.
- Codex model deadlines are separate from the run budget. Supervise gateway metadata externally; keep unknown observation failures distinct from API failures. Heartbeats are not effective progress, and deliberate caller cancellation is not proof of an upstream fault. Do not extend coverage claims to arbitrary clients or local training.
- Sandbox supervision applies to all agents. Only explicit platform evidence confirms deletion or OOM; execution 404 and exit 137 alone do not. Keep observation queries bounded and require sustained recovery before resetting an outage. Query immediately before a terminal handoff, before deliberate stopping, not afterwards. Docker agent containers retain state until cleanup; never leave them behind after a normal run.
- Cleanup targets only resources owned by this run. Confirm absence before calling deletion successful. Do not discard a remote-only output copy after transfer failure.
- Persist resource intent before creation and the immutable provider ID when known. Reconcile lost acknowledgements by exact ownership/request metadata. Unknown absent creates remain pending; never infer safety from names alone. Hold RunLock for the whole controller/reclaimer lifetime, do not steal live locks, and preserve result history during cleanup. Linux parent-death guards prevent registered remote-storage writers from surviving the controller; verify stopped writers before purging storage.
- Final result persistence must survive diagnostic failures. Log processing skips links and special files. Resource cleanup failures remain infrastructure failures.
- Local Docker GPU access is explicit and agent-only. A GPU limit alone is not a request. The verifier currently receives no GPU. Remote GPU allocation belongs to external backends.
- All task and hardware policy, resource quantities, network settings, model selection and paths belong in contracts/configuration, not new site-specific constants in the main framework.

## Verify without confusing the evidence

The README CPU quickstart is a scripted infrastructure check. Run `check` first, then `configs/quickstart-cpu.yaml` with Docker if the change affects startup or configuration examples. Do not treat its score as a scientific baseline.

```bash
.venv/bin/python -m science_innovation_exam check examples/integer_echo
sudo .venv/bin/python -B tools/test.py
sudo .venv/bin/python -B tools/test-docker.py
```

Drop sudo when already privileged. Unit tests require local sockets and ownership operations; Docker tests use a local model stub and require openssl. Neither command calls a paid model. Test artifacts live on disk under `storage/`.

Choose checks for the change; documentation-only edits normally need link/config validation and a relevant smoke run, not newly invented unit tests. Do not claim real GPU, live-cluster or provider acceptance from mocked tests. A working task package does not by itself establish scientific validity or the availability of final scoring inputs.

## Results and scope

Runs are evidence directories under configured `output`. `result.json` is the run summary; actions, traces and submissions remain files. `context/state.sqlite3` is private state for a single run, not a global experiment database. `sie cleanup --run` uses the durable journal to reclaim after controller loss, appending recovery evidence without rewriting original outcomes. There is no cross-run database, scheduled sweeper or execution resume command. Keep run paths and originating host identity stable for recovery; old runs lacking journals are not automatically migrated.

Prompt changes belong in the runtime prompt layers. This AI guide and AGENTS.md are maintainer documentation; do not mount them or operator history into a tested agent as an incidental convenience. Report what changed, which checks actually ran and any unverified external prerequisites.
