import simpleeval
from simpleeval import (
    CoalesceAllMissing,
    InvalidExpression,
    PathNotFound,
    PathTypeError,
    simple_eval,
)

from .base import DRYTest


class TestPath(DRYTest):
    def test_follows_dict_and_list_path(self):
        self.s.names = {"order": {"items": [{"city": "Berlin"}]}}
        self.t("path(order, 'items', 0, 'city')", "Berlin")

    def test_empty_path_returns_root(self):
        data = {"a": 1}
        self.s.names = {"data": data}
        self.t("path(data)", data)

    def test_explicit_null_is_a_value(self):
        self.s.names = {"order": {"address": {"city": None}}}
        self.assertIsNone(self.s.eval("path(order, 'address', 'city')"))

    def test_empty_string_is_a_value(self):
        self.s.names = {"order": {"address": {"city": ""}}}
        self.t("path(order, 'address', 'city')", "")

    def test_zero_is_a_value(self):
        self.s.names = {"order": {"address": {"city": 0}}}
        self.t("path(order, 'address', 'city')", 0)

    def test_false_is_a_value(self):
        self.s.names = {"order": {"address": {"city": False}}}
        self.assertIs(self.s.eval("path(order, 'address', 'city')"), False)

    def test_missing_key_reports_where_it_stopped(self):
        self.s.names = {"order": {"a": {}}}
        with self.assertRaises(PathNotFound) as context:
            self.s.eval("path(order, 'a', 'b', 'c')")
        error = context.exception
        self.assertEqual(error.segments, ["a", "b", "c"])
        self.assertEqual(error.stopped_at, 1)
        self.assertEqual(error.segment, "b")
        self.assertIsInstance(error, InvalidExpression)

    def test_index_out_of_range_reports_where_it_stopped(self):
        self.s.names = {"order": {"items": []}}
        with self.assertRaises(PathNotFound) as context:
            self.s.eval("path(order, 'items', 3)")
        error = context.exception
        self.assertEqual(error.segments, ["items", 3])
        self.assertEqual(error.stopped_at, 1)
        self.assertEqual(error.segment, 3)

    def test_string_instead_of_object_is_corrupt_data(self):
        self.s.names = {"order": {"address": "12 Main St"}}
        with self.assertRaises(PathTypeError) as context:
            self.s.eval("path(order, 'address', 'city')")
        error = context.exception
        self.assertEqual(error.segments, ["address", "city"])
        self.assertEqual(error.stopped_at, 1)
        self.assertEqual(error.segment, "city")

    def test_none_instead_of_object_is_corrupt_data(self):
        self.s.names = {"order": {"address": None}}
        with self.assertRaises(PathTypeError):
            self.s.eval("path(order, 'address', 'city')")

    def test_string_root_is_corrupt_data(self):
        self.s.names = {"order": "Berlin"}
        with self.assertRaises(PathTypeError):
            self.s.eval("path(order, 'city')")

    def test_string_index_on_sequence_is_corrupt_data(self):
        self.s.names = {"order": {"items": [1, 2]}}
        with self.assertRaises(PathTypeError):
            self.s.eval("path(order, 'items', 'zero')")

    def test_string_indexing_is_never_implicit(self):
        # Even though Python supports "abc"[0], a str reached on a path is data
        # corruption, not a container to descend into.
        self.s.names = {"order": {"address": "Berlin"}}
        with self.assertRaises(PathTypeError):
            self.s.eval("path(order, 'address', 0)")

    def test_unhashable_mapping_key_is_corrupt_data(self):
        evaluator = simpleeval.EvalWithCompoundTypes()
        evaluator.names = {"order": {"address": {}}}
        with self.assertRaises(PathTypeError) as context:
            evaluator.eval("path(order, 'address', ['city'])")
        self.assertEqual(context.exception.stopped_at, 1)


