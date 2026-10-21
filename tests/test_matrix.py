import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from science_innovation_exam.task import TaskPackage
from science_innovation_exam.common.errors import ContractError
from science_innovation_exam.contracts.validation import validate_matrix_result
from science_innovation_exam.matrix import (
    DevelopmentPythonTransformRunner,
    MatrixContractError,
    MatrixDomainError,
    MatrixEngine,
    MatrixScoringError,
    validate_metric_definition,
)

try:
    from .helpers import _make_package, _meta, _write_json
except ImportError:  # unittest discover imports test modules without the package prefix.
    from helpers import _make_package, _meta, _write_json


DIGEST = "a" * 64


def _metric(
    metric_type="linear_maximize",
    direction="maximize",
    parameters=None,
    lower_raw="10",
    reference_raw="20",
):
    return {
        "description": "Frozen test metric.",
        "type": metric_type,
        "transform": {
            "raw_direction": direction,
            "parameters": {} if parameters is None else parameters,
            "anchors": {
                "lower": {"raw_value": lower_raw, "matrix_score": "0"},
                "reference": {"raw_value": reference_raw, "matrix_score": "1000"},
            },
            "invalid_score": "-100",
            "precision": 50,
            "rounding": {"decimal_places": 6, "mode": "half_even"},
        },
    }


def _verifier(value, direction="maximize", valid=True, error=None):
    return {
        "task_id": "matrix_fixture",
        "valid": valid,
        "primary_metric": {"name": "objective", "value": value, "direction": direction},
        "error": error,
    }


