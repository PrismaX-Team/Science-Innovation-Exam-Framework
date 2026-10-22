"""One configuration file for a CPU experiment; secrets stay in the environment."""
import copy
import math
from pathlib import Path

from .common.errors import ContractError
from .common.security import reject_secret_keys
from .contracts.validation import load_yaml
from .model_monitor_config import MONITOR_DEFAULTS, check_model_monitor
from .sandbox_monitor import SANDBOX_DEFAULTS, check_sandbox_monitor


DEFAULTS = {
    "image": "sie-cpu:local",
    "base_image": "python:3.11-slim-bookworm",
    "agent": {"type": "scripted"},
    "resources": {"cpus": 2, "memory_mb": 2048, "timeout_seconds": 600, "pids": 256, "tmpfs_mb": 64},
    "verifier": {"cpus": 1, "memory_mb": 512, "timeout_seconds": 120, "pids": 256, "tmpfs_mb": 64},
    "build": {"timeout_seconds": 300, "network": "none"},
    "network": {"mode": "proxy", "denied_domains": [], "upstream_proxy": None},
    "limits": {"log_bytes": 16_000_000, "request_bytes": 2_000_000,
               "response_bytes": 8_000_000, "context_bytes": 16_000_000},
    # Which backend runs containers. "docker" is built in; any other type names
    # a plugin module and may carry extra fields that only the plugin validates.
    "container_service": {"type": "docker"},
    "model_monitor": MONITOR_DEFAULTS,
    "sandbox_monitor": SANDBOX_DEFAULTS,
    "provenance": {"timeout_seconds": 5, "maximum_bytes": 1048576},
}

DOCKER_SERVICE_FIELDS = {"type", "action_transport", "poll_seconds", "creation_timeout_seconds"}


def check_container_service(options):
    if not isinstance(options.get("type"), str) or not options["type"]:
        raise ContractError("container_service.type must be a non-empty string")
    if options["type"] == "docker":
        unknown = set(options) - DOCKER_SERVICE_FIELDS
        if unknown:
            raise ContractError("unknown container_service fields for docker: %s" % sorted(unknown))
        if options.get("action_transport", "socket") not in ("socket", "mailbox"):
            raise ContractError("container_service.action_transport must be socket or mailbox")
        poll = options.get("poll_seconds", 0.5)
        if isinstance(poll, bool) or not isinstance(poll, (int, float)) or not math.isfinite(poll) or poll <= 0:
            raise ContractError("container_service.poll_seconds must be positive")
        creation = options.get('creation_timeout_seconds', 60)
        if isinstance(creation, bool) or not isinstance(creation, (int, float)) or not math.isfinite(creation) or creation <= 0:
            raise ContractError('container_service.creation_timeout_seconds must be positive')
    elif not isinstance(options.get("module"), str) or not options["module"]:
        raise ContractError("container_service.module is required for type %r" % options["type"])
    if "module_path" in options:
        if not isinstance(options["module_path"], str) or not options["module_path"]:
            raise ContractError("container_service.module_path must be a non-empty directory path")


def load_config(path):
    path = Path(path).resolve()
    value = load_yaml(path)
    if not isinstance(value, dict):
        raise ContractError("config must be an object")
    reject_secret_keys(value)
    unknown = set(value) - set(DEFAULTS) - {"task", "output"}
    if unknown:
        raise ContractError("unknown config fields: %s" % sorted(unknown))
    config = copy.deepcopy(DEFAULTS)
    for key, item in value.items():
        if isinstance(config.get(key), dict):
            if not isinstance(item, dict):
                raise ContractError("%s must be an object" % key)
            config[key].update(item)
        else:
            config[key] = item
    for key in ("task", "output"):
        if not isinstance(config.get(key), str) or not config[key]:
            raise ContractError("config requires %s" % key)
        config[key] = str((path.parent / config[key]).resolve())
    for group in ("resources", "verifier", "limits"):
        if set(config[group]) != set(DEFAULTS[group]):
            raise ContractError("unknown %s fields" % group)
        for key, number in config[group].items():
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number <= 0:
                raise ContractError("%s.%s must be positive" % (group, key))
            if key != "cpus" and not isinstance(number, int):
                raise ContractError("%s.%s must be an integer" % (group, key))
    agent = config["agent"]
    if agent["type"] not in ("scripted", "codex"):
        raise ContractError("agent.type must be scripted or codex")
    unknown_agent = set(agent) - {"type", "command", "script", "bundle", "model", "reasoning_effort", "prompt"}
    if unknown_agent:
        raise ContractError("unknown agent fields: %s" % sorted(unknown_agent))
    for key in ("bundle", "script", "prompt"):
        if agent.get(key):
            agent[key] = str((path.parent / agent[key]).resolve())
    if agent.get("prompt") and not Path(agent["prompt"]).is_file():
        raise ContractError("agent.prompt does not exist")
    if agent.get("reasoning_effort") is not None and (
            not isinstance(agent["reasoning_effort"], str) or not agent["reasoning_effort"]):
        raise ContractError("agent.reasoning_effort must be a non-empty string")
    if agent["type"] == "scripted":
        command = agent.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
            raise ContractError("scripted agent requires a command array")
    elif not agent.get("bundle") or not agent.get("model"):
        raise ContractError("codex agent requires bundle and model")
    if config["network"]["mode"] not in ("offline", "proxy"):
        raise ContractError("network.mode must be offline or proxy")
    if agent["type"] == "codex" and config["network"]["mode"] == "offline":
        raise ContractError("codex requires network.mode=proxy")
    if not isinstance(config["build"]["timeout_seconds"], int) or config["build"]["timeout_seconds"] <= 0:
        raise ContractError("build.timeout_seconds must be positive")
    check_container_service(config["container_service"])
    check_model_monitor(config["model_monitor"])
    check_sandbox_monitor(config["sandbox_monitor"])
    if set(config['provenance']) != set(DEFAULTS['provenance']):
        raise ContractError('unknown provenance fields')
    for key, number in config['provenance'].items():
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number <= 0:
            raise ContractError('provenance.%s must be positive' % key)
    if type(config['provenance']['maximum_bytes']) is not int:
        raise ContractError('provenance.maximum_bytes must be an integer')
    if "module_path" in config["container_service"]:
        directory = (path.parent / config["container_service"]["module_path"]).resolve()
        if not directory.is_dir():
            raise ContractError("container_service.module_path directory does not exist: %s" % directory)
        config["container_service"]["module_path"] = str(directory)
    return config
