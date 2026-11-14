"""Wave-energy budgets for sea-state reports.

A sea-state report can carry significant wave height, peak period and
direction for more than one source, and each source is tagged with the
caliber of what it measures: the combined (total) sea, wind sea only, or
swell only.  This module turns those mixed-caliber reports into auditable
energy budgets instead of comparing heights directly.

Physical model
--------------
For a sea state with significant wave height ``Hs`` the wave energy per unit
sea surface is ``E = (rho * g / 16) * Hs**2`` (joules per square metre).
Every component carries the same constant factor, so budgets can be kept in
the energy proxy ``Hs**2`` (square metres), which is strictly proportional to
physical energy.  Wind sea and swell are treated as statistically
independent components, so their energies add:

    Hs_total**2 = Hs_wind**2 + Hs_swell**2

Consequences used here:

* Heights are never added or subtracted; only squared energies enter a
  budget, and every result carries a propagated uncertainty.
* With a reported total and swell but no wind sea, the wind-sea energy is
  the residual ``Hs_total**2 - Hs_swell**2``.  A peak period can never be
  recovered that way: periods are carried through verbatim and are never
  combined (in particular, no wind period is produced by subtracting two
  peak periods).  A derived component therefore always has ``period`` and
  ``direction`` set to ``None``.
* AIS heights are quantized to 0.1 m steps, i.e. a decoded height ``H``
  represents the interval ``[H - 0.05, H + 0.05]``.  The worst-case
  uncertainty of the energy proxy is ``2 * H * 0.05 = 0.1 * H``.  A negative
  residual no larger than the propagated tolerance is a quantization effect,
  not an incompatible report; residuals beyond that tolerance are flagged as
  genuinely incompatible and kept separate in the monthly table.
"""

import enum
import math
import typing

import attr


#: Standard gravitational acceleration (m/s**2).
GRAVITY = 9.80665
#: Typical seawater density (kg/m**3).
WATER_DENSITY = 1025.0
#: Factor converting the Hs**2 energy proxy (m**2) into joules per m**2.
WAVE_ENERGY_FACTOR = WATER_DENSITY * GRAVITY / 16.0
#: Quantization step of the AIS significant-wave-height fields (metres).
DEFAULT_HEIGHT_STEP = 0.1
#: Raw 8-bit height code 255 decodes to 25.5 m and means "not available".
MISSING_HEIGHT = 25.5
#: Raw 6-bit period code 63 means "not available".
MISSING_PERIOD = 63
#: Direction codes that mean "not available" (360 for IMO289, 511 for IMO236).
MISSING_DIRECTIONS = (360, 511)
#: Relative slack on tolerance comparisons, absorbing floating-point noise at
#: the exact quantization boundary without changing the physical criterion.
_TOLERANCE_SLACK = 1e-12


def _beyond_tolerance(residual: float, tolerance: float) -> bool:
    """True when a negative residual cannot be explained by quantization."""
    return residual < -tolerance * (1.0 + _TOLERANCE_SLACK)


class WaveComponent(enum.Enum):
    """Caliber of a wave observation: which part of the sea state it measures."""

    TOTAL = "total"
    WIND = "wind"
    SWELL = "swell"


class BudgetStatus(enum.Enum):
    """Outcome of an energy-budget closure check."""

    #: Total and components agree within the propagated quantization tolerance.
    BALANCED = "balanced"
    #: Negative residual, but small enough to be explained by quantization.
    NEGATIVE_WITHIN_TOLERANCE = "negative_within_tolerance"
    #: Component energy exceeds the reported total beyond quantization.
    INCOMPATIBLE = "incompatible"
    #: Positive residual beyond tolerance: the listed components do not close
    #: the reported total (component energy is missing).
    UNCLOSED = "unclosed"
    #: A missing component (or the total) was inferred from the other terms.
    DERIVED = "derived"
    #: Too few independent terms to build any budget.
    INSUFFICIENT = "insufficient"


