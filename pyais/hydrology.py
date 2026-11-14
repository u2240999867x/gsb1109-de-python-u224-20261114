"""Offline hydrological data consolidation.

A payload packs several sensor sub-reports back to back. Every sub-report
occupies a fixed number of bytes (a *slot*); the final sub-report of a
payload may be truncated and is then kept as an unparsed tail segment
together with its original byte range instead of being dropped.

Two kinds of sub-reports exist:

* temperature reports, which become rows of the resulting long table, and
* station description reports, which only provide the station name.

The long table is organized by sub-report type, station id, observation
timestamp and layer. Two temperature layers of the same station never
overwrite each other, a description record never becomes a temperature row
and the first valid station name is kept forever - a later rename never
rewrites samples that were recorded earlier.
"""
import datetime
import typing
from dataclasses import dataclass
from enum import Enum


class SubReportType(Enum):
    """Known sub-report kinds packed into a payload."""

    TEMPERATURE = "temperature"
    STATION_DESCRIPTION = "station_description"


@dataclass(frozen=True)
class TemperatureRow:
    """One long-table row: a single temperature observation."""

    report_type: SubReportType
    station_id: str
    observed_at: datetime.datetime
    layer: int
    value: float
    station_name: typing.Optional[str]


@dataclass(frozen=True)
class TailSegment:
    """A trailing sub-report shorter than a complete slot.

    The segment is kept in place instead of being decoded. ``start`` and
    ``end`` are the original half-open byte offsets inside the payload
    identified by ``payload_index``.
    """

    payload_index: int
    start: int
    end: int
    raw: bytes


@dataclass(frozen=True)
class _StationInfo:
    name: str
    observed_at: datetime.datetime


_TIMESTAMP_FORMAT = "%Y%m%d%H%M%S"


def _parse_timestamp(value: typing.Union[str, datetime.datetime]) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        return value
    try:
        return datetime.datetime.strptime(value, _TIMESTAMP_FORMAT)
    except ValueError as exc:
        raise ValueError(f"invalid observation timestamp {value!r}") from exc


