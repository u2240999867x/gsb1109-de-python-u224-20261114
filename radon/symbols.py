'''Source-ordered symbol records derived from Radon's complexity analysis.

Radon's traditional output is a flat list of :class:`~radon.visitors.Function`
and :class:`~radon.visitors.Class` blocks.  That representation is convenient
for the command line, but it is lossy for consumers that need to store one row
per definition:

* a function only knows its immediate class name, so two methods called ``run``
  in two different classes, or a method and a closure with the same name, are
  indistinguishable by their short name;
* closures and inner classes live in separate attributes and are not part of
  the source-ordered ``blocks`` list;
* definitions nested inside functions (including classes) are not visited at
  all;
* two definitions with the same name in the same scope produce two blocks that
  cannot be told apart once a name-keyed table silently keeps only the last
  one;
* the complexity of a class is an aggregate over its methods, while the
  complexity of a function is that function's own metric.  Summing both kinds
  of value counts the methods twice.

This module addresses those problems by flattening the syntax tree into
:class:`SymbolRecord` objects.  Every record keeps:

* the containing scope and a fully qualified name (``A.run.run``);
* the definition's position in its scope and in the module;
* its exact line range;
* a unique ``symbol_id`` (qualified name plus same-name occurrence);
* the complexity value together with its metric level (``class_aggregate`` or
  ``*_own``), so aggregate and self measurements are never mixed up.

:class:`SymbolTable` additionally provides module totals which count every
definition's contribution exactly once, including definitions nested inside
other definitions.

The records are derived from the AST instead of from the flat block list, so
the reported hierarchy and line ranges stay correct even where Radon's block
model drops or misplaces nested definitions.  Complexity values follow Radon's
definitions: a function's value counts its own decision points plus one, while
a class's value is the aggregate Radon reports for the class.
'''

import ast
import collections

from radon.complexity import cc_rank
from radon.visitors import Class, ComplexityVisitor, Function

__all__ = [
    'CLASS_AGGREGATE',
    'FUNCTION_OWN',
    'METHOD_OWN',
    'MODULE_SCOPE',
    'SymbolRecord',
    'Symbol',
    'SymbolTable',
    'build_symbol_table',
    'collect_symbols',
    'symbol_records',
    'symbol_table',
    'symbols_from_ast',
    'symbols_from_blocks',
    'symbols_from_code',
    'symbols_from_tree',
    'symbols_from_visitor',
]

MODULE_SCOPE = ''

#: Complexity of a class: an aggregate computed from the class body and its
#: methods.  The aggregate is not a self measurement and must not be summed
#: together with the methods' own values.
CLASS_AGGREGATE = 'class_aggregate'
#: Complexity of a method, measured on the method itself.
METHOD_OWN = 'method_own'
#: Complexity of a function/closure, measured on the function itself.
FUNCTION_OWN = 'function_own'

_KINDS = {'class': 'C', 'method': 'M', 'function': 'F'}


