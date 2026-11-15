"""
SimpleEval - (C) 2013-2026 Daniel Fairhead
-------------------------------------

A short, easy to use, safe and reasonably extensible expression evaluator.
Designed for things like in a website where you want to allow the user to
generate a string, or a number from some other input, without allowing full
eval() or other unsafe or needlessly complex linguistics.

-------------------------------------

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.

-------------------------------------

Initial idea copied from J.F. Sebastian on Stack Overflow
( http://stackoverflow.com/a/9558001/1973500 ) with
modifications and many improvements.

-------------------------------------
Contributors:
- corro (Robin Baumgartner) (py3k)
- dratchkov (David R) (nested dicts)
- marky1991 (Mark Young) (slicing)
- T045T (Nils Berg) (!=, py3kstr, obj.
- perkinslr (Logan Perkins) (.__globals__ or .func_ breakouts)
- impala2 (Kirill Stepanov) (massive _eval refactor)
- gk (ugik) (Other iterables than str can DOS too, and can be made)
- daveisfera (Dave Johansen) 'not' Boolean op, Pycharm, pep8, various other fixes
- xaled (Khalid Grandi) method chaining correctly, double-eval bugfix.
- EdwardBetts (Edward Betts) spelling correction.
- charlax (Charles-Axel Dein charlax) Makefile and cleanups
- mommothazaz123 (Andrew Zhu) f"string" support, Python 3.8 support
- lubieowoce (Uryga) various potential vulnerabilities
- JCavallo (Jean Cavallo) names dict shouldn't be modified
- Birne94 (Daniel Birnstiel) for fixing leaking generators, star expressions
- patricksurry (Patrick Surry) or should return last value, even if falsy.
- shughes-uk (Samantha Hughes) python w/o 'site' should not fail to import.
- KOLANICH packaging / deployment / setup help & << + >> & other bit ops
- graingert (Thomas Grainger) packaging / deployment / setup help
- bozokopic (Bozo Kopic) Memory leak fix
- daxamin (Dax Amin) Better error for attempting to eval empty string
- smurfix (Matthias Urlichs) Allow clearing functions / operators / etc completely
- koenigsley (Mikhail Yeremeyev) documentation typos correction.
- kurtmckee (Kurt McKee) Infrastructure updates
- edgarrmondragon (Edgar Ramírez-Mondragón) Address Python 3.12+ deprecation warnings,
  performance fixes
- cedk (Cédric Krier) <ced@b2ck.com> Allow running tests with Werror
- decorator-factory <decorator-factory@protonmail.com> More security fixes
- lkruitwagen (Lucas Kruitwagen) Adding support for dict comprehensions
- ByamB4 (Byambadalai) Reported breakout via module & disallowed functions as object attrs
- Charles Vosburgh Reported & tested dangerous functions in `os`
- Tradi3 (github.com/krutftw) Reported escape via operator module functions
- arpitjain099 (Arpit Jain) Reported missing set & frozenset for names

-------------------------------------
Basic Usage:

>>> s = SimpleEval()
>>> s.eval("20 + 30")
50

You can add your own functions easily too:

if file.txt contents is "11"

>>> def get_file():
...     with open("file.txt", 'r') as f:
...         return f.read()

>>> s.functions["get_file"] = get_file
>>> s.eval("int(get_file()) + 31")
42

For more information, see the full package documentation on pypi, or the github
repo.

-----------

If you don't need to re-use the evaluator (with it's names, functions, etc),
then you can use the simple_eval() function:

>>> simple_eval("21 + 19")
40

You can pass names, operators and functions to the simple_eval function as
well:

>>> simple_eval("40 + two", names={"two": 2})
42

"""

from __future__ import annotations

import ast
import datetime
import operator as op
import os
import sys
import types
import warnings
from collections import defaultdict
from dataclasses import dataclass
from random import random

########################################
# Module wide 'globals'

MAX_STRING_LENGTH = 100000
MAX_COMPREHENSION_LENGTH = 10000
MAX_POWER = 4000000  # highest exponent
MAX_SHIFT = 10000  # highest << or >> (lshift / rshift)
MAX_SHIFT_BASE = int(sys.float_info.max)  # highest on left side of << or >>
DISALLOW_PREFIXES = ["_", "func_"]
DISALLOW_METHODS = [
    "format",
    "format_map",
    "mro",
    "tb_frame",
    "gi_frame",
    "ag_frame",
    "cr_frame",
    "exec",
]

########################################
# Tiny helpers:

# Primitive types that are always safe: can't be modules, can't be in DISALLOW_FUNCTIONS,
# and don't need recursive container checks. Used as a fast path in _check_disallowed_items.
_PRIMITIVE_TYPES = frozenset({int, float, str, bool, type(None), bytes, complex})


# Disallow functions:
# This, strictly speaking, is not necessary.  These /should/ never be accessible anyway,
# if DISALLOW_PREFIXES and DISALLOW_METHODS are all right.  This is here to try and help
# people not be stupid.  Allowing these functions opens up all sorts of holes - if any of
# their functionality is required, then please wrap them up in a safe container.  And think
# very hard about it first.  And don't say I didn't warn you.
# builtins is a dict in python >3.6 but a module before
DISALLOW_FUNCTIONS = {
    type,
    isinstance,
    eval,
    getattr,
    setattr,
    repr,
    compile,
    open,
    exec,
    globals,
    locals,
    os.popen,
    os.system,
    *(getattr(os, func) for func in dir(os) if func.startswith(("exec", "spawn", "posix_spawn"))),
    op.attrgetter,
    op.itemgetter,
    op.methodcaller,
}
if hasattr(__builtins__, "help") or (
    hasattr(__builtins__, "__contains__") and "help" in __builtins__  # type: ignore
):
    # PyInstaller environment doesn't include this module.
    DISALLOW_FUNCTIONS.add(help)

