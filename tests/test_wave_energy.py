import math
import types
import unittest

from pyais.wave_energy import (
    DEFAULT_HEIGHT_STEP,
    GRAVITY,
    WAVE_ENERGY_FACTOR,
    WATER_DENSITY,
    BudgetStatus,
    MonthlyWaveRow,
    WaveComponent,
    WaveEnergyBudget,
    WaveEnergyMonthlyTable,
    WaveObservation,
    energy_uncertainty,
    observations_from_report,
    physical_wave_energy,
    wave_energy,
    wave_energy_budget,
)


def _obs(component, height, period=None, direction=None, source=""):
    return WaveObservation(
        component=component,
        height=height,
        period=period,
        direction=direction,
        source=source,
    )


class WaveEnergyFunctionsTests(unittest.TestCase):
    def test_energy_proxy_is_height_squared(self):
        self.assertEqual(wave_energy(2.0), 4.0)
        self.assertAlmostEqual(wave_energy(1.4), 1.96)

    def test_physical_energy_uses_constant_factor(self):
        self.assertAlmostEqual(
            WAVE_ENERGY_FACTOR, WATER_DENSITY * GRAVITY / 16.0
        )
        self.assertAlmostEqual(physical_wave_energy(2.0), WAVE_ENERGY_FACTOR * 4.0)

    def test_energy_uncertainty_follows_0_1m_quantization(self):
        # H stands for [H-0.05, H+0.05], so |d(H**2)| <= 2*H*0.05 = 0.1*H.
        self.assertAlmostEqual(energy_uncertainty(1.4), 0.14)
        self.assertAlmostEqual(
            energy_uncertainty(1.4, height_step=0.2), 0.28
        )


class ThreeWayClosureTests(unittest.TestCase):
    def test_balanced_budget_closes_with_zero_residual(self):
        # 1.0**2 = 0.8**2 + 0.6**2
        budget = wave_energy_budget(
            [
                _obs(WaveComponent.TOTAL, 1.0, period=8, direction=270),
                _obs(WaveComponent.WIND, 0.6, period=5, direction=250),
                _obs(WaveComponent.SWELL, 0.8, period=9, direction=300),
            ]
        )
        self.assertIs(budget.status, BudgetStatus.BALANCED)
        self.assertAlmostEqual(budget.total_energy, 1.0)
        self.assertAlmostEqual(budget.residual, 0.0)
        self.assertAlmostEqual(budget.residual_tolerance, 0.24)
        self.assertEqual(len(budget.components), 3)

    def test_small_negative_residual_is_quantization_not_incompatible(self):
        # component energy 1.13 vs total 1.0: residual -0.13, tolerance 0.25
        budget = wave_energy_budget(
            [
                _obs(WaveComponent.TOTAL, 1.0),
                _obs(WaveComponent.WIND, 0.7),
                _obs(WaveComponent.SWELL, 0.8),
            ]
        )
        self.assertIs(
            budget.status, BudgetStatus.NEGATIVE_WITHIN_TOLERANCE
        )
        self.assertAlmostEqual(budget.residual, -0.13)
        self.assertGreater(budget.residual_tolerance, abs(budget.residual))

    def test_large_negative_residual_is_incompatible(self):
        # total 0.5 (0.25) cannot contain a 0.8 m wind sea (0.64)
        budget = wave_energy_budget(
            [
                _obs(WaveComponent.TOTAL, 0.5),
                _obs(WaveComponent.WIND, 0.8),
                _obs(WaveComponent.SWELL, 0.3),
            ]
        )
        self.assertIs(budget.status, BudgetStatus.INCOMPATIBLE)
        self.assertLess(budget.residual, -budget.residual_tolerance)

    def test_positive_residual_beyond_tolerance_is_unclosed(self):
        budget = wave_energy_budget(
            [
                _obs(WaveComponent.TOTAL, 3.0),
                _obs(WaveComponent.WIND, 1.0),
                _obs(WaveComponent.SWELL, 1.0),
            ]
        )
        self.assertIs(budget.status, BudgetStatus.UNCLOSED)
        self.assertAlmostEqual(budget.residual, 7.0)
        self.assertGreater(budget.residual, budget.residual_tolerance)

    def test_duplicate_component_is_rejected(self):
        with self.assertRaises(ValueError):
            wave_energy_budget(
                [
                    _obs(WaveComponent.TOTAL, 2.0, source="a"),
                    _obs(WaveComponent.TOTAL, 2.1, source="b"),
                ]
            )


