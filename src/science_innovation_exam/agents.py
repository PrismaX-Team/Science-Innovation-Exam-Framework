"""Prepare a script or a Codex bundle for one experiment.

A script is small, so it is copied into the run directory. A Codex bundle is
identified by a content fingerprint and mounted from its existing location;
it is not copied once per run. The same three prompt layers are written for
every agent: the fixed general text, an environment note from the container
service (empty today), and an optional agent note.
"""
import json
import os
import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .common.errors import ContractError
from .common.fingerprint import content_fingerprint
from .common.serialization import atomic_write_text
from .container_service.interface import MountSpec


GENERAL_PROMPT = Path(__file__).resolve().parent / "prompts" / "general.json"
# Codex locks its state and, on project storage, its binary cannot be executed.
# Both limits belong to the mount, so any agent with the same needs uses a local directory.
CODEX_MOUNT = "/opt/codex"
CODEX_LOCAL = "/var/lib/sie-codex"
HOME_LOCAL = "/var/lib/sie-home"
GATEWAY_PORT = 18765


@dataclass
class AgentLaunch:
    command: list
    mounts: list
    environment: dict
    secrets: list = field(default_factory=list)
    # Shell commands a backend runs before the agent when its mounts cannot execute or lock.
    # ``{owner}`` is replaced with ``uid:gid``. Empty when the mount is an ordinary disk.
    local_setup: list = field(default_factory=list)
    command_rewrites: list = field(default_factory=list)
    log_paths: list = field(default_factory=list)
    local_home: str = ""
    local_reason: str = ""


def _contents(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, list) or not value:
        raise ContractError("prompt file must be a non-empty list: %s" % path)
    messages = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("content"), str) or not item["content"].strip():
            raise ContractError("prompt file entries need a content string: %s" % path)
        messages.append(item["content"].strip())
    return messages


def prompt_for(config, environment_prompt=()):
    """General text, then the container service's environment note, then the agent note."""
    messages = _contents(GENERAL_PROMPT)
    for line in environment_prompt or ():
        if not isinstance(line, str) or not line.strip():
            raise ContractError("environment prompt entries must be non-empty strings")
        messages.append(line.strip())
    agent_prompt = config["agent"].get("prompt")
    if agent_prompt:
        messages.extend(_contents(agent_prompt))
    return "\n\n".join(messages)


def rewrite_prefixes(argv, pairs):
    rewritten = []
    for part in argv:
        for old, new in pairs:
            if part == old or part.startswith(old + "/"):
                part = new + part[len(old):]
                break
        rewritten.append(part)
    return rewritten


def archive_logs_command(log_paths):
    """Copy only the declared log paths onto /state. Missing paths are skipped."""
    parts = []
    for source, name in log_paths:
        path = Path(name)
        if path.is_absolute() or not path.parts or any(part in ("..", ".") for part in path.parts):
            raise ContractError("agent log name must stay inside /state: %s" % name)
        destination = "/state/" + path.as_posix()
        source_q, dest_q = shlex.quote(source), shlex.quote(destination)
        parts.append(
            "if [ -d %s ]; then rm -rf %s && mkdir -p %s && cp -a %s/. %s/; "
            "elif [ -e %s ]; then mkdir -p %s && cp -a %s %s; fi"
            % (source_q, dest_q, dest_q, source_q, dest_q, source_q, shlex.quote(str(Path(destination).parent)), source_q, dest_q)
        )
    return " && ".join(parts)


def _link_or_copy(source, destination, *, follow_symlinks=True):
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination, follow_symlinks=follow_symlinks)


def stage(source, run_dir, relative):
    """Copy one configured file into the run directory and return its relative path."""
    source, destination = Path(source), run_dir / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(str(source), str(destination), symlinks=True, copy_function=_link_or_copy)
    else:
        shutil.copy2(str(source), str(destination))
    return relative


def _codex_setup():
    # Project storage is mounted noexec and returns ENOLCK. Copy the program and the home
    # onto the sandbox's own disk, then chown them to the workload user.
    bundle = ("rm -rf %s && mkdir -p %s && cp -a %s/. %s/ && chmod -R a+rx %s && test -x %s/bin/codex"
              % (CODEX_LOCAL, CODEX_LOCAL, CODEX_MOUNT, CODEX_LOCAL, CODEX_LOCAL, CODEX_LOCAL))
    home = ("rm -rf %s && mkdir -p %s && cp -a /state/. %s/ && chown -R {owner} %s %s"
            % (HOME_LOCAL, HOME_LOCAL, HOME_LOCAL, HOME_LOCAL, CODEX_LOCAL))
    return [bundle, home]


