'''Continuity analysis between two or more source snapshots.

The functions in this module answer a simple question: when a block of code is
renamed, moved, split, or copied between two revisions, which block in the new
revision continues a block in the old one?

Matching is deliberately evidence based.  A relation is only *grounded* (and
therefore eligible for complexity comparisons) when there is exactly one
plausible continuation.  When several blocks are equally plausible, the
relation is reported as ambiguous instead of being guessed.  Splits are kept
as one source with several targets, so the provenance of every target is
preserved.

The public entry point is :func:`compare_snapshots`.  :func:`build_trends`
uses those relations to keep a metric's history connected only while grounded
one-to-one continuations exist.
'''

import ast
import hashlib
import io
import json
import os

from radon.complexity import cc_rank
from radon.visitors import ComplexityVisitor

try:
    text_type = unicode
except NameError:  # pragma: no cover - Python 3
    text_type = str

__all__ = [
    'AmbiguousRelation',
    'FunctionRecord',
    'Relation',
    'Snapshot',
    'SnapshotComparison',
    'Trend',
    'build_trends',
    'compare',
    'compare_snapshots',
    'load_snapshot',
    'rank_complexity_changes',
    'track_functions',
]


# ---------------------------------------------------------------------------
# Structural fingerprints
# ---------------------------------------------------------------------------

def _is_docstring(node):
    '''Return True if *node* is a plain string expression (a docstring).'''
    value = getattr(node, 'value', None)
    if not isinstance(node, ast.Expr) or value is None:
        return False
    if isinstance(value, ast.Constant):
        return isinstance(value.value, text_type)
    return isinstance(value, getattr(ast, 'Str', ()))


def _body_nodes(body):
    '''Return the statements of *body*, ignoring a leading docstring.'''
    body = list(body)
    if body and _is_docstring(body[0]):
        body = body[1:]
    return body


def _canonical(node):
    '''Return a name- and position-independent representation of *node*.

    Local identifiers are erased on purpose: renaming a function, its
    arguments, or a local variable must not change its structural
    fingerprint.  Constants, operators, attribute names, and control flow are
    retained, so two wrappers that merely look alike do not collide unless
    their structure really is identical.
    '''
    if node is None:
        return None

    if isinstance(node, list):
        return tuple(_canonical(item) for item in node)

    if not isinstance(node, ast.AST):
        if isinstance(node, bytes):
            return ('bytes', node.decode('latin-1'))
        if isinstance(node, (text_type, int, float, bool)) or node is None:
            return node
        return repr(node)

    name = type(node).__name__

    if name in ('FunctionDef', 'AsyncFunctionDef'):
        return (
            name,
            _canonical(node.args),
            tuple(_canonical(stmt) for stmt in _body_nodes(node.body)),
            tuple(
                _canonical(dec)
                for dec in getattr(node, 'decorator_list', ())
            ),
        )

    if name == 'Lambda':
        return (name, _canonical(node.args), _canonical(node.body))

    if name == 'arguments':
        args = list(getattr(node, 'args', ()))
        defaults = list(getattr(node, 'defaults', ()))
        kwonly = list(getattr(node, 'kwonlyargs', ()))
        kw_defaults = list(getattr(node, 'kw_defaults', ()))
        return (
            name,
            tuple(_canonical(arg.annotation) for arg in args),
            tuple(_canonical(value) for value in defaults),
            tuple(_canonical(arg.annotation) for arg in kwonly),
            tuple(
                _canonical(value) for value in kw_defaults if value is not None
            ),
            bool(getattr(node, 'vararg', None)),
            bool(getattr(node, 'kwarg', None)),
        )

    if name == 'arg':
        return (name, _canonical(getattr(node, 'annotation', None)))

    if name == 'Name':
        return (name,)

    if name == 'Attribute':
        return (name, getattr(node, 'attr', ''), _canonical(node.value))

    if name == 'keyword':
        return (
            name,
            getattr(node, 'arg', None),
            _canonical(getattr(node, 'value', None)),
        )

    if name == 'alias':
        return (name, _canonical(getattr(node, 'name', None)))

    if name in ('Global', 'Nonlocal'):
        return (name,)

    ignored = {'name', 'ctx', 'type_comment'}
    fields = []
    for field, value in ast.iter_fields(node):
        if field in ignored:
            continue
        if field in ('body', 'orelse', 'finalbody') and isinstance(
            value, list
        ):
            fields.append(
                (field, tuple(_canonical(item) for item in value))
            )
        else:
            fields.append((field, _canonical(value)))
    return (name, tuple(fields))


def _digest(value):
    payload = repr(value).encode('utf-8')
    return hashlib.sha1(payload).hexdigest()


def _function_fingerprint(node):
    '''The structural fingerprint of a function definition.'''
    return _digest(_canonical(node))


def _statement_digests(node):
    '''Return a multiset of statement fingerprints from a function body.

    The result is used as conservative evidence for a split: when several new
    functions are assembled solely from statements of one old function, the
    old block is a plausible origin for all of them.
    '''
    digests = []
    for stmt in _body_nodes(node.body):
        for child in ast.walk(stmt):
            if isinstance(child, ast.stmt) and not _is_docstring(child):
                digests.append(_digest(_canonical(child)))
    return tuple(sorted(digests))


# ---------------------------------------------------------------------------
# Snapshot extraction
# ---------------------------------------------------------------------------

