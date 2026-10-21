from decimal import Decimal
from typing import Any, Mapping, Sequence, Tuple

from .errors import MatrixContractError, MatrixDomainError


BUILTIN_TYPES = frozenset(
    {
        "linear_maximize",
        "linear_minimize",
        "log_ratio_maximize",
        "log_ratio_minimize",
        "upper_residual_log",
        "lower_residual_log",
        "power_maximize",
        "power_minimize",
        "monotone_piecewise_maximize",
        "monotone_piecewise_minimize",
    }
)

MATRIX_TYPES = frozenset(set(BUILTIN_TYPES) | {"custom", "raw_only"})

# These types need no extra scientific constants. transform may be omitted when
# baseline.score and baseline.bottom_line_score supply the two display anchors.
ANCHOR_ONLY_TYPES = frozenset(
    {
        "linear_maximize",
        "linear_minimize",
        "log_ratio_maximize",
        "log_ratio_minimize",
    }
)

DIRECTION_BY_TYPE = {
    "linear_maximize": "maximize",
    "linear_minimize": "minimize",
    "log_ratio_maximize": "maximize",
    "log_ratio_minimize": "minimize",
    "upper_residual_log": "maximize",
    "lower_residual_log": "minimize",
    "power_maximize": "maximize",
    "power_minimize": "minimize",
    "monotone_piecewise_maximize": "maximize",
    "monotone_piecewise_minimize": "minimize",
}


def decimal_value(value: Any, field: str, strings_only: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise MatrixContractError("%s must be a finite decimal" % field)
    if strings_only and not isinstance(value, str):
        raise MatrixContractError("%s must be a decimal string" % field)
    if not isinstance(value, (str, int, float, Decimal)):
        raise MatrixContractError("%s must be a finite decimal" % field)
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as error:
        raise MatrixContractError("%s is not a decimal: %s" % (field, error))
    if not result.is_finite():
        raise MatrixContractError("%s must be finite" % field)
    return result


def _exact_parameters(
    parameters: Mapping[str, Any], expected: Sequence[str], metric_type: str
) -> None:
    expected_set = set(expected)
    actual = set(parameters)
    if actual != expected_set:
        raise MatrixContractError(
            "%s parameters mismatch; missing=%s unknown=%s"
            % (metric_type, sorted(expected_set - actual), sorted(actual - expected_set))
        )


def validate_builtin_parameters(metric_type: str, parameters: Mapping[str, Any]) -> None:
    if metric_type in (
        "linear_maximize",
        "linear_minimize",
        "log_ratio_maximize",
        "log_ratio_minimize",
    ):
        _exact_parameters(parameters, (), metric_type)
        return
    if metric_type in ("upper_residual_log", "lower_residual_log"):
        _exact_parameters(parameters, ("bound", "epsilon"), metric_type)
        decimal_value(parameters["bound"], "metric.transform.parameters.bound", strings_only=True)
        epsilon = decimal_value(
            parameters["epsilon"], "metric.transform.parameters.epsilon", strings_only=True
        )
        if epsilon <= 0:
            raise MatrixContractError("residual-log epsilon must be positive")
        return
    if metric_type in ("power_maximize", "power_minimize"):
        _exact_parameters(parameters, ("origin", "exponent"), metric_type)
        decimal_value(
            parameters["origin"],
            "metric.transform.parameters.origin",
            strings_only=True,
        )
        exponent = decimal_value(
            parameters["exponent"], "metric.transform.parameters.exponent", strings_only=True
        )
        if exponent <= 0:
            raise MatrixContractError("power exponent must be positive")
        return
    if metric_type in ("monotone_piecewise_maximize", "monotone_piecewise_minimize"):
        _exact_parameters(parameters, ("knots",), metric_type)
        knots = parameters["knots"]
        if not isinstance(knots, list) or len(knots) < 2:
            raise MatrixContractError("piecewise transform requires at least two knots")
        parsed = _piecewise_knots(knots)
        raw_values = [item[0] for item in parsed]
        utility_values = [item[1] for item in parsed]
        if any(right <= left for left, right in zip(raw_values, raw_values[1:])):
            raise MatrixContractError("piecewise raw knot values must strictly increase")
        if metric_type.endswith("maximize"):
            monotone = all(right > left for left, right in zip(utility_values, utility_values[1:]))
        else:
            monotone = all(right < left for left, right in zip(utility_values, utility_values[1:]))
        if not monotone:
            raise MatrixContractError(
                "piecewise utility knots must strictly follow the declared raw direction"
            )
        return
    raise MatrixContractError("unsupported built-in Matrix type: %s" % metric_type)


def _piecewise_knots(knots: Sequence[Any]) -> Tuple[Tuple[Decimal, Decimal], ...]:
    result = []
    for index, item in enumerate(knots):
        if not isinstance(item, Mapping) or set(item) != {"raw_value", "utility_value"}:
            raise MatrixContractError(
                "piecewise knot %d must contain exactly raw_value and utility_value" % index
            )
        result.append(
            (
                decimal_value(
                    item["raw_value"],
                    "metric.transform.parameters.knots[%d].raw_value" % index,
                    strings_only=True,
                ),
                decimal_value(
                    item["utility_value"],
                    "metric.transform.parameters.knots[%d].utility_value" % index,
                    strings_only=True,
                ),
            )
        )
    return tuple(result)


def builtin_utility(
    metric_type: str, raw_value: Decimal, parameters: Mapping[str, Any]
) -> Decimal:
    if metric_type == "linear_maximize":
        return raw_value
    if metric_type == "linear_minimize":
        return -raw_value
    if metric_type in ("log_ratio_maximize", "log_ratio_minimize"):
        if raw_value <= 0:
            raise MatrixDomainError("log-ratio raw value must be positive")
        utility = raw_value.ln()
        return utility if metric_type.endswith("maximize") else -utility
    if metric_type in ("upper_residual_log", "lower_residual_log"):
        bound = decimal_value(parameters["bound"], "bound", strings_only=True)
        epsilon = decimal_value(parameters["epsilon"], "epsilon", strings_only=True)
        residual = bound - raw_value if metric_type == "upper_residual_log" else raw_value - bound
        if residual < 0:
            raise MatrixDomainError("raw value crosses the declared scientific bound")
        return -(max(residual, epsilon).ln())
    if metric_type in ("power_maximize", "power_minimize"):
        origin = decimal_value(parameters["origin"], "origin", strings_only=True)
        exponent = decimal_value(parameters["exponent"], "exponent", strings_only=True)
        distance = raw_value - origin if metric_type.endswith("maximize") else origin - raw_value
        if distance < 0:
            raise MatrixDomainError("raw value crosses the declared power-transform origin")
        return distance ** exponent
    if metric_type in ("monotone_piecewise_maximize", "monotone_piecewise_minimize"):
        knots = _piecewise_knots(parameters["knots"])
        if raw_value < knots[0][0] or raw_value > knots[-1][0]:
            raise MatrixDomainError("raw value is outside the frozen piecewise domain")
        for raw, utility in knots:
            if raw_value == raw:
                return utility
        for (left_raw, left_utility), (right_raw, right_utility) in zip(knots, knots[1:]):
            if left_raw < raw_value < right_raw:
                fraction = (raw_value - left_raw) / (right_raw - left_raw)
                return left_utility + fraction * (right_utility - left_utility)
        raise MatrixDomainError("piecewise transform could not locate the raw value")
    raise MatrixContractError("unsupported built-in Matrix type: %s" % metric_type)
