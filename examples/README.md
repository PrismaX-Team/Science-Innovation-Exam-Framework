# Public infrastructure examples

These small packages exercise the framework. They contain no hidden scientific
labels and are not scientific benchmarks.

- `integer_echo/`: copy a public integer, request mid-run validation and submit.
  Run with [the CPU quickstart configuration](../configs/quickstart-cpu.yaml).
- `echo-agent.py`: deterministic scripted agent for that quickstart.
- `gpu_hello/`: CUDA vector addition for GPU infrastructure acceptance. See
  [GPU execution](../docs/gpu-tasks.md) for requirements and evidence limits.

Real scientific packages are supplied independently through the experiment
YAML's `task` path. Do not add operator-only scoring datasets here. A task's
expected five-part directory layout is described in the
[task contract](../contributor/build-sie-task-package/task-contract.md).
