from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class MatrixResult:
    task_id: str
    task_payload_sha256: str
    status: str
    metric_type: Optional[str]
    metric_name: str
    raw_value: Decimal
    raw_direction: str
    raw_valid: bool
    matrix_config_sha256: str
    utility_value: Optional[Decimal]
    matrix_score: Optional[Decimal]
    score_decimal_places: int
    error: Optional[str]

    @staticmethod
    def _plain(value: Decimal, decimal_places: Optional[int] = None) -> str:
        if value.is_zero():
            if decimal_places is None:
                return "0"
            value = value.copy_abs()
        if decimal_places is not None:
            return format(value, ".%df" % decimal_places)
        # ``format(value, "f")`` can expand a compact value such as 1E+1000000
        # into a million-character artifact. Decimal's string form stays exact
        # and uses scientific notation when that is the safe representation.
        text = str(value)
        if "." in text and "e" not in text.lower():
            text = text.rstrip("0").rstrip(".")
        return text or "0"

    def as_mapping(self) -> Mapping[str, Any]:
        return {
            "schema_version": "1.0",
            "task_id": self.task_id,
            "task_payload_sha256": self.task_payload_sha256,
            "status": self.status,
            "metric_type": self.metric_type,
            "raw_metric": {
                "name": self.metric_name,
                "value": self._plain(self.raw_value),
                "direction": self.raw_direction,
                "valid": self.raw_valid,
            },
            "matrix_config_sha256": self.matrix_config_sha256,
            "utility_value": (
                None if self.utility_value is None else self._plain(self.utility_value)
            ),
            "matrix_score": (
                None
                if self.matrix_score is None
                else self._plain(self.matrix_score, self.score_decimal_places)
            ),
            "error": self.error,
        }


@dataclass(frozen=True)
class DisplayScoreResult:
    benchmark_release_id: str
    task_id: str
    task_payload_sha256: str
    status: str
    metric_name: str
    raw_value: Decimal
    raw_direction: str
    raw_valid: bool
    verifier_result_sha256: str
    display_scale_sha256: str
    display_score: Optional[Decimal]
    score_decimal_places: int
    error: Optional[str]

    def as_mapping(self) -> Mapping[str, Any]:
        return {
            "schema_version": "1.0",
            "benchmark_release_id": self.benchmark_release_id,
            "task_id": self.task_id,
            "task_payload_sha256": self.task_payload_sha256,
            "status": self.status,
            "raw_metric": {
                "name": self.metric_name,
                "value": MatrixResult._plain(self.raw_value),
                "direction": self.raw_direction,
                "valid": self.raw_valid,
            },
            "verifier_result_sha256": self.verifier_result_sha256,
            "display_scale_sha256": self.display_scale_sha256,
            "display_score": (
                None
                if self.display_score is None
                else MatrixResult._plain(self.display_score, self.score_decimal_places)
            ),
            "error": self.error,
        }


class CustomUtilityEvaluator:
    """Small interface implemented by an isolated task-custom transform runner."""

    def evaluate(
        self,
        raw_values: tuple,
        parameters: Mapping[str, Any],
        precision: int,
    ) -> tuple:
        raise NotImplementedError
