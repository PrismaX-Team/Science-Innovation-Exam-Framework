import copy
import unittest
from decimal import Decimal

from science_innovation_exam.common.errors import ContractError
from science_innovation_exam.contracts.validation import (
    validate_cross_task_arena_result,
    validate_display_score_result,
)
from science_innovation_exam.matrix import (
    ArenaContractError,
    BradleyTerryArenaEngine,
    DisplayScaleEngine,
    MatrixContractError,
    validate_display_scale_definition,
)


DIGEST = "a" * 64


def _display_scale(direction="maximize", lower="0", reference="10", source="calibration_best"):
    return {
        "schema_version": "1.0",
        "benchmark_release_id": "release-2026-09",
        "task_id": "display_fixture",
        "task_payload_sha256": DIGEST,
        "raw_metric": {"name": "objective", "direction": direction},
        "calibration": {
            "aggregation": "mean",
            "artifact_sha256": "b" * 64,
            "selection_partition_sha256": "c" * 64,
            "confirmation_partition_sha256": "d" * 64,
            "lower_anchor": {
                "source": "calibration_worst",
                "competitor_id": "first-batch-worst",
                "raw_value": lower,
                "evidence_sha256": "e" * 64,
            },
            "reference_anchor": {
                "source": source,
                "competitor_id": "reference",
                "raw_value": reference,
                "evidence_sha256": "f" * 64,
            },
        },
        "mapping": {
            "lower_score": "0",
            "reference_score": "1000",
            "precision": 50,
            "rounding": {"decimal_places": 6, "mode": "half_even"},
        },
    }


def _verifier(value, direction="maximize", valid=True, error=None):
    return {
        "task_id": "display_fixture",
        "valid": valid,
        "primary_metric": {"name": "objective", "value": value, "direction": direction},
        "error": error,
    }


def _base_arena_input():
    competitors = [
        {"id": "agent_strong", "name": "Strong", "kind": "agent", "configuration_sha256": "a" * 64},
        {"id": "agent_weak", "name": "Weak", "kind": "agent", "configuration_sha256": "b" * 64},
        {"id": "reference", "name": "Reference portfolio", "kind": "reference_portfolio", "configuration_sha256": "c" * 64},
    ]
    tasks = [
        {
            "id": "bio_task",
            "payload_sha256": "1" * 64,
            "domain": "biology",
            "task_family": "sequence_design",
            "metric": {"name": "fitness", "direction": "maximize", "tie_tolerance": "0"},
            "reference": {"source": "calibration_best", "artifact_sha256": "6" * 64},
            "repetition_ids": ["seed-1", "seed-2"],
        },
        {
            "id": "chem_task",
            "payload_sha256": "2" * 64,
            "domain": "chemistry",
            "task_family": "molecule_design",
            "metric": {"name": "error", "direction": "minimize", "tie_tolerance": "0"},
            "reference": {"source": "reproducible_baseline", "artifact_sha256": "7" * 64},
            "repetition_ids": ["seed-1", "seed-2"],
        },
    ]
    values = {
        "bio_task": {
            "agent_strong": ["10", "9"],
            "reference": ["5", "5"],
            "agent_weak": ["1", "2"],
        },
        "chem_task": {
            "agent_strong": ["1", "2"],
            "reference": ["5", "5"],
            "agent_weak": ["10", "9"],
        },
    }
    observations = []
    for task in tasks:
        for competitor in competitors:
            for index, repetition_id in enumerate(task["repetition_ids"]):
                observations.append(
                    {
                        "competitor_id": competitor["id"],
                        "task_id": task["id"],
                        "repetition_id": repetition_id,
                        "status": "valid",
                        "raw_value": values[task["id"]][competitor["id"]][index],
                        "result_sha256": ("d" if index == 0 else "e") * 64,
                    }
                )
    return {
        "schema_version": "1.0",
        "snapshot_id": "arena-2026-09-03",
        "benchmark_release_sha256": DIGEST,
        "experiment_policy_sha256": "f" * 64,
        "reference_competitor_id": "reference",
        "competitors": competitors,
        "tasks": tasks,
        "observations": observations,
        "weighting": {
            "domains": "equal",
            "task_families_within_domain": "equal",
            "tasks_within_family": "equal",
            "repetitions_within_task": "equal",
            "pairs_within_repetition": "equal",
        },
        "model": {
            "type": "bradley_terry",
            "l2_regularization": "0.01",
            "max_iterations": 100,
            "convergence_tolerance": "0.0000000001",
            "rank_tolerance": "0.000001",
            "output_decimal_places": 6,
        },
        "uncertainty": {
            "method": "cluster_bootstrap_percentile",
            "cluster_unit": "task_family",
            "confidence_level": "0.95",
            "resamples": 100,
            "seed": 17,
        },
        "representativeness_gate": {
            "minimum_domains": 2,
            "minimum_task_families": 2,
            "minimum_tasks": 2,
            "maximum_pairwise_probability_error": "1",
            "maximum_leave_one_domain_out_rank_shift": 0,
        },
        "require_complete_results": True,
    }


