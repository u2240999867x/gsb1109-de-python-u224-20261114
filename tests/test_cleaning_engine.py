import unittest

from cleaning import (
    CircularReferenceError,
    CleaningEngine,
    ColumnLineage,
    RuleEvaluationError,
)


def member_engine():
    """The discount scenario from the task description."""
    engine = CleaningEngine()
    engine.add_rule(
        "final_price",
        "price * discount(level) if is_member else price",
    )
    engine.add_table("discount", {"gold": 0.8, "silver": 0.9})
    return engine


MEMBER_ROWS = [
    {"id": 1, "price": 100, "level": "gold", "is_member": False},
    {"id": 2, "price": 50, "level": "silver", "is_member": True},
    {"id": 3, "price": 20, "level": "gold", "is_member": True},
]


class TestBasicDerivedColumns(unittest.TestCase):
    def test_derived_column_uses_source_columns(self):
        engine = CleaningEngine()
        engine.add_rule("double", "value * 2")
        result = engine.run([{"id": 1, "value": 21}])
        self.assertEqual(result[1]["double"], 42)

    def test_derived_column_can_use_earlier_derived_column(self):
        engine = CleaningEngine()
        engine.add_rule("a", "x + 1")
        engine.add_rule("b", "a * 2")
        result = engine.run([{"id": 1, "x": 10}])
        self.assertEqual(result[1]["a"], 11)
        self.assertEqual(result[1]["b"], 22)

    def test_rules_are_lazy_so_definition_order_does_not_matter(self):
        engine = CleaningEngine()
        engine.add_rule("b", "a * 2")
        engine.add_rule("a", "x + 1")
        result = engine.run([{"id": 1, "x": 10}])
        self.assertEqual(result[1]["b"], 22)

    def test_chained_derived_columns_lineage_is_direct_only(self):
        engine = CleaningEngine()
        engine.add_rule("a", "x + 1")
        engine.add_rule("b", "a * 2")
        engine.add_rule("c", "b - 3")
        engine.run([{"id": 1, "x": 10}])
        lineage = engine.lineage(1)
        self.assertEqual(lineage["a"].dependencies, frozenset({"x"}))
        self.assertEqual(lineage["b"].dependencies, frozenset({"a"}))
        self.assertEqual(lineage["c"].dependencies, frozenset({"b"}))

    def test_helper_functions_are_supported(self):
        engine = CleaningEngine()
        engine.add_function("double_it", lambda value: value * 2)
        engine.add_rule("out", "double_it(value)")
        result = engine.run([{"id": 1, "value": 7}])
        self.assertEqual(result[1]["out"], 14)

    def test_builtin_functions_still_work(self):
        engine = CleaningEngine()
        engine.add_rule("out", "str(int(value))")
        result = engine.run([{"id": 1, "value": 3.9}])
        self.assertEqual(result[1]["out"], "3")


class TestLineageRecording(unittest.TestCase):
    def test_member_branch_records_table_and_discount_columns(self):
        engine = member_engine()
        engine.run(MEMBER_ROWS)
        lineage = engine.lineage(2)["final_price"]
        self.assertEqual(
            lineage.dependencies, frozenset({"price", "level", "is_member"})
        )
        self.assertEqual(lineage.tables, frozenset({("discount", 1)}))
        self.assertEqual(lineage.rule_version, 1)

    def test_non_member_branch_never_reads_discount_table(self):
        engine = member_engine()
        engine.run(MEMBER_ROWS)
        lineage = engine.lineage(1)["final_price"]
        self.assertEqual(lineage.dependencies, frozenset({"price", "is_member"}))
        self.assertEqual(lineage.tables, frozenset())

    def test_short_circuiting_boolops_are_tracked_precisely(self):
        engine = CleaningEngine()
        engine.add_table("discount", {"gold": 0.8})
        engine.add_rule("out", "price * discount(level) if is_member and level else price")
        engine.run([
            {"id": 1, "price": 100, "level": "gold", "is_member": False},
            {"id": 2, "price": 100, "level": "gold", "is_member": True},
        ])
        self.assertEqual(engine.lineage(1)["out"].tables, frozenset())
        self.assertEqual(
            engine.lineage(2)["out"].tables, frozenset({("discount", 1)})
        )

    def test_columns_only_in_untaken_branch_are_not_read(self):
        engine = CleaningEngine()
        engine.add_rule("out", "secret * 2 if flag else 0")
        engine.run([{"id": 1, "flag": False, "secret": 41}])
        self.assertEqual(
            engine.lineage(1)["out"].dependencies, frozenset({"flag"})
        )

    def test_lineage_is_recorded_per_record(self):
        engine = member_engine()
        engine.run(MEMBER_ROWS)
        self.assertNotEqual(
            engine.lineage(1)["final_price"].tables,
            engine.lineage(2)["final_price"].tables,
        )

    def test_callable_table_is_tracked_with_version(self):
        calls = []

        def lookup(level):
            calls.append(level)
            return {"gold": 0.8}[level]

        engine = CleaningEngine()
        engine.add_table("discount", lookup)
        engine.add_rule("out", "price * discount(level) if is_member else price")
        engine.run([{"id": 1, "price": 100, "level": "gold", "is_member": True}])
        self.assertEqual(calls, ["gold"])
        self.assertEqual(
            engine.lineage(1)["out"].tables, frozenset({("discount", 1)})
        )


