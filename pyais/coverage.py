"""Receive-completeness ("coverage") reporting for AIS receiver stations.

The archive publishes the completeness of the received data.  The old
algorithm counted every night a station was offline as plain zero.  This
module instead derives, for each time bucket, the *effective coverage
duration* from the station open intervals and the outage records, and
the share of covered sub-periods that actually contain at least one
valid (decodable) message.

Semantics
---------
* Intervals are half-open: ``[start, end)``.
* Overlapping stations are combined as a union, so overlapping coverage
  is never double counted.
* Adjacent intervals are merged; splitting a station's open hours into
  neighbouring segments therefore does not change any report.
* Windows whose open/closed status is unknown are listed separately and
  are never counted as online.
* If a whole bucket is offline (no effective coverage), the message
  ratio is left blank (``None``).
"""
import argparse
import csv
import datetime as dt
import sys
import typing
from dataclasses import dataclass
from typing import Iterable, Iterator, List, Optional, Tuple

Interval = Tuple[dt.datetime, dt.datetime]

STATUS_OPEN = 'open'
STATUS_UNKNOWN = 'unknown'

UTC = dt.timezone.utc


@dataclass(frozen=True)
class StationWindow:
    """A period during which a station is known (or not known) to be open."""
    station: str
    start: dt.datetime
    end: dt.datetime
    status: str = STATUS_OPEN


@dataclass(frozen=True)
class Outage:
    """A recorded outage (停采) of a station."""
    station: str
    start: dt.datetime
    end: dt.datetime


@dataclass(frozen=True)
class MessageRecord:
    """An anonymized historical message with its reception timestamp."""
    ts: dt.datetime
    payload: str


@dataclass
class BucketReport:
    """Completeness figures for a single time bucket."""
    start: dt.datetime
    end: dt.datetime
    covered_seconds: float = 0.0
    outage_seconds: float = 0.0
    unknown_seconds: float = 0.0
    active_subperiods: int = 0
    observed_subperiods: int = 0

    @property
    def message_ratio(self) -> Optional[float]:
        """Share of covered sub-periods with at least one valid message.

        ``None`` (rendered blank) when the bucket has no effective
        coverage at all, e.g. the whole bucket was offline.
        """
        if self.active_subperiods == 0:
            return None
        return self.observed_subperiods / self.active_subperiods


@dataclass
class DailyReport:
    """Completeness figures aggregated over a UTC day."""
    day: dt.date
    covered_seconds: float = 0.0
    outage_seconds: float = 0.0
    unknown_seconds: float = 0.0
    active_subperiods: int = 0
    observed_subperiods: int = 0

    @property
    def message_ratio(self) -> Optional[float]:
        if self.active_subperiods == 0:
            return None
        return self.observed_subperiods / self.active_subperiods