class TestCoalesce(DRYTest):
    def test_first_path_wins(self):
        self.s.names = {"order": {"address": {"city": "Berlin"}, "shipping": {"city": "Hamburg"}}}
        self.t(
            "coalesce(path(order, 'address', 'city'), path(order, 'shipping', 'city'))",
            "Berlin",
        )

    def test_falls_back_to_second_path(self):
        self.s.names = {"order": {"address": {}, "shipping": {"city": "Hamburg"}}}
        self.t(
            "coalesce(path(order, 'address', 'city'), path(order, 'shipping', 'city'))",
            "Hamburg",
        )

    def test_explicit_null_stops_the_scan(self):
        calls = []

        def expensive():
            calls.append("called")
            return "Hamburg"

        self.s.functions["expensive"] = expensive
        self.s.names = {"order": {"address": {"city": None}, "shipping": {}}}
        self.assertIsNone(
            self.s.eval(
                "coalesce(path(order, 'address', 'city'), "
                "path(order, 'shipping', 'city'), expensive())"
            )
        )
        self.assertEqual(calls, [])

    def test_empty_string_and_zero_stop_the_scan(self):
        self.s.names = {"order": {"address": {"city": ""}, "shipping": {"city": "Hamburg"}}}
        self.t(
            "coalesce(path(order, 'address', 'city'), path(order, 'shipping', 'city'))",
            "",
        )

        self.s.names = {"order": {"address": {"city": 0}, "shipping": {"city": 2}}}
        self.t(
            "coalesce(path(order, 'address', 'city'), path(order, 'shipping', 'city'))",
            0,
        )

    def test_later_arguments_are_not_evaluated(self):
        calls = []

        def expensive(value):
            calls.append(value)
            return value

        self.s.functions["expensive"] = expensive
        self.s.names = {"order": {"address": {"city": "Berlin"}, "shipping": {}}}
        result = self.s.eval("coalesce(path(order, 'address', 'city'), expensive('fallback'))")
        self.assertEqual(result, "Berlin")
        self.assertEqual(calls, [])

    def test_fallback_is_evaluated_only_when_needed(self):
        calls = []

        def expensive(value):
            calls.append(value)
            return value

        self.s.functions["expensive"] = expensive
        self.s.names = {"order": {"address": {}, "shipping": {}}}
        result = self.s.eval("coalesce(path(order, 'address', 'city'), expensive('fallback'))")
        self.assertEqual(result, "fallback")
        self.assertEqual(calls, ["fallback"])

    def test_all_missing_reports_each_stop_position(self):
        self.s.names = {"order": {"address": {}, "shipping": {}}}
        with self.assertRaises(CoalesceAllMissing) as context:
            self.s.eval(
                "coalesce(path(order, 'address', 'city'), path(order, 'shipping', 'city'))"
            )
        failures = context.exception.failures
        self.assertEqual(len(failures), 2)
        self.assertEqual(failures[0].segments, ["address", "city"])
        self.assertEqual(failures[0].stopped_at, 1)
        self.assertEqual(failures[1].segments, ["shipping", "city"])
        self.assertEqual(failures[1].stopped_at, 1)
        self.assertIsInstance(context.exception, InvalidExpression)

    def test_falls_back_to_plain_value(self):
        self.s.names = {"order": {"address": {}}}
        self.t("coalesce(path(order, 'address', 'city'), 'unknown')", "unknown")

    def test_corrupt_data_is_not_swallowed(self):
        # A type error means broken data, not a missing optional path: it must
        # propagate rather than silently moving to the next argument.
        self.s.names = {"order": {"address": "oops", "shipping": {"city": "Hamburg"}}}
        with self.assertRaises(PathTypeError):
            self.s.eval(
                "coalesce(path(order, 'address', 'city'), path(order, 'shipping', 'city'))"
            )

    def test_unrelated_errors_propagate(self):
        def boom():
            raise ValueError("kaboom")

        self.s.functions["boom"] = boom
        self.s.names = {"order": {"address": {}}}
        with self.assertRaises(ValueError):
            self.s.eval("coalesce(path(order, 'address', 'city'), boom())")

    def test_requires_arguments(self):
        with self.assertRaises(InvalidExpression):
            self.s.eval("coalesce()")

    def test_rejects_keyword_arguments(self):
        with self.assertRaises(InvalidExpression):
            self.s.eval("coalesce(path(order, 'a'), fallback=1)")


class TestSimpleEvalFunction(DRYTest):
    def test_supplier_scenario_end_to_end(self):
        supplier_payload = {"address": {"city": None}, "shipping": {"city": "Potsdam"}}
        # Explicit null wins and must not trigger the fallback lookup.
        result = simple_eval(
            "coalesce(path(payload, 'address', 'city'), path(payload, 'shipping', 'city'))",
            names={"payload": supplier_payload},
        )
        self.assertIsNone(result)

        supplier_payload = {"address": {}, "shipping": {"city": "Potsdam"}}
        result = simple_eval(
            "coalesce(path(payload, 'address', 'city'), path(payload, 'shipping', 'city'))",
            names={"payload": supplier_payload},
        )
        self.assertEqual(result, "Potsdam")

        supplier_payload = {"address": {}, "shipping": {}}
        with self.assertRaises(simpleeval.CoalesceAllMissing):
            simple_eval(
                "coalesce(path(payload, 'address', 'city'), path(payload, 'shipping', 'city'))",
                names={"payload": supplier_payload},
            )