@attr.s(slots=True, frozen=True)
class WaveObservation:
    """One wave measurement tagged with the caliber its source reports.

    ``period`` (seconds) and ``direction`` (degrees, incoming direction) are
    descriptive only.  They are stored and forwarded but never enter the
    energy arithmetic, so a missing peak period does not block a budget and a
    period can never be fabricated from two other periods.
    """

    component: WaveComponent = attr.ib()
    height: float = attr.ib()
    period: typing.Optional[int] = attr.ib(default=None)
    direction: typing.Optional[int] = attr.ib(default=None)
    height_step: float = attr.ib(default=DEFAULT_HEIGHT_STEP)
    source: str = attr.ib(default="")
    quantized: bool = attr.ib(default=True)


@attr.s(slots=True, frozen=True)
class ComponentEnergy:
    """Energy of one component after the budget is built."""

    component: WaveComponent = attr.ib()
    energy: float = attr.ib()
    uncertainty: float = attr.ib()
    height: typing.Optional[float] = attr.ib(default=None)
    period: typing.Optional[int] = attr.ib(default=None)
    direction: typing.Optional[int] = attr.ib(default=None)
    derived: bool = attr.ib(default=False)
    source: str = attr.ib(default="")


@attr.s(slots=True, frozen=True)
class WaveEnergyBudget:
    """Result of an energy-budget calculation.

    ``residual`` is the directly verifiable gap
    ``E_total - sum(reported component energies)`` (``None`` when no total was
    reported) and ``residual_tolerance`` is the worst-case quantization bound
    against which it is judged.
    """

    status: BudgetStatus = attr.ib()
    total_energy: typing.Optional[float] = attr.ib()
    total_uncertainty: float = attr.ib()
    residual: typing.Optional[float] = attr.ib()
    residual_tolerance: float = attr.ib()
    components: typing.Tuple[ComponentEnergy, ...] = attr.ib()
    notes: typing.Tuple[str, ...] = attr.ib()


def wave_energy(height: float) -> float:
    """Energy proxy ``Hs**2`` (m**2), proportional to physical wave energy."""
    return height * height


def physical_wave_energy(height: float) -> float:
    """Physical wave energy per unit sea surface (J/m**2)."""
    return WAVE_ENERGY_FACTOR * wave_energy(height)


def energy_uncertainty(height: float, height_step: float = DEFAULT_HEIGHT_STEP) -> float:
    """Worst-case uncertainty of the ``Hs**2`` proxy from height quantization.

    A decoded height ``H`` stands for ``[H - step/2, H + step/2]``; linear
    propagation gives ``|d(H**2)| <= 2 * H * step / 2 = H * step``.
    """
    return height * height_step


def _component_from_observation(
    observation: WaveObservation,
    *,
    derived: bool = False,
    energy_override: typing.Optional[float] = None,
    uncertainty_override: typing.Optional[float] = None,
) -> ComponentEnergy:
    energy = (
        wave_energy(observation.height) if energy_override is None else energy_override
    )
    uncertainty = (
        energy_uncertainty(observation.height, observation.height_step)
        if uncertainty_override is None
        else uncertainty_override
    )
    return ComponentEnergy(
        component=observation.component,
        energy=energy,
        uncertainty=uncertainty,
        height=math.sqrt(energy) if energy > 0.0 else 0.0,
        period=None if derived else observation.period,
        direction=None if derived else observation.direction,
        derived=derived,
        source="derived" if derived else observation.source,
    )


