"""Default backend: local Docker CLI with CPU limits, bounded logs, explicit cleanup
and an optional egress proxy that is the agent container's only internet route."""
import os
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

from ..common.errors import InfrastructureError
from ..common.host import writable
from ..common.serialization import atomic_write_json
from .interface import AgentObservation, ContainerService, Handle, ImageRef


class Process:
    def __init__(self, argv, logs, maximum_bytes, environment=None):
        logs = Path(logs)
        logs.mkdir(parents=True, exist_ok=True)
        self.process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        env=environment, stdin=subprocess.DEVNULL)
        self.threads = []
        for stream, name in ((self.process.stdout, "stdout.log"), (self.process.stderr, "stderr.log")):
            thread = threading.Thread(target=self._capture, args=(stream, logs / name, maximum_bytes), daemon=True)
            thread.start()
            self.threads.append(thread)

    @staticmethod
    def _capture(stream, path, maximum):
        with stream, path.open("wb") as output:
            written = 0
            while True:
                block = stream.read(65536)
                if not block:
                    break
                selected = block[:max(0, maximum - written)]
                output.write(selected)
                output.flush()
                written += len(selected)

    def poll(self):
        return self.process.poll()

    def wait(self, timeout=None):
        code = self.process.wait(timeout=timeout)
        for thread in self.threads:
            thread.join(timeout=5)
        return code


