import json
import math
from decimal import Decimal, InvalidOperation
from importlib import resources
from pathlib import Path
from typing import Any, Dict, Mapping

import jsonschema
import yaml
from referencing import Registry
from referencing.jsonschema import DRAFT7

from ..common.errors import ContractError
from ..common.hashing import normalized_sha256
from ..common.security import reject_secret_keys


def schema_path(name: str) -> Path:
    try:
        return Path(resources.files("science_innovation_exam.contracts.schemas").joinpath(name))
    except AttributeError:
        with resources.path("science_innovation_exam.contracts.schemas", name) as path:
            return Path(path)


def load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError("cannot read JSON %s: %s" % (path, error))


def load_yaml(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as error:
        raise ContractError("cannot read YAML %s: %s" % (path, error))


def _format_error(error: jsonschema.ValidationError) -> str:
    location = ".".join(str(item) for item in error.absolute_path) or "root"
    return "%s: %s" % (location, error.message)


def _schema_registry(root: Path) -> Registry:
    resources_by_uri = []
    for path in sorted(root.glob("*.json")):
        contents = load_json(path)
        resources_by_uri.append((path.as_uri(), DRAFT7.create_resource(contents)))
    return Registry().with_resources(resources_by_uri)


def validate_schema(value: Any, schema_name: str) -> None:
    selected_path = schema_path(schema_name)
    schema = dict(load_json(selected_path))
    # The packaged schemas deliberately have no deployment-specific $id. Attach
    # the current file URI to the in-memory root so relative sibling references
    # resolve only against the preloaded package registry, never the network.
    schema.setdefault("$id", selected_path.as_uri())
    validator = jsonschema.Draft7Validator(schema, registry=_schema_registry(selected_path.parent))
    errors = sorted(validator.iter_errors(value), key=lambda item: list(item.absolute_path))
    if errors:
        raise ContractError("%s contract errors:\n- %s" % (schema_name, "\n- ".join(_format_error(error) for error in errors)))


def validate_matrix_result(value: Mapping[str, Any]) -> None:
    validate_schema(value, "matrix_result.schema.json")
    normalized_sha256(str(value["task_payload_sha256"]))
    normalized_sha256(str(value["matrix_config_sha256"]))
    for field in ("raw_metric.value", "utility_value", "matrix_score"):
        if field == "raw_metric.value":
            selected = value["raw_metric"]["value"]
        else:
            selected = value[field]
        if selected is None:
            continue
        try:
            number = Decimal(str(selected))
        except InvalidOperation:
            raise ContractError("%s must be a decimal string" % field)
        if not number.is_finite():
            raise ContractError("%s must be finite" % field)
    status = value["status"]
    utility = value["utility_value"]
    score = value["matrix_score"]
    error = value["error"]
    if status == "scored":
        if value["metric_type"] in (None, "raw_only"):
            raise ContractError("scored Matrix result requires a comparable metric type")
        if (
            not value["raw_metric"]["valid"]
            or utility is None
            or score is None
            or error is not None
        ):
            raise ContractError(
                "scored Matrix result requires valid raw metric, utility, and score"
            )
    elif status == "invalid":
        if value["metric_type"] in (None, "raw_only"):
            raise ContractError("invalid Matrix penalty requires a comparable metric type")
        if value["raw_metric"]["valid"] or utility is not None or score is None or not error:
            raise ContractError(
                "invalid Matrix result requires invalid raw metric, penalty, and error"
            )
    else:
        expected_type = "raw_only" if status == "raw_only" else None
        if value["metric_type"] != expected_type:
            raise ContractError("Matrix status and metric_type disagree")
        if utility is not None or score is not None:
            raise ContractError(
                "raw-only/unconfigured Matrix result cannot contain utility or score"
            )
        if bool(error) == bool(value["raw_metric"]["valid"]):
            raise ContractError("raw-only/unconfigured raw validity and error disagree")


def _finite_decimal(value: Any, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ContractError("%s must be a decimal string" % field)
    if not number.is_finite():
        raise ContractError("%s must be finite" % field)
    return number


def validate_display_scale(value: Mapping[str, Any]) -> None:
    validate_schema(value, "display_scale.schema.json")
    reject_secret_keys(value, "display_scale")
    normalized_sha256(str(value["task_payload_sha256"]))
    calibration = value["calibration"]
    for field in (
        "artifact_sha256",
        "selection_partition_sha256",
        "confirmation_partition_sha256",
    ):
        selected = calibration[field]
        if selected is not None:
            normalized_sha256(str(selected))
    for name in ("lower_anchor", "reference_anchor"):
        normalized_sha256(str(calibration[name]["evidence_sha256"]))
        _finite_decimal(calibration[name]["raw_value"], "calibration.%s.raw_value" % name)
    direction = str(value["raw_metric"]["direction"])
    lower = _finite_decimal(
        calibration["lower_anchor"]["raw_value"], "calibration.lower_anchor.raw_value"
    )
    reference = _finite_decimal(
        calibration["reference_anchor"]["raw_value"],
        "calibration.reference_anchor.raw_value",
    )
    directional_span = reference - lower if direction == "maximize" else lower - reference
    if directional_span <= 0:
        raise ContractError(
            "display reference anchor must be strictly better than the calibration-worst anchor"
        )


def validate_display_score_result(value: Mapping[str, Any]) -> None:
    validate_schema(value, "display_score_result.schema.json")
    normalized_sha256(str(value["task_payload_sha256"]))
    normalized_sha256(str(value["verifier_result_sha256"]))
    normalized_sha256(str(value["display_scale_sha256"]))
    _finite_decimal(value["raw_metric"]["value"], "raw_metric.value")
    if value["display_score"] is not None:
        _finite_decimal(value["display_score"], "display_score")
    if value["status"] == "scored":
        if not value["raw_metric"]["valid"] or value["display_score"] is None:
            raise ContractError("scored display result requires a valid raw metric and score")
        if value["error"] is not None:
            raise ContractError("scored display result cannot contain an error")
    else:
        if value["raw_metric"]["valid"] or value["display_score"] is not None:
            raise ContractError("invalid display result cannot contain a display score")
        if not value["error"]:
            raise ContractError("invalid display result requires an error")


def validate_cross_task_arena_input(value: Mapping[str, Any]) -> None:
    validate_schema(value, "cross_task_arena_input.schema.json")
    reject_secret_keys(value, "cross_task_arena_input")
    normalized_sha256(str(value["benchmark_release_sha256"]))
    normalized_sha256(str(value["experiment_policy_sha256"]))

    competitors = value["competitors"]
    competitor_ids = [str(item["id"]) for item in competitors]
    for competitor in competitors:
        normalized_sha256(str(competitor["configuration_sha256"]))
    if len(competitor_ids) != len(set(competitor_ids)):
        raise ContractError("cross-task Arena competitors contain duplicate ids")
    reference_id = str(value["reference_competitor_id"])
    references = [
        str(item["id"]) for item in competitors if item["kind"] == "reference_portfolio"
    ]
    if references != [reference_id]:
        raise ContractError(
            "cross-task Arena requires exactly one declared reference portfolio"
        )

    tasks = value["tasks"]
    task_ids = [str(item["id"]) for item in tasks]
    if len(task_ids) != len(set(task_ids)):
        raise ContractError("cross-task Arena tasks contain duplicate ids")
    expected = set()
    for task in tasks:
        normalized_sha256(str(task["payload_sha256"]))
        normalized_sha256(str(task["reference"]["artifact_sha256"]))
        tolerance = _finite_decimal(
            task["metric"]["tie_tolerance"],
            "tasks[%s].metric.tie_tolerance" % task["id"],
        )
        if tolerance < 0:
            raise ContractError("task tie tolerance cannot be negative")
        repetitions = [str(item) for item in task["repetition_ids"]]
        if len(repetitions) != len(set(repetitions)):
            raise ContractError("task repetition_ids contain duplicates: %s" % task["id"])
        for competitor_id in competitor_ids:
            for repetition_id in repetitions:
                expected.add((competitor_id, str(task["id"]), repetition_id))

    actual = set()
    unavailable = []
    for observation in value["observations"]:
        normalized_sha256(str(observation["result_sha256"]))
        identity = (
            str(observation["competitor_id"]),
            str(observation["task_id"]),
            str(observation["repetition_id"]),
        )
        if identity in actual:
            raise ContractError("cross-task Arena observations contain duplicate identities")
        actual.add(identity)
        if identity not in expected:
            raise ContractError("cross-task Arena observation is outside the frozen grid: %s" % (identity,))
        status = str(observation["status"])
        raw_value = observation["raw_value"]
        if status == "valid":
            if raw_value is None:
                raise ContractError("valid Arena observation requires raw_value")
            _finite_decimal(raw_value, "observations.raw_value")
        elif raw_value is not None:
            raise ContractError("non-valid Arena observation must set raw_value to null")
        if identity[0] == reference_id and status != "valid":
            raise ContractError("reference portfolio must have a valid result in every grid cell")
        if status in ("infrastructure_failed", "scorer_failed"):
            unavailable.append(identity)
    if actual != expected:
        missing = sorted(expected - actual)
        raise ContractError(
            "cross-task Arena observations do not fill the frozen grid; missing=%s" % missing[:10]
        )
    if value["require_complete_results"] and unavailable:
        raise ContractError(
            "official cross-task Arena requires infrastructure/scorer failures to be rerun"
        )

    model = value["model"]
    regularization = _finite_decimal(
        model["l2_regularization"], "model.l2_regularization"
    )
    regularization_float = float(regularization)
    if regularization <= 0 or regularization_float <= 0 or not math.isfinite(regularization_float):
        raise ContractError(
            "model.l2_regularization must be positive and representable by the fitter"
        )
    convergence = _finite_decimal(
        model["convergence_tolerance"], "model.convergence_tolerance"
    )
    convergence_float = float(convergence)
    if convergence <= 0 or convergence_float <= 0 or not math.isfinite(convergence_float):
        raise ContractError(
            "model.convergence_tolerance must be positive and representable by the fitter"
        )
    rank_tolerance = _finite_decimal(model["rank_tolerance"], "model.rank_tolerance")
    if rank_tolerance < 0 or not math.isfinite(float(rank_tolerance)):
        raise ContractError(
            "model.rank_tolerance must be non-negative and representable by the fitter"
        )
    level = _finite_decimal(value["uncertainty"]["confidence_level"], "uncertainty.confidence_level")
    if not Decimal("0.5") < level < Decimal(1):
        raise ContractError("uncertainty.confidence_level must be between 0.5 and 1")
    maximum_error = _finite_decimal(
        value["representativeness_gate"]["maximum_pairwise_probability_error"],
        "representativeness_gate.maximum_pairwise_probability_error",
    )
    if not Decimal(0) <= maximum_error <= Decimal(1):
        raise ContractError("maximum_pairwise_probability_error must be between 0 and 1")


def validate_cross_task_arena_result(value: Mapping[str, Any]) -> None:
    validate_schema(value, "cross_task_arena_result.schema.json")
    normalized_sha256(str(value["benchmark_release_sha256"]))
    normalized_sha256(str(value["experiment_policy_sha256"]))
    normalized_sha256(str(value["input_sha256"]))
    decimal_fields = [
        value["excluded_pair_weight"],
        value["diagnostics"]["weighted_log_loss"],
        value["diagnostics"]["weighted_pairwise_probability_error"],
    ]
    for entry in value["ranking"]:
        decimal_fields.extend(
            [
                entry["bt_strength"],
                entry["reference_win_probability_percent"],
                entry["bt_strength_interval"]["lower"],
                entry["bt_strength_interval"]["upper"],
                entry["reference_win_probability_percent_interval"]["lower"],
                entry["reference_win_probability_percent_interval"]["upper"],
                entry["invalid_observation_rate_percent"],
                entry["unavailable_observation_rate_percent"],
            ]
        )
    for index, selected in enumerate(decimal_fields):
        _finite_decimal(selected, "cross_task_arena_result.decimal[%d]" % index)
    reference = [
        item for item in value["ranking"]
        if item["competitor_id"] == value["reference_competitor_id"]
    ]
    if len(reference) != 1:
        raise ContractError("Arena result must contain its reference portfolio exactly once")
    if _finite_decimal(reference[0]["bt_strength"], "reference.bt_strength") != 0:
        raise ContractError("reference portfolio Bradley-Terry strength must be zero")
    if _finite_decimal(
        reference[0]["reference_win_probability_percent"],
        "reference.reference_win_probability_percent",
    ) != 50:
        raise ContractError("reference portfolio win probability must be 50 percent")
    if value["uncertainty"]["successful_resamples"] != value["uncertainty"]["requested_resamples"]:
        raise ContractError("Arena result must complete every frozen bootstrap resample")
