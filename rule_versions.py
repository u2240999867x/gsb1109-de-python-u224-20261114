"""
Versioned, atomically published rule sets on top of :mod:`simpleeval`.

A night-batch cleaning pipeline switches wholesale from one set of derived
expressions to the next while several worker processes pull orders at the same
time.  This module gives that configuration a publication lifecycle:

* the draft rule set is edited freely and then *published as one atomic unit*;
* publication is refused when a rule references another rule that is missing,
  when an expression does not parse, or when rules reference each other in a
  cycle, so every published version is complete and evaluable on its own;
* an order is evaluated against a single, pinned published version from start
  to finish - workers that start before a switch keep using the old version,
  workers starting afterwards use the new one;
* every published version is immutable and carries a serialisable, checksummed
  snapshot, so past orders can be replayed against the exact original
  configuration and every result can be traced back to a restorable config.

The module only depends on :mod:`simpleeval` and the standard library and is
safe to share between threads.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import keyword
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping

from simpleeval import (
    DEFAULT_FUNCTIONS,
    DEFAULT_NAMES,
    InvalidExpression,
    NameNotDefined,
    SimpleEval,
)

SNAPSHOT_FORMAT = "simpleeval-ruleset/1"


########################################
# Exceptions:


class RuleSetError(Exception):
    """Base class for all rule set lifecycle errors."""


class InvalidRuleSet(RuleSetError):
    """The draft rule set cannot be published in its current state."""


class EmptyRuleSet(InvalidRuleSet):
    """Publishing an empty rule set is not allowed."""


class DuplicateRule(InvalidRuleSet):
    """The same rule name was supplied more than once in one bulk edit."""

    def __init__(self, name):
        self.name = name
        super().__init__(f"Duplicate rule name '{name}'.")


class MissingRuleReference(InvalidRuleSet):
    """A rule refers to another rule that is not part of the draft set."""

    def __init__(self, missing, references):
        self.missing = frozenset(missing)
        self.references = dict(references)
        super().__init__(
            f"Cannot publish: referenced rule(s) missing: {sorted(self.missing)}."
        )


class CyclicRuleReference(InvalidRuleSet):
    """Rules reference each other in a cycle and could never terminate."""

    def __init__(self, cycle):
        self.cycle = tuple(cycle)
        super().__init__(f"Cyclic rule reference detected: {' -> '.join(self.cycle)}.")


class VersionNotFound(RuleSetError):
    """A requested published version does not exist."""

    def __init__(self, version):
        self.version = version
        super().__init__(f"Published version {version!r} does not exist.")


########################################
# Immutable value objects:


@dataclass(frozen=True)
class Rule:
    """One named derived expression in a rule set."""

    name: str
    expression: str
    description: str = ""

    def to_dict(self):
        return {"name": self.name, "expression": self.expression, "description": self.description}

    @classmethod
    def from_dict(cls, data):
        return cls(
            name=data["name"],
            expression=data["expression"],
            description=data.get("description", ""),
        )


@dataclass(frozen=True)
class RuleSetVersion:
    """An immutable, published snapshot of a whole rule set.

    ``rules``, ``parsed`` and ``functions`` are exposed as read-only mappings;
    nothing about a published version can ever mutate, so evaluations of old
    orders stay reproducible forever.
    """

    version: int
    rules: Mapping[str, Rule]
    created_at: datetime
    description: str
    checksum: str
    parsed: Mapping[str, ast.AST]
    functions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "rules", MappingProxyType(dict(self.rules)))
        object.__setattr__(self, "parsed", MappingProxyType(dict(self.parsed)))
        object.__setattr__(self, "functions", MappingProxyType(dict(self.functions)))

    @property
    def rule_names(self):
        return tuple(self.rules)

    def to_dict(self):
        """Serialise to plain JSON-friendly data (functions excluded)."""
        return {
            "format": SNAPSHOT_FORMAT,
            "version": self.version,
            "description": self.description,
            "created_at": self.created_at.isoformat(),
            "checksum": self.checksum,
            "rules": [self.rules[name].to_dict() for name in sorted(self.rules)],
        }

    def snapshot(self):
        """Return a deep, JSON-serialisable configuration snapshot."""
        return copy.deepcopy(self.to_dict())

    @classmethod
    def from_dict(cls, data, functions=None):
        """Rebuild a version from :meth:`to_dict` output.

        The embedded checksum is recomputed and verified so a tampered or
        truncated snapshot cannot silently be replayed.
        """
        if data.get("format") != SNAPSHOT_FORMAT:
            raise InvalidRuleSet(f"Unknown snapshot format: {data.get('format')!r}.")
        rules = [Rule.from_dict(item) for item in data["rules"]]
        rule_map = {rule.name: rule for rule in rules}
        checksum = compute_checksum(rule_map)
        if data.get("checksum") and data["checksum"] != checksum:
            raise InvalidRuleSet(
                f"Checksum mismatch for version {data.get('version')}: "
                "the snapshot does not match its recorded configuration."
            )
        parsed = {name: SimpleEval.parse(rule.expression) for name, rule in rule_map.items()}
        return cls(
            version=int(data["version"]),
            rules=rule_map,
            created_at=datetime.fromisoformat(data["created_at"]),
            description=data.get("description", ""),
            checksum=checksum,
            parsed=parsed,
            functions=dict(functions or {}),
        )


@dataclass(frozen=True)
class OrderEvaluation:
    """The outcome of evaluating one order against one pinned version."""

    order_id: Any
    version: int
    target: Any
    result: Any
    results: Mapping[str, Any]
    configuration: Mapping[str, Any]

    @property
    def checksum(self):
        return self.configuration["checksum"]


########################################
# Helpers:


def compute_checksum(rules):
    """Stable SHA-256 over a rule mapping, independent of dict ordering."""
    payload = json.dumps(
        {name: rule.expression for name, rule in sorted(rules.items())},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def extract_references(expression):
    """Return the set of rule names an expression refers to.

    Bare names used as call targets are treated as function calls, not rule
    references; everything else is a reference to another rule or to an
    external order field.
    """
    tree = ast.parse(expression.strip())
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    bound = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load)
    }
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id not in called
        and node.id not in bound
    }


def _as_rule(name, rule_or_expression):
    if isinstance(rule_or_expression, Rule):
        if rule_or_expression.name != name:
            raise InvalidRuleSet(
                f"Rule name mismatch: got key '{name}', rule is named '{rule_or_expression.name}'."
            )
        return rule_or_expression
    return Rule(name=name, expression=str(rule_or_expression))


class _OrderResolver:
    """Callable ``names`` resolver for one order pinned to one version.

    A fresh resolver (and therefore a fresh memoisation cache and dependency
    stack) is created per order, so diamond references are evaluated once with
    a consistent value while different orders never share state.
    """

    def __init__(self, version, context, functions, evaluator_class=SimpleEval):
        self._version = version
        self._context = context
        self._functions = functions
        self._evaluator_class = evaluator_class
        self._memo = {}
        self._stack = set()

    @property
    def memo(self):
        return self._memo

    def __call__(self, node):
        name = node.id
        if name in self._version.rules:
            return self.resolve_rule(name)
        if name in self._context:
            return self._context[name]
        if name in DEFAULT_NAMES:
            return DEFAULT_NAMES[name]
        raise NameNotDefined(name, f"order context for version {self._version.version}")

    def resolve_rule(self, name):
        if name in self._memo:
            return self._memo[name]
        if name in self._stack:
            chain = " -> ".join([*sorted(self._stack), name])
            raise CyclicRuleReference(chain)
        rule = self._version.rules[name]
        self._stack.add(name)
        try:
            evaluator = self._evaluator_class(functions=dict(self._functions), names=self)
            value = evaluator.eval(
                rule.expression, previously_parsed=self._version.parsed[name]
            )
        finally:
            self._stack.discard(name)
        self._memo[name] = value
        return value


########################################
# The rule set itself:


class RuleSet:
    """A draft rule set plus the immutable history of its published versions.

    All methods are guarded by a re-entrant lock: worker threads may evaluate
    orders while another thread publishes a new version, and every evaluation
    is guaranteed to run against a single complete version.
    """

    def __init__(self, rules=None, *, functions=None, externals=None, evaluator_class=SimpleEval):
        self._lock = threading.RLock()
        self._draft = {}
        self._versions = []
        self._current_version = None
        self._functions = dict(DEFAULT_FUNCTIONS)
        if functions:
            self._functions.update(functions)
        self._externals = set(externals or ())
        self._evaluator_class = evaluator_class
        if rules is not None:
            self.replace_rules(rules)

    # ----- draft editing ----------------------------------------------

    @property
    def current_version(self):
        with self._lock:
            return self._current_version

    @property
    def draft(self):
        with self._lock:
            return MappingProxyType(dict(self._draft))

    @property
    def externals(self):
        with self._lock:
            return frozenset(self._externals)

    def set_externals(self, names):
        """Declare order-context names that rules may reference."""
        with self._lock:
            self._externals = set(names)

    def set_rule(self, name, rule_or_expression, description=""):
        """Add or overwrite one rule in the draft."""
        with self._lock:
            rule = _as_rule(name, rule_or_expression)
            if description and not rule.description:
                rule = Rule(rule.name, rule.expression, description)
            self._draft[name] = rule

    def remove_rule(self, name):
        with self._lock:
            self._draft.pop(name, None)

    def replace_rules(self, rules):
        """Replace the whole draft in one go.

        Accepts a ``{name: expression_or_Rule}`` mapping or an iterable of
        :class:`Rule` objects.
        """
        with self._lock:
            if isinstance(rules, Mapping):
                new_draft = {name: _as_rule(name, rule) for name, rule in rules.items()}
            else:
                new_draft = {}
                for rule in rules:
                    if not isinstance(rule, Rule):
                        raise InvalidRuleSet(f"Expected a Rule, got {type(rule)!r}.")
                    if rule.name in new_draft:
                        raise DuplicateRule(rule.name)
                    new_draft[rule.name] = rule
            self._draft = new_draft

    def clear_draft(self):
        with self._lock:
            self._draft = {}

    # ----- validation --------------------------------------------------

    def validate(self, rules=None):
        """Validate a rule mapping (defaults to the draft).

        Returns the resolved rule mapping, or raises :class:`InvalidRuleSet`.
        """
        with self._lock:
            rule_map = dict(self._draft if rules is None else rules)
            if not rule_map:
                raise EmptyRuleSet("Cannot publish an empty rule set.")

            references = {}
            for name, rule in rule_map.items():
                if not name.isidentifier() or keyword.iskeyword(name):
                    raise InvalidRuleSet(f"'{name}' is not a valid rule name.")
                try:
                    SimpleEval.parse(rule.expression)
                except SyntaxError as exc:
                    raise InvalidRuleSet(
                        f"Rule '{name}' has an unparseable expression: {exc.msg}"
                    ) from exc
                refs = extract_references(rule.expression)
                references[name] = refs

            missing = {}
            for name, refs in references.items():
                unresolved = refs - set(rule_map) - self._externals
                if unresolved:
                    missing[name] = unresolved
            if missing:
                all_missing = set().union(*missing.values())
                raise MissingRuleReference(all_missing, references)

            self._check_cycles(references)
            return MappingProxyType(dict(rule_map))

    @staticmethod
    def _check_cycles(references):
        visited = set()
        stack = []
        stack_set = set()

        def visit(name):
            if name in stack_set:
                cycle = stack[stack.index(name):] + [name]
                raise CyclicRuleReference(cycle)
            if name in visited:
                return
            visited.add(name)
            stack.append(name)
            stack_set.add(name)
            for referenced in sorted(references.get(name, ())):
                if referenced in references:
                    visit(referenced)
            stack.pop()
            stack_set.discard(name)

        for rule_name in sorted(references):
            visit(rule_name)

    # ----- publication -------------------------------------------------

    def publish(self, description=""):
        """Atomically validate and publish the draft as a new version.

        Returns the new version number.  The draft is copied and frozen, so
        later draft edits never touch already published versions.
        """
        with self._lock:
            rules = dict(self.validate())
            parsed = {name: SimpleEval.parse(rule.expression) for name, rule in rules.items()}
            version_number = len(self._versions) + 1
            published = RuleSetVersion(
                version=version_number,
                rules=copy.deepcopy(rules),
                created_at=datetime.now(timezone.utc),
                description=description,
                checksum=compute_checksum(rules),
                parsed=parsed,
                functions=copy.deepcopy(self._functions),
            )
            self._versions.append(published)
            self._current_version = version_number
            return version_number

    # ----- version reads / replay -------------------------------------

    def versions(self):
        with self._lock:
            return tuple(version.version for version in self._versions)

    def get_version(self, version=None):
        """Return the :class:`RuleSetVersion` for ``version`` (default current)."""
        with self._lock:
            if version is None:
                if self._current_version is None:
                    raise VersionNotFound(None)
                version = self._current_version
            for published in self._versions:
                if published.version == version:
                    return published
            raise VersionNotFound(version)

    def snapshot(self, version=None):
        """JSON-serialisable, restorable snapshot of a published version."""
        return self.get_version(version).snapshot()

    def restore(self, snapshot):
        """Load a version snapshot back into the draft (without publishing)."""
        restored = RuleSetVersion.from_dict(snapshot)
        with self._lock:
            self._draft = {name: copy.deepcopy(rule) for name, rule in restored.rules.items()}
        return restored.version

    @classmethod
    def from_snapshot(
        cls,
        snapshot,
        *,
        functions=None,
        externals=None,
        publish=False,
        evaluator_class=SimpleEval,
    ):
        """Rebuild a :class:`RuleSet` from a version snapshot."""
        restored = RuleSetVersion.from_dict(snapshot, functions=functions)
        rule_set = cls(
            functions=functions, externals=externals, evaluator_class=evaluator_class
        )
        rule_set._draft = {  # noqa: SLF001 - construction from trusted snapshot
            name: copy.deepcopy(rule) for name, rule in restored.rules.items()
        }
        if publish:
            rule_set._functions = dict(restored.functions)  # noqa: SLF001
            with rule_set._lock:  # noqa: SLF001
                rule_set._versions = [restored]  # noqa: SLF001
                rule_set._current_version = restored.version  # noqa: SLF001
        return rule_set

    # ----- evaluation --------------------------------------------------

    def _resolver_for(self, version, context):
        return _OrderResolver(
            version, context, version.functions, evaluator_class=self._evaluator_class
        )

    def evaluate(self, rule_name, names=None, *, version=None):
        """Evaluate one rule of a published version against order data."""
        published = self.get_version(version)
        if rule_name not in published.rules:
            raise InvalidRuleSet(
                f"Rule '{rule_name}' does not exist in version {published.version}."
            )
        resolver = self._resolver_for(published, names)
        return resolver.resolve_rule(rule_name)

    def evaluate_order(self, order, target=None, *, version=None, order_id=None):
        """Evaluate an order against one pinned published version.

        ``order`` is a mapping of order fields used as external names.  When
        ``target`` is given only that rule's value is computed; otherwise every
        rule in the version is evaluated.  The returned :class:`OrderEvaluation`
        carries the full configuration snapshot, so each result can always be
        traced back to a complete, restorable config.
        """
        published = self.get_version(version)
        if order_id is None and isinstance(order, Mapping):
            order_id = order.get("order_id")
        resolver = self._resolver_for(published, order)

        if target is None:
            for name in published.rules:
                resolver.resolve_rule(name)
            result = None
        else:
            if target not in published.rules:
                raise InvalidRuleSet(
                    f"Rule '{target}' does not exist in version {published.version}."
                )
            result = resolver.resolve_rule(target)

        return OrderEvaluation(
            order_id=order_id,
            version=published.version,
            target=target,
            result=result,
            results=MappingProxyType(dict(resolver.memo)),
            configuration=MappingProxyType(published.snapshot()),
        )
