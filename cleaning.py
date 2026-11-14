"""
CleaningEngine
--------------

A small row-level data cleaning / feature engineering engine built on top of
:mod:`simpleeval`.

It adds four things on top of plain expression evaluation:

1. Rules may reference previously (or later) defined derived columns - they are
   evaluated lazily and cached per record.
2. Every derived column of every record records its *actual* lineage: which
   source / derived columns were really read (short-circuited branches do not
   count), which rule version produced it, and which lookup tables (with
   versions) were really read.
3. Incremental recomputation: when inputs, rules or tables change, only the
   records and columns that can actually be affected are recomputed.
4. Circular references are only an error when the cycle goes through a branch
   that is actually entered for the current record.

Branch handling
~~~~~~~~~~~~~~~

Python's conditional expression ``body if test else orelse`` only evaluates
one of its two branches.  Runtime short-circuiting is what makes the recorded
lineage precise (columns only referenced by the untaken branch are not read),
but static dependency analysis still needs to see both branches.

Every :class:`ast.IfExp` node is therefore rewritten into a synthetic call::

    _branch(test, body, orelse)

:class:`TrackingEvaluator` intercepts that call: it evaluates ``test`` and
then evaluates only the selected branch.  Static reference collection simply
walks the rewritten tree, naturally seeing every possible branch.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from simpleeval import (
    DEFAULT_FUNCTIONS,
    DEFAULT_NAMES,
    EvalWithCompoundTypes,
    InvalidExpression,
    NameNotDefined,
)


class CircularReferenceError(InvalidExpression):
    """Raised when derived columns reference each other in a cycle through
    branches that are actually entered for a given record."""

    def __init__(self, column: str, chain: Iterable[str]):
        self.column = column
        self.chain = list(chain)
        super().__init__(
            f"Circular reference detected at column '{column}': "
            + " -> ".join(self.chain)
        )


class RuleEvaluationError(InvalidExpression):
    """Wraps the original exception raised while evaluating a rule."""

    def __init__(self, column: str, original: BaseException):
        self.column = column
        self.original = original
        super().__init__(f"Error evaluating rule '{column}': {original!r}")


_MISSING = object()


@dataclass(frozen=True)
class ColumnLineage:
    """How one derived column of one record was actually produced."""

    dependencies: frozenset[str]
    rule_version: int
    tables: frozenset[tuple[str, int]]
    expression: str


@dataclass
class _Rule:
    name: str
    expression: str
    version: int
    parsed: ast.AST


@dataclass
class _RecordState:
    source_row: dict[str, Any]
    values: dict[str, Any] = field(default_factory=dict)
    done: set[str] = field(default_factory=set)
    lineage: dict[str, ColumnLineage] = field(default_factory=dict)
    errors: dict[str, RuleEvaluationError] = field(default_factory=dict)
    failed: set[str] = field(default_factory=set)


class _IfExpRewriter(ast.NodeTransformer):
    """Rewrite ``b if t else e`` into a synthetic ``_branch(t, b, e)`` call.

    :class:`TrackingEvaluator` implements ``_branch`` with proper
    short-circuit semantics.  Nested conditionals are rewritten bottom-up.
    """

    def visit_IfExp(self, node: ast.IfExp):
        self.generic_visit(node)
        call = ast.Call(
            func=ast.Name(id="_branch", ctx=ast.Load()),
            args=[node.test, node.body, node.orelse],
            keywords=[],
        )
        return ast.copy_location(call, node)


class TrackingEvaluator(EvalWithCompoundTypes):
    """Evaluator that records every name and table actually read.

    Names are resolved against the engine, which lazily evaluates derived
    columns, so reading a derived column both records the dependency and
    triggers (or reuses) that column's cached value.
    """

    def __init__(self, engine: CleaningEngine, state: _RecordState,
                 values: dict[str, Any]):
        functions = {**DEFAULT_FUNCTIONS, **engine._functions}
        super().__init__(functions=functions)
        self._engine = engine
        self._state = state
        self._values = values
        self._stack: list[str] = []
        self.accessed: set[str] = set()
        self.tables_read: set[tuple[str, int]] = set()
        self.names = self._resolve_name
        self.nodes[ast.Call] = self._eval_call

    def _resolve_name(self, node: ast.Name):
        name = node.id
        if name in self._engine._rules:
            self._engine._ensure_evaluated(name, self)
            if name in self._state.failed:
                raise self._state.errors[name]
            self.accessed.add(name)
            return self._values[name]
        if name in self._state.source_row:
            self.accessed.add(name)
            return self._state.source_row[name]
        if name in self._values:
            self.accessed.add(name)
            return self._values[name]
        if name in DEFAULT_NAMES:
            return DEFAULT_NAMES[name]
        raise NameNotDefined(name, self.expr)

    def _branch(self, test_value, body_node: ast.AST, orelse_node: ast.AST):
        chosen = body_node if test_value else orelse_node
        return self._eval(chosen)

    def _eval_call(self, node: ast.Call):
        # Intercept the synthetic _branch(...) helper while leaving every
        # other call to the standard simpleeval implementation.
        if isinstance(node.func, ast.Name) and node.func.id == "_branch":
            test_value = self._eval(node.args[0])
            return self._branch(test_value, node.args[1], node.args[2])
        return super()._eval_call(node)


def _collect_static_refs(tree: ast.AST, reserved: set[str]) -> frozenset[str]:
    """All names appearing anywhere in the expression, both branches included."""

    refs = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in reserved:
            refs.add(node.id)
    return frozenset(refs)


class CleaningEngine:
    """Rule based cleaning engine with per-record lineage and incremental runs.

    Example:
        >>> engine = CleaningEngine()
        >>> engine.add_rule("double", "value * 2")
        >>> outputs = engine.run([{"id": 1, "value": 21}], key="id")
        >>> outputs[1]["double"]
        42
    """

    def __init__(self):
        self._rules: dict[str, _Rule] = {}
        self._rule_order: list[str] = []
        self._tables: dict[str, dict[str, Any]] = {}
        self._functions: dict[str, Callable] = {}
        self._states: dict[Any, _RecordState] = {}
        self._active_evaluator: TrackingEvaluator | None = None

    # ------------------------------------------------------------------ setup

    def add_rule(self, name: str, expression: str, version: int = 1) -> None:
        """Register (or replace) a derived-column rule.

        Replacing a rule through :meth:`update_rule` bumps its version and
        triggers incremental recomputation; this method is for initial setup.
        """

        if name in self._rules:
            raise ValueError(f"Rule '{name}' already registered")
        self._compile_rule(name, expression, version)
        self._rule_order.append(name)

    def add_table(self, label: str, provider: dict | Callable, version: int = 1) -> None:
        """Register a lookup table.

        ``provider`` is either a dict (looked up by key) or a callable.  A
        function named after the label becomes available inside expressions;
        every call records the table label and its current version on the
        lineage of the column being evaluated.
        """

        if label in self._tables:
            raise ValueError(f"Table '{label}' already registered")
        self._tables[label] = {"provider": provider, "version": version}
        self._functions[label] = self._make_table_reader(label)

    def add_function(self, name: str, func: Callable) -> None:
        """Register a plain (non-tracked) helper function for expressions."""

        self._functions[name] = func

    def _make_table_reader(self, label: str) -> Callable:
        def reader(lookup: Any = _MISSING) -> Any:
            spec = self._tables[label]
            active = self._active_evaluator
            if active is not None:
                active.tables_read.add((label, spec["version"]))
            provider = spec["provider"]
            if isinstance(provider, dict):
                return provider[lookup]
            if lookup is _MISSING:
                return provider()
            return provider(lookup)

        return reader

    def _compile_rule(self, name: str, expression: str, version: int) -> None:
        parsed = ast.parse(expression.strip(), mode="eval")
        parsed = _IfExpRewriter().visit(parsed)
        ast.fix_missing_locations(parsed)
        rule = _Rule(
            name=name,
            expression=expression,
            version=version,
            parsed=parsed.body,
        )
        self._rules[name] = rule

    def _static_refs(self, rule: _Rule) -> frozenset[str]:
        """Names referenced anywhere in the rule, including untaken branches.

        Computed on demand so that tables / functions registered after the
        rule are always treated as reserved names.
        """

        reserved = set(self._functions) | set(DEFAULT_FUNCTIONS) | set(DEFAULT_NAMES)
        reserved.add("_branch")
        return _collect_static_refs(rule.parsed, reserved)

    # -------------------------------------------------------------------- run

    def run(self, rows: Iterable[dict], key: str = "id") -> dict[Any, dict]:
        """Evaluate every rule for every row and return output rows by key."""

        self._states.clear()
        results = {}
        for row in rows:
            record_key = row[key]
            state = _RecordState(source_row=dict(row))
            self._states[record_key] = state
            self._evaluate_all(state)
            results[record_key] = self._output(state)
        return results

    def _evaluate_all(self, state: _RecordState) -> None:
        evaluator = TrackingEvaluator(self, state, state.values)
        self._active_evaluator = evaluator
        try:
            for name in self._rule_order:
                try:
                    self._ensure_evaluated(name, evaluator)
                except (RuleEvaluationError, CircularReferenceError):
                    # Recorded on the state; unrelated rules still run.
                    pass
        finally:
            self._active_evaluator = None

    def _ensure_evaluated(self, name: str, evaluator: TrackingEvaluator) -> None:
        state = evaluator._state
        if name in state.done or name in state.failed:
            return
        if name in evaluator._stack:
            start = evaluator._stack.index(name)
            raise CircularReferenceError(name, evaluator._stack[start:] + [name])

        rule = self._rules[name]
        evaluator._stack.append(name)
        saved_accessed = evaluator.accessed
        saved_tables = evaluator.tables_read
        evaluator.accessed = set()
        evaluator.tables_read = set()
        try:
            value = evaluator.eval(rule.expression, previously_parsed=rule.parsed)
        except (RuleEvaluationError, CircularReferenceError) as error:
            state.failed.add(name)
            state.errors[name] = error
            raise
        except Exception as original:  # noqa: BLE001 - wrapped with rule context
            error = RuleEvaluationError(name, original)
            state.errors[name] = error
            state.failed.add(name)
            raise error
        finally:
            own_dependencies = frozenset(evaluator.accessed)
            own_tables = frozenset(evaluator.tables_read)
            evaluator.accessed = saved_accessed
            evaluator.tables_read = saved_tables
            evaluator._stack.pop()

        state.values[name] = value
        state.done.add(name)
        state.failed.discard(name)
        state.errors.pop(name, None)
        state.lineage[name] = ColumnLineage(
            dependencies=own_dependencies,
            rule_version=rule.version,
            tables=own_tables,
            expression=rule.expression,
        )

    def _output(self, state: _RecordState) -> dict:
        output = dict(state.source_row)
        for name in self._rule_order:
            if name in state.done:
                output[name] = state.values[name]
        return output

    # ------------------------------------------------------------- incremental

    def update_inputs(self, changes: dict[Any, dict[str, Any]]) -> dict[Any, dict]:
        """Apply source value changes and recompute only affected records.

        ``changes`` maps a record key to the columns that changed on that
        record.  A column is forced to recompute when it either historically
        read a changed column *or* its expression statically references one
        (the latter covers the case where membership just changed and the old
        execution path never read the membership column).
        """

        results = {}
        for record_key, row_changes in changes.items():
            changed = set(row_changes)
            state = self._states[record_key]
            state.source_row.update(row_changes)
            forced = set(row_changes)
            for rule in self._rules.values():
                if self._static_refs(rule) & changed:
                    forced.add(rule.name)
            self._recompute(state, forced)
            results[record_key] = self._output(state)
        return results

    def update_rule(
        self,
        name: str,
        expression: str | None = None,
        version: int | None = None,
    ) -> dict[Any, dict]:
        """Change a rule expression and/or its version, then recompute users."""

        existing = self._rules[name]
        new_version = existing.version + 1 if version is None else version
        new_expression = existing.expression if expression is None else expression
        self._compile_rule(name, new_expression, new_version)

        results = {}
        for record_key, state in self._states.items():
            self._recompute(state, {name})
            results[record_key] = self._output(state)
        return results

    def update_table(
        self,
        label: str,
        provider: dict | Callable | None = None,
        version: int | None = None,
    ) -> dict[Any, dict]:
        """Change a lookup table and recompute records that actually read it."""

        spec = self._tables[label]
        if provider is not None:
            spec["provider"] = provider
        if version is not None:
            spec["version"] = version
        elif provider is not None:
            spec["version"] += 1

        results = {}
        for record_key, state in self._states.items():
            forced = {
                column
                for column, lineage in state.lineage.items()
                if any(table == label for table, _version in lineage.tables)
            }
            # A failed column recorded no lineage; retry it as well in case
            # the failure came from this table (e.g. a missing lookup key).
            forced |= set(state.failed)
            if forced:
                self._recompute(state, forced)
            results[record_key] = self._output(state)
        return results

    def _recompute(self, state: _RecordState, seeds: set[str]) -> None:
        affected = set(seeds)
        while True:
            grew = False
            for column, lineage in state.lineage.items():
                if column not in affected and lineage.dependencies & affected:
                    affected.add(column)
                    grew = True
            if not grew:
                break

        # Failed columns never recorded lineage and hold no cached value, so
        # the closure above cannot see them.  Retry every failed column to
        # match a from-scratch run and let recovered upstreams unblock them.
        affected |= set(state.failed)

        for column in affected:
            state.done.discard(column)
            state.failed.discard(column)
            state.values.pop(column, None)
            state.lineage.pop(column, None)
            state.errors.pop(column, None)

        evaluator = TrackingEvaluator(self, state, state.values)
        self._active_evaluator = evaluator
        try:
            for column in self._rule_order:
                if column in affected:
                    try:
                        self._ensure_evaluated(column, evaluator)
                    except (RuleEvaluationError, CircularReferenceError):
                        pass
        finally:
            self._active_evaluator = None

    # -------------------------------------------------------------- inspection

    def lineage(self, key: Any) -> dict[str, ColumnLineage]:
        """Return the per-derived-column lineage recorded for one record."""

        return dict(self._states[key].lineage)

    def errors(self, key: Any) -> dict[str, RuleEvaluationError]:
        """Return the rule evaluation errors recorded for one record."""

        return dict(self._states[key].errors)