class HydrologyLongTable:
    """Builds a long table from a stream of hydrological payloads.

    Feed one payload after another with :meth:`feed_payload` (fixed-width
    text slots) or already decoded sub-report mappings with
    :meth:`add_sub_report`. State is kept between calls, so a truncated
    final sub-report never invalidates the complete layers received before
    it.
    """

    def __init__(self) -> None:
        self._rows: typing.List[TemperatureRow] = []
        self._row_keys: typing.Set[typing.Tuple[SubReportType, str, datetime.datetime, int]] = set()
        self._stations: typing.Dict[str, _StationInfo] = {}
        self._tails: typing.List[TailSegment] = []
        self._payload_count = 0

    @property
    def rows(self) -> typing.Tuple[TemperatureRow, ...]:
        """All temperature rows, in arrival order."""
        return tuple(self._rows)

    @property
    def tails(self) -> typing.Tuple[TailSegment, ...]:
        """All truncated tail segments, in arrival order."""
        return tuple(self._tails)

    def station_name(self, station_id: str) -> typing.Optional[str]:
        """Return the first valid name recorded for a station, if any."""
        info = self._stations.get(station_id)
        return info.name if info is not None else None

    def feed_payload(
        self, payload: typing.Union[bytes, bytearray, memoryview], slot_length: int
    ) -> typing.Tuple[TemperatureRow, ...]:
        """Split one payload into fixed-width slots and consume them.

        A slot is ASCII text with ``|`` separated fields, padded with
        spaces or NUL bytes:

        * ``T|<station_id>|<timestamp>|<layer>|<value>``
        * ``D|<station_id>|<timestamp>|<station_name>``

        Timestamps use the ``YYYYMMDDHHmmss`` format. A trailing chunk
        shorter than ``slot_length`` is stored as a tail segment together
        with its original byte range; complete slots remain queryable.
        Returns the temperature rows newly created by this payload.
        """
        if slot_length <= 0:
            raise ValueError("slot_length must be a positive integer")

        data = bytes(payload)
        payload_index = self._payload_count
        self._payload_count += 1
        new_rows: typing.List[TemperatureRow] = []

        for start in range(0, len(data), slot_length):
            end = min(start + slot_length, len(data))
            slot = data[start:end]
            if len(slot) < slot_length:
                # The last sub-report is truncated: keep it in place.
                self._tails.append(TailSegment(payload_index, start, end, slot))
                break
            record = _decode_slot(slot)
            row = self.add_sub_report(record)
            if row is not None:
                new_rows.append(row)

        return tuple(new_rows)

    def add_sub_report(
        self, record: typing.Mapping[str, typing.Any]
    ) -> typing.Optional[TemperatureRow]:
        """Consume one decoded sub-report mapping.

        Returns the new :class:`TemperatureRow` for a temperature report,
        or ``None`` for description reports, invalid descriptions and
        duplicate temperature observations.
        """
        report_type = _coerce_report_type(record["type"])
        station_id = str(record["station_id"])
        observed_at = _parse_timestamp(record["observed_at"])

        if report_type == SubReportType.STATION_DESCRIPTION:
            self._register_description(station_id, observed_at, record.get("name"))
            return None

        layer = int(record["layer"])
        value = float(record["value"])
        key = (report_type, station_id, observed_at, layer)
        if key in self._row_keys:
            # Layers never overwrite each other; the first sample wins.
            return None
        self._row_keys.add(key)

        row = TemperatureRow(
            report_type=report_type,
            station_id=station_id,
            observed_at=observed_at,
            layer=layer,
            value=value,
            station_name=self.station_name(station_id),
        )
        self._rows.append(row)
        return row

    def _register_description(
        self,
        station_id: str,
        observed_at: datetime.datetime,
        name: typing.Any,
    ) -> None:
        if name is None:
            return
        name = str(name).strip()
        if not name:
            # An empty description is not a valid naming record.
            return
        if station_id in self._stations:
            # Renames never rewrite existing samples or the registered name.
            return
        self._stations[station_id] = _StationInfo(name=name, observed_at=observed_at)


def _coerce_report_type(value: typing.Union[str, SubReportType]) -> SubReportType:
    if isinstance(value, SubReportType):
        return value
    try:
        return SubReportType(value)
    except ValueError as exc:
        raise ValueError(f"unknown sub-report type {value!r}") from exc


def _decode_slot(slot: bytes) -> typing.Dict[str, typing.Any]:
    text = slot.rstrip(b" \x00").decode("ascii")
    fields = text.split("|")
    kind = fields[0]

    if kind == "T":
        if len(fields) != 5:
            raise ValueError(f"malformed temperature slot {text!r}")
        _, station_id, timestamp, layer, value = fields
        try:
            return {
                "type": SubReportType.TEMPERATURE,
                "station_id": station_id,
                "observed_at": _parse_timestamp(timestamp),
                "layer": int(layer),
                "value": float(value),
            }
        except ValueError as exc:
            raise ValueError(f"malformed temperature slot {text!r}") from exc

    if kind == "D":
        if len(fields) != 4:
            raise ValueError(f"malformed station description slot {text!r}")
        _, station_id, timestamp, name = fields
        return {
            "type": SubReportType.STATION_DESCRIPTION,
            "station_id": station_id,
            "observed_at": _parse_timestamp(timestamp),
            "name": name,
        }

    raise ValueError(f"unknown sub-report kind {kind!r} in slot {text!r}")


def build_long_table(
    payloads: typing.Iterable[typing.Union[bytes, bytearray, memoryview]],
    slot_length: int,
) -> HydrologyLongTable:
    """Convenience helper that feeds several payloads into one table."""
    table = HydrologyLongTable()
    for payload in payloads:
        table.feed_payload(payload, slot_length)
    return table