class DerivedComponentTests(unittest.TestCase):
    def test_wind_energy_derived_from_total_and_swell(self):
        budget = wave_energy_budget(
            [
                _obs(WaveComponent.TOTAL, 2.0, period=8, direction=270),
                _obs(WaveComponent.SWELL, 1.2, period=10, direction=310),
            ]
        )
        self.assertIs(budget.status, BudgetStatus.DERIVED)
        wind = next(
            item
            for item in budget.components
            if item.component is WaveComponent.WIND
        )
        self.assertTrue(wind.derived)
        self.assertAlmostEqual(wind.energy, 4.0 - 1.44)
        self.assertAlmostEqual(wind.height, 1.6)
        # An inferred energy residual carries no period or direction.
        self.assertIsNone(wind.period)
        self.assertIsNone(wind.direction)

    def test_derived_wind_never_uses_period_subtraction(self):
        budget = wave_energy_budget(
            [
                _obs(WaveComponent.TOTAL, 2.0, period=8),
                _obs(WaveComponent.SWELL, 1.2, period=11),
            ]
        )
        wind = next(
            item
            for item in budget.components
            if item.component is WaveComponent.WIND
        )
        self.assertIsNone(wind.period)
        self.assertNotIn(8 - 11, (wind.period,))
        self.assertNotIn(11 - 8, (wind.period,))

    def test_total_derived_from_independent_wind_and_swell(self):
        budget = wave_energy_budget(
            [
                _obs(WaveComponent.WIND, 0.6, period=5),
                _obs(WaveComponent.SWELL, 0.8, period=9),
            ]
        )
        self.assertIs(budget.status, BudgetStatus.DERIVED)
        self.assertAlmostEqual(budget.total_energy, 1.0)
        total = next(
            item
            for item in budget.components
            if item.component is WaveComponent.TOTAL
        )
        self.assertTrue(total.derived)
        self.assertAlmostEqual(total.height, 1.0)
        self.assertIsNone(total.period)

    def test_derived_wind_negative_within_tolerance_is_clamped(self):
        # swell 1.1 (1.21) vs total 1.0 (1.00): residual -0.21 equals the
        # propagated tolerance 0.1 * (1.0 + 1.1) = 0.21, so quantization alone
        # explains the negative inferred wind-sea energy.
        budget = wave_energy_budget(
            [
                _obs(WaveComponent.TOTAL, 1.0),
                _obs(WaveComponent.SWELL, 1.1),
            ]
        )
        self.assertIs(
            budget.status, BudgetStatus.NEGATIVE_WITHIN_TOLERANCE
        )
        wind = next(
            item
            for item in budget.components
            if item.component is WaveComponent.WIND
        )
        self.assertAlmostEqual(wind.energy, 0.0)
        self.assertTrue(wind.derived)

    def test_derived_wind_beyond_tolerance_is_incompatible(self):
        budget = wave_energy_budget(
            [
                _obs(WaveComponent.TOTAL, 0.5),
                _obs(WaveComponent.SWELL, 0.8),
            ]
        )
        self.assertIs(budget.status, BudgetStatus.INCOMPATIBLE)
        self.assertIsNotNone(budget.residual)
        self.assertLess(budget.residual, -budget.residual_tolerance)

    def test_single_observation_is_insufficient(self):
        budget = wave_energy_budget([_obs(WaveComponent.TOTAL, 2.0)])
        self.assertIs(budget.status, BudgetStatus.INSUFFICIENT)
        self.assertIsNone(budget.residual)
        self.assertEqual(len(budget.components), 1)


