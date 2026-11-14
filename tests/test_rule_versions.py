import json
import threading
import unittest

from simpleeval import EvalWithCompoundTypes, InvalidExpression, NameNotDefined

from rule_versions import (
    CyclicRuleReference,
    DuplicateRule,
    EmptyRuleSet,
    InvalidRuleSet,
    MissingRuleReference,
    OrderEvaluation,
    Rule,
    RuleSet,
    RuleSetVersion,
    VersionNotFound,
    compute_checksum,
)


V1_RULES = {
    "base_fee": "amount * 0.05",
    "surcharge": "base_fee + (10 if region == 'VIP' else 0)",
    "total": "round(surcharge + amount, 2)",
}

V2_RULES = {
    "base_fee": "amount * 0.08",
    "surcharge": "base_fee + (15 if region == 'VIP' else 0)",
    "total": "round(surcharge + amount, 2)",
}

ORDER = {"order_id": "ORD-1", "amount": 100, "region": "VIP"}


def make_ruleset(rules=V1_RULES, externals=("amount", "region")):
    rule_set = RuleSet(functions={"round": round}, externals=externals)
    rule_set.replace_rules(rules)
    return rule_set


class RuleDraftTests(unittest.TestCase):
    def test_set_and_replace_draft(self):
        rule_set = RuleSet(externals=["amount", "region"])
        rule_set.set_rule("base_fee", "amount * 0.05")
        rule_set.set_rule("surcharge", Rule("surcharge", "base_fee + 1"))
        self.assertEqual(set(rule_set.draft), {"base_fee", "surcharge"})

        rule_set.replace_rules([Rule("a", "1"), Rule("b", "a + 1")])
        self.assertEqual(tuple(sorted(rule_set.draft)), ("a", "b"))

    def test_replace_rules_rejects_duplicates(self):
        rule_set = RuleSet()
        with self.assertRaises(DuplicateRule):
            rule_set.replace_rules([Rule("a", "1"), Rule("a", "2")])

    def test_rule_name_mismatch_rejected(self):
        rule_set = RuleSet()
        with self.assertRaises(InvalidRuleSet):
            rule_set.set_rule("a", Rule("b", "1"))


class ValidationTests(unittest.TestCase):
    def test_publish_empty_rejected(self):
        rule_set = RuleSet()
        with self.assertRaises(EmptyRuleSet):
            rule_set.publish()

    def test_missing_reference_rejected(self):
        rule_set = RuleSet(externals=["amount"])
        rule_set.replace_rules({"total": "amount + missing_rule"})
        with self.assertRaises(MissingRuleReference) as ctx:
            rule_set.publish()
        self.assertEqual(set(ctx.exception.missing), {"missing_rule"})

    def test_dropping_referenced_rule_rejects_publish(self):
        rule_set = make_ruleset()
        self.assertEqual(rule_set.publish("v1"), 1)

        new_draft = dict(V2_RULES)
        del new_draft["base_fee"]
        rule_set.replace_rules(new_draft)
        with self.assertRaises(MissingRuleReference):
            rule_set.publish("broken v2")

        self.assertEqual(rule_set.versions(), (1,))
        self.assertEqual(rule_set.current_version, 1)

    def test_external_names_are_accepted(self):
        rule_set = RuleSet(externals=["amount", "region"])
        rule_set.replace_rules({"r": "amount if region == 'X' else 0"})
        self.assertEqual(rule_set.publish(), 1)

    def test_unparseable_expression_rejected(self):
        rule_set = RuleSet(externals=["amount"])
        rule_set.replace_rules({"bad": "amount +"})
        with self.assertRaises(InvalidRuleSet):
            rule_set.publish()

    def test_invalid_rule_name_rejected(self):
        rule_set = RuleSet()
        rule_set.set_rule("1bad", "1")
        with self.assertRaises(InvalidRuleSet):
            rule_set.publish()

    def test_cycle_rejected(self):
        rule_set = RuleSet()
        rule_set.replace_rules({"a": "b + 1", "b": "c + 1", "c": "a + 1"})
        with self.assertRaises(CyclicRuleReference) as ctx:
            rule_set.publish()
        self.assertEqual(ctx.exception.cycle[0], ctx.exception.cycle[-1])

    def test_self_cycle_rejected(self):
        rule_set = RuleSet()
        rule_set.replace_rules({"a": "a + 1"})
        with self.assertRaises(CyclicRuleReference):
            rule_set.publish()

    def test_function_calls_not_treated_as_references(self):
        def double(value):
            return value * 2

        rule_set = RuleSet(functions={"double": double}, externals=["amount"])
        rule_set.replace_rules({"r": "double(amount)"})
        self.assertEqual(rule_set.publish(), 1)
        self.assertEqual(rule_set.evaluate("r", {"amount": 21}), 42)

    def test_comprehension_bindings_not_references(self):
        rule_set = RuleSet(
            functions={"sum": sum},
            externals=["values"],
            evaluator_class=EvalWithCompoundTypes,
        )
        rule_set.replace_rules({"total": "sum([item * 2 for item in values])"})
        self.assertEqual(rule_set.publish(), 1)
        self.assertEqual(rule_set.evaluate("total", {"values": [1, 2, 3]}), 12)


