'''Derive project-wide top-K complexity symbols from truncated summaries.

A quality service may only retain, for every module, the highest-scoring
``limit`` blocks together with the total number of blocks and the score at the
truncation point.  The unseen blocks of such a module can have any score up to
that cutoff, so the project-wide top-K is not always known exactly.  This
module computes what is certain and what is still possible without inventing
an ordering for tied symbols.

Ties are deliberately not resolved with module or symbol names.  With the
default ``tie_mode='include'`` a top-K result contains every symbol whose score
is greater than or equal to the K-th highest score.  Consequently a tie at the
boundary can make the result larger than K.  The ``'exact'`` mode models a
caller that insists on exactly K entries and may break ties arbitrarily; in
that mode a symbol at the boundary is only reported as possible unless its
whole tie group fits inside K.

The public entry points are :func:`summarize_blocks`,
:func:`summarize_module`, :func:`summarize_path`, :func:`derive_top_k` and
:class:`TopKAnalyzer`.  :func:`top_k_from_blocks` is the direct aggregation
used as the reference when every module has been restored.
'''

import bisect
import collections
import json
import os
import re

__all__ = [
    'Symbol',
    'ModuleSummary',
    'TopKResult',
    'TopKAnalyzer',
    'summarize_blocks',
    'summarize_blocks_by_module',
    'summarize_module',
    'summarize_file',
    'summarize_path',
    'summarize_paths',
    'derive',
    'derive_top_k',
    'project_top_k',
    'top_k',
    'top_k_from_blocks',
    'top_k_from_paths',
    'certain_symbols',
    'possible_symbols',
    'remaining_slots',
]

TIE_INCLUDE = 'include'
TIE_EXACT = 'exact'

_INCLUDE_ALIASES = frozenset(('include', 'inclusive', 'ties', 'all'))
_EXACT_ALIASES = frozenset(('exact', 'strict', 'k'))


class Symbol(collections.namedtuple('_Symbol', ('module', 'name', 'score'))):
    '''A block of code identified by its module and its qualified name.'''

    __slots__ = ()

    @property
    def complexity(self):
        '''Alias for :attr:`score`, matching Radon's block attribute.'''
        return self.score

    @property
    def identifier(self):
        '''A stable, human-readable identity for the symbol.'''
        if self.module:
            return '{0}:{1}'.format(self.module, self.name)
        return self.name

    def __str__(self):
        return self.identifier