class TestIncrementalInputs(unittest.TestCase):
    def test_only_affected_records_are_returned(self):
        engine = member_engine()
        engine.run(MEMBER_ROWS)
        untouched_lineage = engine.lineage(2)["final_price"]
        result = engine.update_inputs({1: {"price": 200}})
        self.assertEqual(set(result), {1})
        self.assertEqual(result[1]["final_price"], 200)
        self.assertIs(engine.lineage(2)["final_price"], untouched_lineage)

    def test_change_propagates_through_derived_chain(self):
        engine = CleaningEngine()
        engine.add_rule("a", "x + 1")
        engine.add_rule("b", "a * 2")
        engine.add_rule("c", "b - 3")
        engine.run([{"id": 1, "x": 10}])
        engine.update_inputs({1: {"x": 20}})
        self.assertEqual(engine.lineage(1)["a"].dependencies, frozenset({"x"}))
        self.assertEqual(engine._states[1].values["c"], 39)

    def test_membership_flip_switches_dependency_even_if_old_path_skipped_table(self):
        engine = member_engine()
        engine.run(MEMBER_ROWS)
        self.assertEqual(engine.lineage(1)["final_price"].tables, frozenset())

        result = engine.update_inputs({1: {"is_member": True}})

        self.assertEqual(result[1]["final_price"], 80.0)
        lineage = engine.lineage(1)["final_price"]
        self.assertEqual(
            lineage.dependencies, frozenset({"price", "level", "is_member"})
        )
        self.assertEqual(lineage.tables, frozenset({("discount", 1)}))

    def test_membership_flip_back_drops_table_dependency(self):
        engine = member_engine()
        engine.run(MEMBER_ROWS)
        engine.update_inputs({1: {"is_member": True}})
        engine.update_inputs({1: {"is_member": False}})
        self.assertEqual(engine.lineage(1)["final_price"].tables, frozenset())
        self.assertEqual(engine._states[1].values["final_price"], 100)

    def test_static_reference_forces_recompute_when_branch_switches(self):
        # The rule only mentions `level` inside the member branch.  Before the
        # flip `level` was never read, but changing `is_member` must still
        # invalidate the cached result via the static reference set.
        engine = member_engine()
        engine.run(MEMBER_ROWS)
        result = engine.update_inputs({1: {"is_member": True, "level": "silver"}})
        self.assertEqual(result[1]["final_price"], 90.0)


