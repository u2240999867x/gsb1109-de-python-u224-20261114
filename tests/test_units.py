import unittest

from simpleeval import (
    IncompatibleUnits,
    Unit,
    UnitChecker,
    UnitFunction,
    check_units,
)


class TestUnitParsing(unittest.TestCase):
    """Units are dimensions, not opaque labels."""

    def test_simple_units(self):
        self.assertEqual(str(Unit("CNY")), "CNY")
        self.assertEqual(str(Unit("件")), "件")
        self.assertEqual(str(Unit("CNY")), Unit("CNY"))

    def test_amount_and_count_are_different_dimensions(self):
        self.assertTrue(Unit("CNY").compatible(Unit("USD")))
        self.assertTrue(Unit("元").compatible(Unit("CNY")))
        self.assertFalse(Unit("CNY").compatible(Unit("件")))
        self.assertFalse(Unit("CNY").compatible(Unit("笔")))

    def test_compound_units(self):
        self.assertEqual(str(Unit("CNY/件")), "CNY/件")
        self.assertEqual(str(Unit("CNY") / Unit("件")), "CNY/件")
        self.assertEqual(str(Unit("CNY") / Unit("件") * Unit("件")), "CNY")
        self.assertEqual(str(Unit("CNY") ** 2), "CNY^2")
        self.assertTrue(Unit("CNY/件").is_compound)
        self.assertEqual(Unit("CNY/件") * Unit("件"), Unit("CNY"))

    def test_missing_and_dimensionless(self):
        self.assertTrue(Unit(None).is_missing)
        self.assertTrue(Unit("缺值").is_missing)
        self.assertTrue(Unit("missing").is_missing)
        self.assertTrue(Unit(1).is_dimensionless)
        self.assertTrue(Unit("倍数").is_dimensionless)
        self.assertTrue(Unit(None).compatible(Unit("CNY")))

    def test_unknown_unit_is_permissive_but_visible(self):
        self.assertTrue(Unit("nonsense^").is_unknown)
        self.assertTrue(Unit("nonsense^").compatible(Unit("CNY")))

    def test_equality_is_stricter_than_compatibility(self):
        self.assertTrue(Unit("CNY").compatible(Unit("USD")))
        self.assertNotEqual(Unit("CNY"), Unit("USD"))
        self.assertEqual(Unit("CNY"), "CNY")

    def test_dimension_mapping_constructor(self):
        self.assertTrue(Unit({"currency": 1}).compatible(Unit("CNY")))
        compound = Unit({"currency": 1, "count": -1})
        self.assertTrue(compound.compatible(Unit("CNY/件")))
        self.assertEqual(compound.dimensions["currency"], 1)
        self.assertEqual(compound.dimensions["count"], -1)


class TestUnitCheckerBasics(unittest.TestCase):
    def test_derived_unit(self):
        report = check_units("amount / count", names={"amount": "CNY", "count": "件"})
        self.assertTrue(report.ok)
        self.assertEqual(report.result_unit, Unit("CNY/件"))

    def test_multiplied_units(self):
        report = check_units("amount * count", names={"amount": "CNY", "count": "件"})
        self.assertTrue(report.ok)
        self.assertEqual(report.result_unit, Unit("CNY·件"))

    def test_unit_cancellation(self):
        report = check_units(
            "amount / count * count",
            names={"amount": "CNY", "count": "件"},
        )
        self.assertTrue(report.ok)
        self.assertEqual(report.result_unit, Unit("CNY"))

    def test_count_times_unit_price(self):
        report = check_units(
            "count * price",
            names={"count": "件", "price": "CNY/件"},
        )
        self.assertTrue(report.ok)
        self.assertEqual(report.result_unit, Unit("CNY"))

    def test_numeric_constant_is_neutral(self):
        self.assertEqual(
            check_units("amount * 0.9", names={"amount": "CNY"}).result_unit,
            Unit("CNY"),
        )
        self.assertEqual(
            check_units("amount / 1000", names={"amount": "CNY"}).result_unit,
            Unit("CNY"),
        )
        self.assertTrue(check_units("amount > 10000", names={"amount": "CNY"}).ok)

    def test_standalone_number_is_dimensionless(self):
        self.assertEqual(check_units("42").result_unit, Unit.dimensionless())
        self.assertEqual(check_units("1 + 2 * 3").result_unit, Unit.dimensionless())

    def test_reciprocal_unit(self):
        report = check_units("1 / amount", names={"amount": "CNY"})
        self.assertTrue(report.ok)
        self.assertEqual(report.result_unit, Unit("1/CNY"))

    def test_power(self):
        report = check_units("amount ** 2", names={"amount": "CNY"})
        self.assertTrue(report.ok)
        self.assertEqual(report.result_unit, Unit("CNY^2"))
        self.assertTrue(check_units("amount ** 0", names={"amount": "CNY"}).ok)
        self.assertEqual(
            check_units("amount ** 0", names={"amount": "CNY"}).result_unit,
            Unit.dimensionless(),
        )

    def test_non_dimensionless_exponent_is_rejected(self):
        report = check_units("amount ** count", names={"amount": "CNY", "count": "件"})
        self.assertFalse(report.ok)
        self.assertEqual(report.conflicts[0].code, "exponent-unit")