class ModuleSummary(object):
    '''The retained part of one module's complexity table.

    :param name: Module name or path.
    :param entries: Known symbols, normally the highest-scoring ``limit``
        blocks.  Entries may be :class:`Symbol` instances, ``(name, score)``
        pairs, dictionaries or Radon block objects.
    :param total: Total number of symbols in the module.  When omitted the
        number of known entries is assumed.
    :param cutoff: Score at the truncation point.  Any unseen symbol has a
        score less than or equal to this value.  When omitted it is inferred
        from the last known entry.
    :param complete: Assert that the full table is known.  A summary whose
        ``total`` equals the number of known entries is always complete.
    '''

    def __init__(self, name, entries=None, total=None, cutoff=None,
                 complete=None):
        self.name = name
        if isinstance(entries, dict):
            entries = list(entries.items())
        symbols = [
            _coerce_symbol(entry, name=name, index=index)
            for index, entry in enumerate(entries or ())
        ]
        symbols.sort(key=lambda symbol: -symbol.score)
        if total is None:
            total = len(symbols)
        total = int(total)
        if total < 0:
            raise ValueError('total must be non-negative')
        if total < len(symbols):
            raise ValueError(
                'module {0!r} has {1} known symbols but total is {2}'.format(
                    name, len(symbols), total
                )
            )

        is_complete = total == len(symbols)
        if complete is True and not is_complete:
            raise ValueError(
                'module {0!r} is marked complete but has {1} unknown '
                'symbols'.format(name, total - len(symbols))
            )
        if not is_complete:
            is_complete = False
        self.total = total
        self.complete = is_complete
        if is_complete:
            self.cutoff = None
            self.unknown = 0
        else:
            if not symbols and cutoff is None:
                raise ValueError(
                    'module {0!r} has unknown symbols and no cutoff'.format(
                        name
                    )
                )
            if cutoff is None:
                cutoff = symbols[-1].score
            self.cutoff = _number(cutoff, 'cutoff')
            self.unknown = total - len(symbols)
        self.symbols = tuple(symbols)

    @property
    def known(self):
        '''Known symbols in descending score order.'''
        return self.symbols

    @property
    def known_count(self):
        return len(self.symbols)

    def restore(self, entries):
        '''Build a complete summary from all of this module's symbols.'''
        symbols = [
            _coerce_symbol(entry, name=self.name, index=index)
            for index, entry in enumerate(entries)
        ]
        return ModuleSummary(
            self.name, symbols, total=len(symbols), complete=True
        )

    def to_dict(self, include_symbols=True):
        result = {
            'name': self.name,
            'total': self.total,
            'cutoff': self.cutoff,
            'complete': self.complete,
        }
        if include_symbols:
            result['symbols'] = [
                {'module': s.module, 'name': s.name, 'score': s.score}
                for s in self.symbols
            ]
        return result

    @classmethod
    def from_dict(cls, data):
        '''Build a summary from the commonly used dictionary spellings.'''
        return _coerce_summary(data)

    @classmethod
    def from_blocks(cls, name, blocks, limit=10):
        return summarize_blocks(blocks, name=name, limit=limit)

    def __repr__(self):
        return (
            'ModuleSummary(name={0!r}, total={1}, known={2}, '
            'cutoff={3!r}, complete={4!r})'
        ).format(
            self.name, self.total, self.known_count, self.cutoff,
            self.complete
        )


class TopKResult(dict):
    '''Mapping with attribute access for a top-K derivation result.

    The most important keys are ``certain`` (symbols present in every
    tie-consistent top-K), ``possible`` (known symbols that may be present),
    ``uncertain`` (``possible`` minus ``certain``), ``remaining`` (additional
    symbols that can still enter, including unseen ones), ``seats_open``
    (``K`` minus the number of certain symbols) and ``complete``.
    '''

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    @property
    def certain_names(self):
        return tuple(symbol.name for symbol in self['certain'])

    @property
    def possible_names(self):
        return tuple(symbol.name for symbol in self['possible'])

    @property
    def uncertain_names(self):
        return tuple(symbol.name for symbol in self['uncertain'])

    @property
    def guaranteed(self):
        return self['certain']

    @property
    def contenders(self):
        return self['uncertain']

    def to_dict(self, json_safe=False):
        '''Return a plain dictionary.

        With *json_safe* the symbols are serialized as dictionaries, which
        makes the result suitable for ``json.dumps``.
        '''
        result = dict(self)
        if json_safe:
            for key in ('certain', 'possible', 'uncertain'):
                result[key] = [
                    {'module': s.module, 'name': s.name, 'score': s.score}
                    for s in self[key]
                ]
        return result


