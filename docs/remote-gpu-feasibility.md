# Remote GPU execution boundary

This page replaces the early SSH design proposal. The framework supports two
deployment routes; their runtime and acceptance requirements differ.

| Route | GPU location | Preparation and access |
|---|---|---|
| Local Docker | Controller host | Operator installs drivers and a GPU-enabled Docker runtime; the agent receives explicitly requested devices |
| External backend | Separately allocated machine | Plugin allocates the machine, stages public input, supplies access instructions and releases its allocation |

The framework selects the backend with YAML `container_service.module` and
optional `module_path`; there is no required sibling checkout or private
configuration file. See [configuration](../README.md) and the
[backend contract](../src/science_innovation_exam/container_service/interface.py).
Remote allocation, storage mapping, accounts and credentials are configured by
the plugin, not the public framework. Refer to the separately distributed
plugin's documentation for deployment-specific commands and acceptance records.

For either route:

- `execution.gpu_count` requests devices; a resource ceiling alone does not.
- Preparation precedes the agent budget. Preparation and execution have
  separate timings, while total wall time includes cleanup.
- Only public task input may be staged on the research GPU machine. Hidden
  scoring input and the private verification state stay outside agent access.
- The agent must return its declared final files to its canonical `/submission`
  before requesting `test`; the framework does not collect a remote submission.
- The backend must stop its remote writers before scoring and confirm resource
  deletion. Failed cleanup is an infrastructure failure, not a scientific score.
- Operator credential isolation, network policy, controller-crash recovery and
  detection of disappeared remote machines need deployment-specific validation.
  A successful SSH command alone does not establish those properties.

The current verifier receives CPU only and runs offline. Local Docker GPU device
exposure has unit coverage but still needs real-device end-to-end acceptance.
The [GPU example and evidence limits](gpu-tasks.md) explain what the public
infrastructure test can establish; it is not a scientific benchmark.