class SymbolRecord(
    collections.namedtuple(
        'SymbolRecord',
        [
            'module',
            'symbol_id',
            'name',
            'qualname',
            'scope',
            'kind',
            'order',
            'scope_order',
            'occurrence',
            'lineno',
            'col_offset',
            'endline',
            'complexity',
            'real_complexity',
            'metric_level',
            'parent_id',
            'depth',
        ],
    )
):
    '''A single class, method, function or closure definition.

    ``module``
        Name of the module the definition belongs to.
    ``symbol_id``
        Identifier unique within the module.  It is the qualified name plus the
        one-based ``occurrence`` of that name in its scope, for example
        ``A.run#1`` or ``same#2``.  Two same-named definitions in one scope
        therefore keep distinct identifiers.
    ``name``
        Short name as written in the source.
    ``qualname``
        Name qualified with every containing scope (``A.run.run``).
    ``scope``
        Qualified name of the containing scope.  It is empty for module-level
        definitions.
    ``kind``
        ``'class'``, ``'method'`` or ``'function'``.  Definitions directly in a
        class body are methods; everything else is a function.
    ``order``
        One-based source order of the definition in the whole module.
    ``scope_order``
        One-based source order of the definition among all definitions that
        share its immediate ``scope``.
    ``occurrence``
        One-based occurrence of this *name* within its immediate ``scope``.
        It is greater than one for repeated definitions such as two ``same``
        functions in the same module.
    ``lineno``, ``col_offset``, ``endline``
        The definition's exact line range.
    ``complexity``
        The metric for this record.  It is the class aggregate for classes and
        the block's own complexity for functions and methods.
    ``real_complexity``
        For a class, the aggregate total Radon uses to derive
        :attr:`complexity`; ``None`` for functions and methods.
    ``metric_level``
        ``'class_aggregate'`` for classes, ``'method_own'`` for methods and
        ``'function_own'`` for functions/closures.  It records whether the
        number is an aggregate or a self measurement.
    ``parent_id``
        ``symbol_id`` of the containing definition, or ``None`` at module
        level.
    ``depth``
        Nesting depth.  Module-level definitions have depth ``0``.
    '''

    __slots__ = ()

    @property
    def letter(self):
        '''The letter used by Radon's terminal output.'''
        return _KINDS[self.kind]

    @property
    def rank(self):
        '''The A-F rank of :attr:`complexity`.'''
        return cc_rank(self.complexity)

    @property
    def line_range(self):
        '''The ``(lineno, endline)`` pair.'''
        return (self.lineno, self.endline)

    @property
    def start_line(self):
        '''Alias for :attr:`lineno`.'''
        return self.lineno

    @property
    def end_line(self):
        '''Alias for :attr:`endline`.'''
        return self.endline

    @property
    def qualified_name(self):
        '''Alias for :attr:`qualname`.'''
        return self.qualname

    @property
    def fullname(self):
        '''Alias for :attr:`qualname`, mirroring Radon's block API.'''
        return self.qualname

    @property
    def uid(self):
        '''Alias for :attr:`symbol_id`.'''
        return self.symbol_id

    @property
    def scope_name(self):
        '''Alias for :attr:`scope`.'''
        return self.scope

    @property
    def scope_id(self):
        '''Alias for :attr:`parent_id`.'''
        return self.parent_id

    @property
    def definition_order(self):
        '''Alias for :attr:`order`.'''
        return self.order

    @property
    def definition_index(self):
        '''Alias for :attr:`order`.'''
        return self.order

    @property
    def complexity_level(self):
        '''Alias for :attr:`metric_level`.'''
        return self.metric_level

    @property
    def measurement_level(self):
        '''The entity level the metric describes: class, method or function.'''
        if self.metric_level == CLASS_AGGREGATE:
            return 'class'
        return self.kind

    @property
    def metric_kind(self):
        '''``'aggregate'`` for class records, ``'own'`` otherwise.'''
        return 'aggregate' if self.metric_level == CLASS_AGGREGATE else 'own'

    @property
    def is_aggregate(self):
        '''Whether :attr:`complexity` is a class-level aggregate.'''
        return self.metric_level == CLASS_AGGREGATE

    @property
    def aggregate_complexity(self):
        '''The class aggregate, or ``None`` for non-class records.'''
        return self.complexity if self.is_aggregate else None

    @property
    def own_complexity(self):
        '''The definition's own complexity.

        It is ``None`` for class records, whose value is an aggregate.
        '''
        return None if self.is_aggregate else self.complexity

    @property
    def is_class(self):
        '''Whether this record describes a class.'''
        return self.kind == 'class'

    @property
    def is_method(self):
        '''Whether this record describes a method.'''
        return self.kind == 'method'

    @property
    def is_function(self):
        '''Whether this record describes a function or closure.'''
        return self.kind == 'function'

    @property
    def classname(self):
        '''Qualified name of the containing class, or ``None``.'''
        if self.kind != 'method':
            return None
        return self.scope

    def as_dict(self):
        '''Return the record as a plain, JSON-serialisable dictionary.

        In addition to the named fields, the descriptive aliases and the
        derived properties are included, so the record can be written directly
        as a database row.
        '''
        result = dict(self._asdict())
        result.update(
            qualified_name=self.qualname,
            definition_order=self.order,
            definition_index=self.order,
            scope_name=self.scope,
            scope_id=self.parent_id,
            measurement_level=self.measurement_level,
            metric_kind=self.metric_kind,
            is_aggregate=self.is_aggregate,
            aggregate_complexity=self.aggregate_complexity,
            own_complexity=self.own_complexity,
            rank=self.rank,
            letter=self.letter,
        )
        return result

    def to_dict(self):
        '''Alias for :meth:`as_dict`.'''
        return self.as_dict()