def wave_energy_budget(
    observations: typing.Sequence[WaveObservation],
) -> WaveEnergyBudget:
    """Build an independent-component energy budget from tagged observations.

    At most one observation per :class:`WaveComponent` is accepted per report;
    duplicate calibers raise ``ValueError`` rather than being silently merged.
    """
    bucket: typing.Dict[WaveComponent, WaveObservation] = {}
    for observation in observations:
        if observation.component in bucket:
            raise ValueError(
                f"duplicate observation for component {observation.component.value!r}; "
                "reconcile same-caliber sources before building a budget"
            )
        bucket[observation.component] = observation

    total = bucket.get(WaveComponent.TOTAL)
    wind = bucket.get(WaveComponent.WIND)
    swell = bucket.get(WaveComponent.SWELL)
    notes: typing.List[str] = []

    if total is not None and wind is not None and swell is not None:
        return _close_three_way(total, wind, swell)

    if total is not None:
        if wind is not None or swell is not None:
            known = wind if wind is not None else swell
            assert known is not None
            missing = (
                WaveComponent.SWELL if wind is not None else WaveComponent.WIND
            )
            return _derive_missing_component(total, known, missing, notes)
        notes.append("only a total-wave observation is available; nothing to close")
        return WaveEnergyBudget(
            status=BudgetStatus.INSUFFICIENT,
            total_energy=wave_energy(total.height),
            total_uncertainty=energy_uncertainty(total.height, total.height_step),
            residual=None,
            residual_tolerance=0.0,
            components=(_component_from_observation(total),),
            notes=tuple(notes),
        )

    if wind is not None and swell is not None:
        return _derive_total(wind, swell, notes)

    notes.append(
        "fewer than two independent wave terms; no energy budget can be built"
    )
    available = tuple(
        _component_from_observation(item) for item in (wind, swell) if item is not None
    )
    return WaveEnergyBudget(
        status=BudgetStatus.INSUFFICIENT,
        total_energy=None,
        total_uncertainty=0.0,
        residual=None,
        residual_tolerance=0.0,
        components=available,
        notes=tuple(notes),
    )


def _close_three_way(
    total: WaveObservation,
    wind: WaveObservation,
    swell: WaveObservation,
) -> WaveEnergyBudget:
    """Check a fully reported total against its wind-sea and swell terms."""
    energy_total = wave_energy(total.height)
    energy_components = wave_energy(wind.height) + wave_energy(swell.height)
    tolerance = energy_uncertainty(total.height, total.height_step)
    tolerance += energy_uncertainty(wind.height, wind.height_step)
    tolerance += energy_uncertainty(swell.height, swell.height_step)
    residual = energy_total - energy_components
    notes: typing.List[str] = []

    if _beyond_tolerance(residual, tolerance):
        status = BudgetStatus.INCOMPATIBLE
        notes.append(
            "component energy exceeds the total beyond the quantization tolerance"
        )
    elif residual < 0.0:
        status = BudgetStatus.NEGATIVE_WITHIN_TOLERANCE
        notes.append(
            "negative residual is explained by 0.1 m height quantization"
        )
    elif residual <= tolerance:
        status = BudgetStatus.BALANCED
        notes.append("energy budget closes within the quantization tolerance")
    else:
        status = BudgetStatus.UNCLOSED
        notes.append("reported components do not account for all of the total energy")

    return WaveEnergyBudget(
        status=status,
        total_energy=energy_total,
        total_uncertainty=energy_uncertainty(total.height, total.height_step),
        residual=residual,
        residual_tolerance=tolerance,
        components=(
            _component_from_observation(total),
            _component_from_observation(wind),
            _component_from_observation(swell),
        ),
        notes=tuple(notes),
    )