class TopKAnalyzer(object):
    '''Incremental top-K analysis over module summaries.

    Add truncated summaries with :meth:`add_summary` or :meth:`add_blocks`.
    When a module's full table becomes available, :meth:`restore_module`
    replaces the truncated entry and the next :meth:`derive` call reflects the
    narrowed bounds.
    '''

    def __init__(self, k, summaries=None, tie_mode=TIE_INCLUDE,
                 min_score=0.0):
        self.k = _positive_k(k)
        self.tie_mode = _tie_mode(tie_mode)
        self.min_score = _number(min_score, 'min_score')
        self._summaries = collections.OrderedDict()
        for summary in _coerce_summaries(summaries or ()):
            self.add_summary(summary)

    def add_summary(self, summary):
        summary = _coerce_summary(summary)
        self._summaries[summary.name] = summary
        return self

    add_module = add_summary

    def add_blocks(self, name, blocks, limit=10):
        '''Add a module from its blocks, retaining *limit* rows.'''
        self.add_summary(summarize_blocks(blocks, name=name, limit=limit))
        return self

    def restore_module(self, name, blocks=None, entries=None):
        '''Replace a module with its complete table.

        Pass either Radon blocks/block-like entries as *blocks* or summary
        entries as *entries*.  The next derivation sees no unknown symbols
        for this module.
        '''
        if (blocks is None) == (entries is None):
            raise TypeError('pass exactly one of blocks or entries')
        values = blocks if blocks is not None else entries
        symbols = [
            _coerce_symbol(value, name=name, index=index)
            for index, value in enumerate(values)
        ]
        self._summaries[name] = ModuleSummary(
            name, symbols, total=len(symbols), complete=True
        )
        return self

    restore = restore_module
    complete_module = restore_module

    def remove_module(self, name):
        del self._summaries[name]
        return self

    @property
    def summaries(self):
        return tuple(self._summaries.values())

    @property
    def complete(self):
        return all(summary.complete for summary in self._summaries.values())

    def derive(self):
        return derive_top_k(
            self._summaries.values(), self.k, tie_mode=self.tie_mode,
            min_score=self.min_score
        )

    top_k = derive
    project = derive

    def __len__(self):
        return len(self._summaries)

    def __repr__(self):
        return 'TopKAnalyzer(k={0!r}, modules={1!r}, complete={2!r})'.format(
            self.k, list(self._summaries), self.complete
        )


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError('{0} must be a number'.format(label))
    return value


def _positive_k(k):
    if isinstance(k, bool) or not isinstance(k, int):
        raise TypeError('k must be an integer')
    if k <= 0:
        raise ValueError('k must be positive')
    return k


def _tie_mode(mode):
    if not isinstance(mode, str):
        raise TypeError('tie_mode must be a string')
    lowered = mode.lower()
    if lowered in _INCLUDE_ALIASES:
        return TIE_INCLUDE
    if lowered in _EXACT_ALIASES:
        return TIE_EXACT
    raise ValueError(
        'unknown tie_mode {0!r}; use include or exact'.format(mode)
    )


def _first(data, names, default=None):
    for name in names:
        if name in data and data[name] is not None:
            return data[name]
    return default


def _module_name(entry, name):
    if name is not None:
        return name
    if isinstance(entry, Symbol):
        return entry.module or '<module>'
    if isinstance(entry, dict):
        return _first(entry, ('module', 'path', 'file'), '<module>')
    return getattr(entry, 'module', None) or '<module>'


def _coerce_symbol(entry, name=None, index=0):
    if isinstance(entry, Symbol):
        return Symbol(_module_name(entry, name), entry.name, entry.score)
    if isinstance(entry, dict):
        symbol_name = _first(entry, ('name', 'symbol', 'fullname', 'id'))
        score = _first(
            entry, ('score', 'complexity', 'value', 'cc', 'rank_score')
        )
        module = _module_name(entry, name)
        if symbol_name is None or score is None:
            raise ValueError(
                'entry {0} needs name and score'.format(index)
            )
        return Symbol(module, str(symbol_name), _number(score, 'score'))
    if isinstance(entry, (tuple, list)) and not hasattr(entry, 'complexity'):
        if len(entry) < 2:
            raise ValueError('entry {0} needs name and score'.format(index))
        left, right = entry[0], entry[1]
        if isinstance(left, str) and isinstance(right, (int, float)):
            symbol_name, score = left, right
        elif isinstance(right, str) and isinstance(left, (int, float)):
            score, symbol_name = left, right
        else:
            symbol_name, score = left, right
        return Symbol(
            _module_name(entry, name), str(symbol_name),
            _number(score, 'score')
        )
    if isinstance(entry, str):
        match = re.match(
            r'^\s*(?P<name>.+?)\s*(?:[:=]\s*|\s+\(\s*|\s+)'
            r'(?P<score>\d+(?:\.\d+)?)\s*\)?\s*$',
            entry,
        )
        if match:
            return Symbol(
                name, match.group('name').strip(),
                _number(float(match.group('score')), 'score')
            )
        raise ValueError('cannot read symbol from {0!r}'.format(entry))
    symbol_name = getattr(entry, 'fullname', None) or getattr(
        entry, 'name', None
    )
    score = getattr(entry, 'complexity', None)
    if symbol_name is None or score is None:
        raise ValueError('cannot read symbol from {0!r}'.format(entry))
    return Symbol(name, symbol_name, _number(score, 'score'))