class PublicationTests(unittest.TestCase):
    def test_versions_are_sequential_and_immutable(self):
        rule_set = make_ruleset()
        self.assertEqual(rule_set.publish("first"), 1)
        self.assertEqual(rule_set.publish("second"), 2)
        self.assertEqual(rule_set.versions(), (1, 2))
        self.assertEqual(rule_set.current_version, 2)

        version_one = rule_set.get_version(1)
        with self.assertRaises(TypeError):
            version_one.rules["x"] = Rule("x", "1")
        with self.assertRaises(TypeError):
            version_one.parsed["x"] = None

    def test_editing_draft_does_not_change_published(self):
        rule_set = make_ruleset()
        rule_set.publish()
        original = rule_set.get_version(1).rules["base_fee"].expression

        rule_set.set_rule("base_fee", "amount * 0.99")
        self.assertEqual(rule_set.get_version(1).rules["base_fee"].expression, original)

    def test_unknown_version_raises(self):
        rule_set = make_ruleset()
        rule_set.publish()
        with self.assertRaises(VersionNotFound):
            rule_set.get_version(99)
        with self.assertRaises(VersionNotFound):
            RuleSet().get_version()

    def test_checksum_changes_only_with_expressions(self):
        rule_set = make_ruleset()
        rule_set.publish("a")
        checksum_one = rule_set.get_version(1).checksum

        rule_set.set_rule("base_fee", "amount * 0.05", description="annotated")
        rule_set.publish("b")
        self.assertEqual(rule_set.get_version(2).checksum, checksum_one)

        rule_set.set_rule("base_fee", "amount * 0.06")
        rule_set.publish("c")
        self.assertNotEqual(rule_set.get_version(3).checksum, checksum_one)
        self.assertEqual(
            rule_set.get_version(3).checksum,
            compute_checksum(rule_set.get_version(3).rules),
        )


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.rule_set = make_ruleset()
        self.rule_set.publish("v1")
        self.rule_set.replace_rules(V2_RULES)
        self.rule_set.publish("v2")

    def test_derived_rules_reference_each_other(self):
        evaluation = self.rule_set.evaluate_order(ORDER, target="total", version=1)
        self.assertEqual(evaluation.result, 115.0)
        self.assertEqual(
            evaluation.results,
            {"base_fee": 5.0, "surcharge": 15.0, "total": 115.0},
        )

    def test_order_is_pinned_to_one_version(self):
        pinned_v1 = self.rule_set.evaluate_order(ORDER, target="total", version=1)
        pinned_v2 = self.rule_set.evaluate_order(ORDER, target="total", version=2)
        current = self.rule_set.evaluate_order(ORDER, target="total")

        self.assertEqual(pinned_v1.result, 115.0)
        self.assertEqual(pinned_v2.result, 123.0)
        self.assertEqual(current.result, pinned_v2.result)
        self.assertEqual(pinned_v1.version, 1)
        self.assertEqual(pinned_v2.version, 2)

    def test_full_order_evaluates_every_rule(self):
        evaluation = self.rule_set.evaluate_order(ORDER)
        self.assertIsInstance(evaluation, OrderEvaluation)
        self.assertIsNone(evaluation.target)
        self.assertEqual(set(evaluation.results), {"base_fee", "surcharge", "total"})
        self.assertEqual(evaluation.order_id, "ORD-1")

    def test_missing_order_field_raises(self):
        with self.assertRaises(NameNotDefined):
            self.rule_set.evaluate_order({"amount": 100}, target="total", version=1)

    def test_unknown_rule_in_version_raises(self):
        with self.assertRaises(InvalidRuleSet):
            self.rule_set.evaluate_order(ORDER, target="nope")

    def test_replay_of_old_order_uses_old_expressions(self):
        result_before = self.rule_set.evaluate_order(ORDER, target="total", version=1)
        self.assertEqual(result_before.result, 115.0)

        self.rule_set.replace_rules(
            {
                "base_fee": "amount * 0.20",
                "surcharge": "base_fee",
                "total": "surcharge + amount",
            }
        )
        self.rule_set.publish("v3")

        replay = self.rule_set.evaluate_order(ORDER, target="total", version=1)
        self.assertEqual(replay.result, result_before.result)
        self.assertEqual(replay.checksum, result_before.checksum)
        self.assertEqual(replay.configuration["version"], 1)

    def test_evaluate_single_rule_helper(self):
        self.assertEqual(self.rule_set.evaluate("base_fee", ORDER, version=1), 5.0)
        self.assertEqual(self.rule_set.evaluate("base_fee", ORDER, version=2), 8.0)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.rule_set = make_ruleset()
        self.rule_set.publish("v1")

    def test_snapshot_is_complete_and_json_serialisable(self):
        snapshot = self.rule_set.snapshot(1)
        encoded = json.dumps(snapshot)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["format"], "simpleeval-ruleset/1")
        self.assertEqual(len(decoded["rules"]), 3)
        self.assertEqual(
            {rule["name"] for rule in decoded["rules"]},
            set(V1_RULES),
        )

    def test_snapshot_restores_and_reproduces_results(self):
        snapshot = self.rule_set.snapshot(1)
        original = self.rule_set.evaluate_order(ORDER, target="total", version=1)

        restored_set = RuleSet.from_snapshot(
            snapshot, functions={"round": round}, externals=["amount", "region"]
        )
        self.assertEqual(restored_set.restore(snapshot), 1)
        restored_set.publish("restored")
        replay = restored_set.evaluate_order(ORDER, target="total")

        self.assertEqual(replay.result, original.result)
        self.assertEqual(replay.checksum, original.checksum)
        self.assertEqual(
            restored_set.get_version(1).rules["total"].expression,
            V1_RULES["total"],
        )

    def test_from_snapshot_can_publish_directly(self):
        snapshot = self.rule_set.snapshot(1)
        restored_set = RuleSet.from_snapshot(
            snapshot, functions={"round": round}, publish=True
        )
        self.assertEqual(restored_set.versions(), (1,))
        self.assertEqual(restored_set.evaluate_order(ORDER, target="total").result, 115.0)

    def test_tampered_snapshot_rejected(self):
        snapshot = self.rule_set.snapshot(1)
        snapshot["rules"][0]["expression"] = "amount * 0.99"
        with self.assertRaises(InvalidRuleSet):
            RuleSetVersion.from_dict(snapshot)

    def test_unknown_snapshot_format_rejected(self):
        snapshot = self.rule_set.snapshot(1)
        snapshot["format"] = "something-else/9"
        with self.assertRaises(InvalidRuleSet):
            RuleSetVersion.from_dict(snapshot)

    def test_every_result_carries_restorable_configuration(self):
        evaluation = self.rule_set.evaluate_order(ORDER, target="total")
        rebuilt = RuleSet.from_snapshot(
            dict(evaluation.configuration), functions={"round": round}, publish=True
        )
        self.assertEqual(
            rebuilt.evaluate_order(ORDER, target="total").result,
            evaluation.result,
        )


