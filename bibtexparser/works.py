"""Work/version indexing across bibliographic records from different sources.

This module builds a non-destructive index over a `Library`, grouping
entries into works (intellectual units) and versions (individual source
records). Records are linked by:

- canonical DOIs: different spellings of the same DOI (case, ``doi:``
  prefix, ``doi.org`` URLs) identify one work;
- explicit erratum/correction relations: biblatex ``related``/``relatedtype``
  or dedicated fields such as ``erratumof``;
- title-based candidates: records without a DOI whose (normalized) title
  matches exactly one known work.

Every source record is preserved: the library is never modified, and each
entry ends up either as a version of exactly one work or in
`WorkIndex.pending`.

A record carrying two or more mutually exclusive authoritative identifiers
(e.g. two different DOIs) is a "bridge" record. It is never used to merge
works; instead it is reported in `WorkIndex.pending`, so that the
originally independent works at its ends remain separate.
"""

import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field

from bibtexparser.library import Library
from bibtexparser.model import Entry

__all__ = [
    "canonicalize_doi",
    "normalize_title",
    "Version",
    "Work",
    "WorkRelation",
    "PendingRecord",
    "WorkIndex",
    "build_work_index",
]

_DOI_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "doi.org/",
    "dx.doi.org/",
    "doi:",
)

_DOI_PATTERN = re.compile(r"^10\.\d{4,9}/\S+$")

_ERRATUM_ENTRY_TYPES = ("erratum", "corrigendum", "correction")

_ERRATUM_RELATED_TYPES = (
    "erratum",
    "erratumof",
    "corrigendum",
    "correction",
    "correctionof",
    "corrects",
)

#: Fields whose value references the corrected work (by DOI or entry key).
_ERRATUM_TARGET_FIELDS = ("erratumof", "erratumfor", "correctionof", "corrects")

_ERRATUM_TITLE_PREFIX = re.compile(r"^(erratum|errata|corrigendum|correction)( to)?\s+")


def canonicalize_doi(value: object) -> str | None:
    """Canonical form of a DOI, or ``None`` if the value is not a DOI.

    Handles common spellings of the same DOI: ``doi:`` prefixes,
    ``doi.org``/``dx.doi.org`` URLs (http and https), surrounding
    whitespace and case differences. The canonical form is the bare,
    lowercased DOI (DOIs are case-insensitive).
    """
    if value is None:
        return None
    text = str(value).strip()
    lowered = text.lower()
    for prefix in _DOI_PREFIXES:
        if lowered.startswith(prefix):
            text = text[len(prefix) :]
            break
    text = text.strip().rstrip("/").strip().lower()
    if not _DOI_PATTERN.match(text):
        return None
    return text