if hasattr(op, "call"):
    DISALLOW_FUNCTIONS.add(op.call)

# Opt-in type safety experiment. Will be opt-out in 2.x

BASIC_ALLOWED_ATTRS: dict[type | None, set] = {
    int: {
        "as_integer_ratio",
        "bit_length",
        "conjugate",
        "denominator",
        "from_bytes",
        "imag",
        "numerator",
        "real",
        "to_bytes",
    },
    float: {
        "as_integer_ratio",
        "conjugate",
        "fromhex",
        "hex",
        "imag",
        "is_integer",
        "real",
    },
    str: {
        "capitalize",
        "casefold",
        "center",
        "count",
        "encode",
        "endswith",
        "expandtabs",
        "find",
        "format",
        "format_map",
        "index",
        "isalnum",
        "isalpha",
        "isascii",
        "isdecimal",
        "isdigit",
        "isidentifier",
        "islower",
        "isnumeric",
        "isprintable",
        "isspace",
        "istitle",
        "isupper",
        "join",
        "ljust",
        "lower",
        "lstrip",
        "maketrans",
        "partition",
        "removeprefix",
        "removesuffix",
        "replace",
        "rfind",
        "rindex",
        "rjust",
        "rpartition",
        "rsplit",
        "rstrip",
        "split",
        "splitlines",
        "startswith",
        "strip",
        "swapcase",
        "title",
        "translate",
        "upper",
        "zfill",
    },
    bool: {
        "as_integer_ratio",
        "bit_length",
        "conjugate",
        "denominator",
        "from_bytes",
        "imag",
        "numerator",
        "real",
        "to_bytes",
    },
    None: set(),
    dict: {
        "clear",
        "copy",
        "fromkeys",
        "get",
        "items",
        "keys",
        "pop",
        "popitem",
        "setdefault",
        "update",
        "values",
    },
    list: {
        "pop",
        "append",
        "index",
        "reverse",
        "count",
        "sort",
        "copy",
        "extend",
        "clear",
        "insert",
        "remove",
    },
    set: {
        "pop",
        "intersection_update",
        "intersection",
        "issubset",
        "symmetric_difference_update",
        "discard",
        "isdisjoint",
        "difference_update",
        "issuperset",
        "add",
        "copy",
        "union",
        "clear",
        "update",
        "symmetric_difference",
        "difference",
        "remove",
    },
    tuple: {"index", "count"},
}


########################################
# Exceptions:


class TypeNotSpecified(Exception):
    pass


class InvalidExpression(Exception):
    """Generic Exception"""


class FunctionNotDefined(InvalidExpression):
    """sorry! That function isn't defined!"""

    def __init__(self, func_name, expression):
        self.message = f"Function '{func_name}' not defined, for expression '{expression}'."
        self.func_name = func_name  # bypass 2to3 confusion.
        self.expression = expression

        super(InvalidExpression, self).__init__(self.message)


class NameNotDefined(InvalidExpression):
    """a name isn't defined."""

    def __init__(self, name, expression):
        self.name = name
        self.message = f"'{name}' is not defined for expression '{expression}'"
        self.expression = expression

        super(InvalidExpression, self).__init__(self.message)


class AttributeDoesNotExist(InvalidExpression):
    """attribute does not exist"""

    def __init__(self, attr, expression):
        self.message = f"Attribute '{attr}' does not exist in expression '{expression}'"
        self.attr = attr
        self.expression = expression

        super(InvalidExpression, self).__init__(self.message)


class OperatorNotDefined(InvalidExpression):
    """operator does not exist"""

    def __init__(self, attr, expression):
        self.message = f"Operator '{attr}' does not exist in expression '{expression}'"
        self.attr = attr
        self.expression = expression

        super(InvalidExpression, self).__init__(self.message)


class FeatureNotAvailable(InvalidExpression):
    """What you're trying to do is not allowed."""


class NumberTooHigh(InvalidExpression):
    """Sorry! That number is too high. I don't want to spend the
    next 10 years evaluating this expression!"""


class IterableTooLong(InvalidExpression):
    """That iterable is **way** too long, baby."""


class AssignmentAttempted(UserWarning):
    """Assignment not allowed in SimpleEval"""


class MultipleExpressions(UserWarning):
    """Only the first expression parsed will be used"""


# Sentinal used during attr access
_ATTR_NOT_FOUND = object()


class ModuleWrapper:
    """Wraps a module to safely expose it in expressions.

    By default, modules are not allowed in simpleeval names to prevent
    accidental or malicious access to dangerous functions. ModuleWrapper
    allows explicit opt-in to module access while still enforcing
    restrictions on dangerous methods and functions.

    Example:
        >>> from simpleeval import SimpleEval, ModuleWrapper
        >>> import os.path
        >>> s = SimpleEval(names={'path': ModuleWrapper(os.path)})
        >>> s.eval('path.exists("/etc/passwd")')  # Works
    """

    def __init__(self, module, allowed_attrs=None):
        """
        Args:
            module: The module to wrap
            allowed_attrs: Optional set of allowed attribute names.
                          If None, all public attributes are allowed
                          (but still subject to DISALLOW_METHODS checks).
        """
        if not isinstance(module, types.ModuleType):
            raise TypeError(f"ModuleWrapper requires a module, got {type(module)}")
        self._module = module
        self._allowed_attrs = allowed_attrs

    def __getattr__(self, name):
        # Block private/magic attributes
        if name.startswith("_"):
            raise FeatureNotAvailable(f"Access to private attribute '{name}' is not allowed")

        # Check if attribute is in disallowed methods list
        if name in DISALLOW_METHODS:
            raise FeatureNotAvailable(f"Method '{name}' is not allowed on modules")

        # Check allowed_attrs whitelist if specified
        if self._allowed_attrs is not None and name not in self._allowed_attrs:
            raise FeatureNotAvailable(f"Access to '{name}' is not allowed on this wrapped module")

        return getattr(self._module, name)