class FunctionRecord(object):
    '''A function or method found in one snapshot.

    ``path`` is relative to the snapshot root (so a file move is visible),
    while ``qualname`` includes enclosing classes.
    '''

    __slots__ = (
        'path',
        'qualname',
        'name',
        'classname',
        'lineno',
        'endline',
        'complexity',
        'fingerprint',
        'statements',
        'is_method',
        'uid',
        'occurrence',
    )

    def __init__(
        self,
        path,
        qualname,
        name,
        classname,
        lineno,
        endline,
        complexity,
        fingerprint,
        statements=(),
        is_method=False,
        occurrence=1,
    ):
        self.path = path
        self.qualname = qualname
        self.name = name
        self.classname = classname
        self.lineno = lineno
        self.endline = endline
        self.complexity = complexity
        self.fingerprint = fingerprint
        self.statements = tuple(statements)
        self.is_method = is_method
        self.occurrence = occurrence
        suffix = '' if occurrence == 1 else '#{0}'.format(occurrence)
        self.uid = '{0}::{1}{2}'.format(path, qualname, suffix)

    @property
    def identifier(self):
        return self.uid

    @property
    def fullname(self):
        return self.qualname

    def as_dict(self):
        return {
            'id': self.uid,
            'path': self.path,
            'qualname': self.qualname,
            'name': self.name,
            'classname': self.classname,
            'lineno': self.lineno,
            'endline': self.endline,
            'complexity': self.complexity,
            'rank': cc_rank(self.complexity),
            'fingerprint': self.fingerprint,
            'statements': list(self.statements),
            'is_method': self.is_method,
            'occurrence': self.occurrence,
        }

    def __repr__(self):
        return '<FunctionRecord {0} ({1})>'.format(self.uid, self.complexity)


_COMPOUND_BODIES = ('body', 'orelse', 'finalbody')


def _walk_definitions(body, prefix='', classname=None):
    '''Yield ``(node, qualname, classname, is_method)`` for all definitions.'''
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qualname = prefix + node.name
            yield node, qualname, classname, classname is not None
            for item in _walk_definitions(
                node.body, prefix=qualname + '.', classname=None
            ):
                yield item
        elif isinstance(node, ast.ClassDef):
            qualname = prefix + node.name
            for item in _walk_definitions(
                node.body, prefix=qualname + '.', classname=qualname
            ):
                yield item
        else:
            # Definitions under ``if``/``try``/``for`` are still members of
            # the enclosing module, class, or function.
            for field in _COMPOUND_BODIES:
                child = getattr(node, field, None)
                if isinstance(child, list):
                    for item in _walk_definitions(child, prefix, classname):
                        yield item
            for handler in getattr(node, 'handlers', ()) or ():
                for item in _walk_definitions(
                    handler.body, prefix, classname
                ):
                    yield item
            for case in getattr(node, 'cases', ()) or ():
                for item in _walk_definitions(case.body, prefix, classname):
                    yield item


def _function_complexity(node, no_assert=False):
    '''Compute cyclomatic complexity for a single function definition.'''
    body_complexity = 1
    for child in node.body:
        visitor = ComplexityVisitor(off=False, no_assert=no_assert)
        visitor.visit(child)
        body_complexity += visitor.complexity
    return body_complexity


def _endline(node):
    endline = getattr(node, 'end_lineno', None)
    if endline is not None:
        return endline
    return getattr(node, 'lineno', 0)


def _path_key(path):
    return os.path.normpath(path).replace(os.sep, '/')