def _derive_missing_component(
    total: WaveObservation,
    known: WaveObservation,
    missing: WaveComponent,
    notes: typing.List[str],
) -> WaveEnergyBudget:
    """Infer the one missing component from the total and the other component."""
    energy_total = wave_energy(total.height)
    tolerance = energy_uncertainty(
        total.height, total.height_step
    ) + energy_uncertainty(known.height, known.height_step)
    residual = energy_total - wave_energy(known.height)

    if _beyond_tolerance(residual, tolerance):
        status = BudgetStatus.INCOMPATIBLE
        derived_energy = 0.0
        notes.append(
            f"inferred {missing.value} energy is negative beyond the "
            "quantization tolerance; reports are incompatible"
        )
    elif residual < 0.0:
        status = BudgetStatus.NEGATIVE_WITHIN_TOLERANCE
        derived_energy = 0.0
        notes.append(
            f"slightly negative inferred {missing.value} energy is explained by "
            "quantization; clamped to zero"
        )
    else:
        status = BudgetStatus.DERIVED
        derived_energy = residual
        notes.append(
            f"{missing.value} energy inferred as the energy residual of the total"
        )

    placeholder = WaveObservation(
        component=missing,
        height=math.sqrt(derived_energy) if derived_energy > 0.0 else 0.0,
        period=None,
        direction=None,
        height_step=DEFAULT_HEIGHT_STEP,
        source="derived",
    )
    notes.append(
        "peak period and direction of a derived component cannot be inferred "
        "and are left unknown"
    )

    return WaveEnergyBudget(
        status=status,
        total_energy=energy_total,
        total_uncertainty=energy_uncertainty(total.height, total.height_step),
        residual=residual,
        residual_tolerance=tolerance,
        components=(
            _component_from_observation(total),
            _component_from_observation(known),
            _component_from_observation(
                placeholder,
                derived=True,
                energy_override=derived_energy,
                uncertainty_override=tolerance,
            ),
        ),
        notes=tuple(notes),
    )


def _derive_total(
    wind: WaveObservation,
    swell: WaveObservation,
    notes: typing.List[str],
) -> WaveEnergyBudget:
    """Sum independent component energies when no total was reported."""
    energy_total = wave_energy(wind.height) + wave_energy(swell.height)
    tolerance = energy_uncertainty(
        wind.height, wind.height_step
    ) + energy_uncertainty(swell.height, swell.height_step)
    notes.append("total energy inferred from independent wind-sea and swell terms")
    placeholder = WaveObservation(
        component=WaveComponent.TOTAL,
        height=math.sqrt(energy_total),
        period=None,
        direction=None,
        height_step=DEFAULT_HEIGHT_STEP,
        source="derived",
    )
    return WaveEnergyBudget(
        status=BudgetStatus.DERIVED,
        total_energy=energy_total,
        total_uncertainty=tolerance,
        residual=None,
        residual_tolerance=tolerance,
        components=(
            _component_from_observation(wind),
            _component_from_observation(swell),
            _component_from_observation(
                placeholder,
                derived=True,
                energy_override=energy_total,
                uncertainty_override=tolerance,
            ),
        ),
        notes=tuple(notes),
    )


def _field(report: typing.Any, *names: str) -> typing.Any:
    if isinstance(report, dict):
        for name in names:
            if name in report:
                return report[name]
        return None
    for name in names:
        if hasattr(report, name):
            return getattr(report, name)
    return None


def _clean_period(value: typing.Any) -> typing.Optional[int]:
    if value is None or value == MISSING_PERIOD:
        return None
    return int(value)


def _clean_direction(value: typing.Any) -> typing.Optional[int]:
    if value is None or value in MISSING_DIRECTIONS:
        return None
    return int(value)