########################################
# As-of ("bitemporal") code table lookup:
#
# A code table can contain more than one declaration for the same business key.
# Each declaration has a business-time validity interval and a knowledge time
# (when the declaration became known).  Lookups therefore need both a business
# date and a knowledge cutoff:
#
# - business date: which day the transaction happened;
# - knowledge cutoff: what was known at that point in time;
# - business key: the row to look up.
#
# Validity intervals are half-open: [valid_from, valid_to).  A declaration
# published after the knowledge cutoff is ignored, so backfilled corrections
# only affect queries made at or after their publication.  Among the visible,
# currently-valid declarations, the highest priority wins; at equal priority,
# the latest publication wins.  Declarations still tied after that are all
# returned as ambiguous rather than picked by input order.

_MISSING = object()


def _coerce_date(value, field_name="date"):
    """Return a calendar date from a date, datetime, or ISO-8601 string."""
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            return datetime.date.fromisoformat(text)
        except ValueError:
            try:
                return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).date()
            except ValueError as exc:
                raise ValueError(f"{field_name} is not an ISO-8601 date: {value!r}") from exc
    raise TypeError(f"{field_name} must be a date, datetime, or ISO-8601 string")


def _coerce_knowledge(value, *, end_of_day=False):
    """Return a comparable knowledge-time value.

    Date-only cutoffs mean "by the end of that day", while date-only
    declarations mean "known from the start of that day".  This makes a
    correction published on the same calendar day visible to a same-day
    cutoff.
    """
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.date):
        moment = datetime.time.max if end_of_day else datetime.time.min
        return datetime.datetime.combine(value, moment)
    if isinstance(value, str):
        text = value.strip()
        try:
            day = datetime.date.fromisoformat(text)
        except ValueError:
            try:
                return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(
                    f"knowledge time is not an ISO-8601 date or datetime: {value!r}"
                ) from exc
        moment = datetime.time.max if end_of_day else datetime.time.min
        return datetime.datetime.combine(day, moment)
    raise TypeError("knowledge time must be a date, datetime, or ISO-8601 string")


def _known_sort_key(value):
    if value is None:
        return (0, "")
    return (1, _coerce_knowledge(value))


def _priority_sort_key(value):
    if value is None:
        return (0, 0)
    if isinstance(value, bool):
        return (1, int(value))
    if isinstance(value, (int, float)):
        return (1, value)
    return (2, str(value))


def _source_sort_key(value):
    return (type(value).__name__, repr(value))


def _declaration_sort_key(declaration):
    return (
        _source_sort_key(declaration.source),
        repr(declaration.code),
        repr(declaration.valid_from),
        repr(declaration.valid_to),
        repr(declaration.known_at),
        _priority_sort_key(declaration.priority),
    )


@dataclass(frozen=True, init=False)
class CodeDeclaration:
    """One declaration in a bitemporal code table.

    ``valid_from``/``valid_to`` describe when the code applies in business
    time.  The interval is half-open, so ``valid_to`` itself is not included.
    ``known_at`` (alias ``published_at``) is when the declaration became
    available to the business.  ``priority`` resolves declarations from
    different authorities; ``source`` identifies the declaration origin.

    Keyword aliases are accepted for convenience because code tables often
    use different column names.  In particular, ``published_at``,
    ``effective_from``, ``effective_to``, ``key``, ``value``, and ``source_id``
    are accepted.
    """

    business_key: object
    code: object
    valid_from: object
    valid_to: object
    known_at: object
    priority: object
    source: object

    def __init__(
        self,
        business_key=_MISSING,
        code=_MISSING,
        valid_from=_MISSING,
        valid_to=_MISSING,
        known_at=_MISSING,
        priority=_MISSING,
        source=_MISSING,
        **aliases,
    ):
        alias_names = {
            "key": "business_key",
            "business_id": "business_key",
            "department_key": "business_key",
            "department_code": "code",
            "dept_code": "code",
            "value": "code",
            "result": "code",
            "effective_from": "valid_from",
            "from_date": "valid_from",
            "start": "valid_from",
            "start_date": "valid_from",
            "valid_start": "valid_from",
            "effective_to": "valid_to",
            "to_date": "valid_to",
            "end": "valid_to",
            "end_date": "valid_to",
            "valid_end": "valid_to",
            "published_at": "known_at",
            "published": "known_at",
            "publication_time": "known_at",
            "known_from": "known_at",
            "knowledge_time": "known_at",
            "version_time": "known_at",
            "created_at": "known_at",
            "updated_at": "known_at",
            "precedence": "priority",
            "rank": "priority",
            "source_id": "source",
            "origin": "source",
            "provenance": "source",
        }
        values = {
            "business_key": business_key,
            "code": code,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "known_at": known_at,
            "priority": priority,
            "source": source,
        }
        for alias, canonical in alias_names.items():
            if alias not in aliases:
                continue
            alias_value = aliases.pop(alias)
            current = values[canonical]
            if current is not _MISSING and current != alias_value:
                raise TypeError(f"CodeDeclaration got both {canonical!r} and its alias {alias!r}")
            values[canonical] = alias_value
        if aliases:
            unexpected = ", ".join(sorted(aliases))
            raise TypeError(f"CodeDeclaration got unexpected arguments: {unexpected}")

        for required in ("business_key", "code", "valid_from"):
            if values[required] is _MISSING:
                raise TypeError(f"CodeDeclaration requires {required!r}")

        if values["valid_to"] is _MISSING:
            values["valid_to"] = None
        if values["known_at"] is _MISSING:
            values["known_at"] = None
        if values["priority"] is _MISSING:
            values["priority"] = 0
        if values["source"] is _MISSING:
            values["source"] = None

        valid_from_value = values["valid_from"]
        valid_to_value = values["valid_to"]
        if isinstance(valid_from_value, str):
            valid_from_value = _coerce_date(valid_from_value, "valid_from")
        if isinstance(valid_to_value, str):
            valid_to_value = _coerce_date(valid_to_value, "valid_to")

        if valid_to_value is not None:
            try:
                empty = valid_to_value <= valid_from_value
            except TypeError:
                empty = _coerce_date(valid_to_value, "valid_to") <= _coerce_date(
                    valid_from_value, "valid_from"
                )
            if empty:
                raise ValueError("valid_to must be after valid_from")

        known_at_value = values["known_at"]
        if isinstance(known_at_value, str):
            try:
                known_at_value = _coerce_knowledge(known_at_value)
            except ValueError:
                known_at_value = _coerce_date(known_at_value, "known_at")

        object.__setattr__(self, "business_key", values["business_key"])
        object.__setattr__(self, "code", values["code"])
        object.__setattr__(self, "valid_from", valid_from_value)
        object.__setattr__(self, "valid_to", valid_to_value)
        object.__setattr__(self, "known_at", known_at_value)
        object.__setattr__(
            self, "priority", 0 if values["priority"] is None else values["priority"]
        )
        object.__setattr__(self, "source", values["source"])

    def __repr__(self):
        return (
            "CodeDeclaration("
            f"business_key={self.business_key!r}, code={self.code!r}, "
            f"valid_from={self.valid_from!r}, valid_to={self.valid_to!r}, "
            f"known_at={self.known_at!r}, priority={self.priority!r}, "
            f"source={self.source!r})"
        )

    @property
    def key(self):
        return self.business_key

    @property
    def value(self):
        return self.code

    @property
    def published_at(self):
        return self.known_at

    @property
    def effective_from(self):
        return self.valid_from

    @property
    def effective_to(self):
        return self.valid_to

    @property
    def source_id(self):
        return self.source

    def covers(self, business_date):
        """Whether the half-open validity interval contains ``business_date``."""
        day = _coerce_date(business_date, "business_date")
        start = _coerce_date(self.valid_from, "valid_from")
        if day < start:
            return False
        if self.valid_to is None:
            return True
        return day < _coerce_date(self.valid_to, "valid_to")