def normalize_title(value: object) -> str:
    """Normalized title for matching records across sources.

    Lowercases, strips bibtex/LaTeX markup characters and punctuation, and
    removes a leading erratum/correction prefix (e.g. "Erratum to: ..."),
    so that an erratum's title matches the corrected work's title.
    """
    if value is None:
        return ""
    text = str(value).lower()
    text = re.sub(r"[{}\\$]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = " ".join(text.split())
    match = _ERRATUM_TITLE_PREFIX.match(text)
    if match is not None and text[match.end() :]:
        text = text[match.end() :]
    return text


def _field_value(entry: Entry, key: str) -> object | None:
    field = entry.get(key)
    return None if field is None else field.value


def _canonical_dois_of(entry: Entry, doi_fields: tuple[str, ...]) -> list[str]:
    """Distinct canonical DOIs carried by an entry, in order of appearance."""
    dois = []
    for field_key in doi_fields:
        value = _field_value(entry, field_key)
        if value is None:
            continue
        for token in re.split(r"[\s,;]+", str(value)):
            doi = canonicalize_doi(token)
            if doi is not None and doi not in dois:
                dois.append(doi)
    return dois


def _erratum_target_refs(entry: Entry) -> list[str]:
    """Raw references (DOIs or entry keys) to the work this entry corrects."""
    refs = []
    relatedtype = _field_value(entry, "relatedtype")
    if relatedtype is not None and str(relatedtype).strip().lower() in _ERRATUM_RELATED_TYPES:
        related = _field_value(entry, "related")
        if related is not None:
            refs.extend(ref.strip() for ref in str(related).split(",") if ref.strip())
    for field_key in _ERRATUM_TARGET_FIELDS:
        value = _field_value(entry, field_key)
        if value is not None and str(value).strip():
            refs.append(str(value).strip())
    return refs


def _is_erratum(entry: Entry) -> bool:
    return entry.entry_type.lower() in _ERRATUM_ENTRY_TYPES or bool(_erratum_target_refs(entry))


def _role_of(entry: Entry) -> str:
    return "erratum" if _is_erratum(entry) else "main"


@dataclass
class Version:
    """A single source record, as a version of a work.

    :param entry: The source record (never modified).
    :param link: Evidence linking the record to the work:
        ``"doi"`` (shared canonical DOI), ``"title-candidate"`` (no DOI,
        matched by normalized title) or ``"singleton"`` (no link found).
    :param role: ``"erratum"`` if the record declares itself an
        erratum/correction, else ``"main"``.
    """

    entry: Entry
    link: str
    role: str = "main"


@dataclass(eq=False)
class Work:
    """An intellectual work, grouping one or more source records (versions)."""

    work_id: str
    canonical_doi: str | None = None
    versions: list[Version] = dataclass_field(default_factory=list)

    @property
    def entries(self) -> list[Entry]:
        """All source records that are versions of this work."""
        return [version.entry for version in self.versions]

    def __repr__(self) -> str:
        keys = [entry.key for entry in self.entries]
        return f"Work(work_id={self.work_id!r}, entries={keys!r})"


@dataclass
class WorkRelation:
    """An explicit relation between works, e.g. an erratum.

    :param kind: The relation kind, currently always ``"erratum"``.
    :param source: The work of the record declaring the relation
        (``None`` if that record is pending).
    :param target: The work the relation points to
        (``None`` if the target could not be resolved).
    :param entry: The record declaring the relation.
    :param target_ref: The raw target reference (DOI or entry key).
    """

    kind: str
    source: Work | None
    target: Work | None
    entry: Entry
    target_ref: str


@dataclass
class PendingRecord:
    """A source record that could not be assigned unambiguously.

    :param entry: The source record (preserved, not part of any work).
    :param reason: ``"conflicting-identifiers"`` for bridge records carrying
        mutually exclusive authoritative identifiers, or
        ``"ambiguous-title"`` for DOI-less records matching several works.
    :param identifiers: The conflicting canonical identifiers, if any.
    :param candidate_works: The works the record could belong to.
    """

    entry: Entry
    reason: str
    identifiers: tuple[str, ...] = ()
    candidate_works: tuple[Work, ...] = ()


class WorkIndex:
    """Non-destructive work/version index over a library.

    Built by `build_work_index`. Every entry of the library is accounted
    for: either as a version of exactly one work, or as a pending record.
    """

    def __init__(
        self,
        works: list[Work],
        pending: list[PendingRecord],
        relations: list[WorkRelation],
    ):
        self.works = works
        self.pending = pending
        self.relations = relations
        self._work_by_entry_id = {}
        for work in works:
            for version in work.versions:
                self._work_by_entry_id[id(version.entry)] = work

    def work_of(self, entry: Entry) -> Work | None:
        """The work an entry is a version of, or ``None`` if it is pending."""
        return self._work_by_entry_id.get(id(entry))

    def errata_of(self, work: Work) -> list[WorkRelation]:
        """All erratum relations pointing to the given work."""
        return [r for r in self.relations if r.kind == "erratum" and r.target is work]

    @property
    def indexed_entries(self) -> list[Entry]:
        """All source records covered by this index (works and pending)."""
        entries = [version.entry for work in self.works for version in work.versions]
        entries.extend(record.entry for record in self.pending)
        return entries


def build_work_index(
    library: Library,
    doi_fields: tuple[str, ...] = ("doi",),
    title_field: str = "title",
) -> WorkIndex:
    """Build a work/version index over the entries of a library.

    The library is only read, never modified; every source record is
    preserved and accounted for in the returned index.

    :param library: The library to index.
    :param doi_fields: Entry fields to read DOIs from (in order).
    :param title_field: Entry field to read the title from.
    :return: A `WorkIndex` grouping entries into works and versions.
    """
    entries = library.entries
    works: list[Work] = []
    pending: list[PendingRecord] = []
    relations: list[WorkRelation] = []
    work_by_doi: dict[str, Work] = {}

    def new_work(work_id: str, canonical_doi: str | None = None) -> Work:
        work = Work(work_id=work_id, canonical_doi=canonical_doi)
        works.append(work)
        return work

    # Pass 1: group records carrying DOIs by canonical DOI.
    doi_less = []
    for entry in entries:
        dois = _canonical_dois_of(entry, doi_fields)
        if len(dois) > 1:
            # Bridge record: mutually exclusive authoritative identifiers.
            # It must not merge the works at its ends; defer to judgement.
            candidate_works = tuple(work_by_doi[doi] for doi in dois if doi in work_by_doi)
            pending.append(
                PendingRecord(
                    entry=entry,
                    reason="conflicting-identifiers",
                    identifiers=tuple(dois),
                    candidate_works=candidate_works,
                )
            )
        elif len(dois) == 1:
            doi = dois[0]
            work = work_by_doi.get(doi)
            if work is None:
                work = new_work(work_id=f"doi:{doi}", canonical_doi=doi)
                work_by_doi[doi] = work
            work.versions.append(Version(entry=entry, link="doi", role=_role_of(entry)))
        else:
            doi_less.append(entry)

    # Pass 2: attach DOI-less records by normalized title, or as singletons.
    works_by_title: dict[str, list[Work]] = {}
    for work in works:
        for version in work.versions:
            title = normalize_title(_field_value(version.entry, title_field))
            if title:
                titled = works_by_title.setdefault(title, [])
                if not any(candidate is work for candidate in titled):
                    titled.append(work)

    for entry in doi_less:
        title = normalize_title(_field_value(entry, title_field))
        candidates = works_by_title.get(title, []) if title else []
        if len(candidates) == 1:
            work = candidates[0]
            work.versions.append(
                Version(entry=entry, link="title-candidate", role=_role_of(entry))
            )
        elif len(candidates) > 1:
            pending.append(
                PendingRecord(
                    entry=entry,
                    reason="ambiguous-title",
                    candidate_works=tuple(candidates),
                )
            )
        else:
            work = new_work(work_id=f"key:{entry.key}")
            work.versions.append(Version(entry=entry, link="singleton", role=_role_of(entry)))
            if title:
                works_by_title.setdefault(title, []).append(work)

    index = WorkIndex(works=works, pending=pending, relations=relations)

    # Pass 3: explicit erratum/correction relations between works.
    for entry in entries:
        for ref in _erratum_target_refs(entry):
            target = _resolve_target(ref, work_by_doi, library, index)
            relations.append(
                WorkRelation(
                    kind="erratum",
                    source=index.work_of(entry),
                    target=target,
                    entry=entry,
                    target_ref=ref,
                )
            )

    return index


def _resolve_target(
    ref: str,
    work_by_doi: dict[str, Work],
    library: Library,
    index: WorkIndex,
) -> Work | None:
    """Resolve an erratum target reference (DOI or entry key) to a work."""
    doi = canonicalize_doi(ref)
    if doi is not None and doi in work_by_doi:
        return work_by_doi[doi]
    target_entry = library.entries_dict.get(ref)
    if target_entry is not None:
        return index.work_of(target_entry)
    return None