class ConcurrencyTests(unittest.TestCase):
    def test_workers_keep_consistent_version_across_switch(self):
        rule_set = make_ruleset()
        rule_set.publish("v1")
        start = threading.Event()
        results = []
        results_lock = threading.Lock()

        def worker(order):
            start.wait()
            pinned = rule_set.get_version()
            evaluation = rule_set.evaluate_order(order, target="total", version=pinned.version)
            with results_lock:
                results.append((evaluation.version, evaluation.result, evaluation.checksum))

        threads = [threading.Thread(target=worker, args=(ORDER,)) for _ in range(8)]
        for thread in threads:
            thread.start()
        start.set()
        for thread in threads:
            thread.join()

        rule_set.replace_rules(V2_RULES)
        self.assertEqual(rule_set.publish("v2"), 2)
        late = rule_set.evaluate_order(ORDER, target="total")

        self.assertTrue(all(version == 1 for version, _, _ in results))
        self.assertTrue(all(result == 115.0 for _, result, _ in results))
        self.assertEqual(late.version, 2)
        self.assertEqual(late.result, 123.0)

    def test_concurrent_publish_and_evaluate(self):
        rule_set = make_ruleset()
        rule_set.publish("v1")
        stop = threading.Event()
        errors = []

        def publisher():
            draft = dict(V1_RULES)
            counter = 0
            while not stop.is_set():
                counter += 1
                draft["base_fee"] = f"amount * {0.01 + counter * 0.001}"
                rule_set.replace_rules(dict(draft))
                try:
                    rule_set.publish(f"v{counter}")
                except InvalidRuleSet as exc:
                    errors.append(exc)

        def evaluator():
            while not stop.is_set():
                version = rule_set.current_version
                evaluation = rule_set.evaluate_order(ORDER, target="total", version=version)
                expected_checksum = rule_set.get_version(version).checksum
                if evaluation.checksum != expected_checksum:
                    errors.append("checksum mismatch mid-evaluation")

        threads = [threading.Thread(target=publisher), threading.Thread(target=evaluator)]
        for thread in threads:
            thread.start()
        stop.wait(0.5)
        stop.set()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertGreater(len(rule_set.versions()), 1)


if __name__ == "__main__":
    unittest.main()