class Snapshot(object):
    '''A collection of function records extracted from source code.'''

    def __init__(self, records, label=None, errors=None):
        self.records = list(records)
        self.label = label
        self.errors = list(errors or ())
        self._by_uid = {}
        for record in self.records:
            self._by_uid[record.uid] = record

    @classmethod
    def from_sources(cls, sources, label=None, no_assert=False):
        '''Build a snapshot from a mapping of ``path -> source``.'''
        records = []
        errors = []
        for path in sorted(sources):
            source = sources[path]
            try:
                tree = ast.parse(source, filename=path)
            except (SyntaxError, ValueError, TypeError) as exc:
                errors.append({'path': path, 'error': str(exc)})
                continue
            occurrences = {}
            for node, qualname, classname, is_method in _walk_definitions(
                tree.body
            ):
                key = _path_key(path)
                occurrence_key = (key, qualname)
                occurrences[occurrence_key] = occurrences.get(
                    occurrence_key, 0
                ) + 1
                records.append(
                    FunctionRecord(
                        path=key,
                        qualname=qualname,
                        name=node.name,
                        classname=classname,
                        lineno=getattr(node, 'lineno', 0),
                        endline=_endline(node),
                        complexity=_function_complexity(node, no_assert),
                        fingerprint=_function_fingerprint(node),
                        statements=_statement_digests(node),
                        is_method=is_method,
                        occurrence=occurrences[occurrence_key],
                    )
                )
        return cls(records, label=label, errors=errors)

    @classmethod
    def from_code(cls, source, label='<code>', no_assert=False):
        '''Build a snapshot from a single source string.'''
        return cls.from_sources(
            {label: source}, label=label, no_assert=no_assert
        )

    @classmethod
    def from_directory(cls, root, label=None, no_assert=False):
        '''Build a snapshot by walking a directory for Python files.'''
        sources = {}
        errors = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                name for name in dirnames if not name.startswith('.')
            )
            for filename in sorted(filenames):
                if not filename.endswith('.py'):
                    continue
                path = os.path.join(dirpath, filename)
                relpath = _path_key(os.path.relpath(path, root))
                try:
                    with io.open(path, 'rb') as fobj:
                        sources[relpath] = fobj.read().decode('utf-8')
                except (IOError, OSError, UnicodeDecodeError) as exc:
                    errors.append({'path': relpath, 'error': str(exc)})
        snapshot = cls.from_sources(
            sources,
            label=label if label is not None else root,
            no_assert=no_assert,
        )
        snapshot.errors.extend(errors)
        return snapshot

    @classmethod
    def from_path(cls, path, label=None, no_assert=False):
        '''Load a snapshot from a directory, a Python file, or JSON data.'''
        if isinstance(path, Snapshot):
            return path
        if isinstance(path, dict) and not os.path.exists(str(path)):
            return cls.from_sources(path, label=label, no_assert=no_assert)
        if os.path.isdir(path):
            return cls.from_directory(path, label=label, no_assert=no_assert)
        with io.open(path, 'rb') as fobj:
            raw = fobj.read().decode('utf-8')
        if path.endswith('.json'):
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
            if isinstance(data, dict) and (
                'functions' in data or _looks_like_cc_json(data)
            ):
                return cls.from_json_data(data, label=label or path)
        return cls.from_sources(
            {_path_key(os.path.basename(path)): raw},
            label=label or path,
            no_assert=no_assert,
        )

    @classmethod
    def from_json_data(cls, data, label=None):
        '''Load a saved snapshot or ``radon cc --json`` output.'''
        if isinstance(data, dict) and 'functions' in data:
            records = []
            for item in data['functions']:
                records.append(
                    FunctionRecord(
                        path=item.get('path', ''),
                        qualname=item.get('qualname', item.get('name', '')),
                        name=item.get('name', ''),
                        classname=item.get('classname'),
                        lineno=item.get('lineno', 0),
                        endline=item.get('endline', item.get('lineno', 0)),
                        complexity=item.get('complexity', 0),
                        fingerprint=item.get('fingerprint', ''),
                        statements=tuple(item.get('statements', ())),
                        is_method=item.get('is_method', False),
                        occurrence=item.get('occurrence', 1),
                    )
                )
            return cls(records, label=label or data.get('label'))
        records = []
        for path, blocks in data.items():
            if not isinstance(blocks, list):
                continue
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                name = block.get('name', '')
                classname = block.get('classname')
                qualname = (
                    '{0}.{1}'.format(classname, name) if classname else name
                )
                records.append(
                    FunctionRecord(
                        path=_path_key(str(path)),
                        qualname=qualname,
                        name=name,
                        classname=classname,
                        lineno=block.get('lineno', 0),
                        endline=block.get('endline', block.get('lineno', 0)),
                        complexity=block.get('complexity', 0),
                        fingerprint='',
                        is_method=block.get('type') == 'method',
                    )
                )
        return cls(records, label=label)

    @property
    def uids(self):
        return sorted(self._by_uid)

    def get(self, uid):
        return self._by_uid.get(uid)

    def find(self, reference):
        '''Resolve a human-readable reference to matching records.

        Accepted forms are a full ``path::qualname`` identifier, a bare
        qualified name, a path and qualified name pair, or a record itself.
        The return value is always a list so callers can detect ambiguity.
        '''
        if isinstance(reference, FunctionRecord):
            return [reference]
        if isinstance(reference, (list, tuple)) and len(reference) == 2:
            path, qualname = reference
            reference = '{0}::{1}'.format(_path_key(str(path)), qualname)
        reference = str(reference)
        if reference in self._by_uid:
            return [self._by_uid[reference]]
        matches = [
            record
            for record in self.records
            if record.qualname == reference
        ]
        if matches:
            return matches
        normalised = reference.replace('\\', '/')
        return [
            record
            for record in self.records
            if record.uid.replace('\\', '/') == normalised
        ]

    def as_dict(self):
        return {
            'label': self.label,
            'functions': [record.as_dict() for record in self.records],
            'errors': list(self.errors),
        }

    def to_json(self):
        return json.dumps(self.as_dict(), indent=2, sort_keys=True)

    def save(self, path):
        with io.open(path, 'w', encoding='utf-8') as fobj:
            fobj.write(self.to_json())


def _looks_like_cc_json(data):
    if not isinstance(data, dict) or not data:
        return False
    for value in data.values():
        if not isinstance(value, list):
            return False
    return True


# ---------------------------------------------------------------------------
# Relations
# ---------------------------------------------------------------------------

class Relation(object):
    '''A reviewable continuation relation between old and new blocks.

    ``status`` is one of ``grounded`` (an evidence-backed one-to-one or
    one-to-many relation), ``split``, or ``ambiguous``.  Only grounded
    one-to-one relations are rankable.
    '''

    __slots__ = ('sources', 'targets', 'kind', 'status', 'evidence', 'group')

    def __init__(self, sources, targets, kind, status, evidence, group=None):
        self.sources = tuple(sources)
        self.targets = tuple(targets)
        self.kind = kind
        self.status = status
        self.evidence = evidence
        self.group = group

    @property
    def rankable(self):
        return (
            self.status == 'grounded'
            and len(self.sources) == 1
            and len(self.targets) == 1
        )

    @property
    def source(self):
        return self.sources[0] if len(self.sources) == 1 else None

    @property
    def target(self):
        return self.targets[0] if len(self.targets) == 1 else None

    def links(self):
        '''Return every source/target pair represented by this relation.'''
        return [
            {
                'source': source.uid,
                'target': target.uid,
                'kind': self.kind,
                'status': self.status,
                'group': self.group,
            }
            for source in self.sources
            for target in self.targets
        ]

    def as_dict(self):
        result = {
            'sources': [record.uid for record in self.sources],
            'targets': [record.uid for record in self.targets],
            'kind': self.kind,
            'status': self.status,
            'rankable': self.rankable,
            'evidence': self.evidence,
            'links': self.links(),
        }
        if self.group is not None:
            result['group'] = self.group
        if self.source is not None:
            result['source'] = self.source.uid
        if self.target is not None:
            result['target'] = self.target.uid
        return result

    def __repr__(self):
        return '<Relation {0} {1} -> {2} ({3})>'.format(
            self.kind,
            [record.uid for record in self.sources],
            [record.uid for record in self.targets],
            self.status,
        )


