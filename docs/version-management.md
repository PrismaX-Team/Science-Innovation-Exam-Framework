# Version management

Public distribution starts with a fresh reviewed snapshot on 2026-10-21. Older
commit IDs and acceptance records below describe privately retained development
history; they are not ancestors of the public initial commit. The previous GitHub
repository remains private as `Science-Innovation-Exam-Framework-Private-History`.
Never merge or mirror that archive into public history. The new public tree keeps
the tested runtime code, examples, documentation and MIT license, excluding old
scientific scoring inputs, credentials, bundles and run artifacts. The site adapter
is independently published to the private `Science-Innovation-Exam-D-Cluster-Adapter`
repository with member/AI setup guidance. No runtime behavior changes in publication.

The framework and optional site adapters are independent Git repositories.
Adapters are loaded through YAML; no relative checkout location is required.

The 2026-10-12 framework snapshot captures the existing working implementation
before remote GPU development. Its compatible adapter snapshot is
`a5e1d39b3dca19a14c0e61e55fdf832379f1b0c8`.

Validation: `python3 -B tools/test.py`: 58 tests, 51 passed and 7 real-Docker
tests skipped. Tests need local sockets and ownership changes; they passed
outside the restricted execution sandbox. No paid model, real Docker or live
cluster acceptance was run for this snapshot. Both repositories passed
`git diff --check`; a scan found no common private-key or access-token formats
in tracked and proposed files. This is not a guarantee of absence of secrets.

Code, schemas, templates and the registered protein task belong to the snapshot.
Credentials, local configurations, downloaded agent bundles, images and run
outputs remain excluded by `.gitignore`. Hidden scoring data in the registered
task is operator material: exclude it from any future agent-facing distribution.

No remote was configured in either repository. Local commits provide rollback,
but do not provide off-machine disaster recovery. Set a user-designated private
remote before publishing or backing up operator task material.

For subsequent changes, use focused commits and record the framework and
adapter revisions together. Experiment provenance should eventually record both
code revisions/dirty state, GPU image digest and GPU allocation, in addition to
the existing task and agent-bundle fingerprints. This provenance extension is
planned, not implemented in this snapshot.

2026-10-13 GPU extension: task `execution.gpu_count`, backend preparation and
resource metadata hooks, local Docker device exposure, and the `gpu_hello` (now under `examples/`)
infrastructure package. D → H model acceptance is recorded in the adapter's
`GPU_AUTOMATION.md`; its `VERSION_MANAGEMENT.md` records the compatible framework
revision. Unit tests: 61 total, 54 passed, 7 real-Docker checks skipped. Local
Docker GPU exposure has unit coverage, not live GPU acceptance.

2026-10-14 backend/lifecycle reliability: optional YAML `module_path` resolves
relative to the experiment config and loads external backends without sibling
repository assumptions. Agent preparation and execution times are recorded
separately. Diagnostic failures retain the final result, and Docker removal
requires confirmed absence. The adapter records the compatible framework
revision for its transfer, GPU lifetime and workspace changes.

Validation on the current workspace: 67 unit tests passed, 8 Docker checks
skipped by the unit runner; the separate full Docker run passed all 8 checks.
Its HTTPS fixture now explicitly trusts its own certificate with a matching
hostname; production TLS verification was not changed. Existing PR24 submission-collection changes remain
uncommitted and were present during validation; this reliability commit does
not include those changes. Full Docker and adapter evidence is retained under
`storage/reliability-*-tests.log`; no new H GPU allocation was made.

2026-10-14 submission collection: the generic fix previously left outside the
reliability commit is now included on `main`. Only declared regular files are
copied into an independent read-only scoring directory. Extra files remain in
the original output; missing files and symlinks in declared paths are rejected.
No task-package source or historical experiment results were changed.
Validation: 67 unit tests passed, 8 Docker tests skipped by the unit runner;
the same implementation passed all 8 Docker checks during the preceding
reliability acceptance. This commit did not rerun models or allocate machines.

2026-10-19 public distribution preparation: added the MIT license and moved the
protein scientific package, its scripted producer and operator CPU config to a
separate local task repository. All 33 scientific package file hashes remained
unchanged; historical run artifacts were retained. No task-loader changes were
needed. Public examples now live under `examples/`; the CPU quickstart copies a
public integer and contains no scientific hidden labels. Public documentation
links stay within this repository, including the Chinese contract distribution
copy. The base-image builder no longer defaults to an internal proxy endpoint.

