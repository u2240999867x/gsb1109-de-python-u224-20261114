import unittest

from simpleeval import (
    EvalWithCompoundTypes,
    NameNotDefined,
    SimpleEval,
    UnknownDependent,
    simple_eval,
    unknown,
)


class TernaryTestBase:
    def make_eval(self, names=None):
        raise NotImplementedError

    def result(self, expr, names=None):
        evaluator = self.make_eval(names)
        return evaluator.eval(expr)

    def pending(self, expr, names=None):
        """Return the unknown_names of the expected UnknownDependent error."""
        evaluator = self.make_eval(names)
        with self.assertRaises(UnknownDependent) as context:
            evaluator.eval(expr)
        return context.exception.unknown_names

    # basics ---------------------------------------------------------------

    def test_default_mode_still_raises_name_not_defined(self):
        evaluator = SimpleEval()
        self.assertFalse(evaluator.ternary)
        with self.assertRaises(NameNotDefined):
            evaluator.eval("missing")

    def test_known_expression_evaluates_normally(self):
        self.assertEqual(self.result("1 + 2"), 3)
        self.assertEqual(self.result("a * 2", {"a": 21}), 42)

    def test_bare_missing_name_is_pending(self):
        self.assertEqual(self.pending("eligible"), ("eligible",))

    def test_missing_names_are_deduplicated_and_ordered(self):
        self.assertEqual(self.pending("a or a"), ("a",))
        self.assertEqual(self.pending("b or a"), ("b", "a"))

    # and / or kleene logic ------------------------------------------------

    def test_unknown_and_false_is_definitely_false(self):
        self.assertFalse(self.result("eligible and False"))
        self.assertFalse(self.result("missing and False"))

    def test_false_and_unknown_short_circuits_without_visiting_unknown(self):
        evaluator = self.make_eval()
        self.assertFalse(evaluator.eval("False and missing"))
        self.assertEqual(evaluator._unknown_names, [])

    def test_unknown_or_false_stays_pending(self):
        self.assertEqual(self.pending("missing or False"), ("missing",))

    def test_unknown_or_true_is_definitely_true(self):
        self.assertTrue(self.result("missing or True"))
        self.assertTrue(self.result("False or True"))

    def test_true_or_unknown_short_circuits_without_visiting_unknown(self):
        evaluator = self.make_eval()
        self.assertTrue(evaluator.eval("True or missing"))
        self.assertEqual(evaluator._unknown_names, [])

    def test_unknown_and_true_stays_pending(self):
        self.assertEqual(self.pending("missing and True"), ("missing",))

    def test_nested_and_or(self):
        # settled by the final False, even though both missing names were seen
        evaluator = self.make_eval()
        self.assertFalse(evaluator.eval("(a or b) and False"))
        self.assertEqual(evaluator._unknown_names, ["a", "b"])

        # the missing names sit behind a short-circuited branch: never visited
        evaluator = self.make_eval()
        self.assertFalse(evaluator.eval("False and (a or b)"))
        self.assertEqual(evaluator._unknown_names, [])

    # function call protection ---------------------------------------------

    def test_functions_after_a_settled_result_are_not_called(self):
        calls = []

        def fetch(value):
            calls.append(value)
            return value

        evaluator = self.make_eval()
        evaluator.functions["fetch"] = fetch

        self.assertFalse(evaluator.eval("False and fetch(1)"))
        self.assertTrue(evaluator.eval("True or fetch(2)"))
        self.assertEqual(calls, [])

    def test_function_with_unknown_argument_is_not_called(self):
        calls = []

        def fetch(value):
            calls.append(value)
            return value

        evaluator = self.make_eval()
        evaluator.functions["fetch"] = fetch

        self.assertTrue(evaluator.eval("fetch(missing) or True"))
        self.assertEqual(calls, [])
        self.assertEqual(evaluator._unknown_names, ["missing"])

        with self.assertRaises(UnknownDependent):
            evaluator.eval("fetch(missing) or False")
        self.assertEqual(calls, [])

    # unary not -------------------------------------------------------------

    def test_not_unknown_propagates(self):
        self.assertEqual(self.pending("not missing"), ("missing",))
        self.assertTrue(self.result("not missing or True"))
        self.assertFalse(self.result("not missing and False"))

    # comparisons -----------------------------------------------------------

    def test_comparison_with_unknown(self):
        self.assertEqual(self.pending("missing == 1"), ("missing",))
        self.assertTrue(self.result("missing == 1 or True"))
        self.assertEqual(self.pending("1 == 2 or missing"), ("missing",))

    def test_chained_comparison_short_circuits_like_and(self):
        self.assertEqual(self.pending("1 < 2 < missing"), ("missing",))

        evaluator = self.make_eval()
        self.assertFalse(evaluator.eval("2 < 1 < missing"))
        self.assertEqual(evaluator._unknown_names, [])

    # if-expressions --------------------------------------------------------

    def test_if_expression_with_unknown_test_skips_both_branches(self):
        self.assertEqual(self.pending("1 if missing else 2"), ("missing",))
        self.assertEqual(self.result("1 if True else missing"), 1)
        self.assertEqual(self.result("missing if False else 42"), 42)

    # arithmetic propagation ------------------------------------------------

    def test_arithmetic_with_unknown_propagates(self):
        self.assertTrue(self.result("(missing + 1) or True"))
        self.assertEqual(self.pending("(missing + 1) or False"), ("missing",))

    # the actual ordering-cleaning scenario --------------------------------

    def test_explicit_false_is_different_from_missing_eligibility(self):
        # eligibility explicitly supplied as False settles the filter
        evaluator = self.make_eval({"eligible": False})
        self.assertFalse(evaluator.eval("eligible and other_flag"))
        self.assertEqual(evaluator._unknown_names, [])

        # eligibility omitted entirely: both names are visited, so both
        # missing columns genuinely affect the undecidable result
        self.assertEqual(self.pending("eligible and other_flag"), ("eligible", "other_flag"))

    # bookkeeping -----------------------------------------------------------

    def test_unknown_names_reset_between_runs(self):
        evaluator = self.make_eval()
        with self.assertRaises(UnknownDependent):
            evaluator.eval("missing")
        self.assertEqual(evaluator.eval("1 + 1"), 2)
        self.assertEqual(evaluator._unknown_names, [])

    def test_callable_names_resolver_name_not_defined_becomes_unknown(self):
        def resolver(node):
            raise NameNotDefined(node.id, self.expr)

        evaluator = self.make_eval()
        evaluator.names = resolver
        self.assertEqual(self.pending("missing"), ("missing",))

    def test_unknown_singleton_has_no_truthiness(self):
        with self.assertRaises(TypeError):
            bool(unknown)

    # propagation through other nodes ---------------------------------------

    def test_subscript_with_unknown_propagates(self):
        self.assertEqual(self.pending("records[missing]", {"records": [1, 2]}), ("missing",))
        self.assertEqual(self.pending("missing[0]", {"records": [1, 2]}), ("missing",))

    def test_attribute_access_on_unknown_propagates(self):
        self.assertEqual(self.pending("missing.field"), ("missing",))

    def test_method_call_on_unknown_is_not_invoked(self):
        self.assertEqual(self.pending("missing.upper()"), ("missing",))

    def test_fstring_with_unknown_propagates(self):
        self.assertEqual(self.pending("f'value: {missing}'"), ("missing",))
        self.assertEqual(self.pending("f'{missing:0.2f}'"), ("missing",))
        self.assertTrue(self.result("f'{missing}' or True"))