@dataclass(frozen=True, init=False)
class CodeLookupResult:
    """Result of an as-of code lookup.

    ``code`` is the selected code, or ``None`` when equal-priority sources
    disagree.  ``sources`` always contains every declaration that remains tied
    for the selected priority and knowledge time, in a deterministic order.
    ``ambiguous`` is true when more than one declaration remains tied.
    """

    code: object
    sources: tuple
    declarations: tuple
    ambiguous: bool
    business_date: object
    knowledge_cutoff: object
    business_key: object

    def __init__(
        self,
        code=None,
        sources=(),
        declarations=(),
        ambiguous=False,
        business_date=None,
        knowledge_cutoff=None,
        business_key=None,
    ):
        declarations = tuple(declarations)
        sources = tuple(sources)
        if declarations and not sources:
            sources = tuple(declaration.source for declaration in declarations)
        if len(declarations) > 1:
            ambiguous = True
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "declarations", declarations)
        object.__setattr__(self, "ambiguous", bool(ambiguous))
        object.__setattr__(self, "business_date", business_date)
        object.__setattr__(self, "knowledge_cutoff", knowledge_cutoff)
        object.__setattr__(self, "business_key", business_key)

    @property
    def found(self):
        return self.code is not None or bool(self.declarations)

    @property
    def department_code(self):
        return self.code

    @property
    def department(self):
        return self.code

    @property
    def value(self):
        return self.code

    @property
    def source(self):
        return self.sources[0] if len(self.sources) == 1 else None

    @property
    def is_ambiguous(self):
        return self.ambiguous

    def as_dict(self):
        return {
            "code": self.code,
            "sources": self.sources,
            "declarations": self.declarations,
            "ambiguous": self.ambiguous,
        }

    def get(self, key, default=None):
        return self.as_dict().get(key, default)

    def keys(self):
        return self.as_dict().keys()

    def values(self):
        return self.as_dict().values()

    def items(self):
        return self.as_dict().items()

    def __getitem__(self, key):
        return self.as_dict()[key]

    def __eq__(self, other):
        if isinstance(other, CodeLookupResult):
            return (
                self.code == other.code
                and self.sources == other.sources
                and self.declarations == other.declarations
                and self.ambiguous == other.ambiguous
            )
        if isinstance(other, str):
            return self.code == other
        if isinstance(other, dict):
            return all(self.get(key, _MISSING) == value for key, value in other.items())
        if isinstance(other, tuple) and len(other) == 2:
            return self.code == other[0] and self.sources == other[1]
        return NotImplemented

    def __hash__(self):
        return hash((self.code, self.sources, self.ambiguous))

    def __iter__(self):
        return iter((self.code, self.sources))

    def __len__(self):
        return 2

    def __str__(self):
        return "" if self.code is None else str(self.code)

    def __repr__(self):
        return (
            "CodeLookupResult("
            f"code={self.code!r}, sources={self.sources!r}, "
            f"ambiguous={self.ambiguous!r})"
        )


