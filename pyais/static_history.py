"""Historical view over AIS Static Data Reports (message type 24).

A type 24 report is split into two independent halves:

* Part A carries the vessel name.
* Part B carries the remaining static data. For ordinary vessels this
  includes the ship dimensions (to_bow/to_stern/to_port/to_starboard);
  for auxiliary craft (MMSI of the form 98XXXYYYY) the same bits hold
  the mothership MMSI instead.

Both halves arrive independently, may originate from different import
batches and each half has its own expiry. StaticDataHistory keeps a
version chain per half, so that queries can be answered for any given
observation time with exactly the data that was valid at that moment.
Out of order imports are safe: a late imported, older record never
displaces a newer one.
"""
import bisect
import dataclasses
import math
import time
import typing
from enum import Enum

from pyais.messages import (
    AISSentence,
    MessageType24PartA,
    MessageType24PartB,
    MessageType24PartBAuxiliaryCraft,
)

PART_A_FIELDS = ('shipname',)
PART_B_FIELDS = (
    'ship_type', 'vendorid', 'model', 'serial', 'callsign',
    'to_bow', 'to_stern', 'to_port', 'to_starboard', 'mothership_mmsi',
)


def now() -> float:
    """Current time as UNIX time (seconds)"""
    return time.time()


class StaticHalfState(Enum):
    """Lifecycle state of one half (Part A or Part B) at query time."""
    VALID = 'valid'
    EXPIRED = 'expired'
    MISSING = 'missing'


@dataclasses.dataclass
class _StaticHalf:
    """A single observed version of one half of a static data report."""
    observed_at: float
    expires_at: float  # math.inf if the record never expires
    fields: typing.Dict[str, typing.Any]

    def is_valid_at(self, at_epoch: float) -> bool:
        return self.observed_at <= at_epoch < self.expires_at


@dataclasses.dataclass
class StaticSnapshot:
    """The static data of a single vessel, as seen at `observed_at`.

    Fields of a half that is not VALID at query time are None. In
    particular, dimensions of an expired Part B are never leaked into
    the snapshot. If a half expired, its `*_observed_at`/`*_expires_at`
    attributes carry the evidence of when it was seen and when it lapsed.
    """
    mmsi: int
    observed_at: float
    a_state: StaticHalfState
    b_state: StaticHalfState
    # Part A
    shipname: typing.Optional[str] = None
    a_observed_at: typing.Optional[float] = None
    a_expires_at: typing.Optional[float] = None
    # Part B
    b_observed_at: typing.Optional[float] = None
    b_expires_at: typing.Optional[float] = None
    is_auxiliary: typing.Optional[bool] = None
    ship_type: typing.Optional[int] = None
    vendorid: typing.Optional[str] = None
    model: typing.Optional[int] = None
    serial: typing.Optional[int] = None
    callsign: typing.Optional[str] = None
    to_bow: typing.Optional[int] = None
    to_stern: typing.Optional[int] = None
    to_port: typing.Optional[int] = None
    to_starboard: typing.Optional[int] = None
    mothership_mmsi: typing.Optional[int] = None


