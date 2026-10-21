import math
import random
from dataclasses import dataclass, replace
from decimal import Decimal
from itertools import combinations
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..common.errors import ContractError
from ..common.hashing import normalized_sha256, sha256_mapping
from ..contracts.validation import (
    validate_cross_task_arena_input,
    validate_cross_task_arena_result,
)
from .errors import ArenaContractError, ArenaScoringError


@dataclass(frozen=True)
class _Match:
    left: str
    right: str
    outcome: float
    weight: float
    domain: str
    task_family: str
    task_id: str
    repetition_id: str

    def cluster(self, unit: str) -> Tuple[str, ...]:
        if unit == "domain":
            return (self.domain,)
        if unit == "task_family":
            return (self.domain, self.task_family)
        if unit == "task":
            return (self.task_id,)
        if unit == "task_repetition":
            return (self.task_id, self.repetition_id)
        raise ArenaContractError("unsupported bootstrap cluster unit: %s" % unit)


@dataclass(frozen=True)
class _Fit:
    strengths: Mapping[str, float]
    iterations: int


def _sigmoid(value: float) -> float:
    if value >= 0:
        exponential = math.exp(-value)
        return 1.0 / (1.0 + exponential)
    exponential = math.exp(value)
    return exponential / (1.0 + exponential)


def _log_sigmoid(value: float) -> float:
    if value >= 0:
        return -math.log1p(math.exp(-value))
    return value - math.log1p(math.exp(value))


def _format(value: float, decimal_places: int) -> str:
    if not math.isfinite(value):
        raise ArenaScoringError("Arena output contains a non-finite number")
    threshold = 0.5 * (10.0 ** (-decimal_places)) if decimal_places else 0.5
    if abs(value) < threshold:
        value = 0.0
    return format(value, ".%df" % decimal_places)


def _canonical_input(value: Mapping[str, Any]) -> Mapping[str, Any]:
    normalized = dict(value)
    normalized["competitors"] = sorted(
        (dict(item) for item in value["competitors"]), key=lambda item: str(item["id"])
    )
    normalized_tasks = []
    for task in value["tasks"]:
        selected = dict(task)
        selected["repetition_ids"] = sorted(str(item) for item in task["repetition_ids"])
        normalized_tasks.append(selected)
    normalized["tasks"] = sorted(normalized_tasks, key=lambda item: str(item["id"]))
    normalized["observations"] = sorted(
        (dict(item) for item in value["observations"]),
        key=lambda item: (
            str(item["competitor_id"]),
            str(item["task_id"]),
            str(item["repetition_id"]),
        ),
    )
    return normalized


def _solve_linear(matrix: Sequence[Sequence[float]], vector: Sequence[float]) -> List[float]:
    size = len(vector)
    augmented = [list(matrix[row]) + [float(vector[row])] for row in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) <= 1e-18:
            raise ArenaScoringError("Bradley-Terry Hessian is singular")
        if pivot != column:
            augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        for index in range(column, size + 1):
            augmented[column][index] /= divisor
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if factor == 0:
                continue
            for index in range(column, size + 1):
                augmented[row][index] -= factor * augmented[column][index]
    return [augmented[row][size] for row in range(size)]


def _connected(competitor_ids: Sequence[str], matches: Sequence[_Match]) -> bool:
    adjacency = {item: set() for item in competitor_ids}
    for match in matches:
        if match.weight <= 0:
            continue
        adjacency[match.left].add(match.right)
        adjacency[match.right].add(match.left)
    visited = set()
    frontier = [competitor_ids[0]]
    while frontier:
        selected = frontier.pop()
        if selected in visited:
            continue
        visited.add(selected)
        frontier.extend(sorted(adjacency[selected] - visited))
    return len(visited) == len(competitor_ids)


def _objective(
    strengths: Mapping[str, float],
    matches: Sequence[_Match],
    regularization: float,
    exposures: Mapping[str, float],
    reference_id: str,
) -> float:
    value = 0.0
    for match in matches:
        difference = strengths[match.left] - strengths[match.right]
        value += match.weight * (
            match.outcome * _log_sigmoid(difference)
            + (1.0 - match.outcome) * _log_sigmoid(-difference)
        )
    value -= 0.5 * regularization * sum(
        exposures[item] * strengths[item] * strengths[item]
        for item in strengths
        if item != reference_id
    )
    return value