class AmbiguousRelation(Relation):
    '''A set of candidate continuations that cannot be resolved.'''

    def __init__(self, sources, targets, kind, evidence, group=None):
        super(AmbiguousRelation, self).__init__(
            sources,
            targets,
            kind,
            'ambiguous',
            evidence,
            group=group,
        )


class SnapshotComparison(object):
    '''The result of comparing an old snapshot with a new one.'''

    def __init__(self, old, new, relations, hints=None):
        self.old = old
        self.new = new
        self.relations = list(relations)
        self.hints = list(hints or ())

    @property
    def grounded(self):
        return [relation for relation in self.relations if relation.rankable]

    @property
    def ambiguous(self):
        return [
            relation
            for relation in self.relations
            if relation.status == 'ambiguous'
        ]

    @property
    def splits(self):
        return [
            relation
            for relation in self.relations
            if relation.status == 'split'
        ]

    def _matched_old(self):
        return {
            record.uid
            for relation in self.relations
            for record in relation.sources
        }

    def _matched_new(self):
        return {
            record.uid
            for relation in self.relations
            for record in relation.targets
        }

    @property
    def unmatched_old(self):
        matched = self._matched_old()
        return [
            record for record in self.old.records if record.uid not in matched
        ]

    @property
    def unmatched_new(self):
        matched = self._matched_new()
        return [
            record for record in self.new.records if record.uid not in matched
        ]

    def complexity_changes(self, only_changed=False):
        '''Rank complexity changes for grounded one-to-one relations only.

        Ambiguous relations, splits, and new/deleted functions intentionally
        do not contribute a delta: without a defensible continuation there is
        no single "before" value to compare against.
        '''
        changes = []
        for relation in self.grounded:
            source, target = relation.source, relation.target
            delta = target.complexity - source.complexity
            if only_changed and delta == 0:
                continue
            changes.append(
                {
                    'source': source.uid,
                    'target': target.uid,
                    'path': target.path,
                    'qualname': target.qualname,
                    'kind': relation.kind,
                    'before': source.complexity,
                    'after': target.complexity,
                    'delta': delta,
                    'before_rank': cc_rank(source.complexity),
                    'after_rank': cc_rank(target.complexity),
                }
            )
        changes.sort(
            key=lambda item: (-item['delta'], item['target'], item['source'])
        )
        for index, item in enumerate(changes, 1):
            item['rank'] = index
        return changes

    def trend(self):
        '''Return two-snapshot metric series.

        A grounded relation produces one connected segment.  Ambiguous and
        split groups produce separate segments, making the discontinuity
        explicit instead of drawing a line through an unsupported match.
        '''
        trends = []
        consumed_old = set()
        consumed_new = set()
        for relation in self.relations:
            if relation.rankable:
                source, target = relation.source, relation.target
                consumed_old.add(source.uid)
                consumed_new.add(target.uid)
                trends.append(
                    Trend(
                        segments=[
                            [
                                _point(0, source),
                                _point(1, target),
                            ]
                        ],
                        breaks=[],
                        status='connected',
                        relation=relation,
                    )
                )
            elif relation.status in ('split', 'ambiguous'):
                consumed_old.update(source.uid for source in relation.sources)
                consumed_new.update(target.uid for target in relation.targets)
                segments = [
                    [_point(0, source)] for source in relation.sources
                ]
                segments.extend(
                    [_point(1, target)] for target in relation.targets
                )
                reason = (
                    'split'
                    if relation.status == 'split'
                    else 'ambiguous continuation'
                )
                trends.append(
                    Trend(
                        segments=segments,
                        breaks=[{'after_snapshot': 0, 'reason': reason}],
                        status='disconnected',
                        relation=relation,
                    )
                )
        for record in self.old.records:
            if record.uid not in consumed_old:
                trends.append(
                    Trend(
                        segments=[[_point(0, record)]],
                        breaks=[],
                        status='unmatched',
                    )
                )
        for record in self.new.records:
            if record.uid not in consumed_new:
                trends.append(
                    Trend(
                        segments=[[_point(1, record)]],
                        breaks=[],
                        status='unmatched',
                    )
                )
        trends.sort(key=_trend_sort_key)
        return trends

    def as_dict(self):
        relations = sorted(
            self.relations,
            key=lambda relation: (
                relation.status,
                relation.kind,
                [record.uid for record in relation.sources],
                [record.uid for record in relation.targets],
            ),
        )
        return {
            'old': self.old.as_dict(),
            'new': self.new.as_dict(),
            'relations': [relation.as_dict() for relation in relations],
            'hints': list(self.hints),
            'complexity_changes': self.complexity_changes(),
            'unmatched': {
                'old': [record.uid for record in self.unmatched_old],
                'new': [record.uid for record in self.unmatched_new],
            },
            'ambiguous': [
                relation.as_dict()
                for relation in sorted(
                    self.ambiguous,
                    key=lambda relation: (
                        relation.kind,
                        [record.uid for record in relation.sources],
                        [record.uid for record in relation.targets],
                    ),
                )
            ],
            'splits': [
                relation.as_dict()
                for relation in sorted(
                    self.splits,
                    key=lambda relation: (
                        [record.uid for record in relation.sources],
                        [record.uid for record in relation.targets],
                    ),
                )
            ],
            'trends': [trend.as_dict() for trend in self.trend()],
        }

    def to_json(self):
        return json.dumps(self.as_dict(), indent=2, sort_keys=True)


def _point(index, record):
    return {
        'snapshot': index,
        'id': record.uid,
        'complexity': record.complexity,
    }