class MatrixTests(unittest.TestCase):
    def test_linear_type_can_score_from_baseline_without_transform(self):
        metric = {"description": "Higher is better.", "type": "linear_maximize"}
        validate_metric_definition(metric)
        baseline = {"existing_baseline": False, "name": "", "source": "", "score": 20.0, "bottom_line_score": 10.0}
        result = MatrixEngine().score(
            "matrix_fixture", DIGEST, metric, _verifier(15), baseline=baseline
        ).as_mapping()
        self.assertEqual(result["matrix_score"], "500.000000")
        with self.assertRaises(MatrixContractError):
            MatrixEngine().score("matrix_fixture", DIGEST, metric, _verifier(15))

    def test_fixed_anchor_score_is_independent_of_later_results(self):
        engine = MatrixEngine()
        metric = _metric()
        first = engine.score("matrix_fixture", DIGEST, metric, _verifier(15)).as_mapping()
        later = engine.score("matrix_fixture", DIGEST, metric, _verifier(25)).as_mapping()
        repeated = engine.score("matrix_fixture", DIGEST, metric, _verifier(15)).as_mapping()
        self.assertEqual(first, repeated)
        self.assertEqual(first["matrix_score"], "500.000000")
        self.assertEqual(later["matrix_score"], "1500.000000")
        validate_matrix_result(first)
        validate_matrix_result(later)

    def test_residual_log_rewards_reducing_the_remaining_error(self):
        metric = _metric(
            "upper_residual_log",
            "maximize",
            {"bound": "100", "epsilon": "0.001"},
            lower_raw="10",
            reference_raw="99",
        )
        result = MatrixEngine().score(
            "matrix_fixture", DIGEST, metric, _verifier(99.9)
        )
        self.assertGreater(result.matrix_score, Decimal("1000"))
        with self.assertRaises(MatrixDomainError):
            MatrixEngine().score("matrix_fixture", DIGEST, metric, _verifier(100.1))

    def test_minimize_log_power_and_piecewise_transforms(self):
        cases = (
            (
                _metric(
                    "log_ratio_minimize",
                    "minimize",
                    {},
                    lower_raw="100",
                    reference_raw="10",
                ),
                _verifier(1, direction="minimize"),
            ),
            (
                _metric(
                    "power_maximize",
                    "maximize",
                    {"origin": "0", "exponent": "2"},
                    lower_raw="1",
                    reference_raw="2",
                ),
                _verifier(3),
            ),
            (
                _metric(
                    "monotone_piecewise_minimize",
                    "minimize",
                    {
                        "knots": [
                            {"raw_value": "0", "utility_value": "10"},
                            {"raw_value": "5", "utility_value": "4"},
                            {"raw_value": "10", "utility_value": "0"},
                        ]
                    },
                    lower_raw="10",
                    reference_raw="5",
                ),
                _verifier(2.5, direction="minimize"),
            ),
        )
        for metric, verifier in cases:
            with self.subTest(metric_type=metric["type"]):
                result = MatrixEngine().score(
                    "matrix_fixture", DIGEST, metric, verifier
                )
                self.assertGreater(result.matrix_score, Decimal("1000"))

    def test_unconfigured_raw_only_and_invalid_are_distinct(self):
        engine = MatrixEngine()
        unconfigured = engine.score(
            "matrix_fixture",
            DIGEST,
            {"description": "Unreviewed.", "type": None},
            _verifier(1),
        ).as_mapping()
        raw_only = engine.score(
            "matrix_fixture",
            DIGEST,
            {"description": "Reviewed but not comparable.", "type": "raw_only"},
            _verifier(1),
        ).as_mapping()
        invalid = engine.score(
            "matrix_fixture",
            DIGEST,
            _metric(),
            _verifier(0, valid=False, error="invalid submission"),
        ).as_mapping()
        self.assertEqual(unconfigured["status"], "unconfigured")
        self.assertEqual(raw_only["status"], "raw_only")
        self.assertEqual(invalid["status"], "invalid")
        self.assertEqual(invalid["matrix_score"], "-100.000000")
        for result in (unconfigured, raw_only, invalid):
            validate_matrix_result(result)

    def test_definition_validation_fails_closed(self):
        mismatched = _metric("linear_maximize", "minimize")
        with self.assertRaisesRegex(MatrixContractError, "disagree"):
            validate_metric_definition(mismatched)
        unknown_parameter = _metric(parameters={"curve": "hidden"})
        with self.assertRaisesRegex(MatrixContractError, "unknown"):
            validate_metric_definition(unknown_parameter)
        reversed_anchors = _metric(lower_raw="20", reference_raw="10")
        with self.assertRaisesRegex(MatrixContractError, "greater utility"):
            validate_metric_definition(reversed_anchors)
        collapsed_scores = _metric()
        collapsed_scores["transform"]["anchors"]["lower"]["matrix_score"] = "0.1"
        collapsed_scores["transform"]["anchors"]["reference"]["matrix_score"] = "0.2"
        collapsed_scores["transform"]["rounding"]["decimal_places"] = 0
        with self.assertRaisesRegex(MatrixContractError, "after frozen rounding"):
            validate_metric_definition(collapsed_scores)
        secret_parameter = _metric(
            "custom", "maximize", {"api_key": "must-not-be-present"}
        )
        with self.assertRaises(ContractError):
            validate_metric_definition(secret_parameter)

        with self.assertRaisesRegex(MatrixScoringError, "boolean"):
            MatrixEngine().score(
                "matrix_fixture",
                DIGEST,
                _metric(),
                {**_verifier(1), "valid": "false"},
            )

    def test_result_contract_rejects_status_type_mismatch(self):
        result = MatrixEngine().score(
            "matrix_fixture", DIGEST, _metric(), _verifier(15)
        ).as_mapping()
        inconsistent = dict(result)
        inconsistent["metric_type"] = "raw_only"
        with self.assertRaisesRegex(ContractError, "comparable metric type"):
            validate_matrix_result(inconsistent)

    def test_large_raw_decimal_stays_compact(self):
        result = MatrixEngine().score(
            "matrix_fixture",
            DIGEST,
            {"description": "Unreviewed.", "type": None},
            _verifier("1E+1000000"),
        ).as_mapping()
        self.assertEqual(result["raw_metric"]["value"], "1E+1000000")

    def test_package_owned_custom_function_and_vendored_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "task"
            matrix_root = root / "verifier" / "matrix"
            vendor_root = matrix_root / "vendor"
            vendor_root.mkdir(parents=True)
            (vendor_root / "offset.py").write_text(
                "from decimal import Decimal\nOFFSET = Decimal('1')\n",
                encoding="utf-8",
            )
            (matrix_root / "transform.py").write_text(
                """\
from decimal import Decimal
from offset import OFFSET

def to_utility(raw_value, parameters):
    return raw_value * Decimal(parameters['scale']) + OFFSET
""",
                encoding="utf-8",
            )
            metric = _metric(
                "custom",
                "maximize",
                {"scale": "2"},
                lower_raw="0",
                reference_raw="10",
            )
            runner = DevelopmentPythonTransformRunner(
                matrix_root / "transform.py", sys.executable
            )
            result = MatrixEngine().score(
                "matrix_fixture",
                DIGEST,
                metric,
                _verifier(12),
                custom_evaluator=runner,
            ).as_mapping()
            self.assertEqual(result["matrix_score"], "1200.000000")
            validate_matrix_result(result)

    def test_formal_loader_requires_custom_entry_and_hashes_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package_root = _make_package(root)
            meta = _meta()
            meta["metric"] = _metric(
                "custom", "maximize", {"scale": "1"}, lower_raw="0", reference_raw="1"
            )
            _write_json(package_root / "meta.json", meta)
            with self.assertRaisesRegex(ContractError, "verifier/matrix/transform.py"):
                TaskPackage.load(package_root)
            custom_entry = package_root / "verifier" / "matrix" / "transform.py"
            custom_entry.parent.mkdir()
            custom_entry.write_text(
                "def to_utility(raw_value, parameters):\n    return raw_value\n",
                encoding="utf-8",
            )
            package = TaskPackage.load(package_root)
            self.assertEqual(package.custom_matrix_entry, custom_entry)
            self.assertIn(
                "verifier/matrix/transform.py",
                package.files,
            )

    def test_custom_requires_an_isolated_evaluator(self):
        metric = _metric("custom", "maximize", {}, lower_raw="0", reference_raw="1")
        with self.assertRaisesRegex(MatrixScoringError, "isolated evaluator"):
            MatrixEngine().score("matrix_fixture", DIGEST, metric, _verifier(1))


if __name__ == "__main__":
    unittest.main()
