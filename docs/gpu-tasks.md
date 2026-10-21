# GPU task execution

GPU use is explicit in `instruction.json`:

```json
"execution": {"gpu_count": 1},
"limitation": {"gpu": {"count": 1}}
```

`execution.gpu_count` is the number required for execution. Omitted or zero means CPU-only. `limitation.gpu.count` is a ceiling, not a request; setting a ceiling alone does not allocate hardware. A request above that ceiling fails before starting an experiment.

The runner calls `ContainerService.prepare_task(task)` after publishing the agent image and before preparing its prompt. A backend without GPU support fails explicitly. Local Docker gives only the agent container `--gpus COUNT`; the CPU verifier remains offline and receives no GPU. Prepare an NVIDIA driver, a GPU-enabled Docker runtime and a suitable local base image before selecting Docker GPU tasks. Device exposure has unit coverage; the local Docker GPU path has not yet had real-device end-to-end acceptance.

`examples/gpu_hello` is a five-part package for infrastructure acceptance. Its public CUDA helper runs a vector-add kernel using the NVIDIA driver and standard Python, without downloading PyTorch. The final CPU-only verifier checks the vector result deterministically. The agent trace is needed to confirm that CUDA was actually invoked; the output checker cannot independently attest hardware execution. This is not an eligible scientific benchmark and should not be mixed into scientific performance reports.

Remote GPU allocation belongs to an external container backend, not the built-in Docker backend. Load it with `container_service.module` and optional `module_path`; allocation, credentials, networking and data-mount configuration follow that plugin's own documentation. No private adapter checkout or cluster account is needed for the public CPU quickstart. A backend can add an environment prompt to the framework's general prompt and resource evidence through `run_metadata()`.