class Docker:
    """Thin CLI wrapper. Mounts here are already absolute host paths."""
    def __init__(self, resources=None, creation_timeout=60):
        self.resources, self.creation_timeout = resources, creation_timeout

    def _identity(self, kind, name):
        args = ['ps', '--all'] if kind == 'container' else ['network', 'ls']
        expression = '^/%s$' % name if kind == 'container' else '^%s$' % name
        names = self.control(*(args + ['--filter', 'name=' + expression, '--format',
                                     '{{.Names}}' if kind == 'container' else '{{.Name}}']),
                             timeout=self.creation_timeout)
        if name not in names.splitlines():
            return None
        template = '{"id":{{json .Id}},"labels":%s}' % ('{{json .Config.Labels}}' if kind == 'container' else '{{json .Labels}}')
        args = ['inspect'] if kind == 'container' else ['network', 'inspect']
        return json.loads(self.control(*(args + ['--format', template, name]), timeout=self.creation_timeout))

    def _owned(self, kind, name, entry):
        found = self._identity(kind, name)
        if found is not None:
            labels = found.get('labels') or {}
            if (labels.get('sie.owner') != self.resources.owner or labels.get('sie.request') != entry['request']
                    or labels.get('sie.run') != self.resources.data['run_id']
                    or (entry['id'] is not None and entry['id'] != found['id'])):
                raise InfrastructureError('resource ownership mismatch: ' + name)
        return found

    def remove_owned(self, kind, name):
        key = 'docker:' + kind + ':' + name
        entry = self.resources.data['resources'].get(key)
        if entry is None:
            return  # Never infer ownership from a name alone.
        if entry['state'] == 'removed':
            return
        found = self._owned(kind, name, entry)
        if found is None:
            if entry['state'] == 'intent':
                raise InfrastructureError('creation outcome remains unknown: ' + name)
        else:
            self.resources.update(key, state='live', id=found['id'])
            args = ['rm', '--force'] if kind == 'container' else ['network', 'rm']
            self.control(*(args + [found['id']]), check=False, timeout=self.creation_timeout)
            if self._owned(kind, name, self.resources.data['resources'][key]) is not None:
                raise InfrastructureError('resource deletion not confirmed: ' + name)
        self.resources.removed(key)

    def create_network(self, name):
        if self.resources is None:
            return self.control('network', 'create', '--internal', name)
        key = 'docker:network:' + name
        entry = self.resources.intent(key, 'docker_network', {'name': name})
        value = self.control('network', 'create', '--internal', '--label', 'sie.owner=' + self.resources.owner,
                             '--label', 'sie.request=' + entry['request'], '--label',
                             'sie.run=' + self.resources.data['run_id'], name, timeout=self.creation_timeout)
        self.resources.update(key, state='live', id=value)
        return value

    def control(self, *args, check=True, timeout=60):
        result = subprocess.run(["docker"] + list(args), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=timeout)
        if check and result.returncode:
            raise InfrastructureError("docker %s: %s" % (args[0], result.stderr.strip()))
        return result.stdout.strip()

    def start(self, name, image, command, mounts, resources, logs, maximum_log_bytes,
              network="none", environment=None, workdir="/tmp", extra=(), auto_remove=True):
        entry = None
        if self.resources is not None:
            entry = self.resources.intent('docker:container:' + name, 'docker_container', {'name': name})
            auto_remove = False  # Keep exit/ownership evidence until explicit confirmed deletion.
        memory = str(resources["memory_mb"]) + "m"
        argv = ["docker", "run"] + (["--rm"] if auto_remove else []) + ["--name", name, "--log-driver", "none", "--entrypoint", "",
                "--user", "65532:65532", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--cpus", str(resources["cpus"]),
                "--memory", memory, "--memory-swap", memory, "--pids-limit", str(resources.get("pids", 256)),
                "--tmpfs", "/tmp:rw,nosuid,size=%sm" % resources.get("tmpfs_mb", 64), "--network", network,
                "--workdir", workdir, "--label", "sie.run=" + name.split("-", 1)[1].rsplit("-", 1)[0]]
        for source, target, readonly in mounts:
            argv += ["--mount", "type=bind,src=%s,dst=%s%s" % (Path(source).resolve(), target, ",readonly" if readonly else "")]
        host_env = os.environ.copy()
        if entry:
            argv += ['--label', 'sie.owner=' + self.resources.owner,
                     '--label', 'sie.request=' + entry['request']]
        for key, value in (environment or {}).items():
            argv += ["--env", key]
            host_env[key] = str(value)
        argv += list(extra) + [image] + list(command)
        process = Process(argv, logs, maximum_log_bytes, host_env)
        if entry:
            deadline = time.monotonic() + self.creation_timeout
            try:
                while True:
                    found = self._owned('container', name, entry)
                    if found:
                        self.resources.update('docker:container:' + name, state='live', id=found['id'])
                        break
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise InfrastructureError('container creation not confirmed: ' + name)
                    time.sleep(0.05)
            except BaseException:
                if process.poll() is None:
                    process.process.kill()
                process.wait(10)
                raise
        return process

    def run(self, name, image, command, mounts, resources, logs, maximum_log_bytes,
            network="none", environment=None, workdir="/tmp", extra=()):
        process = self.start(name, image, command, mounts, resources, logs,
                             maximum_log_bytes, network, environment, workdir, extra)
        try:
            return process.wait(resources["timeout_seconds"])
        except subprocess.TimeoutExpired:
            self.remove(name)
            process.wait(10)
            raise TimeoutError("container timed out: %s" % name)
        finally:
            self.remove(name)

    def remove(self, name):
        if self.resources is not None:
            return self.remove_owned('container', name)
        self.control("rm", "--force", name, check=False)
        remaining = self.control("ps", "--all", "--filter", "name=^/%s$" % name, "--format", "{{.Names}}")
        if name in remaining.splitlines():
            raise InfrastructureError("container removal not confirmed: %s" % name)

    def build(self, context, containerfile, tag, logs, build_args=None, network="none", timeout=300, maximum_log_bytes=16_000_000):
        command = ["docker", "build", "--network", network, "-f", str(containerfile), "-t", tag]
        args = dict(build_args or {})
        # A task image that installs wheels can only reach the package index through
        # the builder's proxy. Declared proxy ARGs stay out of the finished image.
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"):
            if key not in args and os.environ.get(key):
                args[key] = os.environ[key]
        for key, value in args.items():
            command += ["--build-arg", "%s=%s" % (key, value)]
        command.append(str(context))
        process = Process(command, logs, maximum_log_bytes)
        try:
            code = process.wait(timeout)
        except subprocess.TimeoutExpired:
            process.process.kill()
            process.wait(10)
            raise InfrastructureError("image build timed out; see %s" % logs)
        if code:
            raise InfrastructureError("image build failed; see %s" % logs)
        return tag

    def image_id(self, tag):
        return self.control("image", "inspect", "--format", "{{.Id}}", tag)


