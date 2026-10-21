import os
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from science_innovation_exam.common.errors import ContractError
from science_innovation_exam.verify_context import VERIFY_CONTEXT_ENV, VerifyContext


class VerifyContextTests(unittest.TestCase):
    def test_log_emits_json_lines_without_changing_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            context = VerifyContext.create(Path(temporary) / "run", maximum_bytes=1024)
            context["round"] = 1
            output = io.StringIO()
            records = [{"round": 2, "mean_ddg": -0.3}, "说明\n第二行"]
            with mock.patch("sys.stdout", output):
                for record in records:
                    context.log(record)
                with self.assertRaisesRegex(ContractError, "finite JSON"):
                    context.log({"value": float("nan")})
            self.assertEqual([json.loads(line) for line in output.getvalue().splitlines()], records)
            self.assertEqual(context.snapshot(), {"round": 1})

    def test_contexts_are_isolated_and_reopen_persisted_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = VerifyContext.create(root / "run-a", maximum_bytes=1024)
            second = VerifyContext.create(root / "run-b", maximum_bytes=1024)
            first["history"] = [{"candidate": "A"}]
            first.state["count"] = 1
            second["count"] = 9

            self.assertEqual(
                VerifyContext(root / "run-a").snapshot(),
                {"count": 1, "history": [{"candidate": "A"}]},
            )
            self.assertEqual(second.snapshot(), {"count": 9})

    def test_current_uses_only_the_framework_injected_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            VerifyContext.create(root, maximum_bytes=1024)
            with mock.patch.dict(os.environ, {VERIFY_CONTEXT_ENV: str(root)}):
                current = VerifyContext.current()
                current["phase"] = "validation"
            self.assertEqual(VerifyContext(root)["phase"], "validation")

    def test_context_rejects_non_json_and_payloads_over_configured_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            context = VerifyContext.create(Path(temporary) / "run", maximum_bytes=20)
            with self.assertRaisesRegex(ContractError, "finite JSON"):
                context["bad"] = float("nan")
            with self.assertRaisesRegex(ContractError, "byte limit"):
                context["too-large"] = "x" * 30
            self.assertEqual(context.snapshot(), {})

    def test_replace_all_is_atomic_and_enforces_the_same_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            context = VerifyContext.create(Path(temporary) / "run", maximum_bytes=40)
            context["old"] = {"value": 1}
            context.replace_all({"new": [1, 2, 3]})
            self.assertEqual(context.snapshot(), {"new": [1, 2, 3]})
            with self.assertRaisesRegex(ContractError, "byte limit"):
                context.replace_all({"too-large": "x" * 50})
            self.assertEqual(context.snapshot(), {"new": [1, 2, 3]})


if __name__ == "__main__":
    unittest.main()