def _trend_sort_key(trend):
    if not trend.segments or not trend.segments[0]:
        return (0, '')
    point = trend.segments[0][0]
    return (point['snapshot'], point['id'])


class Trend(object):
    '''A metric series split into contiguous, evidence-backed segments.

    ``status`` is ``connected`` when every adjacent pair inside the covered
    range is a grounded one-to-one continuation, and ``broken`` when the
    series has a gap.  ``starts_after`` and ``ends_before`` explain a lineage
    that only covers part of the snapshot range.
    '''

    __slots__ = (
        'segments',
        'breaks',
        'status',
        'relation',
        'starts_after',
        'ends_before',
    )

    def __init__(
        self,
        segments,
        breaks,
        status,
        relation=None,
        starts_after=None,
        ends_before=None,
    ):
        self.segments = [list(segment) for segment in segments]
        self.breaks = list(breaks)
        self.status = status
        self.relation = relation
        self.starts_after = starts_after
        self.ends_before = ends_before

    @property
    def values(self):
        return [
            [point['complexity'] for point in segment]
            for segment in self.segments
        ]

    def as_dict(self):
        result = {
            'status': self.status,
            'segments': [
                [
                    {
                        'snapshot': point['snapshot'],
                        'id': point['id'],
                        'complexity': point['complexity'],
                    }
                    for point in segment
                ]
                for segment in self.segments
            ],
            'breaks': list(self.breaks),
        }
        if self.relation is not None:
            result['relation'] = self.relation.as_dict()
        if self.starts_after is not None:
            result['starts_after'] = self.starts_after
        if self.ends_before is not None:
            result['ends_before'] = self.ends_before
        return result


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _eligible(records, claimed):
    return [record for record in records if record.uid not in claimed]


def _resolve_references(snapshot, value):
    '''Resolve a reference or list of references into records.'''
    if isinstance(value, (list, tuple, set)):
        references = list(value)
    else:
        references = [value]
    records = []
    missing = []
    ambiguous = []
    for reference in references:
        matches = snapshot.find(reference)
        if not matches:
            missing.append(reference)
        elif len(matches) > 1:
            ambiguous.extend(matches)
        else:
            records.append(matches[0])
    return records, missing, ambiguous


def _normalise_mappings(mappings):
    '''Accept a mapping, a sequence of pairs, or a list of strings.'''
    if not mappings:
        return []
    if hasattr(mappings, 'items'):
        return list(mappings.items())
    pairs = []
    for item in mappings:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            pairs.append(tuple(item))
        elif isinstance(item, text_type) and '=' in item:
            old, new = item.split('=', 1)
            pairs.append((old.strip(), new.strip()))
    return pairs


def _unique(records):
    seen = set()
    result = []
    for record in records:
        if record.uid not in seen:
            seen.add(record.uid)
            result.append(record)
    return result


def _match_explicit(comparison, mappings, claimed_old, claimed_new, split):
    '''Apply explicit rename or split hints before automatic matching.'''
    declarations = []
    groups = []

    for old_reference, new_reference in _normalise_mappings(mappings):
        old_matches = comparison.old.find(old_reference)
        targets, missing, ambiguous_targets = _resolve_references(
            comparison.new, new_reference
        )
        if not old_matches:
            comparison.hints.append(
                {
                    'old': old_reference,
                    'new': new_reference,
                    'matched': False,
                    'reason': 'source reference not found in the old snapshot',
                }
            )
            continue
        if not targets and not ambiguous_targets:
            comparison.hints.append(
                {
                    'old': old_reference,
                    'new': new_reference,
                    'matched': False,
                    'reason': 'target reference not found in the new snapshot',
                }
            )
            continue
        if (
            len(old_matches) != 1
            or missing
            or ambiguous_targets
            or not targets
        ):
            comparison.hints.append(
                {
                    'old': old_reference,
                    'new': new_reference,
                    'matched': False,
                    'reason': (
                        'mapping does not resolve to a single source and '
                        'a usable target set'
                    ),
                }
            )
            groups.append(
                {
                    'sources': _unique(old_matches),
                    'targets': _unique(list(targets) + ambiguous_targets),
                    'evidence': (
                        'explicit mapping does not resolve to a single '
                        'source and a usable target set'
                    ),
                }
            )
            continue
        comparison.hints.append(
            {
                'old': old_reference,
                'new': new_reference,
                'matched': True,
                'reason': 'explicit mapping',
            }
        )
        declarations.append((old_matches[0], targets))

    # A target claimed by two different explicit sources is a merge, not a
    # one-to-one continuation, so it is ambiguous.
    targets_to_sources = {}
    for source, targets in declarations:
        for target in targets:
            targets_to_sources.setdefault(target.uid, []).append(source)

    conflicted = set()
    for source, targets in declarations:
        if any(len(targets_to_sources[target.uid]) > 1 for target in targets):
            involved_sources = _unique(
                [
                    other
                    for target in targets
                    for other in targets_to_sources[target.uid]
                ]
            )
            conflict_key = (
                tuple(record.uid for record in involved_sources),
                tuple(record.uid for record in targets),
            )
            if conflict_key in conflicted:
                continue
            conflicted.add(conflict_key)
            groups.append(
                {
                    'sources': involved_sources,
                    'targets': targets,
                    'evidence': (
                        'several explicit mappings claim the same target'
                    ),
                }
            )
            for record in involved_sources:
                claimed_old.add(record.uid)
            continue

        if source.uid in claimed_old:
            continue
        usable = [
            target for target in targets if target.uid not in claimed_new
        ]
        if not usable:
            continue
        status = 'split' if len(usable) > 1 else 'grounded'
        kind = 'split' if status == 'split' else (
            'renamed' if source.name != usable[0].name else 'explicit'
        )
        comparison.relations.append(
            Relation(
                [source],
                usable,
                kind,
                status,
                'explicit split mapping'
                if split
                else 'explicit rename mapping',
            )
        )
        claimed_old.add(source.uid)
        for target in usable:
            claimed_new.add(target.uid)

    for group in groups:
        comparison.relations.append(
            AmbiguousRelation(
                _unique(group['sources']),
                _unique(group['targets']),
                'explicit',
                group['evidence'],
            )
        )
        for record in group['sources']:
            claimed_old.add(record.uid)
        for record in group['targets']:
            claimed_new.add(record.uid)


