import unittest
from decimal import Decimal

from simpleeval import (
    Currency,
    CurrencyMismatch,
    EvalWithCompoundTypes,
    Money,
    SimpleEval,
    money,
    settle,
)


class TestMoneyTypes(unittest.TestCase):
    """Money values accept the three incoming numeric shapes."""

    def test_decimal_int_and_text_inputs(self):
        self.assertEqual(money(Decimal("1.2345"), "USD").amount, Decimal("1.2345"))
        self.assertEqual(money(10, "USD").amount, Decimal("10"))
        self.assertEqual(money("2.50", "USD").amount, Decimal("2.50"))
        self.assertEqual(money(" 2.50 ", "USD").amount, Decimal("2.50"))

    def test_float_input_is_rejected(self):
        with self.assertRaises(TypeError):
            money(1.5, "USD")

    def test_currency_reuse_and_equality(self):
        usd = Currency("USD")
        self.assertEqual(money("1.00", usd), Money(Decimal("1.00"), Currency("USD")))
        self.assertEqual(Currency("USD", 2), Currency("USD", 2))
        self.assertNotEqual(Currency("USD"), Currency("EUR"))

    def test_zero_decimal_currency(self):
        yen = money("1234.5", "JPY", places=0)
        self.assertEqual(yen.settle(), money("1235", "JPY", places=0))


class TestMoneyArithmetic(unittest.TestCase):
    """The currency travels with every intermediate value, unrounded."""

    def setUp(self):
        self.s = SimpleEval()
        self.s.names["price"] = money("1.2345", "USD")
        self.s.names["qty"] = 3

    def test_quantity_times_price_is_money(self):
        result = self.s.eval("qty * price")
        self.assertEqual(result, money("3.7035", "USD"))
        self.assertIsInstance(result, Money)
        self.assertEqual(result.currency, Currency("USD"))

    def test_reversed_multiplication(self):
        self.assertEqual(self.s.eval("price * qty"), money("3.7035", "USD"))

    def test_discount_is_applied_before_aggregation_without_rounding(self):
        self.s.names["discount"] = Decimal("0.1")
        result = self.s.eval("qty * price * (1 - discount)")
        self.assertEqual(result, money("3.33315", "USD"))
        self.assertEqual(result.amount, Decimal("3.33315"))

    def test_intermediate_values_keep_full_precision(self):
        result = self.s.eval("price + price + price")
        self.assertEqual(result.amount, Decimal("3.7035"))

    def test_unary_signs_and_subtraction(self):
        self.assertEqual(-money("1.5", "USD"), money("-1.5", "USD"))
        self.assertEqual(+money("1.5", "USD"), money("1.5", "USD"))
        self.assertEqual(self.s.eval("+price").amount, Decimal("1.2345"))
        self.assertEqual(self.s.eval("price - money('0.2345', 'USD')"), money("1.0000", "USD"))

    def test_division_by_quantity_keeps_currency(self):
        result = self.s.eval("price / qty")
        self.assertEqual(result, money("0.4115", "USD"))
        self.assertEqual(result.currency, Currency("USD"))

    def test_money_times_money_is_rejected(self):
        with self.assertRaises(TypeError):
            self.s.eval("price * price")

    def test_money_divided_by_money_is_rejected(self):
        with self.assertRaises(TypeError):
            self.s.eval("price / price")

    def test_plain_arithmetic_is_unchanged(self):
        plain = SimpleEval()
        self.assertEqual(plain.eval("21 + 21"), 42)
        self.assertEqual(plain.eval("'a' + 'b'"), "ab")
        self.assertEqual(plain.eval("6 * 7"), 42)


class TestCurrencyMismatch(unittest.TestCase):
    """Without an exchange rate, two currencies cannot combine."""

    def setUp(self):
        self.s = SimpleEval()
        self.s.names["usd"] = money("10.00", "USD")
        self.s.names["eur"] = money("10.00", "EUR")

    def test_addition_across_currencies_fails(self):
        with self.assertRaises(CurrencyMismatch):
            self.s.eval("usd + eur")

    def test_subtraction_across_currencies_fails(self):
        with self.assertRaises(CurrencyMismatch):
            self.s.eval("usd - eur")

    def test_comparison_across_currencies_fails(self):
        with self.assertRaises(CurrencyMismatch):
            self.s.eval("usd < eur")

    def test_same_currency_combines(self):
        self.assertEqual(self.s.eval("usd + money('5.00', 'USD')"), money("15.00", "USD"))

    def test_explicit_conversion_then_addition(self):
        def convert(amount, rate, code):
            return money(amount.amount * rate, code)

        self.s.functions["convert"] = convert
        result = self.s.eval("usd + convert(eur, Decimal('1.08'), 'USD')")
        self.assertEqual(result, money("20.80", "USD"))