Validation: 67 framework unit checks passed (8 Docker checks skipped by that
runner); 43 adapter unit checks passed (9 live checks skipped). The new public
CPU quickstart ran through actual Docker, returned `scored` in 6.829 seconds and
reported no cleanup errors: run `20261019T031450Z-a394d606`. Evidence is local
`storage/public-echo-quickstart.log` and `storage/task-separation-*-tests.log`.
Both public packages and the independent protein package passed structure checks;
63 local Markdown links and six example/operator configurations were checked.
No paid model calls, GPU allocations or live cluster checks were performed.

The local Git history still contains the former scoring data. This commit is
not permission to publish that history unchanged. Prepare a reviewed public
snapshot with fresh history, retaining this local history privately; see
[task distribution](task-packages.md).

The Python wheel built offline with its MIT license included and no scientific
task data. The separate local task repository starts at `eeadf04`; it has no
remote configured.

2026-10-19 directory submissions: `submission[].path` now accepts regular files
and complete directory trees without a new type field. Template preparation and
final freezing preserve nested files, hidden files and empty directories. All
declared paths are checked before copying; symlinks and special files anywhere
inside declared trees are rejected. The scoring mount remains read-only and
independent of the original output. Validation request/response handling is
unchanged; the task verifier decides the required artifact shape and content.
The independent contributor checker, bundled schemas, contracts and runtime
prompt follow the same rule. Empty directory paths now contribute to content
fingerprints, preventing stale shared-input reuse when directory structure
changes; packages without empty directories retain their existing fingerprints.

Validation: 75 framework unit tests passed (9 Docker checks skipped by that
runner), and 43 adapter unit tests passed (9 live checks skipped). Two focused
real-Docker tests passed: directory-template/mixed-submission scoring and the
existing file-submission flow. The directory Docker check passed again after
the fingerprint change. It verifies recursive contents, empty directories,
ignored unlisted artifacts and a read-only scorer mount. Evidence is retained
locally under `storage/directory-submission-*-tests.log` in each repository.
No paid model calls, GPU allocations or live D/H cluster checks were performed.

2026-10-20 model-request supervision (phase 1): the sandbox-local gateway now
publishes atomic metadata snapshots and distinguishes meaningful protocol
progress, transport activity and protocol completion. Independent first-response,
idle and total request deadlines are configured under `model_monitor`, with
separate warnings and bounded opt-in retries before response delivery. The host
controller supervises snapshots independently and stops the agent on operational
failure; observation failures remain distinct from API failures, with no score.
The snapshot reader has its own bounded wait and at most one outstanding read.
Deliberate caller cancellation is recorded without declaring an upstream fault.
Cleanup failures retain `cleanup_status: pending`. See [the contract](run-supervision.md).

The gateway and its policy remain stdlib-only so minimal agent images need no
controller dependencies. Docker reads a small bind-mounted file; the D plugin
implements a size/time-bounded WebDAV read without bulk-transfer retries.
Agent autonomy, gateway placement, scientific task data and scoring are unchanged.

Validation: 85 framework unit tests passed (10 real-Docker methods skipped),
including controlled silent/keepalive/stalled/truncated streams, absolute total
deadlines across retries, retry exhaustion/recovery, invalid/stale/intermittent
observations, oversized events and a backend reader that ignores its timeout.
45 adapter unit tests passed (9 live checks skipped). Real Docker checks passed
for normal Codex/proxy/TLS operation, keepalive timeout, truncated SSE, and the
existing file-submission flow; faults retain null scores and confirm agent removal.
The final controller reader change was rechecked through normal and fault Docker
methods. Logs: `storage/model-monitor-unit-tests.log` in both repositories,
`storage/model-monitor-docker-tests.log` and `storage/model-monitor-docker-final-tests.log`.

`tools/benchmark-model-monitor.py` records equal local fake streams/concurrency
with monitoring off/on in fresh processes, plus independent controller detection
latency with the internal watchdog disabled. Measurements include CPU, peak RSS,
latency/throughput, metadata writes and local snapshot query counts; retained in
`storage/model-monitor-benchmark.json`. Short local measurements do not establish
production overhead or D platform pressure. No paid API, live D/H allocation or
scientific task run was used. Sandbox management-plane diagnosis and persistent
resource ownership/recovery after controller loss remain future work.

