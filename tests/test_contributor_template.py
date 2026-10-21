import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from science_innovation_exam.task import TaskPackage
from tests.helpers import _make_package, _write_json

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "contributor" / "build-sie-task-package"
TEMPLATE = SKILL / "template"
FRAMEWORK_SCHEMAS = ROOT / "src" / "science_innovation_exam" / "contracts" / "schemas"
SCHEMAS = (
    "instruction.schema.json",
    "meta.schema.json",
    "environment.schema.json",
    "verifier_result.schema.json",
)


class ContributorTemplateTests(unittest.TestCase):
    def test_template_loads_as_a_five_part_package(self):
        task = TaskPackage.load(TEMPLATE)
        self.assertEqual("untitled_task", task.task_id)
        self.assertEqual(["submission.json"], task.submission_paths)
        self.assertIsNone(task.meta["metric"]["type"])
        self.assertEqual(
            (False, "", "", 1.0, 0.0),
            (
                task.meta["baseline"]["existing_baseline"],
                task.meta["baseline"]["name"],
                task.meta["baseline"]["source"],
                task.meta["baseline"]["score"],
                task.meta["baseline"]["bottom_line_score"],
            ),
        )
        self.assertNotIn("source_id", task.meta)
        self.assertEqual(1, len(task.meta["task_sources"]))
        self.assertTrue((TEMPLATE / "input" / "data.json").is_file())
        self.assertFalse((TEMPLATE / "input" / "answer.json").exists())

    def test_bundled_schemas_match_the_framework_copies(self):
        for name in SCHEMAS:
            bundled = (SKILL / "schemas" / name).read_bytes()
            installed = (FRAMEWORK_SCHEMAS / name).read_bytes()
            self.assertEqual(installed, bundled, name)

    def test_check_script_accepts_the_template(self):
        script = SKILL / "scripts" / "check_package.py"
        completed = subprocess.run(
            [sys.executable, str(script), str(TEMPLATE)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual("package_valid", result["status"])
        self.assertEqual("untitled_task", result["task_id"])
        self.assertEqual(["submission.json"], result["submission_paths"])

    def test_fallback_checker_accepts_the_template(self):
        sys.path.insert(0, str(SKILL / "scripts"))
        try:
            import check_package
        finally:
            sys.path.pop(0)
        result = check_package.fallback_check(TEMPLATE)
        self.assertEqual("package_valid", result["status"])
        self.assertEqual("bundled-fallback", result["checker"])

    def test_fallback_checker_accepts_directory_templates_like_the_framework(self):
        sys.path.insert(0, str(SKILL / "scripts"))
        try:
            import check_package
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory() as temporary:
            package = _make_package(Path(temporary))
            instruction = json.loads((package / "instruction.json").read_text())
            instruction["submission"] = [{"path": "model", "format": "directory", "description": "Model"}]
            _write_json(package / "instruction.json", instruction)
            _write_json(package / "input/model/nested/config.json", {"value": 1})
            (package / "input/model/empty").mkdir()
            self.assertEqual(TaskPackage.load(package).submission_paths,
                             check_package.fallback_check(package)["submission_paths"])

    def test_containerfile_example_matches_the_environment_schema(self):
        from science_innovation_exam.contracts.validation import validate_schema

        example = SKILL / "examples" / "environment-containerfile"
        environment = json.loads((example / "environment.json").read_text(encoding="utf-8"))
        validate_schema(environment, "environment.schema.json")
        self.assertEqual("containerfile", environment["type"])
        self.assertTrue((example / environment["containerfile"]).is_file())
        self.assertIn("SIE_BASE_IMAGE", (example / "Containerfile").read_text(encoding="utf-8"))

    def test_template_has_no_score_config_or_test_data_tree(self):
        self.assertFalse((TEMPLATE / "verifier" / "test" / "data").exists())
