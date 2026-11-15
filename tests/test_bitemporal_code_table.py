import datetime
import unittest

from simpleeval import (
    BitemporalCodeTable,
    CodeDeclaration,
    CodeLookupResult,
    SimpleEval,
    install_code_table,
    make_asof_lookup,
    simple_eval,
)


def declaration(key, code, valid_from, valid_to, known_at, **kwargs):
    return CodeDeclaration(
        business_key=key,
        code=code,
        valid_from=valid_from,
        valid_to=valid_to,
        known_at=known_at,
        **kwargs,
    )


class TestCodeDeclaration(unittest.TestCase):
    def test_half_open_interval(self):
        row = declaration("E01", "D1", "2024-01-01", "2024-04-01", "2024-01-01")

        self.assertTrue(row.covers("2024-01-01"))
        self.assertTrue(row.covers("2024-03-31"))
        self.assertFalse(row.covers("2024-04-01"))
        self.assertFalse(row.covers("2023-12-31"))

    def test_open_ended_interval(self):
        row = declaration("E01", "D1", "2024-01-01", None, "2024-01-01")

        self.assertTrue(row.covers("2099-12-31"))

    def test_rejects_inverted_interval(self):
        with self.assertRaises(ValueError):
            declaration("E01", "D1", "2024-04-01", "2024-01-01", "2024-01-01")

        with self.assertRaises(ValueError):
            declaration("E01", "D1", "2024-04-01", "2024-04-01", "2024-01-01")

    def test_aliases(self):
        row = CodeDeclaration(
            key="E01",
            department_code="D1",
            effective_from="2024-01-01",
            effective_to="2024-04-01",
            published_at="2024-01-02",
            precedence=2,
            source_id="seed",
        )

        self.assertEqual(row.business_key, "E01")
        self.assertEqual(row.code, "D1")
        self.assertEqual(row.valid_from, datetime.date(2024, 1, 1))
        self.assertEqual(row.valid_to, datetime.date(2024, 4, 1))
        self.assertEqual(row.known_at, datetime.datetime(2024, 1, 2))
        self.assertEqual(row.priority, 2)
        self.assertEqual(row.source, "seed")

    def test_conflicting_alias_rejected(self):
        with self.assertRaises(TypeError):
            CodeDeclaration(
                business_key="E01",
                key="E02",
                code="D1",
                valid_from="2024-01-01",
                known_at="2024-01-01",
            )

    def test_requires_business_key_code_and_start(self):
        with self.assertRaises(TypeError):
            CodeDeclaration(code="D1", valid_from="2024-01-01")

        with self.assertRaises(TypeError):
            CodeDeclaration(business_key="E01", valid_from="2024-01-01")

        with self.assertRaises(TypeError):
            CodeDeclaration(business_key="E01", code="D1")


class TestAsOfLookup(unittest.TestCase):
    def setUp(self):
        self.table = BitemporalCodeTable(
            [
                declaration("E01", "D1", "2024-01-01", "2024-04-01", "2024-01-01", source="seed"),
                declaration("E01", "D2", "2024-04-01", None, "2024-03-15", source="correction"),
                declaration("E02", "D3", "2024-01-01", None, "2024-01-01", source="seed"),
            ]
        )

    def test_reads_value_effective_on_business_day(self):
        self.assertEqual(self.table.lookup("2024-02-15", "2024-02-15", "E01").code, "D1")
        self.assertEqual(self.table.lookup("2024-04-15", "2024-04-15", "E01").code, "D2")

    def test_half_open_boundary_hits_only_successor(self):
        result = self.table.lookup("2024-04-01", "2024-04-01", "E01")

        self.assertEqual(result.code, "D2")
        self.assertEqual(len(result.declarations), 1)

    def test_backdated_correction_invisible_before_publication(self):
        table = BitemporalCodeTable(
            [
                declaration("E01", "D1", "2024-01-01", None, "2024-01-01", source="seed"),
                declaration("E01", "D2", "2024-01-01", None, "2024-03-15", source="correction"),
            ]
        )

        self.assertEqual(table.lookup("2024-05-01", "2024-03-14", "E01").code, "D1")
        self.assertEqual(table.lookup("2024-05-01", "2024-03-15", "E01").code, "D2")

    def test_correction_does_not_rewrite_past_knowledge(self):
        """A later correction must not change an earlier as-of answer."""
        table = BitemporalCodeTable(
            [
                declaration("E01", "D1", "2024-01-01", None, "2024-01-01", source="seed"),
                declaration("E01", "D2", "2024-01-01", None, "2024-03-15", source="correction"),
            ]
        )

        before = table.lookup("2024-05-01", "2024-03-14", "E01")
        after = table.lookup("2024-05-01", "2024-03-15", "E01")

        self.assertEqual(before.code, "D1")
        self.assertEqual(after.code, "D2")

    def test_missing_key(self):
        result = self.table.lookup("2024-02-15", "2024-02-15", "NOPE")

        self.assertIsNone(result.code)
        self.assertFalse(result.found)
        self.assertFalse(result.ambiguous)
        self.assertEqual(result.sources, ())

    def test_no_declaration_effective_on_business_day(self):
        result = self.table.lookup("2023-12-31", "2024-12-31", "E01")

        self.assertIsNone(result.code)
        self.assertFalse(result.found)

    def test_date_only_cutoff_is_end_of_day(self):
        table = BitemporalCodeTable(
            [declaration("E01", "D1", "2024-01-01", None, "2024-02-01T18:00:00")]
        )

        self.assertTrue(table.lookup("2024-02-15", "2024-02-01", "E01").found)
        self.assertFalse(table.lookup("2024-02-15", "2024-01-31", "E01").found)

    def test_result_supports_mapping_and_scalar_comparisons(self):
        result = self.table.lookup("2024-02-15", "2024-02-15", "E01")

        self.assertIsInstance(result, CodeLookupResult)
        self.assertEqual(result, "D1")
        self.assertEqual(result["code"], "D1")
        self.assertEqual(result.get("code"), "D1")
        self.assertEqual(dict(result)["code"], "D1")
        self.assertEqual(str(result), "D1")
        self.assertEqual(result.department_code, "D1")
        self.assertEqual(tuple(result), ("D1", ("seed",)))


