from decimal import (
    ROUND_DOWN,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    Decimal,
    DecimalException,
    localcontext,
)
from typing import Any, Mapping, Optional, Sequence

from ..common.hashing import normalized_sha256, sha256_mapping
from ..common.security import reject_secret_keys
from .errors import MatrixContractError, MatrixScoringError
from .models import CustomUtilityEvaluator, MatrixResult
from .transforms import (
    ANCHOR_ONLY_TYPES,
    BUILTIN_TYPES,
    DIRECTION_BY_TYPE,
    MATRIX_TYPES,
    builtin_utility,
    decimal_value,
    validate_builtin_parameters,
)

DEFAULT_INVALID_SCORE = "-100"
DEFAULT_PRECISION = 50
DEFAULT_ROUNDING = {"decimal_places": 6, "mode": "half_even"}


ROUNDING_MODES = {
    "half_even": ROUND_HALF_EVEN,
    "half_up": ROUND_HALF_UP,
    "down": ROUND_DOWN,
}


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MatrixContractError("%s must be an object" % field)
    return value


def _exact_keys(value: Mapping[str, Any], expected: Sequence[str], field: str) -> None:
    expected_set = set(expected)
    actual = set(value)
    if actual != expected_set:
        raise MatrixContractError(
            "%s fields mismatch; missing=%s unknown=%s"
            % (field, sorted(expected_set - actual), sorted(actual - expected_set))
        )


def validate_metric_definition(metric: Mapping[str, Any]) -> None:
    if not isinstance(metric, Mapping):
        raise MatrixContractError("metric must be an object")
    if "description" not in metric or "type" not in metric:
        raise MatrixContractError("metric requires description and type")
    if not isinstance(metric["description"], str) or not metric["description"].strip():
        raise MatrixContractError("metric.description must be a non-empty string")
    metric_type = metric["type"]
    if metric_type is not None and metric_type not in MATRIX_TYPES:
        raise MatrixContractError("unsupported metric.type: %s" % metric_type)
    if metric_type in (None, "raw_only"):
        _exact_keys(metric, ("description", "type"), "metric")
        return
    if metric_type in ANCHOR_ONLY_TYPES and "transform" not in metric:
        _exact_keys(metric, ("description", "type"), "metric")
        return
    _exact_keys(metric, ("description", "type", "transform"), "metric")
    transform = _mapping(metric.get("transform"), "metric.transform")
    _exact_keys(
        transform,
        (
            "raw_direction",
            "parameters",
            "anchors",
            "invalid_score",
            "precision",
            "rounding",
        ),
        "metric.transform",
    )
    direction = transform["raw_direction"]
    if direction not in ("maximize", "minimize"):
        raise MatrixContractError("metric.transform.raw_direction must be maximize or minimize")
    if metric_type in DIRECTION_BY_TYPE and DIRECTION_BY_TYPE[metric_type] != direction:
        raise MatrixContractError("metric.type and metric.transform.raw_direction disagree")
    parameters = _mapping(transform["parameters"], "metric.transform.parameters")
    reject_secret_keys(parameters, "metric.transform.parameters")
    if metric_type in BUILTIN_TYPES:
        validate_builtin_parameters(str(metric_type), parameters)
    anchors = _mapping(transform["anchors"], "metric.transform.anchors")
    _exact_keys(anchors, ("lower", "reference"), "metric.transform.anchors")
    for name in ("lower", "reference"):
        anchor = _mapping(anchors[name], "metric.transform.anchors.%s" % name)
        _exact_keys(anchor, ("raw_value", "matrix_score"), "metric.transform.anchors.%s" % name)
        decimal_value(
            anchor["raw_value"],
            "metric.transform.anchors.%s.raw_value" % name,
            strings_only=True,
        )
        decimal_value(
            anchor["matrix_score"],
            "metric.transform.anchors.%s.matrix_score" % name,
            strings_only=True,
        )
    lower_score = decimal_value(
        anchors["lower"]["matrix_score"], "lower matrix score", strings_only=True
    )
    reference_score = decimal_value(
        anchors["reference"]["matrix_score"], "reference matrix score", strings_only=True
    )
    if reference_score <= lower_score:
        raise MatrixContractError("reference Matrix score must exceed lower-anchor score")
    invalid_score = decimal_value(
        transform["invalid_score"], "metric.transform.invalid_score", strings_only=True
    )
    precision = transform["precision"]
    if (
        isinstance(precision, bool)
        or not isinstance(precision, int)
        or not 16 <= precision <= 1000
    ):
        raise MatrixContractError("metric.transform.precision must be an integer from 16 to 1000")
    rounding = _mapping(transform["rounding"], "metric.transform.rounding")
    _exact_keys(rounding, ("decimal_places", "mode"), "metric.transform.rounding")
    decimal_places = rounding["decimal_places"]
    if (
        isinstance(decimal_places, bool)
        or not isinstance(decimal_places, int)
        or not 0 <= decimal_places <= 100
    ):
        raise MatrixContractError("rounding.decimal_places must be an integer from 0 to 100")
    if rounding["mode"] not in ROUNDING_MODES:
        raise MatrixContractError("unsupported Matrix rounding mode")
    try:
        with localcontext() as context:
            context.prec = int(precision)
            context.rounding = ROUNDING_MODES[str(rounding["mode"])]
            quantum = Decimal(1).scaleb(-decimal_places)
            rounded_lower_score = lower_score.quantize(quantum)
            rounded_reference_score = reference_score.quantize(quantum)
            invalid_score.quantize(quantum)
    except DecimalException as error:
        raise MatrixContractError(
            "configured Matrix scores cannot be represented at the frozen precision: %s"
            % error
        )
    if rounded_reference_score <= rounded_lower_score:
        raise MatrixContractError(
            "reference Matrix score must remain above the lower score after frozen rounding"
        )
    if metric_type in BUILTIN_TYPES:
        lower_raw = decimal_value(
            anchors["lower"]["raw_value"], "lower raw", strings_only=True
        )
        reference_raw = decimal_value(
            anchors["reference"]["raw_value"], "reference raw", strings_only=True
        )
        try:
            with localcontext() as context:
                context.prec = int(precision)
                context.rounding = ROUNDING_MODES[str(rounding["mode"])]
                lower_utility = decimal_value(
                    builtin_utility(str(metric_type), lower_raw, parameters),
                    "lower-anchor utility",
                )
                reference_utility = decimal_value(
                    builtin_utility(str(metric_type), reference_raw, parameters),
                    "reference-anchor utility",
                )
        except DecimalException as error:
            raise MatrixContractError(
                "built-in Matrix anchors cannot be evaluated at the frozen precision: %s"
                % error
            )
        if reference_utility <= lower_utility:
            raise MatrixContractError(
                "reference anchor must have greater utility than the lower anchor"
            )