class TestAdditiveAndComparisonConflicts(unittest.TestCase):
    def test_amount_plus_count_conflicts(self):
        report = check_units("amount + count", names={"amount": "CNY", "count": "件"})
        self.assertFalse(report.ok)
        self.assertEqual(report.conflicts[0].code, "additive-units")
        self.assertEqual(report.conflicts[0].units, (Unit("CNY"), Unit("件")))

    def test_compatible_currencies_do_not_conflict(self):
        report = check_units("left + right", names={"left": "CNY", "right": "USD"})
        self.assertTrue(report.ok)

    def test_comparison_of_amount_and_count_conflicts(self):
        report = check_units("amount > count", names={"amount": "CNY", "count": "件"})
        self.assertFalse(report.ok)
        self.assertEqual(report.conflicts[0].code, "comparison-units")
        self.assertEqual(report.conflicts[0].left, Unit("CNY"))
        self.assertEqual(report.conflicts[0].right, Unit("件"))

    def test_chained_comparison_checks_each_pair(self):
        report = check_units(
            "low < amount < count",
            names={"low": "CNY", "amount": "CNY", "count": "件"},
        )
        self.assertFalse(report.ok)
        self.assertEqual(report.conflicts[0].code, "comparison-units")
        self.assertEqual(report.conflicts[0].right, Unit("件"))

    def test_comparison_against_scalar_is_allowed(self):
        self.assertTrue(check_units("amount > 0", names={"amount": "CNY"}).ok)
        self.assertTrue(check_units("amount == 0", names={"amount": "CNY"}).ok)
        self.assertTrue(check_units("amount != 0", names={"amount": "CNY"}).ok)

    def test_comparison_result_is_boolean(self):
        report = check_units("amount > 0", names={"amount": "CNY"})
        self.assertTrue(report.result_unit.is_dimensionless)


