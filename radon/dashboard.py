'''This module implements the building blocks of a quality dashboard that
tracks how Cyclomatic Complexity evolves between two versions of a source
file.

The dashboard receives the lines that changed between an *old* and a *new*
version together with the complexity blocks computed on both versions.
Every changed line is attributed to the innermost block that actually owns
it: added lines are looked up among the blocks of the new version, deleted
lines among the blocks of the old one. Because a line is owned by exactly
one block, a change inside a method is reported on the method only and is
never counted again on the enclosing class (or function) summary.

Blocks whose complexity exceeds the dashboard threshold are reported as
violations. A violation can be silenced by an :class:`Exemption`, but only
while the block sits exactly on the exempted source range in one of the
versions the exemption is valid for: an inner function that was moved
elsewhere cannot reuse the exemption of its original position. When a
block that violated the threshold disappears from the new version, a
removal record is kept so that it is always possible to explain why the
violation is gone.
'''

import collections

from radon.complexity import add_inner_blocks, cc_visit
from radon.visitors import Class

# Name used for the record collecting the changed lines that do not belong
# to any block (i.e. module-level statements).
MODULE_BLOCK_NAME = '<module>'

ExemptionBase = collections.namedtuple(
    'Exemption', ['name', 'lineno', 'endline', 'versions', 'reason']
)


class Exemption(ExemptionBase):
    '''An exemption silences the violation of a single block.

    :param name: The full (dotted) name of the exempted block.
    :param lineno: The first line of the exempted source range.
    :param endline: The last line of the exempted source range.
    :param versions: The versions the exemption is valid for. A single
        value is accepted and converted to a one-element set.
    :param reason: Free-form text explaining why the exemption exists.

    An exemption only applies to a block with the same name that sits
    *exactly* on the exempted source range in one of the valid versions.
    '''

    def __new__(cls, name, lineno, endline, versions, reason=''):
        if isinstance(versions, str) or not hasattr(versions, '__iter__'):
            versions = (versions,)
        return super(Exemption, cls).__new__(
            cls, name, lineno, endline, frozenset(versions), reason
        )

    def covers(self, block, version):
        '''Whether this exemption applies to *block* in *version*.

        The block must have the same full name and occupy exactly the
        exempted source range: a block that was moved elsewhere in the
        file cannot reuse the exemption of its original position.
        '''
        return (
            self.name == block.fullname
            and self.lineno == block.lineno
            and self.endline == block.endline
            and version in self.versions
        )


def block_kind(block):
    '''Return the kind of *block*: ``'class'``, ``'method'`` or
    ``'function'``.'''
    if isinstance(block, Class):
        return 'class'
    return 'method' if block.is_method else 'function'


class ChangeRecord(object):
    '''The changed lines attributed to a single block.

    Added lines are line numbers in the new version, deleted lines are
    line numbers in the old version.
    '''

    def __init__(self, name, kind):
        self.name = name
        self.kind = kind
        self.added_lines = set()
        self.deleted_lines = set()

    @property
    def changed_lines(self):
        '''The total number of changed lines attributed to the block.'''
        return len(self.added_lines) + len(self.deleted_lines)

    @property
    def is_addition(self):
        '''Whether the block only received new lines.'''
        return bool(self.added_lines) and not self.deleted_lines

    @property
    def is_removal(self):
        '''Whether the block only lost lines.'''
        return bool(self.deleted_lines) and not self.added_lines

    def __repr__(self):
        return (
            'ChangeRecord({0!r}, {1!r}, added={2}, deleted={3})'.format(
                self.name,
                self.kind,
                sorted(self.added_lines),
                sorted(self.deleted_lines),
            )
        )

    def __eq__(self, other):
        return (
            isinstance(other, ChangeRecord)
            and self.name == other.name
            and self.kind == other.kind
            and self.added_lines == other.added_lines
            and self.deleted_lines == other.deleted_lines
        )


def _innermost_block(blocks, line):
    '''Return the innermost block containing *line*, or None.

    Among all the blocks whose source range contains the line, the
    innermost one is the one with the smallest span.
    '''
    candidates = [b for b in blocks if b.lineno <= line <= b.endline]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda b: (b.endline - b.lineno, -b.lineno, -b.col_offset),
    )


def attribute_changes(added_lines, deleted_lines, new_blocks, old_blocks):
    '''Attribute changed lines to the innermost block that owns them.

    :param added_lines: Line numbers (of the new version) that were added.
    :param deleted_lines: Line numbers (of the old version) that were
        deleted.
    :param new_blocks: The complexity blocks of the new version, as
        returned by :func:`radon.complexity.cc_visit`.
    :param old_blocks: The complexity blocks of the old version.

    Added lines are looked up among the blocks of the new version, deleted
    lines among the blocks of the old one. Each line is attributed to
    exactly one block, so a change inside a method or a closure is
    reported on that innermost block only and never again on the enclosing
    class or function. Lines that do not belong to any block are collected
    in a module-level record.

    Returns a list of :class:`ChangeRecord` objects sorted by block name.
    '''
    new_flat = add_inner_blocks(list(new_blocks))
    old_flat = add_inner_blocks(list(old_blocks))
    records = {}

    def record_for(block):
        if block is None:
            key = (MODULE_BLOCK_NAME, 'module')
        else:
            key = (block.fullname, block_kind(block))
        if key not in records:
            records[key] = ChangeRecord(*key)
        return records[key]

    for line in added_lines:
        record_for(_innermost_block(new_flat, line)).added_lines.add(line)
    for line in deleted_lines:
        record_for(_innermost_block(old_flat, line)).deleted_lines.add(line)
    return sorted(records.values(), key=lambda r: (r.name, r.kind))