def _match_identity(
    comparison, claimed_old, claimed_new, require_fingerprint=True
):
    '''Match blocks with the same file and qualified name.

    With ``require_fingerprint=True`` only structurally identical blocks are
    paired; this is the strongest form of identity.  Otherwise the identity
    alone is enough, which covers a body edit that has no structural
    counterpart.
    '''
    old_left = _eligible(comparison.old.records, claimed_old)
    new_left = _eligible(comparison.new.records, claimed_new)

    def group_by(records):
        result = {}
        for record in records:
            result.setdefault((record.path, record.qualname), []).append(
                record
            )
        return result

    old_by_key = group_by(old_left)
    new_by_key = group_by(new_left)

    def pair_by_fingerprint(old_matches, new_matches):
        pairs = []
        for source in old_matches:
            candidates = [
                target
                for target in new_matches
                if target.uid not in claimed_new
                and source.fingerprint
                and source.fingerprint == target.fingerprint
            ]
            if len(candidates) == 1:
                pairs.append((source, candidates[0]))
        # Only accept a fingerprint pair when the reverse mapping is also
        # unique; otherwise two candidates remain possible.
        unique = {}
        for source, target in pairs:
            unique.setdefault(target.uid, []).append(source)
        return [
            pair
            for pair in pairs
            if len(unique[pair[1].uid]) == 1
        ]

    for key in sorted(set(old_by_key) & set(new_by_key)):
        old_matches = old_by_key[key]
        new_matches = new_by_key[key]
        if require_fingerprint:
            pairs = pair_by_fingerprint(old_matches, new_matches)
            if pairs:
                for source, target in pairs:
                    comparison.relations.append(
                        Relation(
                            [source],
                            [target],
                            'unchanged',
                            'grounded',
                            'same file, qualified name, and structure',
                        )
                    )
                    claimed_old.add(source.uid)
                    claimed_new.add(target.uid)
            continue

        remaining_old = [
            record for record in old_matches if record.uid not in claimed_old
        ]
        remaining_new = [
            record for record in new_matches if record.uid not in claimed_new
        ]
        if len(remaining_old) == 1 and len(remaining_new) == 1:
            source, target = remaining_old[0], remaining_new[0]
            comparison.relations.append(
                Relation(
                    [source],
                    [target],
                    'modified',
                    'grounded',
                    'same file and qualified name, edited body',
                )
            )
            claimed_old.add(source.uid)
            claimed_new.add(target.uid)
        elif remaining_old or remaining_new:
            comparison.relations.append(
                AmbiguousRelation(
                    remaining_old,
                    remaining_new,
                    'name',
                    'duplicate definitions share file and qualified name',
                )
            )
            for record in remaining_old:
                claimed_old.add(record.uid)
            for record in remaining_new:
                claimed_new.add(record.uid)


def _match_moved(comparison, claimed_old, claimed_new):
    '''Match a unique qualified name that appears under a different file.

    The body may have changed, so this is only evidence of a move when the
    qualified name is unique on both sides and no other block from the old
    file could have been the one moved.
    '''
    old_left = _eligible(comparison.old.records, claimed_old)
    new_left = _eligible(comparison.new.records, claimed_new)

    def group_by(records):
        result = {}
        for record in records:
            result.setdefault(record.qualname, []).append(record)
        return result

    old_by_name = group_by(old_left)
    new_by_name = group_by(new_left)

    for qualname in sorted(set(old_by_name) & set(new_by_name)):
        old_matches = old_by_name[qualname]
        new_matches = new_by_name[qualname]
        if len(old_matches) == 1 and len(new_matches) == 1:
            source, target = old_matches[0], new_matches[0]
            comparison.relations.append(
                Relation(
                    [source],
                    [target],
                    'moved',
                    'grounded',
                    'unique qualified name across files',
                )
            )
            claimed_old.add(source.uid)
            claimed_new.add(target.uid)
        else:
            comparison.relations.append(
                AmbiguousRelation(
                    old_matches,
                    new_matches,
                    'name',
                    'several blocks share the qualified name {0}'.format(
                        qualname
                    ),
                )
            )
            for record in old_matches:
                claimed_old.add(record.uid)
            for record in new_matches:
                claimed_new.add(record.uid)

def _match_structure(comparison, claimed_old, claimed_new):
    old_left = _eligible(comparison.old.records, claimed_old)
    new_left = _eligible(comparison.new.records, claimed_new)
    old_by_fp = {}
    for record in old_left:
        if record.fingerprint:
            old_by_fp.setdefault(record.fingerprint, []).append(record)
    new_by_fp = {}
    for record in new_left:
        if record.fingerprint:
            new_by_fp.setdefault(record.fingerprint, []).append(record)

    for fingerprint in sorted(set(old_by_fp) & set(new_by_fp)):
        old_matches = old_by_fp[fingerprint]
        new_matches = new_by_fp[fingerprint]
        if len(old_matches) == 1 and len(new_matches) == 1:
            source, target = old_matches[0], new_matches[0]
            if source.path == target.path:
                kind = 'renamed'
            elif source.qualname == target.qualname:
                kind = 'moved'
            else:
                kind = 'renamed-moved'
            comparison.relations.append(
                Relation(
                    [source],
                    [target],
                    kind,
                    'grounded',
                    'unique structurally identical candidate',
                )
            )
            claimed_old.add(source.uid)
            claimed_new.add(target.uid)
        else:
            # Ambiguity is reported before any split or heuristic pairing:
            # identical wrappers are not arbitrarily assigned.
            comparison.relations.append(
                AmbiguousRelation(
                    old_matches,
                    new_matches,
                    'structural',
                    'multiple structurally identical candidates',
                )
            )
            for record in old_matches:
                claimed_old.add(record.uid)
            for record in new_matches:
                claimed_new.add(record.uid)