class TestBranches(unittest.TestCase):
    def test_reachable_branches_are_both_checked(self):
        """All current rows may take the amount branch; the other one still counts."""

        report = check_units(
            "amount if is_bulk else count",
            names={
                "amount": "CNY",
                "count": "件",
                "is_bulk": "bool",
            },
        )
        self.assertFalse(report.ok)
        self.assertEqual(report.conflicts[0].code, "if-branch-units")
        self.assertEqual(report.conflicts[0].units, (Unit("CNY"), Unit("件")))

    def test_missing_branch_does_not_change_unit(self):
        report = check_units(
            "amount if available else missing_amount",
            names={
                "amount": "CNY",
                "available": "bool",
                "missing_amount": "缺值",
            },
        )
        self.assertTrue(report.ok)
        self.assertEqual(report.result_unit, Unit("CNY"))
        self.assertTrue(report.may_be_missing)

    def test_none_literal_branch_does_not_change_unit(self):
        report = check_units(
            "amount if available else None",
            names={"amount": "CNY", "available": "bool"},
        )
        self.assertTrue(report.ok)
        self.assertEqual(report.result_unit, Unit("CNY"))

    def test_missing_plus_amount_keeps_amount(self):
        report = check_units(
            "amount + missing_amount",
            names={"amount": "CNY", "missing_amount": "缺值"},
        )
        self.assertTrue(report.ok)
        self.assertEqual(report.result_unit, Unit("CNY"))

    def test_constant_false_branch_is_excluded(self):
        report = check_units(
            "count if 1 == 2 else amount",
            names={"amount": "CNY", "count": "件"},
        )
        self.assertTrue(report.ok)
        self.assertEqual(report.result_unit, Unit("CNY"))
        self.assertEqual(len(report.excluded_branches), 1)
        self.assertIn("always false", report.excluded_branches[0].reason)

    def test_constant_true_branch_is_excluded(self):
        report = check_units(
            "count if True else amount",
            names={"amount": "CNY", "count": "件"},
        )
        self.assertTrue(report.ok)
        self.assertEqual(report.result_unit, Unit("件"))
        self.assertEqual(report.excluded_branches[0].code, "if-always-true")

    def test_none_condition_is_false(self):
        report = check_units(
            "amount if None else count",
            names={"amount": "CNY", "count": "件"},
        )
        self.assertTrue(report.ok)
        self.assertEqual(report.result_unit, Unit("件"))

    def test_known_value_folds_branch(self):
        report = check_units(
            "count if is_bulk else amount",
            names={"amount": "CNY", "count": "件"},
            values={"is_bulk": False},
        )
        self.assertTrue(report.ok)
        self.assertEqual(report.result_unit, Unit("CNY"))
        self.assertEqual(report.excluded_branches[0].code, "if-always-false")

    def test_dimension_map_declarations(self):
        report = check_units(
            "amount / count",
            names={"amount": {"currency": 1}, "count": {"count": 1}},
        )
        self.assertTrue(report.ok)
        self.assertEqual(report.result_unit, Unit("currency/count"))

    def test_nested_field_declarations(self):
        report = check_units(
            "order.amount / order.count",
            names={"order": {"amount": "CNY", "count": "件"}},
        )
        self.assertTrue(report.ok)
        self.assertEqual(report.result_unit, Unit("CNY/件"))

    def test_nested_branch_conflict_is_reported(self):
        report = check_units(
            "amount if outer else (count if inner else amount)",
            names={
                "amount": "CNY",
                "count": "件",
                "outer": "bool",
                "inner": "bool",
            },
        )
        self.assertFalse(report.ok)
        self.assertEqual(report.conflicts[0].code, "if-branch-units")

    def test_short_circuit_excludes_unreachable_operand(self):
        report = check_units(
            "(False and count) or amount",
            names={"amount": "CNY", "count": "件"},
        )
        self.assertTrue(report.ok)
        self.assertEqual(report.result_unit, Unit("CNY"))
        self.assertIn(
            "and-short-circuit",
            [branch.code for branch in report.excluded_branches],
        )

    def test_boolop_truthy_and_operand_is_not_a_result_branch(self):
        report = check_units(
            "True and amount",
            names={"amount": "CNY"},
        )
        self.assertTrue(report.ok)
        self.assertEqual(report.result_unit, Unit("CNY"))