def _coerce_summary(value):
    if isinstance(value, ModuleSummary):
        return value
    if isinstance(value, dict):
        module_name = _first(
            value, ('name', 'module', 'path', 'file', 'filename'),
            '<module>'
        )
        entries = _first(
            value,
            (
                'symbols', 'entries', 'top', 'top_ten', 'top10', 'head',
                'items', 'blocks', 'table', 'rows',
            ),
            (),
        )
        total = _first(
            value,
            (
                'total', 'total_symbols', 'symbol_count', 'count',
                'n_symbols', 'num_symbols', 'size',
            ),
        )
        cutoff = _first(
            value,
            (
                'cutoff', 'cutoff_score', 'truncation_score',
                'score_at_cutoff', 'last_score', 'threshold', 'min_score',
            ),
        )
        complete = _first(
            value, ('complete', 'restored', 'full', 'is_complete')
        )
        if complete is not None and not isinstance(complete, bool):
            complete = bool(complete)
        return ModuleSummary(
            module_name, entries, total=total, cutoff=cutoff,
            complete=complete
        )
    if isinstance(value, (tuple, list)):
        if len(value) == 4:
            return ModuleSummary(
                value[0], value[1], total=value[2], cutoff=value[3]
            )
        if len(value) == 3:
            return ModuleSummary(
                '<module>', value[0], total=value[1], cutoff=value[2]
            )
        if len(value) == 2:
            return ModuleSummary(value[0], value[1])
    raise TypeError('cannot interpret summary {0!r}'.format(value))


def _coerce_summaries(value):
    '''Normalize a sequence or ``{module: summary}`` mapping.'''
    if isinstance(value, ModuleSummary):
        return [value]
    if isinstance(value, dict):
        if any(
            key in value
            for key in ('symbols', 'entries', 'total', 'cutoff')
        ):
            return [_coerce_summary(value)]
        return [
            _named_summary(name, summary)
            for name, summary in value.items()
        ]
    return [_coerce_summary(summary) for summary in value]


def _named_summary(name, value):
    if isinstance(value, ModuleSummary):
        if value.name == name:
            return value
        return ModuleSummary(
            name, value.symbols, total=value.total,
            cutoff=value.cutoff, complete=value.complete
        )
    summary = _coerce_summary(value)
    if summary.name == '<module>':
        return ModuleSummary(
            name, summary.symbols, total=summary.total,
            cutoff=summary.cutoff, complete=summary.complete
        )
    return summary


def summarize_blocks(blocks, name='<module>', limit=10):
    '''Summarize a complete block list into its retained top-*limit* rows.

    The returned :class:`ModuleSummary` is complete when the module has at
    most *limit* blocks.  Otherwise unseen blocks are bounded by the last
    retained score.
    '''
    if limit is None:
        limit = len(blocks) if hasattr(blocks, '__len__') else 10
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError('limit must be a non-negative integer or None')
    symbols = [
        _coerce_symbol(block, name=name, index=index)
        for index, block in enumerate(blocks)
    ]
    symbols.sort(key=lambda symbol: -symbol.score)
    retained = symbols[:limit]
    cutoff = retained[-1].score if len(symbols) > len(retained) else None
    return ModuleSummary(
        name, retained, total=len(symbols), cutoff=cutoff
    )


def summarize_blocks_by_module(blocks, limit=10):
    '''Group block-like objects by module and summarize each module.'''
    groups = collections.OrderedDict()
    for index, block in enumerate(blocks):
        symbol = _coerce_symbol(block, name=None, index=index)
        groups.setdefault(symbol.module or '<module>', []).append(symbol)
    return [
        summarize_blocks(symbols, name=name, limit=limit)
        for name, symbols in groups.items()
    ]