class BitemporalCodeTable:
    """Indexed code table supporting as-of (business time + knowledge time) reads.

    Example:
        >>> table = BitemporalCodeTable([
        ...     {"key": "E01", "code": "D1", "effective_from": "2024-01-01",
        ...      "effective_to": "2024-04-01", "published_at": "2024-01-01",
        ...      "source": "initial"},
        ... ])
        >>> table.lookup("2024-02-01", "2024-02-15", "E01").code
        'D1'
    """

    def __init__(self, declarations=()):
        if isinstance(declarations, BitemporalCodeTable):
            declarations = declarations.declarations
        elif isinstance(declarations, CodeDeclaration):
            declarations = (declarations,)
        elif isinstance(declarations, dict):
            if any(key in declarations for key in ("business_key", "key", "code")):
                declarations = (declarations,)
            else:
                expanded = []
                for key, rows in declarations.items():
                    if isinstance(rows, (str, bytes, CodeDeclaration, dict)):
                        rows = (rows,)
                    for row in rows:
                        if isinstance(row, dict):
                            row = dict(row)
                            row.setdefault("business_key", key)
                        expanded.append(row)
                declarations = expanded

        unique = {}
        for declaration in declarations:
            if not isinstance(declaration, CodeDeclaration):
                if isinstance(declaration, dict):
                    declaration = CodeDeclaration(**declaration)
                elif isinstance(declaration, (tuple, list)):
                    declaration = CodeDeclaration(*declaration)
                else:
                    raise TypeError(
                        "declarations must contain CodeDeclaration objects, mappings, "
                        "or positional tuples"
                    )
            identity = (
                repr(declaration.business_key),
                repr(declaration.code),
                repr(declaration.valid_from),
                repr(declaration.valid_to),
                repr(declaration.known_at),
                repr(declaration.priority),
                repr(declaration.source),
            )
            unique.setdefault(identity, declaration)

        self.declarations = tuple(unique.values())
        self._by_key = defaultdict(list)
        for declaration in self.declarations:
            self._by_key[declaration.business_key].append(declaration)

    def lookup(self, business_date, knowledge_cutoff, business_key):
        """Look up ``business_key`` for a business date and knowledge cutoff.

        The knowledge cutoff is inclusive: a declaration published exactly at
        the cutoff is known.  Date-only cutoffs are interpreted as the end of
        that day.
        """
        day = _coerce_date(business_date, "business_date")
        cutoff = (
            None
            if knowledge_cutoff is None
            else _coerce_knowledge(knowledge_cutoff, end_of_day=True)
        )

        candidates = []
        for declaration in self._by_key.get(business_key, ()):
            if not declaration.covers(day):
                continue
            if cutoff is not None and declaration.known_at is not None:
                if _coerce_knowledge(declaration.known_at) > cutoff:
                    continue
            candidates.append(declaration)

        if not candidates:
            return CodeLookupResult(
                code=None,
                sources=(),
                declarations=(),
                ambiguous=False,
                business_date=business_date,
                knowledge_cutoff=knowledge_cutoff,
                business_key=business_key,
            )

        best_priority = max(_priority_sort_key(d.priority) for d in candidates)
        candidates = [d for d in candidates if _priority_sort_key(d.priority) == best_priority]

        best_known = max(_known_sort_key(d.known_at) for d in candidates)
        candidates = [d for d in candidates if _known_sort_key(d.known_at) == best_known]
        candidates.sort(key=_declaration_sort_key)

        codes = []
        for declaration in candidates:
            if not any(declaration.code == existing for existing in codes):
                codes.append(declaration.code)
        code = codes[0] if len(codes) == 1 else None

        return CodeLookupResult(
            code=code,
            sources=tuple(d.source for d in candidates),
            declarations=tuple(candidates),
            ambiguous=len(candidates) > 1,
            business_date=business_date,
            knowledge_cutoff=knowledge_cutoff,
            business_key=business_key,
        )

    __call__ = lookup

    def as_lookup(self):
        """Return a callable suitable for ``SimpleEval.functions``."""
        return make_asof_lookup(self)

    lookup_as_of = lookup
    lookup_known_at = lookup


class AsOfCodeTable(BitemporalCodeTable):
    """Alias class name for :class:`BitemporalCodeTable`."""


CodeTable = BitemporalCodeTable


def make_asof_lookup(table, function_name="lookup_code"):
    """Bind ``table`` into a three-argument function for expressions.

    The returned function accepts ``(business_date, knowledge_cutoff,
    business_key)`` and returns a :class:`CodeLookupResult`.
    """
    if not isinstance(table, BitemporalCodeTable):
        table = BitemporalCodeTable(table)

    def lookup(business_date, knowledge_cutoff, business_key):
        return table.lookup(business_date, knowledge_cutoff, business_key)

    lookup.__name__ = function_name
    lookup.__qualname__ = function_name
    lookup.table = table
    return lookup


make_as_of_lookup = make_asof_lookup
make_bitemporal_lookup = make_asof_lookup
make_code_lookup = make_asof_lookup
make_lookup = make_asof_lookup


def lookup_as_of(table, business_date, knowledge_cutoff, business_key):
    """Convenience wrapper around ``table.lookup(...)``."""
    return table.lookup(business_date, knowledge_cutoff, business_key)


lookup_asof = lookup_as_of
lookup_known_at = lookup_as_of
bitemporal_lookup = lookup_as_of


def install_code_table(evaluator, table, name="lookup_code"):
    """Register an as-of lookup function on a ``SimpleEval`` instance."""
    evaluator.functions[name] = make_asof_lookup(table, function_name=name)
    return evaluator


########################################
# Default simple functions to include:


def random_int(top):
    """return a random int below <top>"""

    return int(random() * top)


def safe_power(a, b):  # pylint: disable=invalid-name
    """a limited exponent/to-the-power-of function, for safety reasons"""

    if abs(a) > MAX_POWER or abs(b) > MAX_POWER:
        raise NumberTooHigh(f"Sorry! I don't want to evaluate {a} ** {b}")
    return a**b


def safe_mult(a, b):  # pylint: disable=invalid-name
    """limit the number of times an iterable can be repeated..."""

    if hasattr(a, "__len__") and b * len(a) > MAX_STRING_LENGTH:
        raise IterableTooLong("Sorry, I will not evaluate something that long.")
    if hasattr(b, "__len__") and a * len(b) > MAX_STRING_LENGTH:
        raise IterableTooLong("Sorry, I will not evaluate something that long.")

    return a * b


