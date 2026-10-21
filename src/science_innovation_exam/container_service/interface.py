"""The narrow contract between the controller and any container backend.

The controller (runner, verifier, actions) only imports this module. A backend
receives fully described work and never imports controller modules.

Mount sources are usually POSIX paths relative to the run directory, so a
backend can turn them into host bind mounts or remote volume sub-paths alike.
Two sources are absolute: a Unix socket channel (see ``channel_socket.py``)
and content-addressed task or program files. Those set ``shared`` to a
project-storage key. Docker bind-mounts the absolute host path read-only.
A remote backend publishes that path once under ``shared`` and reuses it.
"""
from dataclasses import dataclass, field
import json
import os
import stat
from pathlib import Path
from typing import Callable, Dict, List, Optional


@dataclass(frozen=True)
class MountSpec:
    source: str          # relative to the run directory, or absolute for a socket or shared content
    target: str          # absolute path inside the container
    readonly: bool
    shared: Optional[str] = None  # content-addressed key relative to the storage prefix; None for run files

    def is_relative(self):
        return not self.source.startswith("/")

    def resolve(self, run_dir):
        path = Path(self.source) if not self.is_relative() else Path(run_dir) / self.source
        return path.resolve()


@dataclass(frozen=True)
class ImageRef:
    uri: str             # what the backend passes to its container engine
    digest: str          # local image ID or registry digest, for the result record


@dataclass
class ContainerSpec:
    role: str            # "agent", "validation" or "test"; backends derive names from it
    image: str
    command: List[str]
    mounts: List[MountSpec]
    resources: Dict[str, object]      # cpus, memory_mb, timeout_seconds, pids, tmpfs_mb
    environment: Dict[str, str] = field(default_factory=dict)
    network_mode: str = "offline"     # "offline" or "proxy"; verifiers are always offline
    # Starting directory only. The root filesystem is read-only, so the process
    # starts in the writable temporary directory. This is not a workspace.
    workdir: str = "/tmp"
    logs: str = "agent-logs"          # run-directory-relative directory for stdout/stderr
    log_bytes: int = 16_000_000
    # Set by an agent that must execute or lock files off shared storage.
    # Docker keeps them on a scratch mount it deletes after copying ``log_paths``.
    # A remote backend runs ``local_setup`` (``{owner}`` becomes uid:gid) and rewrites command prefixes.
    local_home: str = ""
    local_setup: List[str] = field(default_factory=list)
    command_rewrites: List[tuple] = field(default_factory=list)
    log_paths: List[tuple] = field(default_factory=list)
    local_reason: str = ""


class Handle:
    """A running agent container."""
    def poll(self) -> Optional[int]:
        raise NotImplementedError

    def observe(self, timeout_seconds):
        """One bounded observation, without stopping anything.

        Remote backends should cross-check their management plane after execution
        query failures. The fallback observes only the process, not the platform.
        Evidence must contain small, credential-free metadata, never raw responses.
        """
        code = self.poll()
        return AgentObservation("running" if code is None else "exited", exit_code=code,
                                evidence={"source": "process"})

    def wait(self, timeout=None) -> int:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class AgentObservation:
    state: str  # running, exited, missing, failed, unknown
    exit_code: Optional[int] = None
    reason: str = ""
    evidence: dict = field(default_factory=dict)


Handler = Callable[[bytes], dict]


class Channel:
    """The action transport. ``handler(raw_bytes) -> response dict`` is invoked
    once per request by the channel's own thread; the channel only moves bytes.
    ``mounts`` and ``environment`` are what the agent container needs to reach it."""
    mounts: List[MountSpec] = []
    environment: Dict[str, str] = {}

    def close(self) -> None:
        raise NotImplementedError


class ContainerService:
    def __init__(self, config, run_dir):
        self.config, self.run_dir = config, Path(run_dir)
        from ..resources import ResourceJournal
        self.resources = ResourceJournal(self.run_dir) if (self.run_dir / 'resources.json').exists() else None

    def recover_resources(self):
        """Reconcile only durable owned resources, confirm deletion, preserve outputs."""
        from ..common.errors import InfrastructureError
        raise InfrastructureError('this backend has no crash-recovery implementation')

    def check(self) -> None:
        """Raise InfrastructureError when the backend cannot run containers now."""
        raise NotImplementedError

    def prepare_task(self, task) -> None:
        """Provision task hardware before building the agent prompt. CPU backends need no work."""
        if task.gpu_count:
            from ..common.errors import InfrastructureError
            raise InfrastructureError("this container backend does not support GPU tasks")

    def run_metadata(self) -> dict:
        """Backend-specific resource evidence; no credentials."""
        return {}

    def read_model_activity(self, timeout_seconds) -> dict:
        """Read at most 1 MiB of latest gateway metadata, never whole session logs.

        Bind-mount backends use this local implementation. Remote backends must
        override it with a bounded remote read honoring timeout_seconds.
        Missing, stale or inaccessible state is an observation failure, not API evidence.
        """
        path = self.run_dir / "agent-state/model-activity.json"
        descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("activity must be a regular file")
            data = source.read(1048577)
        if len(data) > 1048576:
            raise ValueError("activity exceeds size limit")
        return json.loads(data)

    def image_build_args(self) -> Dict[str, str]:
        """Extra build arguments for the framework layer (see Containerfile). Docker needs none;
        a backend whose engine does not set the process user itself may need ``RUNTIME_USER``."""
        return {}

    def agent_proxy_environment(self) -> Dict[str, str]:
        """Proxy variables the agent's research shells must use in ``network_mode == "proxy"``.
        Docker names the run's egress proxy container; a backend whose platform filters egress
        itself has no proxy and returns ``{}`` so the sandbox reaches the network directly."""
        return {}

    def environment_prompt(self) -> List[str]:
        """Facts about this machine that are not in the task package. Empty when the agent
        cannot tell one supported backend from another. A later GPU service can name the device."""
        return []

    def publish(self, image_id: str, local_tag: str) -> ImageRef:
        """Make a locally built image reachable by this backend."""
        raise NotImplementedError

    def start_agent(self, spec: ContainerSpec) -> Handle:
        raise NotImplementedError

    def run_verifier(self, spec: ContainerSpec) -> int:
        """Run to completion offline and return the exit code; raise TimeoutError past the limit."""
        raise NotImplementedError

    def open_channel(self, handler: Handler) -> Channel:
        raise NotImplementedError

    def cleanup(self) -> None:
        """Remove every container, network or remote object this run created."""
        raise NotImplementedError
