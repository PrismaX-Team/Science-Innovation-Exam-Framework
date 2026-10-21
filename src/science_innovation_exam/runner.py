"""The entire experiment lifecycle: prepare, research, freeze, test, save.

The controller talks to containers only through ``container_service.interface``.
Call order per run: check → publish → open_channel → start_agent → run_verifier*
→ Handle.stop → run_verifier(test) → Channel.close → cleanup.
"""
import json
import os
import stat
import subprocess
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import container_service, images
from .actions import Actions
from .agents import prepare_agent
from .common.errors import ContractError, InfrastructureError
from .common.host import require_disk, writable
from .common.serialization import atomic_write_json
from .config import load_config
from .container_service.interface import ContainerSpec, MountSpec
from .model_monitor import ModelFault, ModelMonitor
from .sandbox_monitor import SandboxFault, SandboxMonitor
from .resources import ResourceJournal, RunLock
from .task import TaskPackage
from .verifier import Verifier
from .verify_context import VerifyContext


def redact_logs(root, secrets):
    """Diagnostic files are untrusted; never follow links or block on special files."""
    errors = []
    if not secrets:
        return errors
    for path in root.rglob("*.log"):
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                raise OSError("log is not a regular file")
            descriptor = os.open(str(path), os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "r+b") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise OSError("log is not a regular file")
                data = handle.read()
                for secret in secrets:
                    data = data.replace(secret.encode(), b"[REDACTED]")
                handle.seek(0)
                handle.write(data)
                handle.truncate()
        except Exception as error:
            errors.append("%s: %s" % (path.relative_to(root), error))
    return errors


def respect_task_limits(task, config):
    limits = task.instruction["limitation"]
    checks = [("cpu", "cores", config["resources"]["cpus"]),
              ("memory", "gb", config["resources"]["memory_mb"] / 1024),
              ("runtime", "seconds", config["resources"]["timeout_seconds"])]
    for group, key, actual in checks:
        if group in limits and actual > limits[group][key]:
            raise ContractError("configured %s exceeds the task limit" % group)
    if task.gpu_count > limits.get("gpu", {}).get("count", task.gpu_count):
        raise ContractError("required GPU count exceeds the task limit")


def run(config_path, load_service=container_service.load, prepare_image=images.prepare):
    config = load_config(config_path)
    task = TaskPackage.load(config["task"])
    respect_task_limits(task, config)
    require_disk(config["output"])
    root = Path(config["output"]) / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8])
    root.mkdir(parents=True, mode=0o700)
    with RunLock(root):
        ResourceJournal.create(root, config['container_service'])
        return _run(config, task, root, load_service, prepare_image)