def _aware(ts: dt.datetime) -> dt.datetime:
    """Naive timestamps are interpreted as UTC."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts


def merge_intervals(intervals: Iterable[Interval]) -> List[Interval]:
    """Union of intervals; overlapping *and adjacent* ones are merged."""
    merged: List[Interval] = []
    for start, end in sorted((_aware(s), _aware(e)) for s, e in intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def subtract_intervals(intervals: Iterable[Interval], cuts: Iterable[Interval]) -> List[Interval]:
    """All points of ``intervals`` that are not covered by ``cuts``."""
    merged_cuts = merge_intervals(cuts)
    result: List[Interval] = []
    for start, end in merge_intervals(intervals):
        cursor = start
        for cut_start, cut_end in merged_cuts:
            if cut_end <= cursor or cut_start >= end:
                continue
            if cut_start > cursor:
                result.append((cursor, min(cut_start, end)))
            cursor = max(cursor, cut_end)
            if cursor >= end:
                break
        if cursor < end:
            result.append((cursor, end))
    return result


def intersect_intervals(intervals: Iterable[Interval], others: Iterable[Interval]) -> List[Interval]:
    """All points covered by both ``intervals`` and ``others``."""
    return subtract_intervals(intervals, subtract_intervals(intervals, others))


def clip_interval(start: dt.datetime, end: dt.datetime,
                  lo: dt.datetime, hi: dt.datetime) -> Optional[Interval]:
    """Intersection of ``[start, end)`` with ``[lo, hi)`` or ``None``."""
    clipped = (max(start, lo), min(end, hi))
    return clipped if clipped[1] > clipped[0] else None


def total_seconds(intervals: Iterable[Interval]) -> float:
    return sum((end - start).total_seconds() for start, end in intervals)


def is_valid_message(payload: str) -> bool:
    """A message is valid if it decodes as a proper AIS/NMEA sentence."""
    try:
        from pyais import decode
        decode(payload)
        return True
    except Exception:
        return False


def _subperiod_grid(lo: dt.datetime, hi: dt.datetime,
                    size: dt.timedelta) -> Iterator[Interval]:
    cursor = lo
    while cursor < hi:
        nxt = min(cursor + size, hi)
        yield cursor, nxt
        cursor = nxt


def _overlaps_any(interval: Interval, merged: List[Interval]) -> bool:
    start, end = interval
    for m_start, m_end in merged:
        if m_end <= start:
            continue
        if m_start >= end:
            break
        return True
    return False


def bucket_report(windows: Iterable[StationWindow],
                  outages: Iterable[Outage],
                  valid_times: Iterable[dt.datetime],
                  bucket: Interval,
                  sub_period: dt.timedelta) -> BucketReport:
    """Effective coverage and message ratio for a single time bucket."""
    lo, hi = bucket
    open_by_station: typing.Dict[str, List[Interval]] = {}
    unknown_windows: List[Interval] = []
    for window in windows:
        clipped = clip_interval(_aware(window.start), _aware(window.end), lo, hi)
        if clipped is None:
            continue
        if window.status == STATUS_OPEN:
            open_by_station.setdefault(window.station, []).append(clipped)
        else:
            unknown_windows.append(clipped)

    outages_by_station: typing.Dict[str, List[Interval]] = {}
    for outage in outages:
        clipped = clip_interval(_aware(outage.start), _aware(outage.end), lo, hi)
        if clipped is not None:
            outages_by_station.setdefault(outage.station, []).append(clipped)

    covered: List[Interval] = []
    offline: List[Interval] = []
    for station, station_windows in open_by_station.items():
        station_outages = merge_intervals(outages_by_station.get(station, []))
        offline.extend(intersect_intervals(station_windows, station_outages))
        covered.extend(subtract_intervals(station_windows, station_outages))
    covered = merge_intervals(covered)
    offline = merge_intervals(offline)

    unknown = subtract_intervals(unknown_windows, covered)
    unknown = subtract_intervals(unknown, offline)

    valid_sorted = sorted(ts for ts in valid_times if lo <= _aware(ts) < hi)
    active = 0
    observed = 0
    for sub in _subperiod_grid(lo, hi, sub_period):
        if not _overlaps_any(sub, covered):
            continue
        active += 1
        sub_lo, sub_hi = sub
        if any(sub_lo <= ts < sub_hi for ts in valid_sorted):
            observed += 1

    return BucketReport(
        start=lo,
        end=hi,
        covered_seconds=total_seconds(covered),
        outage_seconds=total_seconds(offline),
        unknown_seconds=total_seconds(unknown),
        active_subperiods=active,
        observed_subperiods=observed,
    )


def iter_buckets(start: dt.datetime, end: dt.datetime,
                 bucket_size: dt.timedelta) -> Iterator[Interval]:
    """Bucket grid over ``[start, end)`` aligned to the UNIX epoch (UTC)."""
    start = _aware(start)
    end = _aware(end)
    size_s = bucket_size.total_seconds()
    if size_s <= 0:
        raise ValueError('bucket_size must be positive')
    anchor = dt.datetime.fromtimestamp(
        int(start.timestamp() // size_s * size_s), tz=UTC,
    )
    cursor = anchor
    while cursor < end:
        nxt = cursor + bucket_size
        clipped = clip_interval(cursor, nxt, start, end)
        if clipped is not None:
            yield clipped
        cursor = nxt


def bucket_reports(windows: Iterable[StationWindow],
                   outages: Iterable[Outage],
                   messages: Iterable[MessageRecord],
                   start: dt.datetime,
                   end: dt.datetime,
                   bucket_size: dt.timedelta = dt.timedelta(hours=1),
                   sub_period: dt.timedelta = dt.timedelta(minutes=5),
                   validator: typing.Callable[[str], bool] = is_valid_message,
                   ) -> List[BucketReport]:
    """Bucket-by-bucket completeness over ``[start, end)``."""
    windows = list(windows)
    outages = list(outages)
    valid_times = sorted(_aware(m.ts) for m in messages if validator(m.payload))
    return [
        bucket_report(windows, outages, valid_times, bucket, sub_period)
        for bucket in iter_buckets(start, end, bucket_size)
    ]


def daily_reports(buckets: Iterable[BucketReport]) -> List[DailyReport]:
    """Aggregate bucket reports into the daily (UTC) report."""
    by_day: typing.Dict[dt.date, DailyReport] = {}
    for bucket in buckets:
        day = bucket.start.astimezone(UTC).date()
        report = by_day.setdefault(day, DailyReport(day=day))
        report.covered_seconds += bucket.covered_seconds
        report.outage_seconds += bucket.outage_seconds
        report.unknown_seconds += bucket.unknown_seconds
        report.active_subperiods += bucket.active_subperiods
        report.observed_subperiods += bucket.observed_subperiods
    return [by_day[day] for day in sorted(by_day)]


def _parse_ts(raw: str) -> dt.datetime:
    raw = raw.strip()
    try:
        return dt.datetime.fromtimestamp(float(raw), tz=UTC)
    except ValueError:
        pass
    if raw.endswith(('Z', 'z')):
        raw = raw[:-1] + '+00:00'
    return _aware(dt.datetime.fromisoformat(raw))


def load_windows_csv(path: str) -> List[StationWindow]:
    """Load station windows: ``station,start,end[,status]``."""
    windows = []
    with open(path, newline='', encoding='utf-8') as csvfile:
        for row in csv.DictReader(csvfile):
            windows.append(StationWindow(
                station=row['station'].strip(),
                start=_parse_ts(row['start']),
                end=_parse_ts(row['end']),
                status=(row.get('status') or STATUS_OPEN).strip().lower(),
            ))
    return windows


def load_outages_csv(path: str) -> List[Outage]:
    """Load outage records: ``station,start,end``."""
    outages = []
    with open(path, newline='', encoding='utf-8') as csvfile:
        for row in csv.DictReader(csvfile):
            outages.append(Outage(
                station=row['station'].strip(),
                start=_parse_ts(row['start']),
                end=_parse_ts(row['end']),
            ))
    return outages


def load_messages_csv(path: str) -> List[MessageRecord]:
    """Load anonymized historical messages: ``timestamp,payload``.

    The payload is everything after the first comma, so NMEA sentences
    (which contain commas themselves) do not need to be quoted.
    """
    messages = []
    with open(path, newline='', encoding='utf-8') as csvfile:
        header = csvfile.readline()
        if not header.lower().startswith('timestamp'):
            csvfile.seek(0)
        for line in csvfile:
            line = line.rstrip('\r\n')
            if not line.strip():
                continue
            ts_raw, _, payload = line.partition(',')
            messages.append(MessageRecord(ts=_parse_ts(ts_raw), payload=payload.strip()))
    return messages


def _ratio_cell(ratio: Optional[float]) -> str:
    """Blank cell when the ratio is undefined (bucket fully offline)."""
    return '' if ratio is None else f'{ratio:.6f}'


def write_bucket_csv(buckets: Iterable[BucketReport], fileobj: typing.IO[str]) -> None:
    writer = csv.writer(fileobj)
    writer.writerow(('bucket_start', 'bucket_end', 'covered_seconds',
                     'outage_seconds', 'unknown_seconds', 'message_ratio'))
    for bucket in buckets:
        writer.writerow((
            bucket.start.isoformat(),
            bucket.end.isoformat(),
            f'{bucket.covered_seconds:.0f}',
            f'{bucket.outage_seconds:.0f}',
            f'{bucket.unknown_seconds:.0f}',
            _ratio_cell(bucket.message_ratio),
        ))


def write_daily_csv(reports: Iterable[DailyReport], fileobj: typing.IO[str]) -> None:
    writer = csv.writer(fileobj)
    writer.writerow(('date', 'covered_seconds', 'outage_seconds',
                     'unknown_seconds', 'message_ratio'))
    for report in reports:
        writer.writerow((
            report.day.isoformat(),
            f'{report.covered_seconds:.0f}',
            f'{report.outage_seconds:.0f}',
            f'{report.unknown_seconds:.0f}',
            _ratio_cell(report.message_ratio),
        ))


def _data_span(windows: List[StationWindow],
               outages: List[Outage],
               messages: List[MessageRecord]) -> Interval:
    starts = [w.start for w in windows] + [o.start for o in outages]
    ends = [w.end for w in windows] + [o.end for o in outages]
    if messages:
        starts.extend(m.ts for m in messages)
        ends.extend(m.ts for m in messages)
    if not starts:
        raise ValueError('no data to derive the report range from')
    return _aware(min(starts)), _aware(max(ends))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog='pyais.coverage',
        description='Per-bucket effective coverage and message completeness '
                    'from station open intervals, outage records and '
                    'anonymized historical messages.',
    )
    parser.add_argument('windows', help='CSV: station,start,end[,status]')
    parser.add_argument('outages', help='CSV: station,start,end')
    parser.add_argument('messages', help='CSV: timestamp,payload')
    parser.add_argument('--start', help='report range start (ISO 8601); '
                                        'defaults to the data span')
    parser.add_argument('--end', help='report range end (ISO 8601); '
                                      'defaults to the data span')
    parser.add_argument('--bucket-minutes', type=float, default=60.0,
                        help='time bucket size in minutes (default: 60)')
    parser.add_argument('--sub-minutes', type=float, default=5.0,
                        help='sub-period size in minutes (default: 5)')
    parser.add_argument('--buckets-out', help='write per-bucket CSV here')
    parser.add_argument('--daily-out', help='write the daily report CSV here '
                                            '(default: stdout)')
    args = parser.parse_args(argv)

    windows = load_windows_csv(args.windows)
    outages = load_outages_csv(args.outages)
    messages = load_messages_csv(args.messages)

    span_start, span_end = _data_span(windows, outages, messages)
    start = _parse_ts(args.start) if args.start else span_start
    end = _parse_ts(args.end) if args.end else span_end

    buckets = bucket_reports(
        windows, outages, messages, start, end,
        bucket_size=dt.timedelta(minutes=args.bucket_minutes),
        sub_period=dt.timedelta(minutes=args.sub_minutes),
    )

    if args.buckets_out:
        with open(args.buckets_out, 'w', newline='', encoding='utf-8') as out:
            write_bucket_csv(buckets, out)
    else:
        write_bucket_csv(buckets, sys.stdout)

    daily = daily_reports(buckets)
    if args.daily_out:
        with open(args.daily_out, 'w', newline='', encoding='utf-8') as out:
            write_daily_csv(daily, out)
    else:
        write_daily_csv(daily, sys.stdout)
    return 0


if __name__ == '__main__':
    sys.exit(main())