class SymbolTable(
    collections.namedtuple(
        'SymbolTable', ['module_name', 'module_complexity', 'records']
    )
):
    '''A module's symbol records plus the information needed for totals.

    ``module_complexity`` is the complexity of the module body itself (the
    value Radon's :class:`~radon.visitors.ComplexityVisitor` exposes as
    ``complexity``).  It includes the module's own decision points and its
    baseline of one, but not the bodies of classes or functions.
    '''

    __slots__ = ()

    def __new__(cls, module_name, module_complexity, records):
        return super(SymbolTable, cls).__new__(
            cls, module_name, module_complexity, tuple(records)
        )

    def __iter__(self):
        return iter(self.records)

    def __len__(self):
        return len(self.records)

    def __getitem__(self, index):
        return self.records[index]

    @property
    def by_symbol_id(self):
        '''Map ``symbol_id`` to :class:`SymbolRecord`.'''
        return {record.symbol_id: record for record in self.records}

    def as_dicts(self):
        '''Return all records as plain dictionaries.'''
        return [record.as_dict() for record in self.records]

    def as_dict(self):
        '''Return the whole table as a JSON-serialisable dictionary.'''
        return {
            'module': self.module_name,
            'module_complexity': self.module_complexity,
            'total_complexity': self.total_complexity,
            'legacy_total_complexity': self.legacy_total_complexity,
            'nested_complexity': self.nested_complexity,
            'records': self.as_dicts(),
        }

    @property
    def nested_complexity(self):
        '''The part of :attr:`total_complexity` contributed by nested symbols.

        It is the difference between the total that includes every nesting
        level and the traditional Radon module total, which only accounts for
        top-level functions/classes and class methods.
        '''
        return self.total_complexity - self.legacy_total_complexity

    @property
    def legacy_total_complexity(self):
        '''The module total using Radon's traditional accounting.

        This is equivalent to
        :attr:`radon.visitors.ComplexityVisitor.total_complexity`: top-level
        functions contribute their decision points, while a top-level class
        contributes ``real_complexity - 1`` (which already contains its
        methods).  Nested functions and inner classes are not added here.
        '''
        total = self.module_complexity
        for record in self.records:
            if record.parent_id is not None:
                continue
            if record.kind == 'class':
                total += record.real_complexity - 1
            else:
                total += record.complexity - 1
        return total

    @property
    def total_complexity(self):
        '''The module total counting every definition exactly once.

        Top-level functions/classes are accounted for exactly like
        :attr:`legacy_total_complexity`.  Contributions which that total does
        not include - closures, inner classes and any other nested definition
        - are then added once at their own level.  Methods are not added again
        because they are already part of their class's ``real_complexity``.
        The result therefore never counts a nested definition twice.
        '''
        children = collections.defaultdict(list)
        for record in self.records:
            children[record.parent_id].append(record)

        total = self.module_complexity
        for record in children.get(None, ()):
            total += _contribution(record, children, include_self=True)
        return total


def _contribution(record, children, include_self):
    '''Return a record's contribution plus the contributions below it.

    ``include_self`` is false for methods: their own complexity is already
    contained in the parent class's ``real_complexity``, but their closures and
    inner classes are not.
    '''
    total = 0
    if include_self:
        if record.kind == 'class':
            total += record.real_complexity - 1
        else:
            total += record.complexity - 1

    for child in children.get(record.symbol_id, ()):
        if record.kind == 'class' and child.kind == 'method':
            total += _contribution(child, children, include_self=False)
        else:
            total += _contribution(child, children, include_self=True)
    return total