def _balanced_input(duplicate_losing_task=False):
    value = _base_arena_input()
    value["competitors"] = [
        {"id": "agent", "name": "Agent", "kind": "agent", "configuration_sha256": "1" * 64},
        {"id": "reference", "name": "Reference", "kind": "reference_portfolio", "configuration_sha256": "2" * 64},
    ]
    value["tasks"] = [
        {
            "id": "domain_a_task",
            "payload_sha256": "3" * 64,
            "domain": "domain_a",
            "task_family": "family_a",
            "metric": {"name": "score", "direction": "maximize", "tie_tolerance": "0"},
            "reference": {"source": "calibration_best", "artifact_sha256": "8" * 64},
            "repetition_ids": ["r1"],
        },
        {
            "id": "domain_b_task_1",
            "payload_sha256": "4" * 64,
            "domain": "domain_b",
            "task_family": "family_b",
            "metric": {"name": "score", "direction": "maximize", "tie_tolerance": "0"},
            "reference": {"source": "reproducible_baseline", "artifact_sha256": "9" * 64},
            "repetition_ids": ["r1"],
        },
    ]
    if duplicate_losing_task:
        value["tasks"].append(
            {
                "id": "domain_b_task_2",
                "payload_sha256": "5" * 64,
                "domain": "domain_b",
                "task_family": "family_b",
                "metric": {"name": "score", "direction": "maximize", "tie_tolerance": "0"},
                "reference": {"source": "reproducible_baseline", "artifact_sha256": "0" * 64},
                "repetition_ids": ["r1"],
            }
        )
    observations = []
    for task in value["tasks"]:
        agent_value = "2" if task["domain"] == "domain_a" else "0"
        for competitor_id, raw_value in (("agent", agent_value), ("reference", "1")):
            observations.append(
                {
                    "competitor_id": competitor_id,
                    "task_id": task["id"],
                    "repetition_id": "r1",
                    "status": "valid",
                    "raw_value": raw_value,
                    "result_sha256": ("a" if competitor_id == "agent" else "b") * 64,
                }
            )
    value["observations"] = observations
    value["representativeness_gate"]["maximum_leave_one_domain_out_rank_shift"] = 1
    return value


class DisplayScaleTests(unittest.TestCase):
    def test_scale_maps_worst_to_zero_reference_to_1000_without_clamping(self):
        engine = DisplayScaleEngine()
        reference = engine.score(_display_scale(), _verifier("10")).as_mapping()
        above = engine.score(_display_scale(), _verifier("15")).as_mapping()
        below = engine.score(_display_scale(), _verifier("-5")).as_mapping()
        self.assertEqual("1000.000000", reference["display_score"])
        self.assertEqual("1500.000000", above["display_score"])
        self.assertEqual("-500.000000", below["display_score"])
        for result in (reference, above, below):
            validate_display_score_result(result)

    def test_minimize_direction_and_invalid_result_are_explicit(self):
        scale = _display_scale("minimize", lower="100", reference="10", source="reproducible_baseline")
        scored = DisplayScaleEngine().score(
            scale, _verifier("55", direction="minimize")
        ).as_mapping()
        invalid = DisplayScaleEngine().score(
            scale,
            _verifier("0", direction="minimize", valid=False, error="invalid submission"),
        ).as_mapping()
        self.assertEqual("500.000000", scored["display_score"])
        self.assertEqual("invalid", invalid["status"])
        self.assertIsNone(invalid["display_score"])
        validate_display_score_result(invalid)

    def test_reference_must_be_strictly_better_than_first_batch_worst(self):
        with self.assertRaisesRegex((ContractError, MatrixContractError), "strictly better"):
            validate_display_scale_definition(
                _display_scale("maximize", lower="10", reference="10")
            )