2026-10-20 sandbox supervision (phase 2): all agents use bounded
`Handle.observe(timeout_seconds)` observations under the top-level YAML
`sandbox_monitor` policy. Missing/terminal platform state is distinct from
unavailable execution state; continuous and intermittent outages share an episode
until sustained recovery. Unknown state cannot pass a terminal scoring handoff.
The host prioritizes sandbox uncertainty over API attribution. Gateway deadlines
still operate independently. Backend errors remain null-score infrastructure
failures. Exit 137 has unknown cause without explicit evidence; Docker OOMKilled
is recorded separately. Normal nonzero agent exits retain the existing incomplete
behavior. Latest observations and state transitions remain in the host run dir.

Docker agent containers retain exit state until framework cleanup (no immediate
auto-removal). Initial CLI/container-registration races are treated as starting,
not disappearance. The D plugin cross-checks management state/events after
execution failures, bounds response size/time, and confirms management 404 before
forgetting a deleted sandbox. Pending cleanup is still a record, not independent
crash recovery. See [supervision contract](run-supervision.md).

Validation: 93 framework checks passed, 12 real-Docker methods skipped by the unit
runner; 52 adapter checks passed, 9 live checks skipped. Fault tests include
continuous/intermittent outages, sustained recovery, blocked and late readers,
invalid observations, deletion/137/OOM attribution, unconfirmed cleanup, and a
terminal test request concurrent with sandbox loss. Real Docker passed 5 methods
(7 scenarios): deliberate platform deletion, exit 137 with OOMKilled false,
normal file submission/offline scoring, keepalive/truncated-stream API faults,
and timeout/ordinary nonzero exit. Initial Docker tests exposed and fixed the
startup-registration race; final evidence is `storage/sandbox-monitor-unit-tests.log`
and `storage/sandbox-monitor-docker-final-tests.log`, not the earlier failed log.
The final controller attribution change also passed 3 real-Docker methods
(5 scenarios): normal Codex/TLS/proxy, both API faults and abort over socket and
mailbox. Evidence: `storage/sandbox-monitor-docker-handoff-tests.log`.
No scientific task/data/score changed, no paid API or D/H allocation was used.
Real D platform outage/pressure acceptance remains unverified; independent
controller-kill recovery remains unimplemented as documented in the contract.

2026-10-20 post-controller-loss cleanup (phase 3): `sie cleanup --run` reclaims
new runs using durable, fsynced resource intents and immutable provider IDs.
The controller and reclaimer hold the same lifetime flock, rejecting live or
concurrent owners. Recovery is restricted to the originating hostname and exact
run path. Docker validates daemon ID, run/owner/request labels and resource IDs;
external backends implement `recover_resources()` without site code in core.
Unknown absent creations remain pending. Existing outcomes are preserved, with
an appended recovery report; missing outcomes become controller_lost/null score.

The D plugin records sandbox requests, H names/namespaces/UIDs, temporary input,
remote storage ownership and mutating transfers. It reconciles lost replies,
verifies deletion, retains failed-download copies, and removes GPU input only
after all known H jobs/replicas disappear. Linux parent-death protection stops
orphan write commands; process start/boot identity prevents confusing reused
PIDs with old writers. Shared images/build caches and forensic scratch copies
are retained. No periodic sweeper, task resume or legacy-journal migration.

Validation: 102 framework unit checks passed (13 real-Docker methods skipped),
including a real SIGKILL lock-release test, guarded orphan writer, foreign/reused
ID rejection, unknown create intent retention, partial/repeated cleanup and
historical outcome preservation. Adapter evidence is in its version log.
Real Docker passed 3 methods (4 scenarios) for SIGKILL/new-process CLI recovery,
normal offline scoring and abort over socket/mailbox; a further 4 methods
(6 scenarios) passed normal Codex/TLS/proxy, API keepalive/truncated-stream faults,
unknown 137 and timeout/nonzero exit. Evidence: ignored
`storage/recovery-unit-tests.log`, `storage/recovery-docker-tests.log`, and
`storage/recovery-docker-regression-tests.log`. A timing regression was made
deterministic to avoid treating shared-disk fsync latency as a preparation-budget
failure; actual Docker budget behavior still passes. No paid model, live D/H
allocation or host reboot; real platform schemas/outages/reboot acceptance remain
pending. No scientific data, verifier or historical score was changed.

Final lock-file validation and transfer-record ordering were rechecked with the
9 focused framework recovery tests and 11 adapter recovery tests; both passed.
Evidence: `storage/recovery-final-boundary-tests.log` in each checkout.
