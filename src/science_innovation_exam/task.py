"""The five-part task contract, copied directly without package locks."""
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from .common.errors import ContractError
from .common.fingerprint import content_fingerprint
from .common.paths import require_safe_relative
from .container_service.interface import MountSpec
from .contracts.validation import load_json, validate_schema
from .matrix.transforms import ANCHOR_ONLY_TYPES, DIRECTION_BY_TYPE


def files_in(root):
    files = []
    for path in sorted(root.rglob("*")):
        kind = path.lstat().st_mode
        if stat.S_ISDIR(kind):
            continue
        if not stat.S_ISREG(kind):
            raise ContractError("links and special files are not allowed: %s" % path)
        files.append(path.relative_to(root).as_posix())
    return files


def readonly(root):
    # Container bind mounts enforce immutability. Keep directory owner-write so
    # a verifier's shutil.copytree can extend its own private copy of a submission.
    for path in root.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o444)
    root.chmod(0o755)


def copy_artifact(source, destination):
    if source.is_dir():
        shutil.copytree(source, destination, copy_function=shutil.copyfile)
    else:
        shutil.copyfile(source, destination)


def check_baseline_anchors(meta):
    """The 1000 anchor must be strictly better than the 0 anchor.

    Direction comes from metric.type. ``raw_only`` and ``null`` declare no
    direction, so the ordering is the task author's responsibility there.
    """
    baseline = meta.get("baseline")
    if baseline is None:
        return
    direction = DIRECTION_BY_TYPE.get(meta["metric"]["type"])
    if direction is None:
        return
    reference, bottom = baseline["score"], baseline["bottom_line_score"]
    better = reference < bottom if direction == "minimize" else reference > bottom
    if not better:
        raise ContractError(
            "baseline.score must be strictly better than baseline.bottom_line_score for a %s metric" % direction
        )


@dataclass
class TaskPackage:
    root: Path
    instruction: dict
    meta: dict
    environment: dict
    files: list

    @classmethod
    def load(cls, root):
        root = Path(root).resolve()
        if not root.is_dir() or {p.name for p in root.iterdir()} != {"instruction.json", "meta.json", "input", "environment", "verifier"}:
            raise ContractError("task must contain exactly instruction.json, input/, environment/, verifier/, meta.json")
        files = files_in(root)
        instruction = load_json(root / "instruction.json")
        meta = load_json(root / "meta.json")
        environment = load_json(root / "environment/environment.json")
        for value, name in ((instruction, "instruction"), (meta, "meta"), (environment, "environment")):
            validate_schema(value, name + ".schema.json")
        for entry in ("validation/run/main.py", "test/run/main.py"):
            if not (root / "verifier" / entry).is_file():
                raise ContractError("missing verifier entry: %s" % entry)
        task = cls(root, instruction, meta, environment, files)
        if meta["metric"]["type"] == "custom" and not task.custom_matrix_entry.is_file():
            raise ContractError("custom metric requires verifier/matrix/transform.py")
        check_baseline_anchors(meta)
        if (
            meta["metric"]["type"] in ANCHOR_ONLY_TYPES
            and "transform" not in meta["metric"]
            and "baseline" not in meta
        ):
            raise ContractError(
                "linear/log-ratio metric without transform requires baseline.score and baseline.bottom_line_score"
            )
        declared = []
        for item in instruction["input"]:
            relative = require_safe_relative(item["path"], "input.path")
            if not (task.input_root / relative).exists():
                raise ContractError("missing input: %s" % relative)
            declared.append(relative)
        outputs = []
        for item in instruction["submission"]:
            relative = require_safe_relative(item["path"], "submission.path")
            if any(relative == p or relative in p.parents or p in relative.parents for p in outputs):
                raise ContractError("overlapping submission paths")
            outputs.append(relative)
            if item.get("has_template", True):
                if not (task.input_root / relative).exists():
                    raise ContractError("missing submission template: %s" % relative)
                declared.append(relative)
        for name in files_in(task.input_root):
            relative = Path(name)
            if not any(relative == p or p in relative.parents for p in declared):
                raise ContractError("undeclared input: %s" % name)
        if environment["type"] == "containerfile":
            relative = require_safe_relative(environment["containerfile"], "environment.containerfile")
            if not (root / "environment" / relative).is_file():
                raise ContractError("missing task Containerfile")
        return task

    @property
    def task_id(self):
        return self.meta["id"]

    @property
    def gpu_count(self):
        return self.instruction.get("execution", {}).get("gpu_count", 0)

    @property
    def input_root(self):
        return self.root / "input"

    @property
    def custom_matrix_entry(self):
        return self.root / "verifier/matrix/transform.py"

    @property
    def submission_paths(self):
        return [item["path"] for item in self.instruction["submission"]]

    def fingerprint(self, refresh=False):
        if refresh or getattr(self, "_fingerprint", None) is None:
            self._fingerprint = content_fingerprint(self.root)
        return self._fingerprint

    def shared(self, relative):
        relative = str(relative).strip("/")
        return "task_packages/%s/%s/%s" % (self.task_id, self.fingerprint(), relative)

    def agent_mounts(self):
        """Read-only mounts of the public task files. ``shared`` is the content-addressed key."""
        return [MountSpec(str(self.input_root), "/input", True, shared=self.shared("input")),
                MountSpec(str(self.root / "instruction.json"), "/instruction.json", True,
                          shared=self.shared("instruction.json"))]

    def verifier_mounts(self):
        return [MountSpec(str(self.input_root), "/input", True, shared=self.shared("input")),
                MountSpec(str(self.root / "verifier"), "/verifier", True, shared=self.shared("verifier"))]

    def prepare_submission(self, destination):
        """Copy templates into the writable submission directory. Paths match ``/input``."""
        destination.mkdir(parents=True)
        for item in self.instruction["submission"]:
            if item.get("has_template", True):
                target = destination / item["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                copy_artifact(self.input_root / item["path"], target)
        return destination

    def freeze(self, source, destination):
        """Copy declared files and directory trees into a separate scoring snapshot."""
        source = Path(source)
        if source.is_symlink() or not source.is_dir():
            raise ContractError("submission root must be a real directory")
        source, destination = source.resolve(), Path(destination).resolve()
        if source == destination or source in destination.parents or destination in source.parents:
            raise ContractError("frozen submission must use a separate directory")
        for name in self.submission_paths:
            selected = source
            for part in Path(name).parts:
                selected = selected / part
                if selected.is_symlink():
                    raise ContractError("submission links are not allowed: %s" % name)
            if not selected.exists():
                raise ContractError("missing submission: %s" % name)
            if selected.is_dir():
                files_in(selected)  # Reject links and special files anywhere in the tree before copying.
            elif not selected.is_file():
                raise ContractError("submission must be a regular file or directory: %s" % name)
        destination.mkdir()
        for name in self.submission_paths:
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            copy_artifact(source / name, target)
        readonly(destination)