def safe_add(a, b):  # pylint: disable=invalid-name
    """iterable length limit again"""

    if hasattr(a, "__len__") and hasattr(b, "__len__") and len(a) + len(b) > MAX_STRING_LENGTH:
        raise IterableTooLong("Sorry, adding those two together would make something too long.")
    return a + b


def safe_rshift(a, b):  # pylint: disable=invalid-name
    """rshift, but with input limits"""
    if abs(b) > MAX_SHIFT or abs(a) > MAX_SHIFT_BASE:
        raise NumberTooHigh(f"Sorry! I don't want to evaluate {a} >> {b}")
    return a >> b


def safe_lshift(a, b):  # pylint: disable=invalid-name
    """lshift, but with input limits"""
    if abs(b) > MAX_SHIFT or abs(a) > MAX_SHIFT_BASE:
        raise NumberTooHigh(f"Sorry! I don't want to evaluate {a} << {b}")
    return a << b


########################################
# Defaults for the evaluator:

DEFAULT_OPERATORS = {
    ast.Add: safe_add,
    ast.Sub: op.sub,
    ast.Mult: safe_mult,
    ast.Div: op.truediv,
    ast.FloorDiv: op.floordiv,
    ast.RShift: safe_rshift,
    ast.LShift: safe_lshift,
    ast.Pow: safe_power,
    ast.Mod: op.mod,
    ast.Eq: op.eq,
    ast.NotEq: op.ne,
    ast.Gt: op.gt,
    ast.Lt: op.lt,
    ast.GtE: op.ge,
    ast.LtE: op.le,
    ast.Not: op.not_,
    ast.USub: op.neg,
    ast.UAdd: op.pos,
    ast.BitXor: op.xor,
    ast.BitOr: op.or_,
    ast.BitAnd: op.and_,
    ast.Invert: op.invert,
    ast.In: lambda x, y: op.contains(y, x),
    ast.NotIn: lambda x, y: not op.contains(y, x),
    ast.Is: lambda x, y: x is y,
    ast.IsNot: lambda x, y: x is not y,
}

DEFAULT_FUNCTIONS = {
    "rand": random,
    "randint": random_int,
    "int": int,
    "float": float,
    "str": str,
}

DEFAULT_NAMES = {"True": True, "False": False, "None": None}

ATTR_INDEX_FALLBACK = True


########################################
# And the actual evaluator:


