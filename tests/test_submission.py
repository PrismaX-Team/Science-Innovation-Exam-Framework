"""File and directory submissions share the same declaration and isolation rules."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from science_innovation_exam.common.errors import ContractError
from science_innovation_exam.task import TaskPackage
from tests.helpers import _make_package, _write_json


class SubmissionTests(unittest.TestCase):
    def package(self, root, template=False):
        package = _make_package(root)
        instruction = json.loads((package / "instruction.json").read_text())
        artifact = {"path": "outputs/model", "format": "directory", "description": "Model artifacts."}
        if template:
            _write_json(package / "input/outputs/model/nested/config.json", {"value": 1})
            (package / "input/outputs/model/empty").mkdir()
        else:
            artifact["has_template"] = False
        instruction["submission"].append(artifact)
        _write_json(package / "instruction.json", instruction)
        return package

    def test_directory_template_is_copied_including_empty_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = TaskPackage.load(self.package(root, template=True))
            submission = task.prepare_submission(root / "submission")
            self.assertTrue((submission / "outputs/model/empty").is_dir())
            config = submission / "outputs/model/nested/config.json"
            self.assertEqual(json.loads(config.read_text()), {"value": 1})
            config.write_text('{"value": 2}')
            self.assertEqual(json.loads((task.input_root / "outputs/model/nested/config.json").read_text()),
                             {"value": 1})

    def test_freeze_mixed_files_and_trees_is_complete_and_independent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self.package(root)
            instruction = json.loads((package / "instruction.json").read_text())
            instruction["submission"].append({"path": "empty", "format": "directory",
                                               "description": "Empty output directory", "has_template": False})
            _write_json(package / "instruction.json", instruction)
            task = TaskPackage.load(package)
            submission = task.prepare_submission(root / "submission")
            self.assertFalse((submission / "outputs/model").exists())
            _write_json(submission / "submission.json", {"value": 7})
            weights = submission / "outputs/model/nested/.weights"
            weights.parent.mkdir(parents=True)
            weights.write_bytes(b"weights")
            (submission / "outputs/model/nested/empty").mkdir()
            (submission / "empty").mkdir()
            (submission / "ignored").symlink_to(root / "absent")
            frozen = root / "frozen"
            task.freeze(submission, frozen)
            self.assertEqual(sorted(path.relative_to(frozen).as_posix() for path in frozen.rglob("*")),
                             ["empty", "outputs", "outputs/model", "outputs/model/nested",
                              "outputs/model/nested/.weights", "outputs/model/nested/empty", "submission.json"])
            self.assertEqual((frozen / "outputs/model/nested/.weights").read_bytes(), b"weights")
            self.assertEqual((frozen / "outputs/model/nested/.weights").stat().st_mode & 0o222, 0)
            weights.write_bytes(b"changed")
            self.assertEqual((frozen / "outputs/model/nested/.weights").read_bytes(), b"weights")
            self.assertTrue((submission / "ignored").is_symlink())

    def test_directory_contents_reject_links_and_special_files_before_copying(self):
        for kind in ("file-link", "directory-link", "broken-link", "internal-link", "fifo"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                task = TaskPackage.load(self.package(root))
                submission = task.prepare_submission(root / "submission")
                _write_json(submission / "submission.json", {"value": 7})
                nested = submission / "outputs/model/nested"
                nested.mkdir(parents=True)
                invalid = nested / "invalid"
                if kind == "fifo":
                    os.mkfifo(invalid)
                else:
                    targets = {"file-link": task.input_root / "value.json", "directory-link": task.input_root,
                               "broken-link": root / "absent", "internal-link": submission / "submission.json"}
                    invalid.symlink_to(targets[kind], target_is_directory=(kind == "directory-link"))
                with self.assertRaisesRegex(ContractError, "links and special files"):
                    task.freeze(submission, root / "frozen")
                self.assertFalse((root / "frozen").exists())

    def test_missing_directory_and_template_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self.package(root)
            task = TaskPackage.load(package)
            submission = task.prepare_submission(root / "submission")
            _write_json(submission / "submission.json", {"value": 7})
            with self.assertRaisesRegex(ContractError, "missing submission: outputs/model"):
                task.freeze(submission, root / "frozen")
            task.instruction["submission"][1].pop("has_template")
            _write_json(package / "instruction.json", task.instruction)
            with self.assertRaisesRegex(ContractError, "missing submission template"):
                TaskPackage.load(package)

    def test_directory_declarations_cannot_overlap(self):
        for paths in (("outputs/model", "outputs/model/nested"),
                      ("outputs/model/nested", "outputs/model"), ("outputs/model", "outputs/model")):
            with self.subTest(paths=paths), tempfile.TemporaryDirectory() as temporary:
                package = self.package(Path(temporary))
                instruction = json.loads((package / "instruction.json").read_text())
                instruction["submission"] = [{"path": path, "format": "directory", "description": "Artifacts",
                                               "has_template": False} for path in paths]
                _write_json(package / "instruction.json", instruction)
                with self.assertRaisesRegex(ContractError, "overlapping submission paths"):
                    TaskPackage.load(package)

    def test_freeze_destination_cannot_be_inside_source_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = TaskPackage.load(self.package(root))
            submission = task.prepare_submission(root / "submission")
            _write_json(submission / "submission.json", {"value": 7})
            model = submission / "outputs/model"
            model.mkdir(parents=True)
            with self.assertRaisesRegex(ContractError, "separate directory"):
                task.freeze(submission, model / "frozen")
            self.assertFalse((model / "frozen").exists())

    def test_empty_directory_changes_invalidate_shared_task_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            task = TaskPackage.load(self.package(Path(temporary), template=True))
            original = task.fingerprint()
            (task.input_root / "outputs/model/empty").rmdir()
            self.assertNotEqual(task.fingerprint(refresh=True), original)
            (task.input_root / "outputs/model/empty").mkdir()
            self.assertEqual(task.fingerprint(refresh=True), original)