class TestRuleAndTableUpdates(unittest.TestCase):
    def test_rule_update_bumps_version_and_recomputes_every_record(self):
        engine = member_engine()
        engine.run(MEMBER_ROWS)
        engine.update_rule(
            "final_price",
            "price * discount(level) * 1.1 if is_member else price",
        )
        for key in (1, 2, 3):
            self.assertEqual(engine.lineage(key)["final_price"].rule_version, 2)
        self.assertAlmostEqual(engine._states[2].values["final_price"], 49.5)

    def test_explicit_rule_version_is_recorded(self):
        engine = CleaningEngine()
        engine.add_rule("out", "x + 1", version=7)
        engine.run([{"id": 1, "x": 1}])
        self.assertEqual(engine.lineage(1)["out"].rule_version, 7)

    def test_table_update_only_recomputes_records_that_read_it(self):
        engine = member_engine()
        engine.run(MEMBER_ROWS)
        untouched = engine.lineage(1)["final_price"]

        engine.update_table("discount", {"gold": 0.5, "silver": 0.6})

        self.assertIs(engine.lineage(1)["final_price"], untouched)
        self.assertEqual(
            engine.lineage(2)["final_price"].tables,
            frozenset({("discount", 2)}),
        )
        self.assertEqual(engine._states[2].values["final_price"], 30.0)
        self.assertEqual(engine._states[3].values["final_price"], 10.0)

    def test_explicit_table_version_is_recorded(self):
        engine = member_engine()
        engine.run(MEMBER_ROWS)
        engine.update_table("discount", {"gold": 0.5}, version=42)
        self.assertEqual(
            engine.lineage(3)["final_price"].tables,
            frozenset({("discount", 42)}),
        )


class TestCircularReferences(unittest.TestCase):
    def test_cycle_in_untaken_branch_is_allowed(self):
        engine = CleaningEngine()
        engine.add_rule("p", "q if False else 1")
        engine.add_rule("q", "p + 1")
        result = engine.run([{"id": 1}])
        self.assertEqual(result[1]["p"], 1)
        self.assertEqual(result[1]["q"], 2)

    def test_cycle_through_taken_branch_raises_with_chain(self):
        engine = CleaningEngine()
        engine.add_rule("p", "q if True else 1")
        engine.add_rule("q", "p + 1")
        engine.run([{"id": 1}])
        error = engine.errors(1)["p"]
        self.assertIsInstance(error, CircularReferenceError)
        self.assertEqual(error.chain, ["p", "q", "p"])
        self.assertEqual(error.column, "p")

    def test_cycle_depends_on_record_data(self):
        engine = CleaningEngine()
        engine.add_rule("p", "q if loop else 1")
        engine.add_rule("q", "p + 1")
        engine.run([
            {"id": 1, "loop": False},
            {"id": 2, "loop": True},
        ])
        self.assertEqual(engine._states[1].values["p"], 1)
        self.assertIn("p", engine._states[2].failed)
        self.assertIsInstance(engine.errors(2)["p"], CircularReferenceError)
        # The healthy record is still fully present in the output.
        self.assertEqual(engine._states[1].values["q"], 2)

    def test_direct_self_reference_is_detected(self):
        engine = CleaningEngine()
        engine.add_rule("p", "p + 1")
        engine.run([{"id": 1}])
        self.assertIsInstance(engine.errors(1)["p"], CircularReferenceError)


class TestRuleErrors(unittest.TestCase):
    def test_error_is_wrapped_and_recorded_per_record(self):
        engine = CleaningEngine()
        engine.add_rule("boom", "1 / 0")
        engine.add_rule("fine", "x + 1")
        engine.run([{"id": 1, "x": 10}])
        error = engine.errors(1)["boom"]
        self.assertIsInstance(error, RuleEvaluationError)
        self.assertEqual(error.column, "boom")
        self.assertIsInstance(error.original, ZeroDivisionError)
        self.assertEqual(engine._states[1].values["fine"], 11)

    def test_failed_rule_retries_after_input_fix(self):
        engine = CleaningEngine()
        engine.add_rule("ratio", "total / part")
        engine.run([{"id": 1, "total": 10, "part": 0}])
        self.assertIn("ratio", engine._states[1].failed)
        engine.update_inputs({1: {"part": 4}})
        self.assertNotIn("ratio", engine._states[1].failed)
        self.assertEqual(engine._states[1].values["ratio"], 2.5)

    def test_downstream_failed_rule_recovers_when_upstream_is_fixed(self):
        engine = CleaningEngine()
        engine.add_rule("ratio", "total / part")
        engine.add_rule("report", "str(ratio)")
        engine.run([{"id": 1, "total": 10, "part": 0}])
        self.assertTrue(engine._states[1].failed)
        engine.update_inputs({1: {"part": 2}})
        self.assertEqual(engine._states[1].failed, set())
        self.assertEqual(engine._states[1].values["report"], "5.0")

    def test_table_failure_recovers_after_table_fix(self):
        engine = CleaningEngine()
        engine.add_rule("out", "price * discount(level)")
        engine.add_table("discount", {"gold": 0.8})
        engine.run([{"id": 1, "price": 100, "level": "bronze"}])
        self.assertIn("out", engine._states[1].failed)
        engine.update_table("discount", {"gold": 0.8, "bronze": 0.5})
        self.assertNotIn("out", engine._states[1].failed)
        self.assertEqual(engine._states[1].values["out"], 50.0)


