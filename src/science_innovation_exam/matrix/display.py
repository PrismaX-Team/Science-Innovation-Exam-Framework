from decimal import Decimal, DecimalException, localcontext
from typing import Any, Mapping

from ..common.errors import ContractError
from ..common.hashing import normalized_sha256, sha256_mapping
from ..contracts.validation import validate_display_scale
from .engine import ROUNDING_MODES
from .errors import MatrixContractError, MatrixScoringError
from .models import DisplayScoreResult
from .transforms import decimal_value


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MatrixScoringError("%s must be an object" % field)
    return value


def validate_display_scale_definition(scale: Mapping[str, Any]) -> None:
    """Validate both the JSON shape and the scientific ordering of the anchors."""

    try:
        validate_display_scale(scale)
    except ContractError as error:
        raise MatrixContractError(str(error))
    direction = str(scale["raw_metric"]["direction"])
    calibration = scale["calibration"]
    lower = decimal_value(
        calibration["lower_anchor"]["raw_value"],
        "calibration.lower_anchor.raw_value",
        strings_only=True,
    )
    reference = decimal_value(
        calibration["reference_anchor"]["raw_value"],
        "calibration.reference_anchor.raw_value",
        strings_only=True,
    )
    directional_span = reference - lower if direction == "maximize" else lower - reference
    if directional_span <= 0:
        raise MatrixContractError(
            "display reference anchor must be strictly better than the calibration-worst anchor"
        )
    mapping = scale["mapping"]
    precision = int(mapping["precision"])
    decimal_places = int(mapping["rounding"]["decimal_places"])
    try:
        with localcontext() as context:
            context.prec = precision
            context.rounding = ROUNDING_MODES[str(mapping["rounding"]["mode"])]
            Decimal("0").quantize(Decimal(1).scaleb(-decimal_places))
            Decimal("1000").quantize(Decimal(1).scaleb(-decimal_places))
    except DecimalException as error:
        raise MatrixContractError(
            "display scores cannot be represented at the frozen precision: %s" % error
        )


class DisplayScaleEngine:
    """Pure direction-aware 0/1000 display mapping for one frozen release."""

    def score(
        self,
        scale: Mapping[str, Any],
        verifier_result: Mapping[str, Any],
    ) -> DisplayScoreResult:
        validate_display_scale_definition(scale)
        if not isinstance(verifier_result, Mapping):
            raise MatrixScoringError("verifier result must be an object")
        task_id = str(scale["task_id"])
        if verifier_result.get("task_id") != task_id:
            raise MatrixScoringError("verifier result belongs to a different task")
        primary = _mapping(verifier_result.get("primary_metric"), "verifier primary_metric")
        try:
            metric_name_value = primary["name"]
            direction_value = primary["direction"]
            raw_value_input = primary["value"]
        except KeyError as error:
            raise MatrixScoringError("verifier primary metric is missing %s" % error)
        expected_metric = scale["raw_metric"]
        if metric_name_value != expected_metric["name"]:
            raise MatrixScoringError("verifier metric name differs from the frozen display scale")
        if direction_value != expected_metric["direction"]:
            raise MatrixScoringError("verifier metric direction differs from the frozen display scale")
        try:
            raw_value = decimal_value(raw_value_input, "verifier primary metric")
        except MatrixContractError as error:
            raise MatrixScoringError(str(error))
        raw_valid = verifier_result.get("valid")
        if not isinstance(raw_valid, bool):
            raise MatrixScoringError("verifier valid flag must be boolean")
        error = verifier_result.get("error")
        if raw_valid and error is not None:
            raise MatrixScoringError("valid verifier result cannot contain an error")
        if not raw_valid and (not isinstance(error, str) or not error):
            raise MatrixScoringError("invalid verifier result requires a non-empty error")
        try:
            verifier_digest = sha256_mapping(verifier_result)
        except (TypeError, ValueError) as digest_error:
            raise MatrixScoringError(
                "verifier result must be canonical JSON for audit hashing: %s" % digest_error
            )

        payload_digest = normalized_sha256(str(scale["task_payload_sha256"]))
        scale_digest = sha256_mapping(scale)
        mapping = scale["mapping"]
        decimal_places = int(mapping["rounding"]["decimal_places"])
        if not raw_valid:
            return DisplayScoreResult(
                str(scale["benchmark_release_id"]),
                task_id,
                payload_digest,
                "invalid",
                str(metric_name_value),
                raw_value,
                str(direction_value),
                False,
                verifier_digest,
                scale_digest,
                None,
                decimal_places,
                str(error),
            )

        calibration = scale["calibration"]
        lower = decimal_value(
            calibration["lower_anchor"]["raw_value"],
            "calibration.lower_anchor.raw_value",
            strings_only=True,
        )
        reference = decimal_value(
            calibration["reference_anchor"]["raw_value"],
            "calibration.reference_anchor.raw_value",
            strings_only=True,
        )
        multiplier = Decimal(1) if direction_value == "maximize" else Decimal(-1)
        precision = int(mapping["precision"])
        try:
            with localcontext() as context:
                context.prec = precision
                context.rounding = ROUNDING_MODES[str(mapping["rounding"]["mode"])]
                lower_utility = multiplier * lower
                reference_utility = multiplier * reference
                raw_utility = multiplier * raw_value
                score = Decimal("1000") * (
                    (raw_utility - lower_utility) / (reference_utility - lower_utility)
                )
                score = score.quantize(Decimal(1).scaleb(-decimal_places))
        except DecimalException as error_value:
            raise MatrixScoringError(
                "display score cannot be represented at the frozen precision: %s" % error_value
            )
        return DisplayScoreResult(
            str(scale["benchmark_release_id"]),
            task_id,
            payload_digest,
            "scored",
            str(metric_name_value),
            raw_value,
            str(direction_value),
            True,
            verifier_digest,
            scale_digest,
            score,
            decimal_places,
            None,
        )
