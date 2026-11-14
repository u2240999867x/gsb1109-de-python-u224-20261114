"""Pick the best wheel for a set of offline environments.

A wheel filename (per :pep:`427`) ends in a *compatibility tag*, made of the
``{python tag}-{abi tag}-{platform tag}`` triple. Wheels that support several
combinations compress those triples into a single filename using the ``.``
separator (e.g. ``py2.py3-none-any``); this module expands such compressed
triples back into the individual tags before matching them.

Each offline environment declares an ordered list of tags it accepts -- the
earlier a tag shows up in that list, the more preferred it is. Candidates are
already known to belong to the same distribution, so selection only considers
tag preference and the wheel build tag.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from installer.utils import parse_wheel_filename

if TYPE_CHECKING:
    from installer.utils import WheelFilename

__all__ = [
    "WheelChoice",
    "expand_wheel_tags",
    "select_wheel",
    "select_wheels",
]

# A wheel's tag triple is "{python tag}-{abi tag}-{platform tag}".
_TAG_PARTS = 3


@dataclass(frozen=True)
class WheelChoice:
    """A wheel selected for an environment.

    :param filename: filename of the chosen wheel
    :param matched_tag: compatibility tag of the wheel that matched the
        environment's preference list
    :param tag_rank: position of ``matched_tag`` in the environment's
        preference list (lower means more preferred)
    :param reason: human-readable explanation of why this wheel was chosen,
        including the tie-break when several wheels are equally good
    """

    filename: str
    matched_tag: str
    tag_rank: int
    reason: str


def expand_wheel_tags(tag: str) -> frozenset[str]:
    """Expand a (possibly compressed) wheel tag into individual tags.

    Each of the three parts of a tag may itself be a ``.``-separated list of
    alternatives; the result is the full cross-product of those alternatives,
    with each combination re-joined into a ``python-abi-platform`` string.

    :param tag: the tag component of a wheel filename
    :raises ValueError: if the tag does not have exactly three parts
    """
    parts = tag.split("-")
    if len(parts) != _TAG_PARTS:
        message = f"Not a valid wheel tag (expected python-abi-platform): {tag}"
        raise ValueError(message)

    python_tags, abi_tags, platform_tags = (part.split(".") for part in parts)
    return frozenset(
        f"{python_tag}-{abi_tag}-{platform_tag}"
        for python_tag in python_tags
        for abi_tag in abi_tags
        for platform_tag in platform_tags
    )


def _parse_build_tag(build_tag: str | None) -> tuple[int, str]:
    """Split a PEP 427 build tag into its numeric part and the remainder.

    Build tags always start with one or more digits. The numeric part decides
    the primary build ordering (compared as a number, so ``10`` beats ``2``)
    and the trailing string breaks further ties. A missing build tag sorts
    before any present one.
    """
    if build_tag is None:
        return (-1, "")

    digits = 0
    while digits < len(build_tag) and build_tag[digits].isdigit():
        digits += 1
    return (int(build_tag[:digits]), build_tag[digits:])


def _candidate_key(parsed: "WheelFilename", rank: int) -> tuple[int, int, str]:
    """Ordering key: tag preference first, then the build tag numerically.

    A smaller rank is more preferred while a larger build number wins, so the
    build number is negated. Missing build tags parse as ``-1`` and thus lose
    to every present build tag.
    """
    build_number, build_suffix = _parse_build_tag(parsed.build_tag)
    return (rank, -build_number, build_suffix)


def _dedupe_candidates(
    candidates: Iterable[str],
) -> dict[str, "WheelFilename"]:
    """Parse filenames once and reject duplicate filenames."""
    parsed_by_filename: dict[str, WheelFilename] = {}
    for filename in candidates:
        if filename in parsed_by_filename:
            message = f"Duplicate candidate wheel filename: {filename}"
            raise ValueError(message)
        parsed_by_filename[filename] = parse_wheel_filename(filename)
    return parsed_by_filename


def select_wheel(
    candidates: Iterable[str],
    supported_tags: Sequence[str],
) -> WheelChoice | tuple[WheelChoice, ...] | None:
    """Select the best wheel for one offline environment.

    Selection proceeds in two stages:

    1. Expand every candidate's compressed tag and keep only those containing
       a tag from ``supported_tags``. The winner is the wheel whose best match
       sits furthest up the environment's preference list.
    2. Among wheels tied on tag preference, compare the build tag by its
       leading number (so build ``10`` outranks build ``2``) and then by the
       trailing suffix. Wheels without a build tag rank lowest.

    When several distinct filenames remain tied on every criterion, none of
    them is discarded: all of them are returned together with a reason that
    records the tie. The order of ``candidates`` never affects the result.

    :param candidates: wheel filenames, all from the same distribution
    :param supported_tags: compatibility tags accepted by the environment,
        most preferred first
    :return: one choice, a tuple of tied choices, or ``None`` if nothing
        matches
    """
    parsed_by_filename = _dedupe_candidates(candidates)
    ranks: dict[str, int] = {}
    for rank, tag in enumerate(supported_tags):
        # Keep the first position if a tag is listed more than once.
        ranks.setdefault(tag, rank)

    scored: list[tuple[tuple[int, int, str], str, str, WheelFilename]] = []
    for filename, parsed in parsed_by_filename.items():
        matches = expand_wheel_tags(parsed.tag) & ranks.keys()
        if not matches:
            continue
        matched_tag = min(matches, key=lambda tag: ranks[tag])
        rank = ranks[matched_tag]
        scored.append((_candidate_key(parsed, rank), filename, matched_tag, parsed))

    if not scored:
        return None

    best_key = min(item[0] for item in scored)
    winners = [item for item in scored if item[0] == best_key]
    # Sort by filename so the outcome does not depend on input ordering.
    winners.sort(key=lambda item: item[1])

    rank = best_key[0]
    if len(winners) == 1:
        _, filename, matched_tag, parsed = winners[0]
        build_number, _ = _parse_build_tag(parsed.build_tag)
        reason = f"best matching tag {matched_tag!r} (rank {rank})" + _build_reason(
            parsed.build_tag, build_number
        )
        return WheelChoice(filename, matched_tag, rank, reason)

    tied_build_tag = winners[0][3].build_tag
    build_number, build_suffix = _parse_build_tag(tied_build_tag)
    filenames = ", ".join(filename for _, filename, _, _ in winners)
    if tied_build_tag is None:
        build_detail = "no build tag"
    else:
        build_detail = f"build tag (number {build_number}, suffix {build_suffix!r})"
    reason = (
        f"tied on matching tag rank {rank} and {build_detail}; "
        f"keeping all tied wheels: {filenames}"
    )
    return tuple(
        WheelChoice(filename, matched_tag, rank, reason)
        for _, filename, matched_tag, _ in winners
    )


def _build_reason(build_tag: str | None, build_number: int) -> str:
    """Describe the build-tag part of a selection reason."""
    if build_tag is None:
        return "; no build tag"
    return f"; build tag {build_tag!r} (number {build_number})"


def select_wheels(
    candidates: Iterable[str],
    environments: dict[str, Sequence[str]],
) -> dict[str, WheelChoice | tuple[WheelChoice, ...] | None]:
    """Select wheels independently for several offline environments.

    :param candidates: wheel filenames, all from the same distribution
    :param environments: mapping of environment name to its ordered list of
        accepted compatibility tags (most preferred first)
    :return: mapping of environment name to the result of
        :func:`select_wheel` for that environment
    """
    # Materialize the candidates once, so every environment sees the same set
    # regardless of the iterable being a one-shot iterator.
    candidate_list = list(candidates)
    return {
        environment: select_wheel(candidate_list, supported_tags)
        for environment, supported_tags in environments.items()
    }
