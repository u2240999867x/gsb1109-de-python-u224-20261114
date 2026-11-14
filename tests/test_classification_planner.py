import json
import unittest
from fractions import Fraction

import simpleeval
from simpleeval import (
    ClassificationPlanner,
    PlanningLimitExceeded,
    UnknownCategory,
)


def change_sets(plan):
    return sorted((dict(s.changes) for s in plan.solutions), key=lambda d: sorted(d.items()))


class TestClassificationPlanner(unittest.TestCase):
    def setUp(self):
        self.planner = ClassificationPlanner(
            rules={
                "fresh": "shelf_life <= 7",
                "bulk": ["weight >= 20", "units >= 12"],
                "standard": "shelf_life > 7 and weight < 20",
            },
            candidates={
                "shelf_life": [3, 7, 14, 30],
                "weight": [5, 20, 30],
                "units": [1, 6, 12],
            },
            costs={"shelf_life": 2, "weight": 3, "units": 1},
            locked=("sku",),
        )
        self.record = {
            "sku": "A-100",
            "shelf_life": 30,
            "weight": 5,
            "units": 6,
        }

    def test_changes_record_into_target(self):
        plan = self.planner.solve(self.record, "fresh")
        self.assertEqual(plan.status, "solved")
        self.assertTrue(plan.has_solution)
        self.assertEqual(plan.cost, 2)
        self.assertEqual(change_sets(plan), [{"shelf_life": 3}, {"shelf_life": 7}])
        for solution in plan.solutions:
            self.assertLessEqual(solution.record["shelf_life"], 7)

    def test_locked_fields_never_change(self):
        plan = self.planner.solve(self.record, "fresh")
        for solution in plan.solutions:
            self.assertNotIn("sku", solution.changes)
            self.assertEqual(solution.record["sku"], "A-100")

    def test_original_values_are_free_options(self):
        plan = self.planner.solve(self.record, "bulk")
        self.assertEqual(plan.cost, 4)
        self.assertEqual(
            change_sets(plan),
            [{"units": 12, "weight": 20}, {"units": 12, "weight": 30}],
        )

    def test_all_equal_cost_solutions_are_returned(self):
        planner = ClassificationPlanner(
            rules={"target": "a == 1 or b == 1"},
            candidates={"a": [0, 1], "b": [0, 1]},
            costs={"a": 1, "b": 1},
            exclusive=False,
        )
        plan = planner.solve({"a": 0, "b": 0}, "target")
        self.assertEqual(plan.cost, 1)
        self.assertEqual(change_sets(plan), [{"a": 1}, {"b": 1}])

    def test_no_solution_is_explicit(self):
        planner = ClassificationPlanner(
            rules={"impossible": "a > 100"},
            candidates={"a": [1, 2]},
        )
        plan = planner.solve({"a": 1}, "impossible")
        self.assertEqual(plan.status, "no_solution")
        self.assertFalse(plan.has_solution)
        self.assertIsNone(plan.cost)
        self.assertEqual(plan.solutions, ())

    def test_two_field_changes_can_be_jointly_necessary(self):
        # Neither field alone changes the category; only together do they.
        planner = ClassificationPlanner(
            rules={
                "paired": "x >= 5 and y >= 5",
                "other": "x < 5 or y < 5",
            },
            candidates={"x": [1, 5], "y": [1, 5]},
            costs={"x": 2, "y": 2},
        )
        plan = planner.solve({"x": 1, "y": 1}, "paired")
        self.assertEqual(plan.status, "solved")
        self.assertEqual(change_sets(plan), [{"x": 5, "y": 5}])

    def test_derived_columns_are_recomputed(self):
        planner = ClassificationPlanner(
            rules={
                "heavy": "total_weight >= 100",
                "light": "total_weight < 100",
            },
            candidates={"units": [1, 10]},
            derived={"total_weight": "units * unit_weight"},
            costs={"units": 1},
        )
        plan = planner.solve({"units": 1, "unit_weight": 10}, "heavy")
        self.assertEqual(plan.status, "solved")
        self.assertEqual(plan.solutions[0].record["total_weight"], 100)
        self.assertEqual(dict(plan.solutions[0].changes), {"units": 10})
        self.assertEqual(planner.rebuild({"units": 2, "unit_weight": 10})["total_weight"], 20)

    def test_derived_chain_uses_prior_derived_values(self):
        planner = ClassificationPlanner(
            rules={"x": "grand >= 10", "y": "grand < 10"},
            candidates={"a": [1, 4]},
            derived={"square": "a * a", "grand": "square + 1"},
            exclusive=False,
        )
        plan = planner.solve({"a": 1}, "x")
        self.assertEqual(plan.cost, 1)
        self.assertEqual(plan.solutions[0].record["grand"], 17)

    def test_initial_record_diagnostics(self):
        planner = ClassificationPlanner(
            rules={"one": "a == 1", "two": "a == 2"},
            candidates={"a": [1, 2, 3]},
            exclusive=False,
        )
        plan = planner.solve({"a": 3}, "two")
        self.assertEqual(plan.initial_categories, ())

    def test_input_record_not_mutated(self):
        before = dict(self.record)
        self.planner.solve(self.record, "fresh")
        self.assertEqual(self.record, before)

    def test_exclusive_rejects_multiple_matches(self):
        planner = ClassificationPlanner(
            rules={"alpha": "a == 1", "beta": "a == 2"},
            candidates={"a": [1, 2, 3]},
            costs={"a": 1},
            exclusive=True,
        )
        # a=2 matches both rules under the earlier overlapping design; with
        # mutually exclusive rules a=2 is already uniquely beta.
        plan = planner.solve({"a": 2}, "beta")
        self.assertEqual(plan.status, "solved")
        self.assertEqual(plan.cost, 0)
        self.assertEqual(change_sets(plan), [{}])

        planner = ClassificationPlanner(
            rules={"alpha": "a == 1", "beta": "a >= 2"},
            candidates={"a": [1, 2, 3]},
            costs={"a": 1},
            exclusive=True,
        )
        plan = planner.solve({"a": 1}, "beta")
        self.assertEqual(plan.status, "solved")
        self.assertEqual(change_sets(plan), [{"a": 2}, {"a": 3}])

    def test_nonexclusive_allows_shared_matches(self):
        planner = ClassificationPlanner(
            rules={"alpha": "a >= 1", "beta": "a >= 2"},
            candidates={"a": [1, 2, 3]},
            costs={"a": 1},
            exclusive=False,
        )
        plan = planner.solve({"a": 0}, "beta")
        self.assertEqual(plan.cost, 1)
        self.assertEqual(change_sets(plan), [{"a": 2}, {"a": 3}])

    def test_per_value_costs_and_callable_costs(self):
        planner = ClassificationPlanner(
            rules={"t": "kind == 'x'"},
            candidates={"kind": ["a", "x", "z"]},
            costs={"kind": {"x": 5}},
            default_cost=9,
            exclusive=False,
        )
        plan = planner.solve({"kind": "a"}, "t")
        self.assertEqual(plan.cost, 5)

        planner = ClassificationPlanner(
            rules={"t": "kind == 'x'"},
            candidates={"kind": ["a", "x"]},
            costs=lambda field, value: 4 if value == "x" else 1,
            exclusive=False,
        )
        plan = planner.solve({"kind": "a"}, "t")
        self.assertEqual(plan.cost, 4)

    def test_decimal_costs_are_exact(self):
        planner = ClassificationPlanner(
            rules={"t": "a == 1"},
            candidates={"a": [1], "b": [1]},
            costs={"a": 0.1, "b": 0.2},
            exclusive=False,
        )
        plan = planner.solve({"a": 0, "b": 0}, "t")
        self.assertEqual(plan.cost, Fraction(1, 10))
        self.assertEqual(len(plan.solutions), 1)

    def test_unknown_category(self):
        with self.assertRaises(UnknownCategory):
            self.planner.solve(self.record, "missing")

    def test_planning_limit_raises_instead_of_false_no_solution(self):
        with self.assertRaises(PlanningLimitExceeded):
            ClassificationPlanner(
                rules={"t": "a == 1"},
                candidates={"a": list(range(10))},
                max_combinations=1,
            ).solve({"a": 0}, "t")

    def test_rule_error_policy(self):
        planner = ClassificationPlanner(
            rules={"t": "missing_field > 0", "u": "True"},
            candidates={},
            exclusive=False,
        )
        self.assertEqual(planner.categories({"x": 1}), ("u",))
        planner = ClassificationPlanner(
            rules={"t": "missing_field > 0"},
            candidates={},
            on_rule_error="raise",
        )
        with self.assertRaises(simpleeval.NameNotDefined):
            planner.categories({"x": 1})

    def test_derived_cannot_be_editable(self):
        with self.assertRaises(ValueError):
            ClassificationPlanner(
                rules={"t": "a == 1"},
                candidates={"total": [1]},
                derived={"total": "a + 1"},
            )

    def test_plan_as_dict_is_json_friendly(self):
        plan = self.planner.solve(self.record, "fresh")
        payload = plan.as_dict()
        json.dumps(payload)
        self.assertEqual(payload["target"], "fresh")
        self.assertEqual(payload["status"], "solved")
        self.assertEqual(payload["cost"], 2)

    def test_callable_rules_and_derived(self):
        planner = ClassificationPlanner(
            rules={
                "hot": lambda env: env["temp"] + env["margin"] > 90,
                "cool": lambda env: env["temp"] + env["margin"] <= 90,
            },
            candidates={"temp": [60, 70, 80], "margin": [5, 15, 25]},
            costs={"temp": 1, "margin": 2},
            derived={"heat_index": lambda env: env["temp"] + env["margin"]},
        )
        plan = planner.solve({"temp": 60, "margin": 5}, "hot")
        self.assertEqual(plan.cost, 3)
        self.assertEqual(
            change_sets(plan),
            [
                {"margin": 15, "temp": 80},
                {"margin": 25, "temp": 70},
                {"margin": 25, "temp": 80},
            ],
        )
        self.assertTrue(all(solution.record["heat_index"] > 90 for solution in plan.solutions))

    def test_ast_expression_rules(self):
        import ast

        planner = ClassificationPlanner(
            rules={"t": ast.parse("a == 1", mode="eval")},
            candidates={"a": [0, 1]},
            exclusive=False,
        )
        plan = planner.solve({"a": 0}, "t")
        self.assertEqual(change_sets(plan), [{"a": 1}])

    def test_absent_field_can_be_set_by_candidate(self):
        planner = ClassificationPlanner(
            rules={"t": "b == 5", "u": "b != 5"},
            candidates={"b": [5]},
            costs={"b": 1},
        )
        plan = planner.solve({"a": 1}, "t")
        self.assertEqual(change_sets(plan), [{"b": 5}])
        self.assertEqual(plan.solutions[0].record["b"], 5)

    def test_multiple_statements_are_rejected(self):
        with self.assertRaises(simpleeval.InvalidExpression):
            ClassificationPlanner(
                rules={"t": "a == 1\nb == 2"},
                candidates={"a": [1]},
            )

    def test_empty_rule_set_is_rejected(self):
        with self.assertRaises(ValueError):
            ClassificationPlanner(rules={}, candidates={"a": [1]})

    def test_mapping_candidates_carry_costs(self):
        planner = ClassificationPlanner(
            rules={"t": "x == 2", "u": "x != 2"},
            candidates={"x": {1: 9, 2: 4}},
        )
        plan = planner.solve({"x": 1}, "t")
        self.assertEqual(plan.cost, 4)
        self.assertEqual(change_sets(plan), [{"x": 2}])

    def test_zero_cost_change_is_impossible_only_if_value_equal(self):
        planner = ClassificationPlanner(
            rules={"t": "x == 5", "u": "x != 5"},
            candidates={"x": {5: 0}},
        )
        plan = planner.solve({"x": 5}, "t")
        self.assertEqual(plan.cost, 0)
        self.assertEqual(change_sets(plan), [{}])


if __name__ == "__main__":
    unittest.main()