class ReportAdapterTests(unittest.TestCase):
    def test_adapts_fid31_style_message_object(self):
        report = types.SimpleNamespace(
            mmsi=2655619,
            waveheight=1.4,
            waveperiod=7,
            wavedir=280,
            swellheight=0.8,
            swellperiod=9,
            swelldir=300,
        )
        observations = observations_from_report(report)
        self.assertEqual(len(observations), 2)
        total, swell = observations
        self.assertIs(total.component, WaveComponent.TOTAL)
        self.assertEqual(total.height, 1.4)
        self.assertEqual(total.period, 7)
        self.assertEqual(total.direction, 280)
        self.assertEqual(total.source, "2655619")
        self.assertIs(swell.component, WaveComponent.SWELL)
        self.assertEqual(swell.height, 0.8)

    def test_adapts_fid26_sea_state_dictionary(self):
        report = {
            "sensor": 7,
            "site": 3,
            "waveheight": 1.4,
            "waveperiod": 7,
            "wavedir": 280,
            "swheight": 0.8,
            "swperiod": 9,
            "swelldir": 300,
        }
        observations = observations_from_report(
            report, wave_component=WaveComponent.WIND
        )
        self.assertEqual(
            [item.component for item in observations],
            [WaveComponent.WIND, WaveComponent.SWELL],
        )
        self.assertEqual(observations[1].period, 9)

    def test_not_available_codes_are_dropped_or_normalized(self):
        report = types.SimpleNamespace(
            mmsi=1,
            waveheight=25.5,
            waveperiod=63,
            wavedir=360,
            swellheight=0.8,
            swellperiod=63,
            swelldir=511,
        )
        observations = observations_from_report(report)
        self.assertEqual(len(observations), 1)
        swell = observations[0]
        self.assertIs(swell.component, WaveComponent.SWELL)
        self.assertIsNone(swell.period)
        self.assertIsNone(swell.direction)

    def test_adapted_report_drives_full_budget(self):
        report = types.SimpleNamespace(
            mmsi=42,
            waveheight=1.0,
            waveperiod=8,
            wavedir=270,
            swellheight=0.8,
            swellperiod=9,
            swelldir=300,
        )
        budget = wave_energy_budget(observations_from_report(report))
        self.assertIn(
            budget.status,
            (
                BudgetStatus.BALANCED,
                BudgetStatus.NEGATIVE_WITHIN_TOLERANCE,
                BudgetStatus.DERIVED,
            ),
        )
        self.assertIsInstance(budget, WaveEnergyBudget)


class MonthlyTableTests(unittest.TestCase):
    def _budgets(self):
        balanced = wave_energy_budget(
            [
                _obs(WaveComponent.TOTAL, 1.0),
                _obs(WaveComponent.WIND, 0.6),
                _obs(WaveComponent.SWELL, 0.8),
            ]
        )
        tiny_negative = wave_energy_budget(
            [
                _obs(WaveComponent.TOTAL, 1.0),
                _obs(WaveComponent.WIND, 0.7),
                _obs(WaveComponent.SWELL, 0.8),
            ]
        )
        incompatible = wave_energy_budget(
            [
                _obs(WaveComponent.TOTAL, 0.5),
                _obs(WaveComponent.WIND, 0.8),
                _obs(WaveComponent.SWELL, 0.3),
            ]
        )
        derived = wave_energy_budget(
            [_obs(WaveComponent.TOTAL, 2.0), _obs(WaveComponent.SWELL, 1.2)]
        )
        return balanced, tiny_negative, incompatible, derived

    def test_table_separates_quantization_negative_from_incompatible(self):
        table = WaveEnergyMonthlyTable()
        balanced, tiny_negative, incompatible, derived = self._budgets()
        table.add("2026-10", "r1", balanced)
        table.add("2026-10", "r2", tiny_negative)
        table.add("2026-10", "r3", incompatible)
        table.add("2026-11", "r4", derived)

        rows = table.rows()
        self.assertEqual([row.month for row in rows], ["2026-10", "2026-11"])

        october = rows[0]
        self.assertIsInstance(october, MonthlyWaveRow)
        self.assertEqual(october.report_count, 3)
        self.assertEqual(october.balanced, 1)
        self.assertEqual(october.negative_within_tolerance, 1)
        self.assertEqual(october.incompatible, 1)
        self.assertEqual(october.incompatible_reports, ("r3",))

        november = rows[1]
        self.assertEqual(november.derived, 1)
        self.assertEqual(november.incompatible, 0)
        self.assertEqual(november.incompatible_reports, ())

    def test_table_accumulates_component_energies(self):
        table = WaveEnergyMonthlyTable()
        balanced, _, _, derived = self._budgets()
        table.add("2026-10", "r1", balanced)
        table.add("2026-10", "r4", derived)

        row = table.rows()[0]
        self.assertAlmostEqual(row.total_energy, 1.0 + 4.0)
        self.assertAlmostEqual(row.swell_energy, 0.64 + 1.44)
        self.assertAlmostEqual(row.wind_energy, 0.36 + (4.0 - 1.44))

        as_dicts = table.to_dicts()
        self.assertEqual(as_dicts[0]["month"], "2026-10")
        self.assertIn("incompatible_reports", as_dicts[0])

    def test_default_quantization_step_matches_ais_fields(self):
        self.assertEqual(DEFAULT_HEIGHT_STEP, 0.1)
        self.assertTrue(math.isclose(WAVE_ENERGY_FACTOR, 628.2385, rel_tol=1e-3))


if __name__ == "__main__":
    unittest.main()