def _contains_all(container, items):
    remaining = list(container)
    for item in items:
        if item not in remaining:
            return False
        remaining.remove(item)
    return True


def _coverage(container, items):
    if not container:
        return 0.0
    remaining = list(container)
    covered = 0
    for item in items:
        if item in remaining:
            remaining.remove(item)
            covered += 1
    return covered / float(len(container))


def _match_splits(comparison, claimed_old, claimed_new):
    '''Discover conservative splits from verbatim statements.

    A split is reported when at least two new functions are built solely from
    statements of one old function and together account for most of that
    function's statements.  If more than one old function could be the origin,
    the result is ambiguous rather than an invented lineage.
    '''
    old_left = _eligible(comparison.old.records, claimed_old)
    new_left = _eligible(comparison.new.records, claimed_new)
    if not old_left or len(new_left) < 2:
        return

    for source in old_left:
        if source.uid in claimed_old:
            continue
        candidates = []
        for target in new_left:
            if target.uid in claimed_new:
                continue
            if not target.statements:
                continue
            if len(target.statements) >= len(source.statements):
                continue
            if _contains_all(source.statements, target.statements):
                candidates.append(target)
        if len(candidates) < 2:
            continue

        target_statements = [
            statement
            for target in candidates
            for statement in target.statements
        ]
        if _coverage(source.statements, target_statements) < 0.8:
            continue

        other_sources = [
            other
            for other in old_left
            if other is not source
            and all(
                _contains_all(other.statements, target.statements)
                for target in candidates
            )
            and _coverage(
                other.statements,
                [
                    statement
                    for target in candidates
                    for statement in target.statements
                ],
            )
            >= 0.8
        ]

        if other_sources:
            comparison.relations.append(
                AmbiguousRelation(
                    [source] + other_sources,
                    candidates,
                    'split',
                    'several old blocks could be the split origin',
                )
            )
            for record in [source] + other_sources:
                claimed_old.add(record.uid)
        else:
            comparison.relations.append(
                Relation(
                    [source],
                    candidates,
                    'split',
                    'split',
                    'new blocks are assembled from statements of this block',
                )
            )
            claimed_old.add(source.uid)
        for target in candidates:
            claimed_new.add(target.uid)


def compare_snapshots(old, new, renames=None, splits=None, no_assert=False):
    '''Compare two snapshots and return a :class:`SnapshotComparison`.

    *old* and *new* may be :class:`Snapshot` instances, directories, Python
    files, mappings of path to source, or ``radon cc --json`` data.

    *renames* is a mapping (or sequence of pairs / ``old=new`` strings) of old
    block references to new block references.  References can be full
    identifiers such as ``pkg/mod.py::Class.method`` or bare qualified names
    such as ``Class.method``.  A value may also be a list, which declares a
    split.

    *splits* is the same kind of mapping, but values are always treated as
    several targets of one source.  Explicit hints are evidence and take
    precedence over automatic matching.

    Only relations with a single defensible source and target are marked
    ``grounded`` and contribute to complexity rankings.
    '''
    old = load_snapshot(old, no_assert=no_assert)
    new = load_snapshot(new, no_assert=no_assert)
    comparison = SnapshotComparison(old, new, [])
    claimed_old = set()
    claimed_new = set()

    _match_explicit(comparison, renames, claimed_old, claimed_new, False)
    _match_explicit(comparison, splits, claimed_old, claimed_new, True)
    _match_identity(comparison, claimed_old, claimed_new, True)
    # Same file and qualified name is direct identity evidence, stronger than
    # a generic structural match: a body edit must not be paired with a newly
    # added copy elsewhere in the tree.
    _match_identity(comparison, claimed_old, claimed_new, False)
    # A unique qualified name across files outranks generic structure: moving
    # an entire module keeps every name, and two identically shaped helpers
    # in that module must not turn into an ambiguity group.
    _match_moved(comparison, claimed_old, claimed_new)
    _match_structure(comparison, claimed_old, claimed_new)
    _match_splits(comparison, claimed_old, claimed_new)
    return comparison


def compare(old, new, **kwargs):
    '''Alias for :func:`compare_snapshots`.'''
    return compare_snapshots(old, new, **kwargs)


def load_snapshot(value, no_assert=False):
    '''Coerce *value* into a :class:`Snapshot`.'''
    if isinstance(value, Snapshot):
        return value
    if isinstance(value, dict):
        return Snapshot.from_sources(value, no_assert=no_assert)
    return Snapshot.from_path(value, no_assert=no_assert)


def rank_complexity_changes(comparison, reverse=True, only_changed=False):
    '''Return complexity deltas for grounded one-to-one continuations.

    ``reverse=True`` puts the largest complexity increases first.  Blocks
    without a defensible one-to-one predecessor are omitted entirely.
    '''
    changes = comparison.complexity_changes(only_changed=only_changed)
    if not reverse:
        changes.sort(
            key=lambda item: (item['delta'], item['target'], item['source'])
        )
        for index, item in enumerate(changes, 1):
            item['rank'] = index
    return changes