def _run(config, task, root, load_service, prepare_image):
    result = {"task_id": task.task_id, "run_directory": str(root), "status": "infrastructure_failed", "score": None,
              "container_service": config["container_service"]["type"]}
    atomic_write_json(root / "config.json", config)
    service = handle = channel = actions = None
    monitor = None
    sandbox_monitor = None
    stopped = False
    secrets = []
    fingerprint = task.fingerprint()
    started = time.monotonic()
    agent_started = agent_ended = None
    try:
        service = load_service(config, root)
        service.check()
        atomic_write_json(root / "task-package.json", {
            "task_id": task.task_id, "fingerprint": fingerprint, "source": str(task.root)})
        result["task_fingerprint"] = fingerprint
        image_id, local_tag = prepare_image(task, config, root, build_args=service.image_build_args())
        result["image"] = image_id
        image = service.publish(image_id, local_tag)
        result["image_uri"] = image.uri
        service.prepare_task(task)
        submission = root / "submission"
        task.prepare_submission(submission)
        context = root / "context"
        VerifyContext.create(context, config["limits"]["context_bytes"])
        verifier = Verifier(service, task, image.uri, context, config, root)
        actions = Actions(verifier, root / "actions", config["limits"])
        channel = service.open_channel(actions.serve_bytes)
        launch = prepare_agent(config, root, channel.environment, service.agent_proxy_environment(),
                               service.environment_prompt())
        secrets = launch.secrets
        writable(submission)
        writable(root / "agent-state")
        mounts = list(launch.mounts) + task.agent_mounts() + [
            MountSpec("submission", "/submission", False)] + list(channel.mounts)
        spec = ContainerSpec(role="agent", image=image.uri, command=launch.command, mounts=mounts,
                             resources=config["resources"], environment=launch.environment,
                             network_mode=config["network"]["mode"], workdir="/tmp",
                             logs="agent-logs", log_bytes=config["limits"]["log_bytes"],
                             local_home=launch.local_home, local_setup=list(launch.local_setup),
                             command_rewrites=list(launch.command_rewrites), log_paths=list(launch.log_paths),
                             local_reason=launch.local_reason)
        handle = service.start_agent(spec)
        agent_started = time.monotonic()
        result["preparation_seconds"] = round(agent_started - started, 3)
        sandbox_monitor = SandboxMonitor(handle, root, config['sandbox_monitor'])
        if config["agent"]["type"] == "codex" and config["model_monitor"]["enabled"]:
            monitor = ModelMonitor(service, root, config["model_monitor"])
        deadline = agent_started + config["resources"]["timeout_seconds"]
        reason = "agent_exited_without_test"
        while not actions.done.wait(0.1):
            observation = sandbox_monitor.check()
            if observation is not None and observation.state == 'exited':
                if observation.exit_code:
                    result.update(failure_phase='agent', failure_reason='agent_exit_nonzero')
                break
            if monitor is not None and observation is not None and observation.state == 'running':
                monitor.check()
            if time.monotonic() >= deadline:
                reason = "agent_timeout"
                break
        agent_ended = time.monotonic()
        # A terminal action is not proof that the sandbox survived until handoff.
        observation = sandbox_monitor.check(force=True) if actions.done.is_set() else sandbox_monitor.latest
        if actions.done.is_set() and observation.state == 'unknown':
            raise SandboxFault('sandbox_status_unavailable_at_handoff')
        # End all Agent writers before examining or copying the final files.
        handle.stop()
        stopped = True
        result["agent_exit_code"] = handle.wait(10)
        if monitor is not None:
            # Inspect terminal API evidence; deliberate stopping need not keep a heartbeat alive.
            monitor.check(force=True, final=True)
        # A validation in flight must settle before the context is retained.
        with actions.lock:
            actions.closed = True
        if actions.failure:
            result.update(actions.failure)
        elif actions.abort_reason is not None:
            result.update(status="aborted", score=None)
        elif task.fingerprint(refresh=True) != fingerprint:
            result.update(status="infrastructure_failed", error="task package changed during the run")
        elif not actions.test_requested:
            result.update(status="incomplete", error=reason)
        else:
            atomic_write_json(root / "context-before-test.json", VerifyContext(context).snapshot())
            try:
                frozen = root / "frozen-submission"
                task.freeze(submission, frozen)
            except ContractError as error:
                result.update(status="invalid_submission", error=str(error))
            else:
                try:
                    score = verifier.invoke("test", root / "test", submission=frozen)
                    result.update(status="scored" if score["valid"] else "invalid_submission", score=score)
                except Exception as error:
                    status = "infrastructure_failed" if isinstance(error, InfrastructureError) else "scorer_failed"
                    result.update(status=status, error=str(error), error_type=type(error).__name__)
        atomic_write_json(root / "context-final.json", VerifyContext(context).snapshot())
    except KeyboardInterrupt:
        result.update(status="incomplete", error="interrupted", failure_phase="controller",
                      failure_reason="user_interrupted")
    except ModelFault as error:
        result.update(status="infrastructure_failed", score=None, error=str(error),
                      failure_phase=error.phase, failure_reason=error.reason, request_id=error.request_id)
    except SandboxFault as error:
        result.update(status='infrastructure_failed', score=None, error=str(error),
                      failure_phase='sandbox', failure_reason=error.reason)
        if error.exit_code is not None:
            result['agent_exit_code'] = error.exit_code
    except Exception as error:
        result.update(status="infrastructure_failed", score=None, error=str(error), error_type=type(error).__name__)
        (root / "controller-error.log").write_text(traceback.format_exc())
    finally:
        if agent_started is not None:
            result["agent_seconds"] = round((agent_ended or time.monotonic()) - agent_started, 3)
        cleanup_errors = []

        def settle_actions():
            if actions is not None:
                with actions.lock:
                    actions.closed = True

        def stop_agent():
            if handle is not None and not stopped:
                handle.stop()
                try:
                    handle.wait(10)
                except subprocess.TimeoutExpired:
                    pass

        for cleanup in (stop_agent, settle_actions,
                        lambda: channel.close() if channel else None,
                        lambda: service.cleanup() if service else None):
            try:
                cleanup()
            except Exception as error:
                cleanup_errors.append(str(error))
        if cleanup_errors:
            result.update(status="infrastructure_failed", score=None, cleanup_errors=cleanup_errors)
        try:
            if ResourceJournal(root).pending():
                cleanup_errors.append('unresolved resource intents; use sie cleanup --run')
        except Exception:
            cleanup_errors.append('resource journal could not be read')
        if cleanup_errors:
            result.update(status='infrastructure_failed', score=None, cleanup_errors=cleanup_errors)
        result["cleanup_status"] = "pending" if cleanup_errors else "completed"
        if monitor is not None:
            result["model_monitor"] = monitor.summary()
        if sandbox_monitor is not None:
            result['sandbox_monitor'] = sandbox_monitor.summary()
        # Actions have settled; retain the explanation even if stopping failed.
        if actions is not None and actions.abort_reason is not None:
            result["abort_reason"] = actions.abort_reason
        result["wall_seconds"] = round(time.monotonic() - started, 3)
        diagnostic_errors = []
        if service is not None:
            try:
                metadata = service.run_metadata()
                json.dumps(metadata, allow_nan=False)
                result.update(metadata)
            except Exception as error:
                diagnostic_errors.append("backend metadata: %s" % error)
        # Model credentials are never part of config; redact accidental log echoes.
        try:
            diagnostic_errors.extend(redact_logs(root, secrets))
        except Exception as error:
            diagnostic_errors.append("log processing: %s" % error)
        if diagnostic_errors:
            result["diagnostic_errors"] = diagnostic_errors
        for secret in secrets:
            escaped = json.dumps(secret)[1:-1]
            result = json.loads(json.dumps(result).replace(escaped, "[REDACTED]"))
        atomic_write_json(root / "result.json", result)
    return result
