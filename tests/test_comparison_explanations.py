import unittest

from simpleeval import (
    ComparisonExplanation,
    ComparisonSegment,
    EvalWithCompoundTypes,
    SimpleEval,
    explain_comparison,
    simple_eval,
)

from .base import DRYTest


class TestComparisonExplanation(DRYTest):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.s.names = {"weight": 5}
        self.s.functions["cap"] = self.cap

    def cap(self):
        self.calls.append("cap")
        return 10

    def test_default_eval_still_returns_plain_value(self):
        self.assertIs(self.s.eval("0 < weight <= cap()"), True)
        self.assertEqual(self.calls, ["cap"])

        self.assertIs(simple_eval("1 < 2"), True)

    def test_explanation_value_matches_normal_eval(self):
        expression = "0 < weight <= cap()"
        explanation = self.s.eval(expression, explain=True)

        self.assertIsInstance(explanation, ComparisonExplanation)
        self.assertIs(explanation.value, True)
        self.assertIs(explanation.result, True)
        self.assertIsNone(explanation.failed_segment)
        self.assertIsNone(explanation.failed_position)

    def test_each_segment_reports_both_sides_truth_and_position(self):
        expression = "0 < weight <= cap()"
        explanation = self.s.eval(expression, explain=True)

        first, second = explanation.segments
        self.assertIsInstance(first, ComparisonSegment)

        self.assertEqual(first.left, 0)
        self.assertEqual(first.right, 5)
        self.assertIs(first.result, True)
        self.assertIs(first.passed, True)
        self.assertEqual(first.operator, "<")
        self.assertEqual(first.index, 0)
        self.assertEqual(first.position, 1)
        self.assertEqual(first.text, "0 < weight")
        self.assertEqual((first.line, first.column), (1, 0))
        self.assertEqual((first.end_line, first.end_column), (1, 10))

        self.assertEqual(second.left, 5)
        self.assertEqual(second.right, 10)
        self.assertIs(second.result, True)
        self.assertIs(second.passed, True)
        self.assertEqual(second.operator, "<=")
        self.assertEqual(second.index, 1)
        self.assertEqual(second.position, 2)
        self.assertEqual(second.text, "weight <= cap()")
        self.assertEqual((second.line, second.column), (1, 4))
        self.assertEqual((second.end_line, second.end_column), (1, 19))

    def test_report_identifies_the_failing_clause(self):
        self.s.names = {"weight": 50}
        explanation = self.s.eval("0 < weight <= cap()", explain=True)

        self.assertIs(explanation.value, False)
        self.assertEqual(len(explanation.segments), 2)
        self.assertEqual(explanation.failed_index, 1)
        self.assertEqual(explanation.failed_position, 2)
        self.assertEqual(explanation.failed_segment.text, "weight <= cap()")
        self.assertIs(explanation.failed_segment.left, 50)
        self.assertIs(explanation.failed_segment.right, 10)
        self.assertIs(explanation.failed_segment.passed, False)

    def test_failed_step_does_not_evaluate_the_rest_of_the_chain(self):
        self.s.names = {"weight": 50}
        self.s.functions["explode"] = self.explode

        explanation = self.s.eval("0 < weight <= cap() < explode()", explain=True)

        self.assertIs(explanation.value, False)
        self.assertEqual(self.calls, ["cap"])
        self.assertEqual(
            [segment.text for segment in explanation.segments],
            [
                "0 < weight",
                "weight <= cap()",
            ],
        )

    def explode(self):  # pragma: no cover - must never run
        raise AssertionError("explode() should never be evaluated")

    def test_skipped_right_hand_side_is_not_evaluated_for_explanation(self):
        self.s.names = {"weight": -1}

        explanation = self.s.eval("0 < weight <= cap()", explain=True)

        self.assertIs(explanation.value, False)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(explanation.segments), 1)
        self.assertEqual(explanation.segments[0].text, "0 < weight")
        self.assertEqual(explanation.failed_position, 1)

    def test_volatile_middle_operand_is_evaluated_once(self):
        values = iter([5, 6, 7])

        def fetch():
            self.calls.append("fetch")
            return next(values)

        self.s.functions["fetch"] = fetch
        explanation = self.s.eval("0 < fetch() <= 10", explain=True)

        self.assertEqual(self.calls, ["fetch"])
        self.assertIs(explanation.value, True)
        self.assertEqual(explanation.segments[0].right, 5)
        self.assertEqual(explanation.segments[1].left, 5)

        self.assertEqual(self.s.eval("0 < fetch() <= 10"), True)
        self.assertEqual(self.calls, ["fetch", "fetch"])
        self.assertEqual(self.s.eval("0 < fetch() <= 10"), True)
        self.assertEqual(self.calls, ["fetch", "fetch", "fetch"])

    def test_volatile_middle_operand_once_with_custom_operator(self):
        values = iter([5, 6, 7])

        def fetch():
            self.calls.append("fetch")
            return next(values)

        seen = []

        def record_eq(left, right):
            seen.append((left, right))
            return left == right

        self.s.functions["fetch"] = fetch
        self.s.operators[type(__import__("ast").Eq())] = record_eq
        explanation = self.s.eval("fetch() == 5", explain=True)

        self.assertEqual(self.calls, ["fetch"])
        self.assertEqual(seen, [(5, 5)])
        self.assertIs(explanation.value, True)

    def test_explanation_for_unrelated_expression_is_empty(self):
        explanation = self.s.eval("weight + 1", explain=True)

        self.assertEqual(explanation.value, 6)
        self.assertEqual(explanation.segments, [])
        self.assertIsNone(explanation.failed_position)

    def test_explanation_supports_dict_and_tuple_access(self):
        explanation = self.s.eval("0 < weight <= cap()", explain=True)
        value, segments = explanation

        self.assertIs(value, True)
        self.assertIs(segments, explanation.segments)
        self.assertIs(explanation["value"], True)
        self.assertIs(explanation["segments"], explanation.segments)
        self.assertEqual(explanation.segments[0]["left"], 0)
        self.assertEqual(explanation.segments[0]["right"], 5)
        self.assertIs(explanation.segments[0]["passed"], True)
        self.assertEqual(explanation.segments[0]["position"], 1)

        data = explanation.as_dict()
        self.assertIs(data["value"], True)
        self.assertEqual(data["segments"][1]["text"], "weight <= cap()")
        self.assertEqual(data["failed_position"], None)

    def test_explanation_constructor_flag(self):
        s = SimpleEval(names={"weight": 5}, functions={"cap": self.cap}, explain=True)
        explanation = s.eval("0 < weight <= cap()")
        self.assertIsInstance(explanation, ComparisonExplanation)

        # Per-call override still gets the plain value back.
        self.assertIs(s.eval("0 < weight <= cap()", explain=False), True)

    def test_explanation_entry_points(self):
        for result in (
            self.s.eval_with_explanation("0 < weight <= cap()"),
            self.s.eval_with_details("0 < weight <= cap()"),
            self.s.explain("0 < weight <= cap()"),
            explain_comparison(
                "0 < weight <= cap()",
                names={"weight": 5},
                functions={"cap": self.cap},
            ),
        ):
            self.assertIsInstance(result, ComparisonExplanation)
            self.assertIs(result.value, True)
            self.assertEqual(len(result.segments), 2)

    def test_last_explanation_is_available(self):
        self.assertIsNone(self.s.last_explanation)
        explanation = self.s.eval("0 < weight <= cap()", explain=True)
        self.assertIs(self.s.last_explanation, explanation)

    def test_compound_types_and_comprehensions(self):
        s = EvalWithCompoundTypes(names={"items": [0, 2, 4]})
        explanation = s.eval("[x for x in items if 0 <= x <= 4]", explain=True)

        self.assertEqual(explanation.value, [0, 2, 4])
        self.assertEqual(len(explanation.segments), 6)

    def test_non_boolable_result_is_preserved(self):
        class Node:
            def __lt__(self, other):
                return self

            def __bool__(self):
                raise TypeError("not boolable")

        node = Node()
        self.s.names = {"node": node}
        explanation = self.s.eval("node < 2", explain=True)

        self.assertIs(explanation.value, node)
        self.assertIs(explanation.segments[0].result, node)
        self.assertIsNone(explanation.segments[0].passed)
        self.assertIsNone(explanation.failed_position)

    def test_nested_comparisons_are_all_recorded(self):
        explanation = self.s.eval("(1 < 2) == (3 < 4)", explain=True)

        self.assertIs(explanation.value, True)
        self.assertEqual(
            [segment.text for segment in explanation.segments],
            [
                "1 < 2",
                "3 < 4",
                "1 < 2) == (3 < 4",
            ],
        )

    def test_in_and_is_operators_are_spelled_out(self):
        s = EvalWithCompoundTypes(names={"weight": 5, "same": 5})
        explanation = s.eval("2 in (1, 2) and weight is same", explain=True)

        self.assertIs(explanation.value, True)
        self.assertEqual([segment.operator for segment in explanation.segments], ["in", "is"])
        first, second = explanation.segments
        self.assertEqual(first.left, 2)
        self.assertEqual(first.right, (1, 2))
        self.assertEqual(second.text, "weight is same")

    def test_short_circuit_explanation_keeps_earlier_segments(self):
        self.s.names = {"weight": 5}
        explanation = self.s.eval("weight == 5 == 5 == 5", explain=True)

        self.assertIs(explanation.value, True)
        self.assertEqual(len(explanation.segments), 3)

        self.s.names = {"weight": 4}
        explanation = self.s.eval("weight == 5 == 5 == 5", explain=True)
        self.assertIs(explanation.value, False)
        self.assertEqual(len(explanation.segments), 1)
        self.assertEqual(explanation.failed_position, 1)

    def test_nested_eval_does_not_leak_into_outer_trace(self):
        inner_results = []

        def nested():
            inner_results.append(self.s.eval("1 < 2"))
            return 3

        self.s.functions["nested"] = nested
        explanation = self.s.eval("1 < nested() < 4", explain=True)

        self.assertIs(explanation.value, True)
        self.assertEqual(inner_results, [True])
        self.assertEqual(
            [segment.text for segment in explanation.segments],
            [
                "1 < nested()",
                "nested() < 4",
            ],
        )

    def test_explanation_is_reset_between_runs(self):
        self.s.eval("0 < weight <= cap()", explain=True)
        self.assertEqual(len(self.s.last_explanation.segments), 2)

        plain = self.s.eval("1 < 2")
        self.assertIs(plain, True)
        self.assertIsNone(self.s.last_explanation)

        explanation = self.s.eval("1 < 2", explain=True)
        self.assertIs(self.s.last_explanation, explanation)
        self.assertEqual(len(explanation.segments), 1)

    def test_eval_error_cleans_up_explanation_state(self):
        self.s.functions["boom"] = self.boom
        with self.assertRaises(RuntimeError):
            self.s.eval("1 < boom()", explain=True)

        self.assertEqual(self.s._comparison_log, [])
        self.assertFalse(self.s._explain_enabled)
        self.assertIs(self.s.eval("1 < 2"), True)

    def boom(self):
        raise RuntimeError("boom")


class TestExplanationWithParsedNodes(unittest.TestCase):
    def test_previously_parsed_is_explained_too(self):
        s = SimpleEval(names={"weight": 5})
        parsed = s.parse("0 < weight <= 10")
        explanation = s.eval("0 < weight <= 10", previously_parsed=parsed, explain=True)

        self.assertIs(explanation.value, True)
        self.assertEqual(len(explanation.segments), 2)
        self.assertEqual(explanation.segments[1].text, "weight <= 10")