def summarize_module(source, name='<module>', limit=10, no_assert=False,
                     show_closures=False):
    '''Parse *source* with Radon and summarize its blocks.'''
    from radon.complexity import add_inner_blocks, cc_visit

    blocks = cc_visit(source, no_assert=no_assert)
    if show_closures:
        blocks = add_inner_blocks(blocks)
    return summarize_blocks(blocks, name=name, limit=limit)


def summarize_file(path, limit=10, no_assert=False, show_closures=False):
    '''Read a Python file and summarize its Radon blocks.'''
    with open(path, 'r') as stream:
        source = stream.read()
    return summarize_module(
        source, name=path, limit=limit, no_assert=no_assert,
        show_closures=show_closures
    )


def _iter_python_files(paths):
    for path in paths:
        if os.path.isfile(path):
            if path.endswith('.py'):
                yield path
            continue
        for root, dirs, files in os.walk(path):
            dirs[:] = sorted(
                directory for directory in dirs
                if not directory.startswith('.')
            )
            for filename in sorted(files):
                if filename.endswith('.py'):
                    yield os.path.join(root, filename)


def summarize_paths(paths, limit=10, no_assert=False, show_closures=False):
    '''Summarize every Python file below *paths*.'''
    return [
        summarize_file(
            path, limit=limit, no_assert=no_assert,
            show_closures=show_closures
        )
        for path in _iter_python_files(paths)
    ]


summarize_path = summarize_file


def _flatten_blocks(blocks):
    if isinstance(blocks, dict):
        for module, values in blocks.items():
            for index, value in enumerate(values):
                yield _coerce_symbol(value, name=module, index=index)
        return
    for index, value in enumerate(blocks):
        yield _coerce_symbol(value, name=None, index=index)


def _kth_largest(scores, k):
    if k <= 0 or len(scores) < k:
        return None
    return sorted(scores, reverse=True)[k - 1]


def _selected(symbols, k, tie_mode):
    if not symbols:
        return ()
    ordered = sorted(symbols, key=lambda symbol: -symbol.score)
    if len(ordered) < k:
        return tuple(ordered)
    threshold = ordered[k - 1].score
    if tie_mode == TIE_EXACT:
        return tuple(ordered[:k])
    return tuple(symbol for symbol in ordered if symbol.score >= threshold)

def top_k_from_blocks(blocks, k, tie_mode=TIE_INCLUDE):
    '''Directly aggregate block-like data into a top-K result.

    This deliberately bypasses summaries and is the reference used to verify
    that a fully restored set of summaries produces the same tie boundary.
    '''
    k = _positive_k(k)
    mode = _tie_mode(tie_mode)
    symbols = list(_flatten_blocks(blocks))
    selected = _selected(symbols, k, mode)
    return tuple(selected)


def top_k_from_paths(paths, k, limit=10, no_assert=False,
                     show_closures=False, tie_mode=TIE_INCLUDE):
    '''Summarize paths and immediately derive their project-wide top-K.

    *limit* is the number of rows retained per module (ten by default).  Use
    ``limit=None`` to keep every block, which produces a complete derivation.
    '''
    summaries = summarize_paths(
        paths, limit=limit, no_assert=no_assert,
        show_closures=show_closures
    )
    return derive_top_k(summaries, k, tie_mode=tie_mode)


def _safe_unknown_floor(min_score, cutoff):
    if min_score > cutoff:
        raise ValueError(
            'min_score {0!r} is above an unseen cutoff {1!r}'.format(
                min_score, cutoff
            )
        )
    return min_score