def transform_from_baseline(metric_type: str, baseline: Mapping[str, Any]) -> dict:
    """Build the default display transform for a type that only needs two anchors."""
    if metric_type not in ANCHOR_ONLY_TYPES:
        raise MatrixContractError("%s cannot derive transform from baseline alone" % metric_type)
    if not isinstance(baseline, Mapping) or "score" not in baseline or "bottom_line_score" not in baseline:
        raise MatrixContractError("baseline must provide score and bottom_line_score")
    direction = DIRECTION_BY_TYPE[metric_type]
    return {
        "raw_direction": direction,
        "parameters": {},
        "anchors": {
            "lower": {
                "raw_value": format(decimal_value(baseline["bottom_line_score"], "baseline.bottom_line_score"), "f"),
                "matrix_score": "0",
            },
            "reference": {
                "raw_value": format(decimal_value(baseline["score"], "baseline.score"), "f"),
                "matrix_score": "1000",
            },
        },
        "invalid_score": DEFAULT_INVALID_SCORE,
        "precision": DEFAULT_PRECISION,
        "rounding": dict(DEFAULT_ROUNDING),
    }


def resolve_metric_transform(
    metric: Mapping[str, Any], baseline: Optional[Mapping[str, Any]] = None
) -> Mapping[str, Any]:
    """Fill transform from baseline for linear/log-ratio types that omitted it."""
    if metric.get("type") not in ANCHOR_ONLY_TYPES or "transform" in metric:
        return metric
    resolved = dict(metric)
    resolved["transform"] = transform_from_baseline(str(metric["type"]), baseline or {})
    return resolved