class CrossTaskArenaTests(unittest.TestCase):
    def test_bradley_terry_uses_raw_same_task_outcomes_and_fixed_reference(self):
        result = BradleyTerryArenaEngine().analyze(_base_arena_input())
        validate_cross_task_arena_result(result)
        by_id = {item["competitor_id"]: item for item in result["ranking"]}
        self.assertEqual(1, by_id["agent_strong"]["rank"])
        self.assertEqual(2, by_id["reference"]["rank"])
        self.assertEqual(3, by_id["agent_weak"]["rank"])
        self.assertGreater(Decimal(by_id["agent_strong"]["bt_strength"]), 0)
        self.assertLess(Decimal(by_id["agent_weak"]["bt_strength"]), 0)
        self.assertEqual("0.000000", by_id["reference"]["bt_strength"])
        self.assertEqual("50.000000", by_id["reference"]["reference_win_probability_percent"])
        self.assertEqual("passed", result["representativeness"]["status"])
        total_pair_weight = sum(
            Decimal(item["compared_weight"]) for item in result["pairwise"]
        )
        self.assertLess(abs(total_pair_weight - Decimal(1)), Decimal("0.00001"))

    def test_equal_hierarchy_prevents_more_tasks_in_one_domain_from_adding_weight(self):
        single = BradleyTerryArenaEngine().analyze(_balanced_input(False))
        duplicated = BradleyTerryArenaEngine().analyze(_balanced_input(True))
        single_agent = next(
            item for item in single["ranking"] if item["competitor_id"] == "agent"
        )
        duplicated_agent = next(
            item for item in duplicated["ranking"] if item["competitor_id"] == "agent"
        )
        self.assertEqual("0.000000", single_agent["bt_strength"])
        self.assertEqual(single_agent["bt_strength"], duplicated_agent["bt_strength"])

    def test_frozen_task_tolerance_turns_small_raw_differences_into_ties(self):
        value = _balanced_input(False)
        for task in value["tasks"]:
            task["metric"]["tie_tolerance"] = "1"
        result = BradleyTerryArenaEngine().analyze(value)
        self.assertEqual(1, len(result["pairwise"]))
        self.assertEqual(0, result["pairwise"][0]["left_wins"])
        self.assertEqual(2, result["pairwise"][0]["ties"])
        self.assertEqual(0, result["pairwise"][0]["left_losses"])

    def test_agent_invalid_is_a_loss_but_infrastructure_failure_is_not_a_score(self):
        invalid_input = _base_arena_input()
        target = next(
            item
            for item in invalid_input["observations"]
            if item["competitor_id"] == "agent_weak"
            and item["task_id"] == "bio_task"
            and item["repetition_id"] == "seed-1"
        )
        target["status"] = "agent_invalid"
        target["raw_value"] = None
        invalid_result = BradleyTerryArenaEngine().analyze(invalid_input)
        weak = next(
            item for item in invalid_result["ranking"] if item["competitor_id"] == "agent_weak"
        )
        self.assertGreater(Decimal(weak["invalid_observation_rate_percent"]), 0)

        unavailable_input = _base_arena_input()
        unavailable_input["require_complete_results"] = False
        target = next(
            item
            for item in unavailable_input["observations"]
            if item["competitor_id"] == "agent_weak"
            and item["task_id"] == "bio_task"
            and item["repetition_id"] == "seed-1"
        )
        target["status"] = "infrastructure_failed"
        target["raw_value"] = None
        provisional = BradleyTerryArenaEngine().analyze(unavailable_input)
        self.assertEqual("provisional_incomplete", provisional["status"])
        self.assertEqual("failed", provisional["representativeness"]["status"])
        unavailable_input["require_complete_results"] = True
        with self.assertRaisesRegex(ContractError, "rerun"):
            BradleyTerryArenaEngine().analyze(unavailable_input)

    def test_snapshot_is_order_independent_and_bootstrap_is_deterministic(self):
        original = _base_arena_input()
        original["uncertainty"]["cluster_unit"] = "task_repetition"
        reordered = copy.deepcopy(original)
        reordered["competitors"].reverse()
        reordered["tasks"].reverse()
        reordered["observations"].reverse()
        for task in reordered["tasks"]:
            task["repetition_ids"].reverse()
        first = BradleyTerryArenaEngine().analyze(original)
        second = BradleyTerryArenaEngine().analyze(reordered)
        self.assertEqual(first, second)

    def test_domain_sensitivity_blocks_representative_claim(self):
        result = BradleyTerryArenaEngine().analyze(_balanced_input(False))
        shift_check = next(
            item
            for item in result["representativeness"]["checks"]
            if item["name"] == "maximum_leave_one_domain_out_rank_shift"
        )
        self.assertTrue(shift_check["passed"])
        strict = _balanced_input(False)
        strict["representativeness_gate"]["maximum_leave_one_domain_out_rank_shift"] = 0
        strict_result = BradleyTerryArenaEngine().analyze(strict)
        self.assertEqual("failed", strict_result["representativeness"]["status"])

    def test_disconnected_provisional_comparison_fails_closed(self):
        value = _base_arena_input()
        value["require_complete_results"] = False
        for observation in value["observations"]:
            if observation["competitor_id"] == "agent_weak":
                observation["status"] = "infrastructure_failed"
                observation["raw_value"] = None
        with self.assertRaisesRegex(ArenaContractError, "disconnected"):
            BradleyTerryArenaEngine().analyze(value)


if __name__ == "__main__":
    unittest.main()
