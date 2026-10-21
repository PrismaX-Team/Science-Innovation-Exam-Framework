"""Select the container backend named by ``config["container_service"]``.

``type: docker`` is the built-in default. Any other type names a plugin
module (``module`` field) that is imported by name and must expose
``create(config, run_dir) -> ContainerService``. The framework never bundles a
plugin; that keeps site-specific backends out of the open-source tree.
"""
import importlib
import sys
from pathlib import Path

from ..common.errors import ContractError
from .interface import AgentObservation, Channel, ContainerService, ContainerSpec, Handle, ImageRef, MountSpec  # noqa: F401


def load(config, run_dir):
    options = config["container_service"]
    if options["type"] == "docker":
        from .docker import DockerService
        return DockerService(config, run_dir)
    directory = options.get("module_path")
    if directory:
        # Keep the search path available for lazy imports inside the plugin.
        sys.path.insert(0, directory)
        importlib.invalidate_caches()
    try:
        module = importlib.import_module(options["module"])
    except ImportError as error:
        raise ContractError("container_service.module cannot be imported: %s" % error)
    if directory and (not getattr(module, "__file__", None)
                      or Path(directory) not in Path(module.__file__).resolve().parents):
        raise ContractError("container_service.module was imported from outside module_path")
    if not callable(getattr(module, "create", None)):
        raise ContractError("container_service.module must define create(config, run_dir)")
    # module_path belongs to the loader, not the backend's configuration schema.
    plugin_config = dict(config, container_service={k: v for k, v in options.items() if k != "module_path"})
    service = module.create(plugin_config, run_dir)
    if not isinstance(service, ContainerService):
        raise ContractError("container_service plugin did not return a ContainerService")
    return service