def _definition_nodes(node):
    '''Yield the definition nodes directly contained in *node*'s scope.

    A definition nested inside an ``if`` or ``try`` block still belongs to the
    enclosing definition's scope, so the whole subtree is searched but the walk
    stops at each definition found.
    '''
    for child in ast.iter_child_nodes(node):
        if isinstance(
            child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            yield child
        else:
            for definition in _definition_nodes(child):
                yield definition


def _own_body_complexity(node, no_assert=False):
    '''Decision points in *node*'s body, excluding nested definitions.'''
    visitor = ComplexityVisitor(off=False, no_assert=no_assert)
    for child in node.body:
        visitor.visit(child)
    # ``visitor.complexity`` excludes the complexity of nested functions and
    # classes, which is exactly the self contribution needed here.
    return visitor.complexity


def _endline(node):
    endline = getattr(node, 'end_lineno', None)
    if endline is None:  # pragma: no cover - Python without end_lineno
        endline = node.lineno
        for child in ast.walk(node):
            child_end = getattr(child, 'end_lineno', None)
            if child_end is not None:
                endline = max(endline, child_end)
            else:
                endline = max(endline, getattr(child, 'lineno', endline))
    return endline


def _class_complexity(real_complexity, method_count):
    '''Radon's ``Class.complexity`` aggregate from a raw total.'''
    if not method_count:
        return real_complexity
    return int(real_complexity / float(method_count)) + (method_count > 1)


class _Identities(object):
    '''Allocate per-scope ordering and module-unique symbol identifiers.'''

    def __init__(self):
        self.scope_orders = collections.Counter()
        self.name_occurrences = collections.Counter()
        self.used_ids = set()

    def allocate(self, parent, name):
        '''Return ``(scope, scope_order, occurrence, qualname, symbol_id)``.'''
        scope_key = MODULE_SCOPE if parent is None else parent.symbol_id
        scope = MODULE_SCOPE if parent is None else parent.qualname
        self.scope_orders[scope_key] += 1
        scope_order = self.scope_orders[scope_key]

        self.name_occurrences[(scope_key, name)] += 1
        occurrence = self.name_occurrences[(scope_key, name)]

        qualname = (
            '{0}.{1}'.format(parent.qualname, name)
            if parent is not None
            else name
        )
        symbol_id = '{0}#{1}'.format(qualname, occurrence)
        if symbol_id in self.used_ids and parent is not None:
            # Two same-named definitions in different scopes can share a
            # qualified name (for example methods of two classes both called
            # ``A``).  Anchor the identifier on the parent in that case.
            symbol_id = '{0}.{1}#{2}'.format(
                parent.symbol_id, name, occurrence
            )
        self.used_ids.add(symbol_id)
        return scope, scope_order, occurrence, qualname, symbol_id


def _build_from_ast(tree, module_name, module_complexity, no_assert=False):
    records = []
    identities = _Identities()

    def visit(node, parent):
        scope, scope_order, occurrence, qualname, symbol_id = (
            identities.allocate(parent, node.name)
        )

        if isinstance(node, ast.ClassDef):
            kind = 'class'
            metric_level = CLASS_AGGREGATE
        else:
            kind = (
                'method'
                if parent is not None and parent.kind == 'class'
                else 'function'
            )
            metric_level = METHOD_OWN if kind == 'method' else FUNCTION_OWN

        children = list(_definition_nodes(node))
        if kind == 'class':
            method_count = sum(
                1
                for child in children
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
            own = _own_body_complexity(node, no_assert)
            methods = 0
            for child in children:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods += _own_body_complexity(child, no_assert) + 1
            real_complexity = 1 + own + methods
            complexity = _class_complexity(real_complexity, method_count)
        else:
            real_complexity = None
            complexity = 1 + _own_body_complexity(node, no_assert)

        record = SymbolRecord(
            module_name,
            symbol_id,
            node.name,
            qualname,
            scope,
            kind,
            len(records) + 1,
            scope_order,
            occurrence,
            node.lineno,
            node.col_offset,
            _endline(node),
            complexity,
            real_complexity,
            metric_level,
            None if parent is None else parent.symbol_id,
            0 if parent is None else parent.depth + 1,
        )
        records.append(record)
        for child in sorted(
            children, key=lambda child: (child.lineno, child.col_offset)
        ):
            visit(child, record)

    top_nodes = sorted(
        _definition_nodes(tree),
        key=lambda node: (node.lineno, node.col_offset),
    )
    for node in top_nodes:
        visit(node, None)

    return records


def _is_method(block):
    return isinstance(block, Function) and block.is_method


def _build_from_blocks(blocks, module_name, module_complexity):
    records = []
    identities = _Identities()

    def visit(block, parent, kind):
        scope, scope_order, occurrence, qualname, symbol_id = (
            identities.allocate(parent, block.name)
        )
        if kind == 'class':
            metric_level = block.metric_level
            real_complexity = block.real_complexity
        else:
            metric_level = block.metric_level
            real_complexity = None

        record = SymbolRecord(
            module_name,
            symbol_id,
            block.name,
            qualname,
            scope,
            kind,
            len(records) + 1,
            scope_order,
            occurrence,
            block.lineno,
            block.col_offset,
            block.endline,
            block.complexity,
            real_complexity,
            metric_level,
            None if parent is None else parent.symbol_id,
            0 if parent is None else parent.depth + 1,
        )
        records.append(record)

        if kind == 'class':
            children = list(block.methods) + list(block.inner_classes)
        else:
            children = list(block.closures)
        for child in sorted(
            children, key=lambda child: (child.lineno, child.col_offset)
        ):
            if isinstance(child, Class):
                child_kind = 'class'
            elif child.is_method:
                child_kind = 'method'
            else:
                child_kind = 'function'
            visit(child, record, child_kind)

    for block in sorted(
        blocks, key=lambda block: (block.lineno, block.col_offset)
    ):
        visit(
            block,
            None,
            'class' if isinstance(block, Class) else 'function',
        )
    return records


def symbols_from_ast(ast_node, module_name='<module>', **kwargs):
    '''Build a :class:`SymbolTable` from an AST node.

    Extra keyword arguments are passed to
    :class:`~radon.visitors.ComplexityVisitor` when computing the module
    complexity and every record (for example ``no_assert=True``).
    '''
    no_assert = kwargs.get('no_assert', False)
    visitor = ComplexityVisitor.from_ast(ast_node, **kwargs)
    return SymbolTable(
        module_name,
        visitor.complexity,
        _build_from_ast(
            ast_node, module_name, visitor.complexity, no_assert
        ),
    )


def symbols_from_code(code, module_name='<module>', **kwargs):
    '''Build a :class:`SymbolTable` from source code.'''
    return symbols_from_ast(ast.parse(code), module_name, **kwargs)


def symbols_from_tree(tree, visitor, module_name='<module>'):
    '''Build a :class:`SymbolTable` from a parsed tree and its visitor.

    ``tree`` is the AST already parsed by the caller and ``visitor`` is a
    :class:`~radon.visitors.ComplexityVisitor` that has already visited it.
    This avoids parsing the source twice when the caller needs both APIs.
    '''
    return SymbolTable(
        module_name,
        visitor.complexity,
        _build_from_ast(
            tree,
            module_name,
            visitor.complexity,
            getattr(visitor, 'no_assert', False),
        ),
    )


def symbols_from_visitor(visitor, module_name='<module>'):
    '''Build a :class:`SymbolTable` from a ``ComplexityVisitor`` instance.

    If the visitor was created with ``from_ast``, it keeps the AST and nested
    definitions are extracted from it.  Otherwise this falls back to the
    visitor's block tree, which has the same records for the definitions Radon
    tracks; definitions nested inside functions are then unavailable.
    '''
    if getattr(visitor, 'ast_node', None) is not None:
        return symbols_from_tree(visitor.ast_node, visitor, module_name)
    blocks = sorted(
        list(visitor.functions) + list(visitor.classes),
        key=lambda block: (block.lineno, block.col_offset),
    )
    return SymbolTable(
        module_name,
        visitor.complexity,
        _build_from_blocks(blocks, module_name, visitor.complexity),
    )


def symbols_from_blocks(blocks, module_name='<module>', module_complexity=1):
    '''Build a :class:`SymbolTable` from a traditional Radon block list.

    ``blocks`` must be the result of :func:`radon.complexity.cc_visit` or
    ``ComplexityVisitor.blocks``.  That list contains methods as separate
    entries next to their class; they are ignored here because the class block
    already carries them.  Passing a list expanded with ``add_inner_blocks``
    would duplicate records and is not supported.

    ``module_complexity`` is the module body's own complexity.  The block list
    does not carry that value, so callers that have the visitor at hand should
    use :func:`symbols_from_visitor` instead.
    '''
    top_level = [block for block in blocks if not _is_method(block)]
    return SymbolTable(
        module_name,
        module_complexity,
        _build_from_blocks(top_level, module_name, module_complexity),
    )


def symbol_table(source, module_name='<module>', **kwargs):
    '''Return a :class:`SymbolTable` for code, an AST, blocks or a visitor.

    ``source`` can be source code (``str``), an AST node, a list of Radon
    blocks, or a :class:`~radon.visitors.ComplexityVisitor` instance.
    '''
    if isinstance(source, str):
        return symbols_from_code(source, module_name, **kwargs)
    if isinstance(source, ast.AST):
        return symbols_from_ast(source, module_name, **kwargs)
    if isinstance(source, (list, tuple)):
        module_complexity = kwargs.pop('module_complexity', 1)
        return symbols_from_blocks(source, module_name, module_complexity)
    if isinstance(source, ComplexityVisitor):
        return symbols_from_visitor(source, module_name)
    raise TypeError(
        'source must be source code, an AST node, a block list or a '
        'ComplexityVisitor'
    )


def symbol_records(source, module_name='<module>', **kwargs):
    '''Return the source-ordered :class:`SymbolRecord` list for *source*.

    *source* accepts the same inputs as :func:`symbol_table`.
    '''
    return list(symbol_table(source, module_name, **kwargs).records)


#: Short alias for :class:`SymbolRecord`.
Symbol = SymbolRecord
#: Alias for :func:`symbol_table`.
build_symbol_table = symbol_table
#: Alias for :func:`symbol_records`.
collect_symbols = symbol_records
