# Repository instructions for coding agents

Read [AI_README.md](AI_README.md) before changing this framework. Human installation and runtime setup are in [README.md](README.md).

- Keep edits focused; preserve unrelated working changes and historical experiment evidence.
- Treat the host controller, benchmarked agent and offline verifier as separate trust boundaries.
- Put site-specific execution in external container backends loaded by YAML. Keep hardware, model, network and path choices configurable.
- Keep credentials out of code, configuration, logs, images and commits. Do not dump the environment to debug setup.
- Keep repositories, environments, datasets, caches, build outputs and run artifacts on disk; RAM-backed temporary paths are for small transient files only.
- Use the repository's existing verification commands and match claims to what actually passed. Clearly separate infrastructure checks, live hardware/provider acceptance and scientific validity.
- This file guides development assistants; benchmarked agents receive the runtime prompts built by the framework.