class Violation(object):
    '''A block whose complexity exceeds the dashboard threshold.'''

    def __init__(self, block, threshold, version, exemption=None):
        self.name = block.fullname
        self.kind = block_kind(block)
        self.complexity = block.complexity
        self.threshold = threshold
        self.lineno = block.lineno
        self.endline = block.endline
        self.version = version
        self.exemption = exemption

    @property
    def exempted(self):
        '''Whether this violation is silenced by an exemption.'''
        return self.exemption is not None

    def __repr__(self):
        return 'Violation({0!r}, complexity={1}, threshold={2})'.format(
            self.name, self.complexity, self.threshold
        )


class RemovalRecord(object):
    '''Explains why a block that violated the threshold disappeared.

    A removal record is created for every block that exceeded the
    threshold in the old version but is no longer present in the new one,
    so that the disappearance can always be traced back, even when the
    deletion of the block was the only change.
    '''

    def __init__(self, block, threshold, old_version, new_version):
        self.name = block.fullname
        self.kind = block_kind(block)
        self.complexity = block.complexity
        self.threshold = threshold
        self.lineno = block.lineno
        self.endline = block.endline
        self.old_version = old_version
        self.new_version = new_version
        self.reason = (
            'block violated the threshold in {0} (complexity {1} > {2}) '
            'and is no longer present in {3}'.format(
                old_version, block.complexity, threshold, new_version
            )
        )

    def __repr__(self):
        return 'RemovalRecord({0!r}, complexity={1})'.format(
            self.name, self.complexity
        )


class DashboardReport(object):
    '''The result of comparing two versions of a source file.'''

    def __init__(
        self, changes, violations, removals, old_version, new_version
    ):
        self.changes = changes
        self.violations = violations
        self.removals = removals
        self.old_version = old_version
        self.new_version = new_version

    @property
    def active_violations(self):
        '''The violations that are not silenced by an exemption.'''
        return [v for v in self.violations if not v.exempted]

    @property
    def exempted_violations(self):
        '''The violations silenced by a valid exemption.'''
        return [v for v in self.violations if v.exempted]

    def changes_for(self, name):
        '''Return the :class:`ChangeRecord` for the block called *name*,
        or None if the block has no attributed changes.'''
        for record in self.changes:
            if record.name == name:
                return record
        return None

    def explain_removal(self, name):
        '''Return the :class:`RemovalRecord` explaining why the
        over-threshold block called *name* disappeared, or None.'''
        for record in self.removals:
            if record.name == name:
                return record
        return None


class QualityDashboard(object):
    '''Compare two versions of a source file from a quality perspective.

    :param threshold: The maximum allowed Cyclomatic Complexity. Blocks
        with a strictly higher complexity are reported as violations.
    :param exemptions: An iterable of :class:`Exemption` objects.
    '''

    def __init__(self, threshold, exemptions=()):
        self.threshold = threshold
        self.exemptions = list(exemptions)

    def compare(
        self,
        old_code,
        new_code,
        added_lines=(),
        deleted_lines=(),
        old_version=None,
        new_version=None,
    ):
        '''Compare two versions of a source file, given their code.

        Either version may be None, in which case it is treated as an
        empty file. Returns a :class:`DashboardReport`.
        '''
        old_blocks = cc_visit(old_code) if old_code is not None else []
        new_blocks = cc_visit(new_code) if new_code is not None else []
        return self.compare_blocks(
            old_blocks,
            new_blocks,
            added_lines,
            deleted_lines,
            old_version,
            new_version,
        )

    def compare_blocks(
        self,
        old_blocks,
        new_blocks,
        added_lines=(),
        deleted_lines=(),
        old_version=None,
        new_version=None,
    ):
        '''Compare two versions given their complexity blocks, as returned
        by :func:`radon.complexity.cc_visit`. Returns a
        :class:`DashboardReport`.'''
        changes = attribute_changes(
            added_lines, deleted_lines, new_blocks, old_blocks
        )
        old_flat = add_inner_blocks(list(old_blocks))
        new_flat = add_inner_blocks(list(new_blocks))
        violations = [
            Violation(
                block,
                self.threshold,
                new_version,
                exemption=self._match_exemption(block, new_version),
            )
            for block in new_flat
            if block.complexity > self.threshold
        ]
        new_names = set(b.fullname for b in new_flat)
        removals = [
            RemovalRecord(block, self.threshold, old_version, new_version)
            for block in old_flat
            if block.complexity > self.threshold
            and block.fullname not in new_names
        ]
        return DashboardReport(
            changes, violations, removals, old_version, new_version
        )

    def _match_exemption(self, block, version):
        '''Return the first exemption covering *block* in *version*.'''
        for exemption in self.exemptions:
            if exemption.covers(block, version):
                return exemption
        return None