def observations_from_report(
    report: typing.Any,
    *,
    wave_component: WaveComponent = WaveComponent.TOTAL,
    source: str = "",
) -> typing.List[WaveObservation]:
    """Build observations from a decoded AIS sea-state report.

    Accepts the FID 11/21/31 message objects (attributes ``waveheight`` /
    ``swellheight``) or a FID 26 sea-state sensor dictionary (keys
    ``waveheight`` / ``swheight``).  The generic ``wave*`` fields are labeled
    with ``wave_component`` (the combined total by IMO convention) and the
    ``swell*`` fields are always labeled as swell.  Not-available codes
    (height 25.5 m, period 63, direction 360/511) are dropped or normalized to
    ``None``.
    """
    source = source or str(
        _field(report, "mmsi") or _field(report, "site") or ""
    )
    observations: typing.List[WaveObservation] = []

    wave_height = _field(report, "waveheight")
    if wave_height is not None and wave_height < MISSING_HEIGHT:
        observations.append(
            WaveObservation(
                component=wave_component,
                height=float(wave_height),
                period=_clean_period(_field(report, "waveperiod")),
                direction=_clean_direction(_field(report, "wavedir")),
                source=source,
            )
        )

    swell_height = _field(report, "swellheight", "swheight")
    if swell_height is not None and swell_height < MISSING_HEIGHT:
        observations.append(
            WaveObservation(
                component=WaveComponent.SWELL,
                height=float(swell_height),
                period=_clean_period(_field(report, "swellperiod", "swperiod")),
                direction=_clean_direction(_field(report, "swelldir")),
                source=source,
            )
        )

    return observations


@attr.s(slots=True, frozen=True)
class MonthlyWaveRow:
    """One month of the quality-controlled wave-energy table."""

    month: str = attr.ib()
    report_count: int = attr.ib()
    balanced: int = attr.ib()
    negative_within_tolerance: int = attr.ib()
    incompatible: int = attr.ib()
    unclosed: int = attr.ib()
    derived: int = attr.ib()
    insufficient: int = attr.ib()
    total_energy: float = attr.ib()
    wind_energy: float = attr.ib()
    swell_energy: float = attr.ib()
    incompatible_reports: typing.Tuple[str, ...] = attr.ib()


class WaveEnergyMonthlyTable:
    """Accumulate budgets per calendar month.

    Reports whose tiny negative residual is explained by quantization are
    counted separately from genuinely incompatible reports, so the monthly
    table never conflates the two.
    """

    def __init__(self) -> None:
        self._entries: typing.Dict[str, typing.Dict[str, typing.Any]] = {}

    def add(
        self,
        month: str,
        report_id: str,
        budget: WaveEnergyBudget,
    ) -> None:
        entry = self._entries.setdefault(
            month,
            {
                "report_count": 0,
                "balanced": 0,
                "negative_within_tolerance": 0,
                "incompatible": 0,
                "unclosed": 0,
                "derived": 0,
                "insufficient": 0,
                "total_energy": 0.0,
                "wind_energy": 0.0,
                "swell_energy": 0.0,
                "incompatible_reports": [],
            },
        )
        entry["report_count"] += 1
        entry[budget.status.value] += 1
        if budget.status is BudgetStatus.INCOMPATIBLE:
            entry["incompatible_reports"].append(report_id)
        if budget.total_energy is not None:
            entry["total_energy"] += budget.total_energy
        for component in budget.components:
            if component.component is WaveComponent.WIND:
                entry["wind_energy"] += component.energy
            elif component.component is WaveComponent.SWELL:
                entry["swell_energy"] += component.energy

    def rows(self) -> typing.Tuple[MonthlyWaveRow, ...]:
        result = []
        for month in sorted(self._entries):
            entry = self._entries[month]
            result.append(
                MonthlyWaveRow(
                    month=month,
                    report_count=entry["report_count"],
                    balanced=entry["balanced"],
                    negative_within_tolerance=entry["negative_within_tolerance"],
                    incompatible=entry["incompatible"],
                    unclosed=entry["unclosed"],
                    derived=entry["derived"],
                    insufficient=entry["insufficient"],
                    total_energy=entry["total_energy"],
                    wind_energy=entry["wind_energy"],
                    swell_energy=entry["swell_energy"],
                    incompatible_reports=tuple(entry["incompatible_reports"]),
                )
            )
        return tuple(result)

    def to_dicts(self) -> typing.List[typing.Dict[str, typing.Any]]:
        return [attr.asdict(row) for row in self.rows()]