class SimpleEval:  # pylint: disable=too-few-public-methods
    """A very simple expression parser.
    >>> s = SimpleEval()
    >>> s.eval("20 + 30 - ( 10 * 5)")
    0
    """

    expr = ""

    def __init__(self, operators=None, functions=None, names=None, allowed_attrs=None):
        """
        Create the evaluator instance.  Set up valid operators (+,-, etc)
        functions (add, random, get_val, whatever) and names."""

        if operators is None:
            operators = DEFAULT_OPERATORS.copy()
        if functions is None:
            functions = DEFAULT_FUNCTIONS.copy()
        if names is None:
            names = DEFAULT_NAMES.copy()

        self.operators = operators
        self.functions = functions
        self.names = names
        self.allowed_attrs = allowed_attrs

        self.nodes = {
            ast.Expr: self._eval_expr,
            ast.Assign: self._eval_assign,
            ast.AugAssign: self._eval_aug_assign,
            ast.Import: self._eval_import,
            ast.Name: self._eval_name,
            ast.UnaryOp: self._eval_unaryop,
            ast.BinOp: self._eval_binop,
            ast.BoolOp: self._eval_boolop,
            ast.Compare: self._eval_compare,
            ast.IfExp: self._eval_ifexp,
            ast.Call: self._eval_call,
            ast.keyword: self._eval_keyword,
            ast.Subscript: self._eval_subscript,
            ast.Attribute: self._eval_attribute,
            ast.Index: self._eval_index,
            ast.Slice: self._eval_slice,
            ast.JoinedStr: self._eval_joinedstr,
            ast.FormattedValue: self._eval_formattedvalue,
            ast.Constant: self._eval_constant,
        }

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            # py3.12 deprecated ast.Num, ast.Str, ast.NameConstant
            # https://docs.python.org/3.12/whatsnew/3.12.html#deprecated
            if Num := getattr(ast, "Num", None):
                self.nodes[Num] = self._eval_num

            if Str := getattr(ast, "Str", None):
                self.nodes[Str] = self._eval_str

            if NameConstant := getattr(ast, "NameConstant", None):
                self.nodes[NameConstant] = self._eval_constant

        # Defaults:

        self.ATTR_INDEX_FALLBACK = ATTR_INDEX_FALLBACK

        # Check for forbidden functions:

        for f in self.functions.values():
            if f in DISALLOW_FUNCTIONS:
                raise FeatureNotAvailable(f"This function {f} is a really bad idea.")

    def __del__(self):
        self.nodes = None

    def _check_disallowed_items(self, item):
        """Check if item contains disallowed functions or modules.
        Recursively checks containers (list, dict, tuple).
        Raises FeatureNotAvailable if forbidden content found.
        ModuleWrapper instances are allowed (explicit opt-in to module access).
        """
        # Fast path: primitive scalars are always safe (most common case)
        if type(item) in _PRIMITIVE_TYPES:
            return

        # Allow ModuleWrapper (explicit opt-in to module access)
        if isinstance(item, ModuleWrapper):
            return

        if isinstance(item, types.ModuleType):
            raise FeatureNotAvailable("Sorry, modules are not allowed")

        if isinstance(item, (list, tuple, set, frozenset)):
            for element in item:
                self._check_disallowed_items(element)
        elif isinstance(item, dict):
            for value in item.values():
                self._check_disallowed_items(value)
        elif callable(item) and item in DISALLOW_FUNCTIONS:
            raise FeatureNotAvailable("This function is forbidden")

    @staticmethod
    def parse(expr):
        """parse an expression into a node tree"""

        parsed = ast.parse(expr.strip())

        if not parsed.body:
            raise InvalidExpression("Sorry, cannot evaluate empty string")
        if len(parsed.body) > 1:
            warnings.warn(
                f"'{expr}' contains multiple expressions. Only the first will be used.",
                MultipleExpressions,
            )
        return parsed.body[0]

    def eval(self, expr, previously_parsed=None):
        """evaluate an expression, using the operators, functions and
        names previously set up."""

        # set a copy of the expression aside, so we can give nice errors...
        self.expr = expr

        return self._eval(previously_parsed or self.parse(expr))

    def _eval(self, node):
        """The internal evaluator used on each node in the parsed tree."""

        try:
            handler = self.nodes[type(node)]
        except KeyError:
            raise FeatureNotAvailable(
                f"Sorry, {type(node).__name__} is not available in this evaluator"
            )

        result = handler(node)
        self._check_disallowed_items(result)
        return result

    def _eval_expr(self, node):
        return self._eval(node.value)

    def _eval_assign(self, node):
        warnings.warn(
            f"Assignment ({self.expr}) attempted, but this is ignored", AssignmentAttempted
        )
        return self._eval(node.value)

    def _eval_aug_assign(self, node):
        warnings.warn(
            f"Assignment ({self.expr}) attempted, but this is ignored", AssignmentAttempted
        )
        return self._eval(node.value)

    @staticmethod
    def _eval_import(node):
        raise FeatureNotAvailable("Sorry, 'import' is not allowed.")

    @staticmethod
    def _eval_num(node):
        return node.n

    @staticmethod
    def _eval_str(node):
        if len(node.s) > MAX_STRING_LENGTH:
            raise IterableTooLong(
                f"String Literal in statement is too long! ({len(node.s)}, when {MAX_STRING_LENGTH} is max)"
            )
        return node.s

    @staticmethod
    def _eval_constant(node):
        if hasattr(node.value, "__len__") and len(node.value) > MAX_STRING_LENGTH:
            raise IterableTooLong(
                f"Literal in statement is too long! ({len(node.value)}, when {MAX_STRING_LENGTH} is max)"
            )
        return node.value

    def _eval_unaryop(self, node):
        try:
            operator = self.operators[type(node.op)]
        except KeyError:
            raise OperatorNotDefined(node.op, self.expr)
        return operator(self._eval(node.operand))

    def _eval_binop(self, node):
        try:
            operator = self.operators[type(node.op)]
        except KeyError:
            raise OperatorNotDefined(node.op, self.expr)
        return operator(self._eval(node.left), self._eval(node.right))

    def _eval_boolop(self, node):
        to_return = False
        if isinstance(node.op, ast.And):
            for value in node.values:
                to_return = self._eval(value)
                if not to_return:
                    break
        elif isinstance(node.op, ast.Or):
            for value in node.values:
                to_return = self._eval(value)
                if to_return:
                    break
        return to_return

    def _eval_compare(self, node):
        right = self._eval(node.left)
        to_return = True
        for operation, comp in zip(node.ops, node.comparators):
            if not to_return:
                break
            left = right
            right = self._eval(comp)
            to_return = self.operators[type(operation)](left, right)
        return to_return

    def _eval_ifexp(self, node):
        return self._eval(node.body) if self._eval(node.test) else self._eval(node.orelse)

    def _eval_call(self, node):
        if isinstance(node.func, ast.Attribute):
            func = self._eval(node.func)
        else:
            try:
                func = self.functions[node.func.id]
            except KeyError:
                raise FunctionNotDefined(node.func.id, self.expr)
            except AttributeError:
                raise FeatureNotAvailable("Lambda Functions not implemented")

            if func in DISALLOW_FUNCTIONS:
                raise FeatureNotAvailable("This function is forbidden")

        return func(
            *(self._eval(a) for a in node.args), **dict(self._eval(k) for k in node.keywords)
        )

    def _eval_keyword(self, node):
        return node.arg, self._eval(node.value)

    def _eval_name(self, node):
        try:
            # This happens at least for slicing
            # This is a safe thing to do because it is impossible
            # that there is a true expression assigning to none
            # (the compiler rejects it, so you can't even
            # pass that to ast.parse)
            return self.names[node.id]

        except (TypeError, KeyError):
            pass

        if callable(self.names):
            try:
                return self.names(node)
            except NameNotDefined:
                pass
        elif not hasattr(self.names, "__getitem__"):
            raise InvalidExpression(
                f'Trying to use name (variable) "{node.id}" when no "names" defined for evaluator'
            )

        if node.id in self.functions:
            return self.functions[node.id]

        raise NameNotDefined(node.id, self.expr)

    def _eval_subscript(self, node):
        container = self._eval(node.value)
        key = self._eval(node.slice)
        # Currently if there's a KeyError, that gets raised straight up.
        # TODO: Should that be wrapped in an InvalidExpression?
        return container[key]

    def _eval_attribute(self, node):
        # DISALLOW_PREFIXES & DISALLOW_METHODS are global, there's never any access to
        # attrs with these names, so we can bail early:
        for prefix in DISALLOW_PREFIXES:
            if node.attr.startswith(prefix):
                raise FeatureNotAvailable(
                    "Sorry, access to __attributes "
                    " or func_ attributes is not available. "
                    f"({node.attr})"
                )
        if node.attr in DISALLOW_METHODS:
            raise FeatureNotAvailable(f"Sorry, this method is not available. ({node.attr})")

        # Evaluate "node" - the thing that we're trying to access an attr of first:
        node_evaluated = self._eval(node.value)

        # If we've opted in to the 'allowed_attrs' checking per type, then since we now
        # know what kind of node we've got, we can check if we're permitted to access this
        # attr name on this node:
        if self.allowed_attrs is not None:
            type_to_check = type(node_evaluated)

            allowed_attrs = self.allowed_attrs.get(type_to_check, TypeNotSpecified)
            if allowed_attrs == TypeNotSpecified:
                raise FeatureNotAvailable(
                    f"Sorry, attribute access not allowed on '{type_to_check}'"
                    f" (attempted to access `.{node.attr}`)"
                )
            if node.attr not in allowed_attrs:
                raise FeatureNotAvailable(
                    f"Sorry, '.{node.attr}' access not allowed on '{type_to_check}'"
                )

        item = _ATTR_NOT_FOUND

        # Maybe the base object is an actual object, not just a dict
        try:
            item = getattr(node_evaluated, node.attr)
        except (AttributeError, TypeError):
            # TODO: is this a good idea?  Try and look for [x] if .x doesn't work?
            if self.ATTR_INDEX_FALLBACK:
                try:
                    item = node_evaluated[node.attr]
                except (KeyError, TypeError):
                    pass

        if item is not _ATTR_NOT_FOUND:
            if isinstance(item, types.ModuleType):
                raise FeatureNotAvailable("Sorry, modules are not allowed in attribute access")
            if callable(item) and item in DISALLOW_FUNCTIONS:
                raise FeatureNotAvailable("This function is forbidden")
            return item

        # If it is neither, raise an exception
        raise AttributeDoesNotExist(node.attr, self.expr)

    def _eval_index(self, node):
        return self._eval(node.value)

    def _eval_slice(self, node):
        lower = upper = step = None
        if node.lower is not None:
            lower = self._eval(node.lower)
        if node.upper is not None:
            upper = self._eval(node.upper)
        if node.step is not None:
            step = self._eval(node.step)
        return slice(lower, upper, step)

    def _eval_joinedstr(self, node):
        length = 0
        evaluated_values = []
        for n in node.values:
            val = str(self._eval(n))
            if len(val) + length > MAX_STRING_LENGTH:
                raise IterableTooLong("Sorry, I will not evaluate something this long.")
            evaluated_values.append(val)
        return "".join(evaluated_values)

    def _eval_formattedvalue(self, node):
        if node.format_spec:
            fmt = "{:" + self._eval(node.format_spec) + "}"
            return fmt.format(self._eval(node.value))
        return self._eval(node.value)