class TestPriorityTies(unittest.TestCase):
    def test_equal_priority_same_time_keeps_both_sources(self):
        rows = [
            declaration("E01", "D1", "2024-01-01", None, "2024-01-01", source="crm"),
            declaration("E01", "D2", "2024-01-01", None, "2024-01-01", source="erp"),
        ]

        first = BitemporalCodeTable(rows).lookup("2024-02-01", "2024-02-01", "E01")
        second = BitemporalCodeTable(list(reversed(rows))).lookup(
            "2024-02-01", "2024-02-01", "E01"
        )

        self.assertTrue(first.ambiguous)
        self.assertIsNone(first.code)
        self.assertEqual(first.sources, ("crm", "erp"))
        self.assertEqual(first.sources, second.sources)
        self.assertEqual(first.declarations, second.declarations)
        self.assertEqual(first, second)

    def test_equal_priority_same_time_but_same_code_is_not_conflict(self):
        rows = [
            declaration("E01", "D1", "2024-01-01", None, "2024-01-01", source="crm"),
            declaration("E01", "D1", "2024-01-01", None, "2024-01-01", source="erp"),
        ]

        result = BitemporalCodeTable(rows).lookup("2024-02-01", "2024-02-01", "E01")

        self.assertEqual(result.code, "D1")
        self.assertTrue(result.ambiguous)
        self.assertEqual(result.sources, ("crm", "erp"))

    def test_higher_priority_wins_regardless_of_input_order(self):
        rows = [
            declaration("E01", "D1", "2024-01-01", None, "2024-01-01", priority=1, source="a"),
            declaration("E01", "D2", "2024-01-01", None, "2024-01-01", priority=2, source="b"),
        ]

        for ordered in (rows, list(reversed(rows))):
            result = BitemporalCodeTable(ordered).lookup("2024-02-01", "2024-02-01", "E01")
            self.assertEqual(result.code, "D2")
            self.assertEqual(result.sources, ("b",))
            self.assertFalse(result.ambiguous)

    def test_latest_publication_wins_at_equal_priority(self):
        rows = [
            declaration("E01", "D1", "2024-01-01", None, "2024-01-01", source="seed"),
            declaration("E01", "D2", "2024-01-01", None, "2024-02-01", source="correction"),
        ]

        result = BitemporalCodeTable(rows).lookup("2024-03-01", "2024-03-01", "E01")

        self.assertEqual(result.code, "D2")
        self.assertEqual(result.sources, ("correction",))

    def test_priority_is_the_only_authority(self):
        rows = [
            declaration("E01", "D1", "2024-01-01", None, "2024-01-01", priority=1, source="seed"),
            declaration(
                "E01", "D2", "2024-01-01", None, "2024-02-01", priority=2, source="correction"
            ),
        ]

        for ordered in (rows, list(reversed(rows))):
            result = BitemporalCodeTable(ordered).lookup("2024-03-01", "2024-03-01", "E01")
            self.assertEqual(result.code, "D2")
            self.assertEqual(result.sources, ("correction",))
            self.assertFalse(result.ambiguous)

    def test_two_equal_priority_publications_at_cutoff_are_both_returned(self):
        rows = [
            declaration("E01", "D1", "2024-01-01", None, "2024-02-01T09:00:00", source="crm"),
            declaration("E01", "D2", "2024-01-01", None, "2024-02-01T09:00:00", source="erp"),
            declaration("E01", "D3", "2024-01-01", None, "2024-02-01T10:00:00", source="manual"),
        ]

        result = BitemporalCodeTable(rows).lookup("2024-03-01", "2024-02-01T09:00:00", "E01")

        self.assertTrue(result.ambiguous)
        self.assertIsNone(result.code)
        self.assertEqual(result.sources, ("crm", "erp"))

    def test_unknown_source_still_deterministic(self):
        rows = [
            declaration("E01", "D1", "2024-01-01", None, "2024-01-01"),
            declaration("E01", "D2", "2024-01-01", None, "2024-01-01"),
        ]

        a = BitemporalCodeTable(rows).lookup("2024-02-01", "2024-02-01", "E01")
        b = BitemporalCodeTable(list(reversed(rows))).lookup("2024-02-01", "2024-02-01", "E01")

        self.assertEqual(a, b)
        self.assertIsNone(a.code)
        self.assertTrue(a.ambiguous)