def derive_top_k(summaries, k, tie_mode=TIE_INCLUDE, min_score=0.0):
    '''Derive the certain and possible parts of the project-wide top-K.

    :param summaries: Iterable of :class:`ModuleSummary` objects, dictionaries
        with the summary fields, or ``(entries, total, cutoff)`` triples.
    :param k: Number of top places to reason about.
    :param tie_mode: ``'include'`` (default) includes every symbol tied at the
        K-th score; ``'exact'`` assumes an exactly-K result whose ties may be
        broken arbitrarily.
    :param min_score: Lower bound for unseen scores.  Radon block scores are
        at least 1, but 0 is a safe generic default.
    '''
    k = _positive_k(k)
    tie_mode = _tie_mode(tie_mode)
    min_score = _number(min_score, 'min_score')
    summaries = _coerce_summaries(summaries)

    known = []
    unknown = []
    total_symbols = 0
    unknown_symbols = 0
    for summary in summaries:
        known.extend(summary.symbols)
        total_symbols += summary.total
        if summary.unknown:
            _safe_unknown_floor(min_score, summary.cutoff)
            unknown.append((summary.name, summary.unknown, summary.cutoff))
            unknown_symbols += summary.unknown

    known_scores = sorted(symbol.score for symbol in known)

    def known_gt(score):
        return len(known_scores) - bisect.bisect_right(known_scores, score)

    def known_ge(score):
        return len(known_scores) - bisect.bisect_left(known_scores, score)

    if unknown_symbols == 0:
        # With a complete table there is no uncertainty left about scores.
        # The inclusive mode selects every symbol at or above the boundary,
        # exactly like direct block aggregation.  In exact mode the result has
        # K entries, so symbols tied at the boundary remain possible but the
        # ones that may be left out are not certain.
        ordered = sorted(known, key=lambda symbol: -symbol.score)
        if len(ordered) <= k:
            selected = tuple(ordered)
            certain_selected = selected
        else:
            threshold = ordered[k - 1].score
            selected = tuple(
                symbol for symbol in ordered if symbol.score >= threshold
            )
            if tie_mode == TIE_EXACT:
                above = tuple(
                    symbol for symbol in ordered if symbol.score > threshold
                )
                tied = tuple(
                    symbol for symbol in ordered if symbol.score == threshold
                )
                if len(tied) <= k - len(above):
                    certain_selected = above + tied
                else:
                    certain_selected = above
            else:
                certain_selected = selected
        selected_scores = sorted(
            (symbol.score for symbol in known), reverse=True
        )
        boundary = _kth_largest(selected_scores, k)
        return TopKResult(
            k=k,
            tie_mode=tie_mode,
            certain=certain_selected,
            possible=selected,
            uncertain=tuple(
                symbol for symbol in selected
                if symbol not in set(certain_selected)
            ),
            remaining=len(selected) - len(certain_selected),
            remaining_slots=len(selected) - len(certain_selected),
            remaining_known=len(selected) - len(certain_selected),
            remaining_unknown=0,
            seats_open=max(0, k - len(certain_selected)),
            unknown_possible=0,
            unknown_slots=0,
            unknown_symbols=0,
            total_symbols=total_symbols,
            modules=len(summaries),
            cutoff_min=boundary,
            cutoff_max=boundary,
            complete=True,
            certain_names=tuple(s.name for s in certain_selected),
            possible_names=tuple(s.name for s in selected),
            uncertain_names=tuple(
                s.name for s in selected if s not in set(certain_selected)
            ),
        )

    certain = []
    possible = []
    for symbol in known:
        score = symbol.score
        # Unknown scores range between min_score and their module cutoff.
        # ``max_gt`` is the worst-case number of symbols strictly above this
        # one; ``min_gt`` is the best-case number.  Both modes report a symbol
        # as possible when a completion can place it in the top K, but the
        # inclusive mode also includes every symbol tied at the boundary.
        max_gt = known_gt(score) + sum(
            count for _, count, cutoff in unknown if cutoff > score
        )
        min_gt = known_gt(score) + sum(
            count for _, count, _ in unknown if min_score > score
        )
        max_ge_others = (
            known_ge(score) - 1
            + sum(count for _, count, cutoff in unknown if cutoff >= score)
        )

        is_certain = max_gt < k
        if tie_mode == TIE_EXACT:
            # Tied symbols may be arbitrarily left out, so only promote a
            # symbol when even the whole worst-case tie group fits.
            is_certain = max_ge_others < k
        is_possible = min_gt < k

        if is_certain:
            certain.append(symbol)
        if is_possible:
            possible.append(symbol)

    certain = tuple(certain)
    possible = tuple(possible)
    certain = tuple(sorted(certain, key=lambda symbol: -symbol.score))
    possible = tuple(sorted(possible, key=lambda symbol: -symbol.score))
    certain_ids = set(certain)
    uncertain = tuple(
        symbol for symbol in possible if symbol not in certain_ids
    )

    # The K-th score can range between two completions: every unseen symbol
    # at its lowest value and every unseen symbol at its cutoff.
    low_scores = list(known_scores) + [min_score] * unknown_symbols
    high_scores = list(known_scores)
    for _, count, cutoff in unknown:
        high_scores.extend([cutoff] * count)
    cutoff_min = _kth_largest(low_scores, k)
    cutoff_max = _kth_largest(high_scores, k)

    # An unseen symbol can enter when fewer than K known symbols are strictly
    # above its module cutoff: assign every other unseen block the lowest
    # possible score and give this block (and its siblings) the cutoff.
    unknown_possible = 0
    for _, count, cutoff in unknown:
        if known_gt(cutoff) < k:
            unknown_possible += count

    # ``remaining`` counts additional symbols (known contenders plus unseen
    # symbols) that can still enter some tie-consistent top-K.  It is not
    # necessarily capped by ``k - len(certain)``: a tie at the boundary may
    # legitimately make an inclusive result larger than K.
    remaining = len(uncertain) + unknown_possible
    seats_open = max(0, k - len(certain))

    result = TopKResult(
        k=k,
        tie_mode=tie_mode,
        certain=certain,
        possible=possible,
        uncertain=uncertain,
        remaining=remaining,
        remaining_slots=remaining,
        remaining_known=len(uncertain),
        remaining_unknown=unknown_possible,
        seats_open=seats_open,
        unknown_possible=unknown_possible,
        unknown_slots=unknown_possible,
        unknown_symbols=unknown_symbols,
        total_symbols=total_symbols,
        modules=len(summaries),
        cutoff_min=cutoff_min,
        cutoff_max=cutoff_max,
        complete=unknown_symbols == 0,
    )
    result['certain_names'] = tuple(s.name for s in certain)
    result['possible_names'] = tuple(s.name for s in possible)
    result['uncertain_names'] = tuple(s.name for s in uncertain)
    return result