class TestIncrementalMatchesFullRerun(unittest.TestCase):
    """The headline property: incremental output must match a from-scratch run."""

    def setUp(self):
        self.rows = [
            {"id": 1, "price": 100, "level": "gold", "is_member": False},
            {"id": 2, "price": 50, "level": "silver", "is_member": True},
            {"id": 3, "price": 20, "level": "gold", "is_member": True},
            {"id": 4, "price": 80, "level": "silver", "is_member": False},
        ]
        self.discount_table = {"gold": 0.8, "silver": 0.9}
        self.expression = "price * discount(level) if is_member else price"

    def _fresh_full_run(self, rows, expression, table):
        engine = CleaningEngine()
        engine.add_rule("final_price", expression)
        engine.add_table("discount", dict(table))
        return engine.run([dict(row) for row in rows])

    def _assert_matches_full_rerun(self, engine, rows, expression, table):
        expected = self._fresh_full_run(rows, expression, table)
        for key, expected_row in expected.items():
            actual_output = engine._output(engine._states[key])
            self.assertEqual(
                actual_output,
                expected_row,
                msg=f"row {key} drifted after incremental update",
            )
            for column, expected_lineage in engine.lineage(key).items():
                self.assertIsInstance(expected_lineage, ColumnLineage)

    def test_after_each_input_change(self):
        engine = member_engine()
        engine.run([dict(row) for row in self.rows])
        changes = [
            {1: {"is_member": True}},
            {2: {"price": 75}},
            {3: {"is_member": False}, 4: {"level": "gold"}},
            {1: {"level": "silver", "price": 120}},
            {2: {"is_member": False}},
        ]
        rows = {row["id"]: dict(row) for row in self.rows}
        for change in changes:
            for key, columns in change.items():
                rows[key].update(columns)
            engine.update_inputs(change)
            self._assert_matches_full_rerun(
                engine, list(rows.values()), self.expression, self.discount_table
            )

    def test_after_rule_update(self):
        engine = member_engine()
        engine.run([dict(row) for row in self.rows])
        new_expression = (
            "price if not is_member else price * discount(level) + 5"
        )
        engine.update_rule("final_price", new_expression)
        self._assert_matches_full_rerun(
            engine, self.rows, new_expression, self.discount_table
        )

    def test_after_table_update(self):
        engine = member_engine()
        engine.run([dict(row) for row in self.rows])
        new_table = {"gold": 0.5, "silver": 0.6}
        engine.update_table("discount", dict(new_table))
        self._assert_matches_full_rerun(
            engine, self.rows, self.expression, new_table
        )

    def test_after_mixed_series_of_updates(self):
        engine = member_engine()
        engine.run([dict(row) for row in self.rows])
        rows = {row["id"]: dict(row) for row in self.rows}
        expression = self.expression
        table = dict(self.discount_table)

        engine.update_inputs({1: {"is_member": True}})
        rows[1]["is_member"] = True
        self._assert_matches_full_rerun(
            engine, list(rows.values()), expression, table
        )

        table = {"gold": 0.75}
        engine.update_table("discount", dict(table))
        self._assert_matches_full_rerun(
            engine, list(rows.values()), expression, table
        )

        expression = "price * discount(level) if is_member else price + 1"
        engine.update_rule("final_price", expression)
        self._assert_matches_full_rerun(
            engine, list(rows.values()), expression, table
        )

        engine.update_inputs({3: {"is_member": False}})
        rows[3]["is_member"] = False
        self._assert_matches_full_rerun(
            engine, list(rows.values()), expression, table
        )


if __name__ == "__main__":
    unittest.main()