class TestTernary(TernaryTestBase, unittest.TestCase):
    def make_eval(self, names=None):
        return SimpleEval(names=names, ternary=True)


class TestTernaryCompoundTypes(TernaryTestBase, unittest.TestCase):
    def make_eval(self, names=None):
        return EvalWithCompoundTypes(names=names, ternary=True)

    def test_collection_literal_with_unknown_propagates(self):
        self.assertEqual(self.pending("[1, missing, 3]"), ("missing",))
        self.assertEqual(self.pending("(1, missing)"), ("missing",))
        self.assertEqual(self.pending("{1, missing}"), ("missing",))
        self.assertEqual(self.pending("{'a': missing}"), ("missing",))
        self.assertEqual(self.pending("[*missing]"), ("missing",))
        self.assertEqual(self.pending("{**missing}"), ("missing",))
        self.assertTrue(self.result("[1, missing] or True"))

    def test_comprehension_with_unknown_iterable_propagates(self):
        self.assertEqual(self.pending("[x for x in missing]"), ("missing",))

    def test_comprehension_with_unknown_filter_propagates(self):
        self.assertEqual(
            self.pending("[x for x in items if missing]", {"items": [1, 2]}),
            ("missing",),
        )

    def test_comprehension_with_unknown_element_propagates(self):
        self.assertEqual(
            self.pending("[missing for x in items]", {"items": [1, 2]}),
            ("missing",),
        )

    def test_known_comprehension_still_works(self):
        self.assertEqual(
            self.result("[x * 2 for x in items if x > 1]", {"items": [1, 2, 3]}),
            [4, 6],
        )


class TestTernarySimpleEvalFunction(unittest.TestCase):
    def test_simple_eval_passes_ternary_through(self):
        self.assertTrue(simple_eval("missing or True", ternary=True))
        with self.assertRaises(UnknownDependent):
            simple_eval("missing or False", ternary=True)

    def test_simple_eval_default_unchanged(self):
        with self.assertRaises(NameNotDefined):
            simple_eval("missing")