class TestSettlement(unittest.TestCase):
    """Rounding happens only at the declared settlement boundary."""

    def test_settle_money_half_up(self):
        self.assertEqual(settle(money("0.005", "USD")), money("0.01", "USD"))
        self.assertEqual(settle(money("0.004", "USD")), money("0.00", "USD"))
        self.assertEqual(settle(money("-0.005", "USD")), money("-0.01", "USD"))

    def test_settle_plain_number_without_currency(self):
        self.assertEqual(settle(Decimal("1.005")), Decimal("1.01"))
        self.assertEqual(settle("1.004"), Decimal("1.00"))

    def test_settle_does_not_mutate_the_original(self):
        raw = money("0.005", "USD")
        rounded = raw.settle()
        self.assertEqual(raw.amount, Decimal("0.005"))
        self.assertEqual(rounded.amount, Decimal("0.01"))

    def test_settle_inside_expression(self):
        s = SimpleEval()
        s.names["raw"] = money("0.126", "USD")
        self.assertEqual(s.eval("settle(raw)"), money("0.13", "USD"))
        self.assertEqual(s.eval("raw + raw"), money("0.252", "USD"))


class TestHalfCentSettlementOrderings(unittest.TestCase):
    """Three half-cent lines, settled per-line versus after summing.

    The two orderings must produce different totals, and both must trace
    back to the same unrounded source lines.
    """

    def setUp(self):
        self.s = EvalWithCompoundTypes()
        # The three lines deliberately use the three incoming shapes:
        # text decimal, Decimal, and an int quantity scaled by a Decimal.
        self.s.names["lines"] = [
            money("0.005", "USD"),
            money(Decimal("0.005"), "USD"),
            money(1, "USD") * Decimal("0.005"),
        ]

    def test_source_lines_are_exact_half_cents(self):
        for line in self.s.names["lines"]:
            self.assertEqual(line.amount, Decimal("0.005"))
            self.assertEqual(line.currency, Currency("USD"))

    def test_settle_each_line_then_sum(self):
        settled = self.s.eval("money_sum([settle(line) for line in lines])")
        self.assertEqual(settled, money("0.03", "USD"))
        # every settled line can be reconciled individually:
        per_line = self.s.eval("[settle(line) for line in lines]")
        self.assertEqual(per_line, [money("0.01", "USD")] * 3)

    def test_sum_then_settle_once(self):
        aggregated = self.s.eval("settle(money_sum(lines))")
        self.assertEqual(aggregated, money("0.02", "USD"))
        # the unrounded aggregate still matches the source lines exactly:
        raw_total = self.s.eval("money_sum(lines)")
        self.assertEqual(raw_total, money("0.015", "USD"))

    def test_two_orderings_legitimately_differ(self):
        per_line = self.s.eval("money_sum([settle(line) for line in lines])")
        summed = self.s.eval("settle(money_sum(lines))")
        self.assertNotEqual(per_line, summed)
        self.assertEqual(per_line.amount, Decimal("0.03"))
        self.assertEqual(summed.amount, Decimal("0.02"))

    def test_discount_then_aggregation_rounds_once(self):
        self.s.names["discount"] = Decimal("0.1")
        result = self.s.eval("settle(money_sum([line * (1 - discount) for line in lines]))")
        # 3 * 0.005 * 0.9 = 0.0135, rounded once at the boundary:
        self.assertEqual(result, money("0.01", "USD"))

    def test_money_sum_rejects_mixed_currencies(self):
        self.s.names["mixed"] = [money("1", "USD"), money("1", "EUR")]
        with self.assertRaises(CurrencyMismatch):
            self.s.eval("money_sum(mixed)")

    def test_money_sum_plain_numbers_is_exact(self):
        self.assertEqual(self.s.eval("money_sum(['0.1', '0.2'])"), Decimal("0.3"))