# The proxy container joins the agent's internal network under the alias ``egress``.
EGRESS_ENVIRONMENT = {"HTTP_PROXY": "http://egress:8080", "HTTPS_PROXY": "http://egress:8080",
                      "http_proxy": "http://egress:8080", "https_proxy": "http://egress:8080",
                      "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}


class EgressProxy:
    """One internal Docker network plus a proxy container; state lives in run_dir/network."""
    def __init__(self, docker, config, root, prefix, image):
        self.docker, self.config, self.root, self.prefix, self.image = docker, config, root, prefix, image
        self.network = None
        self.process = None
        self.environment = {}

    def start(self):
        self.root.mkdir()
        state = self.root / "state"
        state.mkdir()
        writable(state)
        policy = self.root / "policy.json"
        atomic_write_json(policy, self.config["network"], mode=0o444)
        self.docker.create_network(self.prefix + '-net')
        self.network = self.prefix + "-net"
        self.process = self.docker.start(
            self.prefix + "-proxy", self.image,
            ["python3", "-m", "science_innovation_exam.network_proxy", "--policy", "/policy.json", "--state", "/network-state",
             "--maximum-log-bytes", str(self.config["limits"]["log_bytes"])],
            [(policy, "/policy.json", True), (state, "/network-state", False)],
            self.config["verifier"], self.root / "logs", self.config["limits"]["log_bytes"],
            network=self.config["network"].get("external_network", "bridge"), workdir="/tmp",
            extra=["--add-host", "host.docker.internal:host-gateway"])
        deadline = time.monotonic() + 30
        while not (state / "ready").exists():
            if self.process.poll() is not None or time.monotonic() > deadline:
                raise InfrastructureError("network proxy did not start; see %s" % self.root)
            time.sleep(0.1)
        self.docker.control("network", "connect", "--alias", "egress", self.network, self.prefix + "-proxy")
        self.environment = dict(EGRESS_ENVIRONMENT)
        return self

    def exited(self):
        return self.process is not None and self.process.poll() is not None

    def close(self):
        if self.process is not None:
            self.docker.remove(self.prefix + "-proxy")
            self.process.wait(10)
            self.process = None
        if self.network:
            if self.docker.resources is not None:
                self.docker.remove_owned('network', self.network)
            else:
                self.docker.control("network", "rm", self.network)
            self.network = None


def retrieve_scratch(root, scratch, local_home, log_paths):
    """Copy the agent's declared logs out of the scratch home, then delete the rest."""
    scratch = Path(scratch) if scratch else None
    if scratch is None or not scratch.exists():
        return
    home = Path(local_home)
    for source, name in log_paths:
        relative = Path(source)
        if relative.is_absolute():
            relative = relative.relative_to(home)
        if any(part in ("..", ".") for part in relative.parts):
            continue
        host = scratch / relative
        if not host.exists():
            continue
        destination = Path(root) / "agent-state" / name
        if destination.exists():
            shutil.rmtree(destination) if destination.is_dir() else destination.unlink()
        destination.parent.mkdir(parents=True, exist_ok=True)
        if host.is_dir():
            shutil.copytree(str(host), str(destination))
        else:
            shutil.copy2(str(host), str(destination))
    shutil.rmtree(str(scratch))


class DockerHandle(Handle):
    def __init__(self, docker, name, process, proxy, root, service):
        self.docker, self.name, self.process, self.proxy, self.root, self.service = docker, name, process, proxy, root, service
        self.seen = False

    def poll(self):
        if self.proxy is not None and self.proxy.exited():
            raise InfrastructureError("network proxy exited; see %s" % (self.root / "network/logs"))
        return self.process.poll()

    def observe(self, timeout_seconds):
        # Inspect only bounded state fields; a failed CLI call is not evidence of absence.
        try:
            state = json.loads(self.docker.control('inspect', '--format', '{{json .State}}',
                                                  self.name, timeout=timeout_seconds / 2))
        except InfrastructureError:
            # Confirm via an exact-name list. Engine/permission failures must stay unknown.
            names = self.docker.control('ps', '--all', '--filter', 'name=^/%s$' % self.name,
                                        '--format', '{{.Names}}', timeout=timeout_seconds / 2)
            if self.name not in names.splitlines():
                if not self.seen and self.process.poll() is None:
                    return AgentObservation('unknown', reason='docker_starting')
                return AgentObservation('missing', reason='sandbox_missing', evidence={'source': 'docker_list'})
            return AgentObservation('unknown', reason='docker_inspect_failed')
        self.seen = True
        evidence = {'source': 'docker', 'oom_killed': state.get('OOMKilled') is True}
        if evidence['oom_killed']:
            return AgentObservation('failed', state.get('ExitCode'), 'sandbox_oom_killed', evidence)
        if state.get('Running'):
            if self.proxy is not None and self.proxy.exited():
                return AgentObservation('failed', reason='network_proxy_exited', evidence=evidence)
            code = self.process.poll()
            if code is not None:
                # docker run lost its connection while the workload is still alive.
                return AgentObservation('unknown', reason='docker_client_exited', evidence=evidence)
            return AgentObservation('running', evidence=evidence)
        if state.get('Status') in ('exited', 'dead'):
            return AgentObservation('exited', state.get('ExitCode'), evidence=evidence)
        return AgentObservation('unknown', reason='docker_state_unknown', evidence=evidence)

    def wait(self, timeout=None):
        return self.process.wait(timeout)

    def stop(self):
        # End all agent writers before the controller examines or copies files.
        self.docker.remove(self.name)
        try:
            self.process.wait(10)
        except subprocess.TimeoutExpired:
            pass
        self.service.retrieve_local_home()


class DockerService(ContainerService):
    def __init__(self, config, run_dir):
        super().__init__(config, run_dir)
        self.docker = Docker(self.resources, config.get('container_service', {}).get('creation_timeout_seconds', 60))
        self.prefix = "sie-" + self.run_dir.name
        self.proxy = None
        self.options = config.get("container_service", {})
        self.scratch = None
        self.local_home = ""
        self.log_paths = []
        self.scratch_retrieved = False
        self.gpu_count = 0

    def prepare_task(self, task):
        self.gpu_count = task.gpu_count

    def environment_prompt(self):
        if not self.gpu_count:
            return []
        return ["This agent container has %d local GPU(s). Use the local devices directly. "
                "Input is at /input and final deliverables belong under /submission. "
                "Check installed GPU libraries before downloading or installing software." % self.gpu_count]

    def run_metadata(self):
        return {"gpu": {"mode": "local", "count": self.gpu_count}} if self.gpu_count else {}

    def check(self):
        self.docker.control("info", "--format", "{{.ServerVersion}}")
        if self.resources is not None:
            self.resources.bind('docker_engine', self.docker.control('info', '--format', '{{.ID}}'))

    def recover_resources(self):
        self.check()
        errors = []
        for kind, label in (('docker_container', 'container'), ('docker_network', 'network')):
            for key, entry in self.resources.pending({kind}):
                try:
                    self.docker.remove_owned(label, entry['identity']['name'])
                except Exception:
                    self.resources.update(key, error='deletion_unconfirmed')
                    errors.append(key)
        if errors:
            raise InfrastructureError('resource cleanup remains pending')

    def publish(self, image_id, local_tag):
        return ImageRef(uri=image_id, digest=image_id)

    def agent_proxy_environment(self):
        return dict(EGRESS_ENVIRONMENT)

    def _mounts(self, spec):
        resolved = []
        for mount in spec.mounts:
            if mount.shared or not mount.is_relative():
                path = Path(mount.source).resolve()
            else:
                path = mount.resolve(self.run_dir)
            resolved.append((path, mount.target, mount.readonly))
        return resolved

    def retrieve_local_home(self):
        if self.scratch_retrieved:
            return
        self.scratch_retrieved = True
        if self.scratch is not None:
            retrieve_scratch(self.run_dir, self.scratch, self.local_home, self.log_paths)

    def start_agent(self, spec):
        environment = dict(spec.environment)
        network = "none"
        if spec.network_mode == "proxy":
            self.proxy = EgressProxy(self.docker, self.config, self.run_dir / "network", self.prefix, spec.image).start()
            environment.update(self.proxy.environment)
            network = self.proxy.network
        mounts = self._mounts(spec)
        if spec.local_home:
            # The home stays on the host only so declared logs can be copied after the
            # container exits. The rest of this directory is deleted, not recorded.
            scratch = self.run_dir / "local-home"
            if scratch.exists():
                shutil.rmtree(str(scratch))
            shutil.copytree(str(self.run_dir / "agent-state"), str(scratch))
            writable(scratch)
            mounts.append((scratch, spec.local_home, False))
            self.scratch, self.local_home, self.log_paths = scratch, spec.local_home, list(spec.log_paths)
        name = self.prefix + "-" + spec.role
        process = self.docker.start(name, spec.image, spec.command, mounts, spec.resources,
                                    self.run_dir / spec.logs, spec.log_bytes, network=network,
                                    environment=environment, workdir=spec.workdir,
                                    extra=("--gpus", str(self.gpu_count)) if self.gpu_count else (), auto_remove=False)
        return DockerHandle(self.docker, name, process, self.proxy, self.run_dir, self)

    def run_verifier(self, spec):
        return self.docker.run(self.prefix + "-" + spec.role, spec.image, spec.command, self._mounts(spec),
                               spec.resources, self.run_dir / spec.logs, spec.log_bytes,
                               environment=spec.environment, workdir=spec.workdir)

    def open_channel(self, handler):
        transport = self.options.get("action_transport", "socket")
        if transport == "mailbox":
            from .channel_mailbox import MailboxChannel
            return MailboxChannel(self.run_dir / "control", handler, self.options.get("poll_seconds", 0.5))
        from .channel_socket import SocketChannel
        return SocketChannel(handler)

    def cleanup(self):
        errors = []
        try:
            self.retrieve_local_home()
        except Exception as error:
            errors.append(str(error))
        for role in ("agent", "validation", "test"):
            try:
                self.docker.remove(self.prefix + "-" + role)
            except Exception as error:
                errors.append(str(error))
        if self.proxy is not None:
            try:
                self.proxy.close()
            except Exception as error:
                errors.append(str(error))
        if self.resources is not None:
            try:
                self.recover_resources()
            except Exception as error:
                errors.append(str(error))
        if errors:
            raise InfrastructureError("; ".join(errors))
