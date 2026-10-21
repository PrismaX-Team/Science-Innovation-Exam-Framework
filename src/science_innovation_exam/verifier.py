"""Execute task-owned validation and test in temporary offline containers."""
import json
import math

from .common.errors import InfrastructureError, ScoringError
from .common.host import writable
from .common.serialization import atomic_write_json
from .container_service.interface import ContainerSpec, MountSpec
from .contracts.validation import validate_schema
from .verify_context import VerifyContext


class Verifier:
    """Run-owned paths are relative to ``run_dir``. Task input and verifier code are shared mounts."""
    def __init__(self, service, task, image, context, config, run_dir):
        self.service, self.task, self.image = service, task, image
        self.context, self.config, self.run_dir = context, config, run_dir

    def relative(self, path):
        return path.resolve().relative_to(self.run_dir.resolve()).as_posix()

    def invoke(self, kind, directory, request=None, submission=None):
        directory.mkdir(parents=True, exist_ok=False)
        output = directory / "output"
        output.mkdir()
        writable(output)
        writable(self.context)
        mounts = self.task.verifier_mounts() + [
            MountSpec(self.relative(self.context), "/verify-context", False),
            MountSpec(self.relative(output), "/output", False)]
        command = ["python3", "-B", "/verifier/%s/run/main.py" % kind, "--input", "/input"]
        filename = "response.json" if kind == "validation" else "result.json"
        if kind == "validation":
            request_dir = directory / "request"
            request_dir.mkdir()
            atomic_write_json(request_dir / "request.json", request, mode=0o444)
            mounts.append(MountSpec(self.relative(request_dir), "/request", True))
            command += ["--request", "/request/request.json", "--response", "/output/response.json"]
        else:
            mounts.append(MountSpec(self.relative(submission), "/submission", True))
            command += ["--submission", "/submission", "--result", "/output/result.json"]
        before = VerifyContext(self.context).snapshot()
        try:
            spec = ContainerSpec(role=kind, image=self.image, command=command, mounts=mounts,
                                 resources=self.config["verifier"], network_mode="offline", workdir="/verifier",
                                 logs=self.relative(directory / "logs"), log_bytes=self.config["limits"]["log_bytes"],
                                 environment={"SIE_VERIFY_CONTEXT": "/verify-context", "PYTHONDONTWRITEBYTECODE": "1",
                                              "PYTHONHASHSEED": "0", "LANG": "C.UTF-8"})
            code = self.service.run_verifier(spec)
            if code in (125, 126, 127):
                raise InfrastructureError("cannot launch %s container; see %s" % (kind, directory / "logs"))
            if code:
                raise ScoringError("%s exited with code %s; see %s" % (kind, code, directory / "logs"))
            result_path = output / filename
            if result_path.is_symlink() or not result_path.is_file() or result_path.stat().st_size > self.config["limits"]["response_bytes"]:
                raise ScoringError("missing or oversized %s output" % kind)
            value = json.loads(result_path.read_text())
            if not isinstance(value, dict):
                raise ScoringError("%s output must be an object" % kind)
            if kind == "test":
                validate_result(value, self.task.task_id)
            return value
        except Exception as error:
            VerifyContext(self.context).replace_all(before)
            atomic_write_json(directory / "error.json", {"type": type(error).__name__, "message": str(error)})
            raise


def validate_result(value, task_id):
    validate_schema(value, "verifier_result.schema.json")
    if value["task_id"] != task_id or value["valid"] != (value["status"] == "valid"):
        raise ScoringError("test result identity or validity is inconsistent")
    if bool(value["error"]) == value["valid"]:
        raise ScoringError("test result error and validity disagree")
    primary = value["primary_metric"]
    metrics = value["metrics"]
    if not math.isfinite(primary["value"]) or any(not math.isfinite(m["value"]) for m in metrics):
        raise ScoringError("test metrics must be finite")
    if len({m["name"] for m in metrics}) != len(metrics):
        raise ScoringError("duplicate test metrics")
    if not any(m["name"] == primary["name"] and m["value"] == primary["value"] for m in metrics):
        raise ScoringError("primary metric is absent from test metrics")