class EvalWithCompoundTypes(SimpleEval):
    """
    SimpleEval with additional Compound Types, and their respective
    function editions. (list, tuple, dict, set).
    """

    _max_count = 0

    def __init__(self, operators=None, functions=None, names=None, allowed_attrs=None):
        super().__init__(operators, functions, names, allowed_attrs)

        self.functions.update(list=list, tuple=tuple, dict=dict, set=set)

        self.nodes.update(
            {
                ast.Dict: self._eval_dict,
                ast.Tuple: self._eval_tuple,
                ast.List: self._eval_list,
                ast.Set: self._eval_set,
                ast.ListComp: self._eval_comprehension,
                ast.GeneratorExp: self._eval_comprehension,
                ast.DictComp: self._eval_comprehension,
            }
        )

    def eval(self, expr, previously_parsed=None):
        # reset _max_count for each eval run
        self._max_count = 0
        return super().eval(expr, previously_parsed)

    def _eval_dict(self, node):
        result = {}

        for key, value in zip(node.keys, node.values):
            if key is None:
                # "{**x}" gets parsed as a key-value pair of (None, Name(x))
                result.update(self._eval(value))
            else:
                result[self._eval(key)] = self._eval(value)

        return result

    def _eval_list(self, node):
        result = []

        for item in node.elts:
            if isinstance(item, ast.Starred):
                result.extend(self._eval(item.value))
            else:
                result.append(self._eval(item))

        return result

    def _eval_tuple(self, node):
        return tuple(self._eval(x) for x in node.elts)

    def _eval_set(self, node):
        return {self._eval(x) for x in node.elts}

    def _eval_comprehension(self, node):
        if isinstance(node, ast.DictComp):
            to_return = {}
        else:
            to_return = []

        extra_names = {}

        previous_name_evaller = self.nodes[ast.Name]

        def eval_names_extra(node):
            """
            Here we hide our extra scope for within this comprehension
            """
            if node.id in extra_names:
                return extra_names[node.id]
            return previous_name_evaller(node)

        self.nodes.update({ast.Name: eval_names_extra})

        def recurse_targets(target, value):
            """
                Recursively (enter, (into, (nested, name), unpacking)) = \
                             and, (assign, (values, to), each
            """
            if isinstance(target, ast.Name):
                extra_names[target.id] = value
            else:
                for t, v in zip(target.elts, value):
                    recurse_targets(t, v)

        def do_generator(gi=0):
            g = node.generators[gi]
            for i in self._eval(g.iter):
                self._max_count += 1

                if self._max_count > MAX_COMPREHENSION_LENGTH:
                    raise IterableTooLong("Comprehension generates too many elements")
                recurse_targets(g.target, i)
                if all(self._eval(iff) for iff in g.ifs):
                    if len(node.generators) > gi + 1:
                        do_generator(gi + 1)
                    else:
                        if isinstance(to_return, dict):
                            to_return[self._eval(node.key)] = self._eval(node.value)
                        elif isinstance(to_return, list):
                            to_return.append(self._eval(node.elt))

        try:
            do_generator()
        finally:
            self.nodes.update({ast.Name: previous_name_evaller})

        return to_return


def simple_eval(expr, operators=None, functions=None, names=None, allowed_attrs=None):
    """Simply evaluate an expression"""
    s = SimpleEval(
        operators=operators,
        functions=functions,
        names=names,
        allowed_attrs=allowed_attrs,
    )
    return s.eval(expr)