def _fit_bradley_terry(
    competitor_ids: Sequence[str],
    reference_id: str,
    matches: Sequence[_Match],
    regularization: float,
    max_iterations: int,
    tolerance: float,
) -> _Fit:
    if not matches:
        raise ArenaContractError("Bradley-Terry requires at least one comparable match")
    if not _connected(competitor_ids, matches):
        raise ArenaContractError(
            "Bradley-Terry comparison graph is disconnected; all strengths are not comparable"
        )
    variables = [item for item in competitor_ids if item != reference_id]
    variable_index = {item: index for index, item in enumerate(variables)}
    strengths = {item: 0.0 for item in competitor_ids}
    exposures = {item: 0.0 for item in competitor_ids}
    for match in matches:
        exposures[match.left] += match.weight
        exposures[match.right] += match.weight
    if any(exposures[item] <= 0 for item in competitor_ids):
        raise ArenaContractError("every Arena competitor must have positive comparison weight")

    for iteration in range(1, max_iterations + 1):
        size = len(variables)
        gradient = [0.0 for _ in variables]
        information = [[0.0 for _ in variables] for _ in variables]
        for item, index in variable_index.items():
            # Exposure scaling keeps the fitted strengths invariant if every
            # comparison weight is multiplied by the same constant.
            penalty = regularization * exposures[item]
            gradient[index] -= penalty * strengths[item]
            information[index][index] += penalty
        for match in matches:
            difference = strengths[match.left] - strengths[match.right]
            probability = _sigmoid(difference)
            residual = match.weight * (match.outcome - probability)
            curvature = match.weight * probability * (1.0 - probability)
            left_index = variable_index.get(match.left)
            right_index = variable_index.get(match.right)
            if left_index is not None:
                gradient[left_index] += residual
                information[left_index][left_index] += curvature
            if right_index is not None:
                gradient[right_index] -= residual
                information[right_index][right_index] += curvature
            if left_index is not None and right_index is not None:
                information[left_index][right_index] -= curvature
                information[right_index][left_index] -= curvature
        step = _solve_linear(information, gradient)
        current_objective = _objective(
            strengths, matches, regularization, exposures, reference_id
        )
        step_scale = 1.0
        accepted = False
        candidate = dict(strengths)
        while step_scale >= 2.0 ** -30:
            for item, index in variable_index.items():
                candidate[item] = strengths[item] + step_scale * step[index]
            candidate[reference_id] = 0.0
            candidate_objective = _objective(
                candidate, matches, regularization, exposures, reference_id
            )
            if candidate_objective + 1e-14 >= current_objective:
                accepted = True
                break
            step_scale /= 2.0
        if not accepted:
            raise ArenaScoringError("Bradley-Terry Newton step could not improve the objective")
        strengths = dict(candidate)
        maximum_step = max(abs(step_scale * item) for item in step) if step else 0.0
        if maximum_step <= tolerance:
            strengths[reference_id] = 0.0
            return _Fit(strengths, iteration)
    raise ArenaScoringError(
        "Bradley-Terry did not converge within the frozen maximum iteration count"
    )


def _ranks(strengths: Mapping[str, float], rank_tolerance: float) -> Mapping[str, int]:
    ordered = sorted(strengths, key=lambda item: (-strengths[item], item))
    result = {}  # type: Dict[str, int]
    current_rank = 0
    representative = None  # type: Optional[float]
    for position, competitor_id in enumerate(ordered, start=1):
        strength = strengths[competitor_id]
        if representative is None or abs(strength - representative) > rank_tolerance:
            current_rank = position
            representative = strength
        result[competitor_id] = current_rank
    return result