class MatrixEngine:
    """Pure fixed-anchor Matrix scoring; persistence and ranking are callers' concerns."""

    def score(
        self,
        task_id: str,
        task_payload_sha256: str,
        metric: Mapping[str, Any],
        verifier_result: Mapping[str, Any],
        custom_evaluator: Optional[CustomUtilityEvaluator] = None,
        baseline: Optional[Mapping[str, Any]] = None,
    ) -> MatrixResult:
        metric = resolve_metric_transform(metric, baseline)
        validate_metric_definition(metric)
        if not isinstance(task_id, str) or not task_id:
            raise MatrixScoringError("task_id must be a non-empty string")
        payload_digest = normalized_sha256(task_payload_sha256)
        if not isinstance(verifier_result, Mapping):
            raise MatrixScoringError("verifier result must be an object")
        if verifier_result.get("task_id") != task_id:
            raise MatrixScoringError("verifier result belongs to a different task")
        primary = _mapping(verifier_result.get("primary_metric"), "verifier primary_metric")
        try:
            metric_name_value = primary["name"]
            direction_value = primary["direction"]
            raw_metric_value = primary["value"]
        except KeyError as error:
            raise MatrixScoringError("verifier primary metric is missing %s" % error)
        if not isinstance(metric_name_value, str) or not metric_name_value:
            raise MatrixScoringError("verifier primary metric name must be non-empty")
        metric_name = metric_name_value
        if direction_value not in ("maximize", "minimize"):
            raise MatrixScoringError("verifier primary metric has an invalid direction")
        direction = direction_value
        try:
            raw_value = decimal_value(raw_metric_value, "verifier primary metric")
        except MatrixContractError as error:
            raise MatrixScoringError(str(error))
        raw_valid_value = verifier_result.get("valid")
        if not isinstance(raw_valid_value, bool):
            raise MatrixScoringError("verifier valid flag must be boolean")
        raw_valid = raw_valid_value
        error = verifier_result.get("error")
        if raw_valid and error is not None:
            raise MatrixScoringError("valid verifier result cannot contain an error")
        if not raw_valid and (not isinstance(error, str) or not error):
            raise MatrixScoringError("invalid verifier result requires a non-empty error")
        metric_type = metric["type"]
        config_sha256 = sha256_mapping(metric)
        if metric_type is None:
            return MatrixResult(
                task_id,
                payload_digest,
                "unconfigured",
                None,
                metric_name,
                raw_value,
                direction,
                raw_valid,
                config_sha256,
                None,
                None,
                0,
                None if error is None else str(error),
            )
        if metric_type == "raw_only":
            return MatrixResult(
                task_id,
                payload_digest,
                "raw_only",
                "raw_only",
                metric_name,
                raw_value,
                direction,
                raw_valid,
                config_sha256,
                None,
                None,
                0,
                None if error is None else str(error),
            )
        transform = metric["transform"]
        expected_direction = str(transform["raw_direction"])
        if direction != expected_direction:
            raise MatrixScoringError(
                "verifier metric direction differs from frozen Matrix definition"
            )
        decimal_places = int(transform["rounding"]["decimal_places"])
        rounding_mode = ROUNDING_MODES[str(transform["rounding"]["mode"])]
        if not raw_valid:
            invalid_score = decimal_value(
                transform["invalid_score"], "metric.transform.invalid_score", strings_only=True
            )
            with localcontext() as context:
                context.prec = int(transform["precision"])
                context.rounding = rounding_mode
                invalid_score = invalid_score.quantize(Decimal(1).scaleb(-decimal_places))
            return MatrixResult(
                task_id,
                payload_digest,
                "invalid",
                str(metric_type),
                metric_name,
                raw_value,
                direction,
                False,
                config_sha256,
                None,
                invalid_score,
                decimal_places,
                None if error is None else str(error),
            )
        anchors = transform["anchors"]
        lower_raw = decimal_value(anchors["lower"]["raw_value"], "lower raw", strings_only=True)
        reference_raw = decimal_value(
            anchors["reference"]["raw_value"], "reference raw", strings_only=True
        )
        parameters = transform["parameters"]
        precision = int(transform["precision"])
        values = (lower_raw, reference_raw, raw_value)
        with localcontext() as context:
            context.prec = precision
            context.rounding = rounding_mode
            if metric_type == "custom":
                if custom_evaluator is None:
                    raise MatrixScoringError("custom Matrix metric requires an isolated evaluator")
                utilities = custom_evaluator.evaluate(values, parameters, precision)
            else:
                utilities = tuple(
                    builtin_utility(str(metric_type), value, parameters) for value in values
                )
            if len(utilities) != 3:
                raise MatrixScoringError("Matrix evaluator returned the wrong number of utilities")
            normalized_utilities = tuple(
                decimal_value(item, "Matrix utility") for item in utilities
            )
            lower_utility, reference_utility, utility = normalized_utilities
            utility_span = reference_utility - lower_utility
            if utility_span <= 0:
                raise MatrixContractError(
                    "reference anchor must have greater utility than the lower anchor"
                )
            lower_score = decimal_value(
                anchors["lower"]["matrix_score"], "lower score", strings_only=True
            )
            reference_score = decimal_value(
                anchors["reference"]["matrix_score"], "reference score", strings_only=True
            )
            try:
                score = lower_score + (reference_score - lower_score) * (
                    (utility - lower_utility) / utility_span
                )
                score = score.quantize(Decimal(1).scaleb(-decimal_places))
            except DecimalException as error:
                raise MatrixScoringError(
                    "Matrix score cannot be represented at the frozen precision: %s" % error
                )
        return MatrixResult(
            task_id,
            payload_digest,
            "scored",
            str(metric_type),
            metric_name,
            raw_value,
            direction,
            True,
            config_sha256,
            utility,
            score,
            decimal_places,
            None,
        )