class TestTableConstruction(unittest.TestCase):
    def test_accepts_mapping_declarations(self):
        table = BitemporalCodeTable(
            [
                {
                    "key": "E01",
                    "code": "D1",
                    "effective_from": "2024-01-01",
                    "published_at": "2024-01-01",
                }
            ]
        )

        self.assertEqual(table.lookup("2024-02-01", "2024-02-01", "E01").code, "D1")

    def test_accepts_grouped_mapping(self):
        table = BitemporalCodeTable(
            {
                "E01": [
                    {
                        "code": "D1",
                        "valid_from": "2024-01-01",
                        "known_at": "2024-01-01",
                    }
                ]
            }
        )

        self.assertEqual(table.lookup("2024-02-01", "2024-02-01", "E01").code, "D1")

    def test_duplicate_rows_are_collapsed(self):
        row = declaration("E01", "D1", "2024-01-01", None, "2024-01-01", source="seed")
        table = BitemporalCodeTable([row, row])

        result = table.lookup("2024-02-01", "2024-02-01", "E01")

        self.assertEqual(result.code, "D1")
        self.assertFalse(result.ambiguous)


class TestExpressionIntegration(unittest.TestCase):
    def setUp(self):
        self.table = BitemporalCodeTable(
            [
                declaration("E01", "D1", "2024-01-01", None, "2024-01-01", source="seed"),
                declaration("E01", "D2", "2024-01-01", None, "2024-03-15", source="correction"),
            ]
        )

    def test_callable_in_functions(self):
        functions = {"lookup_code": make_asof_lookup(self.table)}
        evaluator = SimpleEval(functions=functions)

        self.assertEqual(
            evaluator.eval("lookup_code('2024-05-01', '2024-03-14', 'E01').code"),
            "D1",
        )
        self.assertEqual(
            evaluator.eval("lookup_code('2024-05-01', '2024-03-15', 'E01').code"),
            "D2",
        )

    def test_install_helper(self):
        evaluator = SimpleEval()
        install_code_table(evaluator, self.table)

        self.assertEqual(
            evaluator.eval("lookup_code('2024-02-15', '2024-02-15', 'E01').code"),
            "D1",
        )

    def test_simple_eval_wiring(self):
        result = simple_eval(
            "lookup_code('2024-05-01', '2024-03-14', 'E01').code",
            functions={"lookup_code": make_asof_lookup(self.table)},
        )
        self.assertEqual(result, "D1")

    def test_ambiguous_result_available_to_expression(self):
        table = BitemporalCodeTable(
            [
                declaration("E01", "D1", "2024-01-01", None, "2024-01-01", source="crm"),
                declaration("E01", "D2", "2024-01-01", None, "2024-01-01", source="erp"),
            ]
        )
        evaluator = SimpleEval(functions={"lookup_code": make_asof_lookup(table)})

        result = evaluator.eval("lookup_code('2024-02-01', '2024-02-01', 'E01')")

        self.assertTrue(result.ambiguous)
        self.assertIsNone(result.code)
        self.assertEqual(result.sources, ("crm", "erp"))
        self.assertEqual(
            evaluator.eval("lookup_code('2024-02-01', '2024-02-01', 'E01').sources[0]"),
            "crm",
        )