def _percentile(sorted_values: Sequence[float], probability: float) -> float:
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = probability * (len(sorted_values) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return float(sorted_values[lower])
    fraction = position - lower
    return float(
        sorted_values[lower] * (1.0 - fraction) + sorted_values[upper] * fraction
    )


def _weighted_log_loss(strengths: Mapping[str, float], matches: Sequence[_Match]) -> float:
    total_weight = sum(item.weight for item in matches)
    if total_weight <= 0:
        raise ArenaContractError("cannot calculate log loss without comparison weight")
    loss = 0.0
    for match in matches:
        difference = strengths[match.left] - strengths[match.right]
        loss -= match.weight * (
            match.outcome * _log_sigmoid(difference)
            + (1.0 - match.outcome) * _log_sigmoid(-difference)
        )
    return loss / total_weight


def _pairwise_summary(
    competitor_ids: Sequence[str],
    strengths: Mapping[str, float],
    matches: Sequence[_Match],
    decimal_places: int,
) -> Tuple[List[Mapping[str, Any]], float]:
    groups = {}  # type: Dict[Tuple[str, str], List[_Match]]
    for match in matches:
        groups.setdefault((match.left, match.right), []).append(match)
    summaries = []
    weighted_error = 0.0
    total_weight = 0.0
    for left, right in combinations(competitor_ids, 2):
        selected = groups.get((left, right), [])
        if not selected:
            continue
        compared_weight = sum(item.weight for item in selected)
        observed = sum(item.weight * item.outcome for item in selected) / compared_weight
        predicted = _sigmoid(strengths[left] - strengths[right])
        weighted_error += compared_weight * abs(observed - predicted)
        total_weight += compared_weight
        summaries.append(
            {
                "left_competitor_id": left,
                "right_competitor_id": right,
                "compared_weight": _format(compared_weight, decimal_places),
                "observed_left_score": _format(observed, decimal_places),
                "model_left_win_probability": _format(predicted, decimal_places),
                "left_wins": sum(item.outcome == 1.0 for item in selected),
                "ties": sum(item.outcome == 0.5 for item in selected),
                "left_losses": sum(item.outcome == 0.0 for item in selected),
            }
        )
    if total_weight <= 0:
        raise ArenaContractError("Arena has no pairwise comparison weight")
    return summaries, weighted_error / total_weight


def _domain_ranking(
    strengths: Mapping[str, float],
    rank_tolerance: float,
    decimal_places: int,
) -> List[Mapping[str, Any]]:
    ranks = _ranks(strengths, rank_tolerance)
    return [
        {
            "rank": ranks[item],
            "competitor_id": item,
            "bt_strength": _format(strengths[item], decimal_places),
            "reference_win_probability_percent": _format(
                100.0 * _sigmoid(strengths[item]), decimal_places
            ),
        }
        for item in sorted(strengths, key=lambda selected: (-strengths[selected], selected))
    ]


class BradleyTerryArenaEngine:
    """Pure cross-task comparison over a complete, frozen result grid."""

    def analyze(self, arena_input: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            validate_cross_task_arena_input(arena_input)
        except ContractError as error:
            raise ArenaContractError(str(error))
        competitors = sorted(
            (dict(item) for item in arena_input["competitors"]),
            key=lambda item: str(item["id"]),
        )
        competitor_ids = [str(item["id"]) for item in competitors]
        reference_id = str(arena_input["reference_competitor_id"])
        model = arena_input["model"]
        regularization = float(Decimal(str(model["l2_regularization"])))
        max_iterations = int(model["max_iterations"])
        tolerance = float(Decimal(str(model["convergence_tolerance"])))
        rank_tolerance = float(Decimal(str(model["rank_tolerance"])))
        decimal_places = int(model["output_decimal_places"])

        matches, excluded_weight, status_weights = self._matches(
            arena_input, competitor_ids
        )
        fit = _fit_bradley_terry(
            competitor_ids,
            reference_id,
            matches,
            regularization,
            max_iterations,
            tolerance,
        )
        strengths = fit.strengths
        ranks = _ranks(strengths, rank_tolerance)
        intervals = self._bootstrap(
            arena_input,
            competitor_ids,
            reference_id,
            matches,
            regularization,
            max_iterations,
            tolerance,
        )

        ranking = []
        competitor_by_id = {str(item["id"]): item for item in competitors}
        for competitor_id in sorted(
            competitor_ids, key=lambda item: (-strengths[item], item)
        ):
            strength_interval = intervals[competitor_id]
            probability_interval = sorted(
                100.0 * _sigmoid(item) for item in strength_interval
            )
            weights = status_weights[competitor_id]
            ranking.append(
                {
                    "rank": ranks[competitor_id],
                    "competitor_id": competitor_id,
                    "name": str(competitor_by_id[competitor_id]["name"]),
                    "kind": str(competitor_by_id[competitor_id]["kind"]),
                    "bt_strength": _format(strengths[competitor_id], decimal_places),
                    "reference_win_probability_percent": _format(
                        100.0 * _sigmoid(strengths[competitor_id]), decimal_places
                    ),
                    "bt_strength_interval": {
                        "lower": _format(strength_interval[0], decimal_places),
                        "upper": _format(strength_interval[1], decimal_places),
                    },
                    "reference_win_probability_percent_interval": {
                        "lower": _format(probability_interval[0], decimal_places),
                        "upper": _format(probability_interval[1], decimal_places),
                    },
                    "invalid_observation_rate_percent": _format(
                        100.0 * weights["agent_invalid"], decimal_places
                    ),
                    "unavailable_observation_rate_percent": _format(
                        100.0
                        * (weights["infrastructure_failed"] + weights["scorer_failed"]),
                        decimal_places,
                    ),
                }
            )

        pairwise, pairwise_error = _pairwise_summary(
            competitor_ids, strengths, matches, decimal_places
        )
        domains, leave_one_out, maximum_rank_shift = self._sensitivity(
            arena_input,
            competitor_ids,
            reference_id,
            matches,
            strengths,
            regularization,
            max_iterations,
            tolerance,
            rank_tolerance,
            decimal_places,
        )
        tasks = arena_input["tasks"]
        domain_count = len({str(item["domain"]) for item in tasks})
        family_count = len(
            {(str(item["domain"]), str(item["task_family"])) for item in tasks}
        )
        gate = arena_input["representativeness_gate"]
        checks = [
            {
                "name": "complete_results",
                "passed": excluded_weight <= 1e-15,
                "observed": excluded_weight <= 1e-15,
                "threshold": True,
            },
            {
                "name": "minimum_domains",
                "passed": domain_count >= int(gate["minimum_domains"]),
                "observed": domain_count,
                "threshold": int(gate["minimum_domains"]),
            },
            {
                "name": "minimum_task_families",
                "passed": family_count >= int(gate["minimum_task_families"]),
                "observed": family_count,
                "threshold": int(gate["minimum_task_families"]),
            },
            {
                "name": "minimum_tasks",
                "passed": len(tasks) >= int(gate["minimum_tasks"]),
                "observed": len(tasks),
                "threshold": int(gate["minimum_tasks"]),
            },
            {
                "name": "maximum_pairwise_probability_error",
                "passed": pairwise_error
                <= float(Decimal(str(gate["maximum_pairwise_probability_error"]))),
                "observed": _format(pairwise_error, decimal_places),
                "threshold": str(gate["maximum_pairwise_probability_error"]),
            },
            {
                "name": "maximum_leave_one_domain_out_rank_shift",
                "passed": maximum_rank_shift is not None
                and maximum_rank_shift
                <= int(gate["maximum_leave_one_domain_out_rank_shift"]),
                "observed": maximum_rank_shift,
                "threshold": int(gate["maximum_leave_one_domain_out_rank_shift"]),
            },
        ]
        uncertainty = arena_input["uncertainty"]
        result = {
            "schema_version": "1.0",
            "snapshot_id": str(arena_input["snapshot_id"]),
            "benchmark_release_sha256": normalized_sha256(
                str(arena_input["benchmark_release_sha256"])
            ),
            "experiment_policy_sha256": normalized_sha256(
                str(arena_input["experiment_policy_sha256"])
            ),
            "input_sha256": sha256_mapping(_canonical_input(arena_input)),
            "status": "provisional_incomplete" if excluded_weight > 1e-15 else "ranked",
            "reference_competitor_id": reference_id,
            "weighting": dict(arena_input["weighting"]),
            "model": dict(model),
            "counts": {
                "competitors": len(competitor_ids),
                "domains": domain_count,
                "task_families": family_count,
                "tasks": len(tasks),
                "repetitions": sum(len(item["repetition_ids"]) for item in tasks),
                "matches": len(matches),
            },
            "ranking": ranking,
            "pairwise": pairwise,
            "domains": domains,
            "diagnostics": {
                "converged": True,
                "iterations": fit.iterations,
                "weighted_log_loss": _format(
                    _weighted_log_loss(strengths, matches), decimal_places
                ),
                "weighted_pairwise_probability_error": _format(
                    pairwise_error, decimal_places
                ),
                "leave_one_domain_out": leave_one_out,
            },
            "representativeness": {
                "status": "passed" if all(item["passed"] for item in checks) else "failed",
                "checks": checks,
            },
            "uncertainty": {
                "method": "cluster_bootstrap_percentile",
                "cluster_unit": str(uncertainty["cluster_unit"]),
                "confidence_level": _format(
                    float(Decimal(str(uncertainty["confidence_level"]))), decimal_places
                ),
                "requested_resamples": int(uncertainty["resamples"]),
                "successful_resamples": int(uncertainty["resamples"]),
                "seed": int(uncertainty["seed"]),
            },
            "excluded_pair_weight": _format(excluded_weight, decimal_places),
        }
        validate_cross_task_arena_result(result)
        return result

    @staticmethod
    def _matches(
        arena_input: Mapping[str, Any],
        competitor_ids: Sequence[str],
    ) -> Tuple[List[_Match], float, Mapping[str, Mapping[str, float]]]:
        tasks = sorted(arena_input["tasks"], key=lambda item: str(item["id"]))
        observations = {
            (
                str(item["competitor_id"]),
                str(item["task_id"]),
                str(item["repetition_id"]),
            ): item
            for item in arena_input["observations"]
        }
        domains = sorted({str(item["domain"]) for item in tasks})
        families_by_domain = {
            domain: sorted(
                {str(item["task_family"]) for item in tasks if item["domain"] == domain}
            )
            for domain in domains
        }
        tasks_by_family = {}  # type: Dict[Tuple[str, str], List[Mapping[str, Any]]]
        for task in tasks:
            tasks_by_family.setdefault(
                (str(task["domain"]), str(task["task_family"])), []
            ).append(task)
        pair_count = len(competitor_ids) * (len(competitor_ids) - 1) // 2
        matches = []  # type: List[_Match]
        excluded_weight = 0.0
        status_weights = {
            competitor_id: {
                "valid": 0.0,
                "agent_invalid": 0.0,
                "infrastructure_failed": 0.0,
                "scorer_failed": 0.0,
            }
            for competitor_id in competitor_ids
        }
        for task in tasks:
            domain = str(task["domain"])
            family = str(task["task_family"])
            repetitions = sorted(str(item) for item in task["repetition_ids"])
            task_count = len(tasks_by_family[(domain, family)])
            observation_weight = (
                1.0
                / len(domains)
                / len(families_by_domain[domain])
                / task_count
                / len(repetitions)
            )
            # Dividing again by the planned pair count keeps each task's total
            # influence fixed when the snapshot contains more competitors.
            match_weight = observation_weight / pair_count
            tolerance = Decimal(str(task["metric"]["tie_tolerance"]))
            direction = str(task["metric"]["direction"])
            for repetition_id in repetitions:
                for competitor_id in competitor_ids:
                    status = str(
                        observations[(competitor_id, str(task["id"]), repetition_id)][
                            "status"
                        ]
                    )
                    status_weights[competitor_id][status] += observation_weight
                for left, right in combinations(competitor_ids, 2):
                    left_observation = observations[(left, str(task["id"]), repetition_id)]
                    right_observation = observations[(right, str(task["id"]), repetition_id)]
                    unavailable = {"infrastructure_failed", "scorer_failed"}
                    if (
                        left_observation["status"] in unavailable
                        or right_observation["status"] in unavailable
                    ):
                        excluded_weight += match_weight
                        continue
                    if left_observation["status"] == "agent_invalid":
                        outcome = (
                            0.5
                            if right_observation["status"] == "agent_invalid"
                            else 0.0
                        )
                    elif right_observation["status"] == "agent_invalid":
                        outcome = 1.0
                    else:
                        left_value = Decimal(str(left_observation["raw_value"]))
                        right_value = Decimal(str(right_observation["raw_value"]))
                        advantage = (
                            left_value - right_value
                            if direction == "maximize"
                            else right_value - left_value
                        )
                        if advantage > tolerance:
                            outcome = 1.0
                        elif advantage < -tolerance:
                            outcome = 0.0
                        else:
                            outcome = 0.5
                    matches.append(
                        _Match(
                            left,
                            right,
                            outcome,
                            match_weight,
                            domain,
                            family,
                            str(task["id"]),
                            repetition_id,
                        )
                    )
        return matches, excluded_weight, status_weights

    @staticmethod
    def _bootstrap(
        arena_input: Mapping[str, Any],
        competitor_ids: Sequence[str],
        reference_id: str,
        matches: Sequence[_Match],
        regularization: float,
        max_iterations: int,
        tolerance: float,
    ) -> Mapping[str, Tuple[float, float]]:
        uncertainty = arena_input["uncertainty"]
        unit = str(uncertainty["cluster_unit"])
        clusters = sorted({item.cluster(unit) for item in matches})
        if not clusters:
            raise ArenaContractError("cluster bootstrap requires at least one comparison cluster")
        generator = random.Random(int(uncertainty["seed"]))
        samples = {item: [] for item in competitor_ids}  # type: Dict[str, List[float]]
        for _ in range(int(uncertainty["resamples"])):
            multiplicity = {item: 0 for item in clusters}
            for _ in clusters:
                selected = clusters[generator.randrange(len(clusters))]
                multiplicity[selected] += 1
            selected_matches = [
                replace(match, weight=match.weight * multiplicity[match.cluster(unit)])
                for match in matches
                if multiplicity[match.cluster(unit)] > 0
            ]
            fit = _fit_bradley_terry(
                competitor_ids,
                reference_id,
                selected_matches,
                regularization,
                max_iterations,
                tolerance,
            )
            for competitor_id in competitor_ids:
                samples[competitor_id].append(fit.strengths[competitor_id])
        level = float(Decimal(str(uncertainty["confidence_level"])))
        tail = (1.0 - level) / 2.0
        return {
            competitor_id: (
                _percentile(sorted(values), tail),
                _percentile(sorted(values), 1.0 - tail),
            )
            for competitor_id, values in samples.items()
        }

    @staticmethod
    def _sensitivity(
        arena_input: Mapping[str, Any],
        competitor_ids: Sequence[str],
        reference_id: str,
        matches: Sequence[_Match],
        overall_strengths: Mapping[str, float],
        regularization: float,
        max_iterations: int,
        tolerance: float,
        rank_tolerance: float,
        decimal_places: int,
    ) -> Tuple[List[Mapping[str, Any]], List[Mapping[str, Any]], Optional[int]]:
        domains = sorted({str(item["domain"]) for item in arena_input["tasks"]})
        domain_results = []
        for domain in domains:
            selected = [item for item in matches if item.domain == domain]
            try:
                fit = _fit_bradley_terry(
                    competitor_ids,
                    reference_id,
                    selected,
                    regularization,
                    max_iterations,
                    tolerance,
                )
            except (ArenaContractError, ArenaScoringError):
                domain_results.append(
                    {
                        "domain": domain,
                        "status": "insufficient_data",
                        "weighted_log_loss": None,
                        "ranking": [],
                    }
                )
                continue
            domain_results.append(
                {
                    "domain": domain,
                    "status": "ranked",
                    "weighted_log_loss": _format(
                        _weighted_log_loss(fit.strengths, selected), decimal_places
                    ),
                    "ranking": _domain_ranking(
                        fit.strengths, rank_tolerance, decimal_places
                    ),
                }
            )

        overall_ranks = _ranks(overall_strengths, rank_tolerance)
        leave_one_out = []
        shifts = []  # type: List[int]
        for omitted in domains:
            if len(domains) < 2:
                leave_one_out.append(
                    {
                        "omitted_domain": omitted,
                        "status": "not_assessable",
                        "maximum_rank_shift": None,
                        "ranking": [],
                    }
                )
                continue
            selected = [item for item in matches if item.domain != omitted]
            try:
                fit = _fit_bradley_terry(
                    competitor_ids,
                    reference_id,
                    selected,
                    regularization,
                    max_iterations,
                    tolerance,
                )
            except (ArenaContractError, ArenaScoringError):
                leave_one_out.append(
                    {
                        "omitted_domain": omitted,
                        "status": "not_assessable",
                        "maximum_rank_shift": None,
                        "ranking": [],
                    }
                )
                continue
            selected_ranks = _ranks(fit.strengths, rank_tolerance)
            maximum_shift = max(
                abs(selected_ranks[item] - overall_ranks[item]) for item in competitor_ids
            )
            shifts.append(maximum_shift)
            leave_one_out.append(
                {
                    "omitted_domain": omitted,
                    "status": "ranked",
                    "maximum_rank_shift": maximum_shift,
                    "ranking": _domain_ranking(
                        fit.strengths, rank_tolerance, decimal_places
                    ),
                }
            )
        maximum_rank_shift = max(shifts) if len(shifts) == len(domains) and shifts else None
        return domain_results, leave_one_out, maximum_rank_shift
