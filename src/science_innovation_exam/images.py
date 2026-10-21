"""Build this run's image on the development machine: base + optional task layer + framework layer.

Building always uses local Docker, whichever backend later runs the containers.
The backend's ``publish`` decides how the built image reaches its engine.
"""
import shutil
from pathlib import Path

from .container_service.docker import Docker


def prepare(task, config, run_dir, docker=None, build_args=None):
    """Return ``(image_id, local_tag)`` for a freshly built image.
    ``build_args`` come from the container service (``image_build_args``) and only reach the framework layer."""
    docker = docker or Docker()
    base = config["base_image"]
    build_options = {"network": config["build"]["network"], "timeout": config["build"]["timeout_seconds"],
                     "maximum_log_bytes": config["limits"]["log_bytes"]}
    tag = config["image"] + "-" + run_dir.name[-8:]
    if task.environment["type"] == "containerfile":
        # Task environments use the existing SIE_BASE_IMAGE build argument.
        base = docker.build(task.root / "environment", task.root / "environment" / task.environment["containerfile"],
                            tag + "-task", run_dir / "environment-build",
                            {"SIE_BASE_IMAGE": base}, **build_options)
    # Add the small VerifyContext SDK and action client to either kind of environment.
    context = run_dir / "runtime-context"
    context.mkdir()
    source = Path(__file__).resolve().parent
    shutil.copytree(str(source), str(context / "science_innovation_exam"), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copyfile(str(source / "Containerfile"), str(context / "Containerfile"))
    local_tag = docker.build(context, context / "Containerfile", tag, run_dir / "runtime-build",
                             dict(build_args or {}, BASE_IMAGE=base), **build_options)
    return docker.image_id(local_tag), local_tag