def prepare_agent(config, run_dir, channel_environment, proxy_environment=None, environment_prompt=()):
    """``proxy_environment`` and ``environment_prompt`` come from the container service."""
    agent = config["agent"]
    state = run_dir / "agent-state"
    state.mkdir()
    prompt = prompt_for(config, environment_prompt)
    atomic_write_text(state / "prompt.txt", prompt, mode=0o444)
    mounts = [MountSpec("agent-state", "/state", False)]
    environment = {"HOME": "/state", "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8",
                   "SIE_ACTION_TIMEOUT": str(config["verifier"]["timeout_seconds"] + 30),
                   "SIE_ACTION_RESPONSE_BYTES": str(config["limits"]["response_bytes"] + 65536)}
    environment.update(channel_environment)
    launch = AgentLaunch(command=list(agent.get("command") or []), mounts=mounts, environment=environment)
    if agent["type"] == "scripted":
        if agent.get("script"):
            script = Path(agent["script"])
            if not script.is_file():
                raise ContractError("agent script does not exist")
            mounts.append(MountSpec(stage(script, run_dir, "agent/agent.py"), "/agent.py", True))
        launch.command = list(agent["command"])
        return launch
    bundle = Path(agent["bundle"])
    if not (bundle / "bin/codex").is_file():
        raise ContractError("Codex bundle is missing bin/codex")
    url, key = os.environ.get("OPENAI_BASE_URL"), os.environ.get("OPENAI_API_KEY")
    if not url or not key:
        raise ContractError("Codex requires OPENAI_BASE_URL and OPENAI_API_KEY in the controller environment")
    fingerprint = content_fingerprint(bundle)
    atomic_write_text(state / "program.json", json.dumps(
        {"kind": "codex", "fingerprint": fingerprint, "source": str(bundle.resolve())}, sort_keys=True) + "\n", mode=0o444)
    mounts.append(MountSpec(str(bundle.resolve()), CODEX_MOUNT, True, shared="agent_bundles/codex/%s" % fingerprint))
    provider_url = "http://127.0.0.1:%d/v1" % GATEWAY_PORT
    environment.update(HOME=HOME_LOCAL, CODEX_HOME=HOME_LOCAL, SIE_MODEL_GATEWAY_CREDENTIAL=key)
    settings = ["model = " + json.dumps(agent["model"]), 'model_provider = "sie"',
                "model_reasoning_effort = " + json.dumps(agent.get("reasoning_effort", "high")),
                "check_for_update_on_startup = false", "[model_providers.sie]",
                'name = "Science Innovation Exam"', "base_url = " + json.dumps(provider_url),
                'wire_api = "responses"', 'env_key = "SIE_MODEL_GATEWAY_CREDENTIAL"']
    atomic_write_text(state / "config.toml", "\n".join(settings) + "\n")
    shell_values = {k: v for k, v in environment.items() if k != "SIE_MODEL_GATEWAY_CREDENTIAL"}
    if config["network"]["mode"] == "proxy":
        shell_values.update(proxy_environment or {})
    with (state / "config.toml").open("a") as output:
        output.write('\n[shell_environment_policy]\ninherit = "none"\nignore_default_excludes = false\n')
        output.write('\n[shell_environment_policy.set]\n')
        for field_name, value in sorted(shell_values.items()):
            output.write('%s = %s\n' % (field_name, json.dumps(value)))
    codex_argv = [CODEX_MOUNT + "/bin/codex", "--strict-config", "--dangerously-bypass-approvals-and-sandbox",
                  "exec", "--json", "--skip-git-repo-check", "--ignore-rules", prompt]
    launch.command = ["python3", "-m", "science_innovation_exam.model_gateway",
                      "--upstream-base-url", url.strip().rstrip("/"),
                      "--upstream-timeout", str(config["model_monitor"]["connect_timeout_seconds"]),
                      "--trace-path", "/state/model-calls.jsonl",
                      "--summary-path", "/state/model-calls-summary.json",
                      "--activity-path", "/state/model-activity.json",
                      "--monitor-options", json.dumps(config["model_monitor"]),
                      "--adapter", "science_innovation_exam.codex_adapter", "--"] + codex_argv
    launch.secrets = [key]
    launch.local_setup = _codex_setup()
    launch.command_rewrites = [(CODEX_MOUNT, CODEX_LOCAL)]
    launch.log_paths = [("%s/sessions" % HOME_LOCAL, "sessions"),
                        ("%s/shell_snapshots" % HOME_LOCAL, "shell_snapshots")]
    launch.local_home = HOME_LOCAL
    launch.local_reason = ("Project storage cannot execute binaries (noexec) or take POSIX locks (ENOLCK). "
                           "The program and its home directory are copied to the sandbox local disk.")
    return launch