class StaticDataHistory:
    """
    Maintains the history of static data reports (message type 24) per vessel.

    Part A and Part B are tracked as independent version chains, because the
    two halves may come from different batches and each half expires on its
    own. Records are selected by their observation time, never by their import
    order: a late imported, older Part B can not overwrite a newer one.

    :param ttl_in_seconds:  default time to live of a record in seconds.
                            May be overridden per update. None means records
                            never expire.
    """

    def __init__(self, ttl_in_seconds: typing.Optional[float] = 86400) -> None:
        self.ttl_in_seconds: typing.Optional[float] = ttl_in_seconds
        # { mmsi: [_StaticHalf, ...] } sorted after observed_at
        self._part_a: typing.Dict[int, typing.List[_StaticHalf]] = {}
        self._part_b: typing.Dict[int, typing.List[_StaticHalf]] = {}

    def __enter__(self) -> "StaticDataHistory":
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        return None

    def update(
        self,
        msg: typing.Union[AISSentence, MessageType24PartA, MessageType24PartB, MessageType24PartBAuxiliaryCraft],
        ts_epoch: typing.Optional[float] = None,
        ttl_in_seconds: typing.Optional[float] = None,
    ) -> bool:
        """Ingest a single static data report.

        NOTE: accepts raw AISSentences as well as decoded messages.

        :param msg:             a decoded message of type 24 (Part A or Part B)
                                or a raw AISSentence holding one.
        :param ts_epoch:        observation time of the message as UNIX time (seconds).
                                If None (default) the current time is used.
        :param ttl_in_seconds:  time to live for this record. Overrides the
                                instance default. None falls back to the default.
        :return:                True if the record became the newest version of its
                                half. False if a newer version already exists, i.e. a
                                late imported, older record that does not take effect.
        """
        decoded = msg.decode() if isinstance(msg, AISSentence) else msg

        field_names: typing.Tuple[str, ...]
        if isinstance(decoded, MessageType24PartA):
            chain = self._part_a
            field_names = PART_A_FIELDS
        elif isinstance(decoded, (MessageType24PartB, MessageType24PartBAuxiliaryCraft)):
            chain = self._part_b
            field_names = PART_B_FIELDS
        else:
            raise TypeError(
                'StaticDataHistory only accepts message type 24 (Part A or Part B), '
                f'got {type(decoded).__name__}'
            )

        observed_at = now() if ts_epoch is None else float(ts_epoch)
        ttl = self.ttl_in_seconds if ttl_in_seconds is None else ttl_in_seconds
        expires_at = math.inf if ttl is None else observed_at + ttl

        fields = {name: getattr(decoded, name) for name in field_names if hasattr(decoded, name)}
        record = _StaticHalf(observed_at=observed_at, expires_at=expires_at, fields=fields)

        mmsi = int(decoded.mmsi)
        versions = chain.setdefault(mmsi, [])
        # Version chains are ordered after observed_at. A late imported,
        # older record is inserted into the chain, but never displaces
        # newer versions.
        observed = [version.observed_at for version in versions]
        index = bisect.bisect_left(observed, observed_at)
        if index < len(versions) and versions[index].observed_at == observed_at:
            # Re-import of the same observation: replace it in place.
            versions[index] = record
        else:
            versions.insert(index, record)
        return index == len(versions) - 1

    @staticmethod
    def _latest_before(versions: typing.List[_StaticHalf], at_epoch: float) -> typing.Optional[_StaticHalf]:
        """The newest version observed at or before at_epoch, or None."""
        observed = [version.observed_at for version in versions]
        index = bisect.bisect_right(observed, at_epoch) - 1
        if index < 0:
            return None
        return versions[index]

    def query(self, mmsi: typing.Union[str, int], at_epoch: typing.Optional[float] = None) -> StaticSnapshot:
        """Assemble the static data of a vessel as seen at `at_epoch`.

        Both halves are resolved independently: the snapshot may combine a
        Part A and a Part B that originate from different batches, each with
        its own expiry. If a half has no valid version at query time, its
        fields are None and its state reports why (MISSING or EXPIRED).

        :param mmsi:     the MMSI of the vessel.
        :param at_epoch: the observation time as UNIX time (seconds).
                         If None (default) the current time is used.
        """
        at = now() if at_epoch is None else float(at_epoch)
        mmsi = int(mmsi)

        snapshot = StaticSnapshot(
            mmsi=mmsi,
            observed_at=at,
            a_state=StaticHalfState.MISSING,
            b_state=StaticHalfState.MISSING,
        )

        part_a = self._latest_before(self._part_a.get(mmsi, []), at)
        if part_a is not None:
            snapshot.a_observed_at = part_a.observed_at
            snapshot.a_expires_at = part_a.expires_at
            if part_a.is_valid_at(at):
                snapshot.a_state = StaticHalfState.VALID
                snapshot.shipname = part_a.fields.get('shipname')
            else:
                snapshot.a_state = StaticHalfState.EXPIRED

        part_b = self._latest_before(self._part_b.get(mmsi, []), at)
        if part_b is not None:
            snapshot.b_observed_at = part_b.observed_at
            snapshot.b_expires_at = part_b.expires_at
            if part_b.is_valid_at(at):
                snapshot.b_state = StaticHalfState.VALID
                # Interpret the payload after the identity of the message:
                # ordinary vessels carry dimensions, auxiliary craft carry
                # the mothership MMSI instead.
                snapshot.is_auxiliary = 'mothership_mmsi' in part_b.fields
                for name, value in part_b.fields.items():
                    setattr(snapshot, name, value)
            else:
                snapshot.b_state = StaticHalfState.EXPIRED

        return snapshot