# ---------------------------------------------------------------------------
# Multi-snapshot trends
# ---------------------------------------------------------------------------

def build_trends(snapshots, labels=None, renames=None, splits=None):
    '''Build metric trends across an ordered sequence of snapshots.

    Each adjacent pair is matched independently.  A trend only connects two
    consecutive points when the corresponding relation is a grounded
    one-to-one continuation; otherwise the lineage is closed and any target
    starts a new, disconnected trend.  :class:`Trend.breaks` records why the
    curve is disconnected.
    '''
    snapshots = [load_snapshot(snapshot) for snapshot in snapshots]
    if labels is not None:
        for snapshot, label in zip(snapshots, labels):
            snapshot.label = label
    if not snapshots:
        return []
    comparisons = [
        compare_snapshots(
            snapshots[index],
            snapshots[index + 1],
            renames=renames,
            splits=splits,
        )
        for index in range(len(snapshots) - 1)
    ]
    return _merge_trends(comparisons, len(snapshots))


def track_functions(snapshots, labels=None, renames=None, splits=None):
    '''Alias for :func:`build_trends`.'''
    return build_trends(
        snapshots, labels=labels, renames=renames, splits=splits
    )


def _trend_status(segments, total):
    '''Classify a lineage by the continuity of its own curve.

    ``broken`` means the series has a gap, ``isolated`` means it is a single
    point with no connected neighbour, ``partial`` means it is contiguous but
    does not span the whole range, and ``connected`` means it does.
    '''
    if not segments:
        return 'isolated'
    if len(segments) > 1:
        return 'broken'
    points = segments[0]
    if len(points) == 1:
        return 'isolated'
    if points[0]['snapshot'] != 0 or points[-1]['snapshot'] != total - 1:
        return 'partial'
    return 'connected'


def _merge_trends(comparisons, total):
    '''Join adjacent-pair grounded relations into multi-snapshot chains.

    A trend's ``status`` describes its own continuity: ``connected`` when
    every adjacent pair in the series is a grounded one-to-one relation.
    ``breaks`` describe why a lineage starts later or stops earlier; a
    lineage that begins *after* a split can still be internally connected.

    Lineages are tracked with synthetic keys rather than block identifiers.
    Reusing an identifier after a break must not reconnect the old and new
    curves, so identifiers are only used to look up the *record* participating
    in a relation.
    '''
    if not comparisons:
        return []
    first_old = comparisons[0].old
    chains = {}
    lineage_of = {}
    break_before = {}
    break_after = {}
    counter = [0]

    def new_key():
        counter[0] += 1
        return 'lineage-{0}'.format(counter[0])

    for record in first_old.records:
        key = new_key()
        chains[key] = [_point(0, record)]
        lineage_of[record.uid] = key

    for index, comparison in enumerate(comparisons, 1):
        next_lineage = {}
        continued = set()

        # Grounded one-to-one continuations extend the existing chain.
        for relation in comparison.grounded:
            source, target = relation.source, relation.target
            key = lineage_of.get(source.uid)
            if key is None:
                continue
            chains[key].append(_point(index, target))
            next_lineage[target.uid] = key
            continued.add(key)

        # Split and ambiguous groups deliberately break the curve.  Each
        # target begins a new chain; its break record explains that it has
        # no connected predecessor, not that its own curve is broken.
        for relation in comparison.relations:
            if relation.rankable:
                continue
            if relation.status not in ('split', 'ambiguous'):
                continue
            for source in relation.sources:
                key = lineage_of.get(source.uid)
                if key is not None:
                    continued.add(key)
                    break_after.setdefault(
                        key,
                        {
                            'after_snapshot': index - 1,
                            'reason': relation.status,
                            'targets': [
                                target.uid for target in relation.targets
                            ],
                            'evidence': relation.evidence,
                        },
                    )
            for target in relation.targets:
                key = new_key()
                chains[key] = [_point(index, target)]
                next_lineage[target.uid] = key
                break_before.setdefault(
                    key,
                    {
                        'before_snapshot': index,
                        'reason': relation.status,
                        'sources': [source.uid for source in relation.sources],
                        'evidence': relation.evidence,
                    },
                )

        # New blocks without evidence start their own disconnected chain.
        for record in comparison.new.records:
            if record.uid in next_lineage:
                continue
            key = new_key()
            chains[key] = [_point(index, record)]
            next_lineage[record.uid] = key
            break_before.setdefault(
                key,
                {
                    'before_snapshot': index,
                    'reason': 'new or unmatched',
                    'sources': [],
                    'evidence': 'no continuation found',
                },
            )

        for key in set(lineage_of.values()) - continued:
            break_after.setdefault(
                key,
                {
                    'after_snapshot': index - 1,
                    'reason': 'deleted or unmatched',
                    'targets': [],
                    'evidence': 'no continuation found',
                },
            )
        lineage_of = next_lineage

    trends = []
    for key, points in chains.items():
        points = sorted(points, key=lambda point: point['snapshot'])
        segments = []
        run = []
        previous = None
        for point in points:
            if previous is not None and point['snapshot'] != previous + 1:
                if run:
                    segments.append(run)
                run = []
            run.append(point)
            previous = point['snapshot']
        if run:
            segments.append(run)

        breaks = []
        if key in break_before:
            breaks.append(break_before[key])
        if key in break_after:
            breaks.append(break_after[key])
        status = _trend_status(segments, total)
        trends.append(
            Trend(
                segments=segments,
                breaks=breaks,
                status=status,
                starts_after=break_before.get(key),
                ends_before=break_after.get(key),
            )
        )

    trends.sort(key=_trend_sort_key)
    return trends