def derive(summaries, k=10, **kwargs):
    '''Alias for :func:`derive_top_k` with a default K of 10.'''
    return derive_top_k(summaries, k, **kwargs)


project_top_k = derive_top_k
top_k = derive_top_k


def certain_symbols(summaries, k, **kwargs):
    '''Return the symbols guaranteed to occur in every tie-consistent top-K.'''
    return derive_top_k(summaries, k, **kwargs)['certain']


def possible_symbols(summaries, k, **kwargs):
    '''Return the known symbols that can occur in some top-K completion.'''
    return derive_top_k(summaries, k, **kwargs)['possible']


def remaining_slots(summaries, k, **kwargs):
    '''Return how many of the K places are not yet determined.'''
    return derive_top_k(summaries, k, **kwargs)['remaining']


def main(argv=None):
    '''Read a JSON list of summaries and print a JSON derivation.'''
    import argparse

    parser = argparse.ArgumentParser(
        prog='python -m radon.topk',
        description='Derive a top-K complexity table from module summaries.',
    )
    parser.add_argument(
        'summary', help='JSON file containing a list of module summaries'
    )
    parser.add_argument(
        '-k', '--top', type=int, default=10, help='number of top places'
    )
    parser.add_argument(
        '--exact', action='store_true',
        help='assume exactly K entries with arbitrary tie-breaking'
    )
    args = parser.parse_args(argv)
    with open(args.summary) as stream:
        summaries = json.load(stream)
    result = derive_top_k(
        summaries, args.top, tie_mode=TIE_EXACT if args.exact else TIE_INCLUDE
    )
    print(json.dumps(result.to_dict(json_safe=True), indent=2, sort_keys=True))


if __name__ == '__main__':  # pragma: no cover
    main()