class TestFunctions(unittest.TestCase):
    def test_return_unit_only(self):
        report = check_units(
            "total(amount)",
            names={"amount": "CNY"},
            functions={"total": "CNY"},
        )
        self.assertTrue(report.ok)
        self.assertEqual(report.result_unit, Unit("CNY"))

    def test_parameter_units_are_checked(self):
        report = check_units(
            "average(amount, count)",
            names={"amount": "CNY", "count": "件"},
            functions={"average": UnitFunction("CNY/件", ["CNY", "件"])},
        )
        self.assertTrue(report.ok)
        self.assertEqual(report.result_unit, Unit("CNY/件"))

        bad = check_units(
            "average(count, amount)",
            names={"amount": "CNY", "count": "件"},
            functions={"average": UnitFunction("CNY/件", ["CNY", "件"])},
        )
        self.assertFalse(bad.ok)
        self.assertEqual(bad.conflicts[0].code, "argument-units")

    def test_keyword_parameter_units_are_checked(self):
        report = check_units(
            "rate(amount=amount, count=count)",
            names={"amount": "CNY", "count": "件"},
            functions={
                "rate": UnitFunction(
                    "CNY/件",
                    ["CNY", "件"],
                    parameter_names=["amount", "count"],
                )
            },
        )
        self.assertTrue(report.ok)

    def test_missing_return_unit(self):
        report = check_units(
            "lookup(amount)",
            names={"amount": "CNY"},
            functions={"lookup": UnitFunction("缺值")},
        )
        self.assertTrue(report.ok)
        self.assertTrue(report.may_be_missing)

    def test_undeclared_function_is_visible(self):
        report = check_units("mystery(amount)", names={"amount": "CNY"})
        self.assertTrue(report.ok)
        self.assertEqual(report.unknown_names[0].kind, "function")
        self.assertTrue(report.result_unit.is_unknown)

    def test_strict_mode_rejects_undeclared_function(self):
        report = check_units("mystery(amount)", names={"amount": "CNY"}, strict=True)
        self.assertFalse(report.ok)

    def test_strict_mode_rejects_undeclared_name(self):
        report = check_units("amount + mystery", names={"amount": "CNY"}, strict=True)
        self.assertFalse(report.ok)


class TestReport(unittest.TestCase):
    def test_conflict_locations(self):
        report = check_units("amount + count", names={"amount": "CNY", "count": "件"})
        conflict = report.conflicts[0]
        self.assertEqual(conflict.position, (1, 0))
        self.assertEqual(conflict.left_position, (1, 0))
        self.assertEqual(conflict.right_position, (1, 9))
        self.assertEqual(conflict.left_source, "amount")
        self.assertEqual(conflict.right_source, "count")

    def test_multiline_locations(self):
        report = check_units(
            "(amount\n + count)",
            names={"amount": "CNY", "count": "件"},
        )
        conflict = report.conflicts[0]
        self.assertEqual(conflict.left_position, (1, 1))
        self.assertEqual(conflict.right_position, (2, 3))

    def test_conflicted_result_is_not_claimed_as_a_valid_unit(self):
        report = check_units("amount + count", names={"amount": "CNY", "count": "件"})
        self.assertTrue(report.result_unit.is_unknown)
        self.assertEqual([str(unit) for unit in report.possible_units], ["CNY", "件"])

    def test_raise_for_conflicts(self):
        report = check_units("amount + count", names={"amount": "CNY", "count": "件"})
        with self.assertRaises(IncompatibleUnits):
            report.raise_for_conflicts()
        self.assertEqual(
            check_units("amount", names={"amount": "CNY"}).raise_for_conflicts(),
            Unit("CNY"),
        )

    def test_parse_error_is_reported(self):
        report = check_units("amount +", names={"amount": "CNY"})
        self.assertFalse(report.ok)
        self.assertIsNotNone(report.parse_error)

    def test_report_string_contains_both_units(self):
        report = check_units("amount + count", names={"amount": "CNY", "count": "件"})
        text = str(report)
        self.assertIn("CNY", text)
        self.assertIn("件", text)

    def test_reusable_checker(self):
        checker = UnitChecker(names={"amount": "CNY", "count": "件"})
        first = checker.check("amount + count")
        second = checker.check("amount / count")
        self.assertFalse(first.ok)
        self.assertTrue(second.ok)
        self.assertEqual(second.result_unit, Unit("CNY/件"))
        self.assertEqual(len(first.conflicts), 1)

    def test_parsed_ast_can_be_checked(self):
        from simpleeval import SimpleEval

        checker = UnitChecker(names={"amount": "CNY"})
        node = SimpleEval.parse("amount * 2")
        self.assertEqual(checker.check_ast(node).result_unit, Unit("CNY"))


if __name__ == "__main__":
    unittest.main()
