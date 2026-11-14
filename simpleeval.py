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
import math
import operator as op
import os
import sys
import types
import warnings
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from random import random
from typing import Any

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


########################################
# Minimal-change classification planning:

_PLAN_ABSENT = object()


class PlanningLimitExceeded(InvalidExpression):
    """Raised when the candidate space is too large to search exhaustively.

    Classification planning never reports a false "no solution": if the
    complete space cannot be visited, this exception is raised instead.
    """


class UnknownCategory(KeyError, InvalidExpression):
    """Raised when a requested target category has no rule."""

    def __str__(self):
        return f"Unknown category: {self.args[0]!r}"


@dataclass(frozen=True)
class FixSolution:
    """One minimal-cost way to make a record match a target category.

    ``changes`` contains only the editable fields whose values differ from
    the input record.  ``record`` is the complete rebuilt record, including
    every derived column recomputed from the changed inputs.
    """

    changes: Mapping[str, Any]
    record: Mapping[str, Any]
    cost: Fraction

    @property
    def changed_fields(self) -> tuple[str, ...]:
        return tuple(self.changes)


@dataclass(frozen=True)
class FixPlan:
    """The result of a minimal-change classification search."""

    target: str
    solutions: tuple[FixSolution, ...]
    cost: Fraction | None
    searched: int
    initial_categories: tuple[str, ...] = ()
    status: str = "no_solution"

    @property
    def has_solution(self) -> bool:
        return bool(self.solutions)

    @property
    def is_ambiguous_initial_record(self) -> bool:
        return len(self.initial_categories) > 1

    def as_dict(self) -> dict[str, Any]:
        """Return a plain, JSON-friendly summary (costs become int/float)."""
        return {
            "target": self.target,
            "status": self.status,
            "cost": None if self.cost is None else _fraction_to_plain(self.cost),
            "searched": self.searched,
            "initial_categories": list(self.initial_categories),
            "solutions": [
                {
                    "changes": dict(solution.changes),
                    "cost": _fraction_to_plain(solution.cost),
                    "record": dict(solution.record),
                }
                for solution in self.solutions
            ],
        }


def _fraction_to_plain(value: Fraction) -> int | float:
    return value.numerator if value.denominator == 1 else float(value)


class ClassificationPlanner:
    """Find minimal field edits that move a record into a target category.

    Classification is defined by one or more independent expressions per
    category.  For example::

        planner = ClassificationPlanner(
            rules={
                "perishable": "shelf_life_days <= 7",
                "bulk": ["weight_kg >= 20", "units >= 12"],
            },
            candidates={
                "shelf_life_days": [3, 7, 30],
                "weight_kg": [5, 20],
                "units": [1, 12],
            },
            costs={"shelf_life_days": 2, "weight_kg": 3, "units": 1},
            locked=("sku",),
            derived={"total_units": "units * pack_size"},
        )
        plan = planner.solve(record, "perishable")

    A category matches when every expression listed for it is truthy.  With
    ``exclusive=True`` (the default) a solution is accepted only if the target
    is the *only* matching category, so records that currently match several
    categories (or none) are handled correctly.

    ``candidates`` maps each editable field to its finite candidate values.  A
    mapping of ``value -> cost`` is also accepted and supplies that field's
    per-value costs directly.  The original value is always a free option,
    even when it is absent from the candidate list.  ``costs`` may be:

    * a number per field: the cost of any change to that field;
    * a mapping per field: the cost of each candidate value (values not
      listed fall back to ``default_cost``);
    * a callable ``cost(field, value)``;
    * omitted, in which case every changed field costs ``default_cost``.

    ``locked`` fields are never edited, even if they appear in ``candidates``.
    ``derived`` maps column names to expressions (or callables receiving the
    working record); they are evaluated in insertion order after every edit,
    so classification always sees recomputed columns.  Derived names cannot
    also be editable fields.

    The search is exhaustive: every combination is considered (subject only
    to ``max_combinations``) and all solutions tied for the lowest exact cost
    are returned.  In particular, changes that only alter the category when
    applied together are found; no single-field shortcut is used.
    """

    def __init__(
        self,
        rules,
        candidates,
        costs=None,
        *,
        locked=(),
        derived=None,
        default_cost=1,
        exclusive=True,
        on_rule_error="ignore",
        evaluator=None,
        max_combinations=1_000_000,
    ):
        if on_rule_error not in ("ignore", "raise"):
            raise ValueError("on_rule_error must be 'ignore' or 'raise'")
        if type(max_combinations) is not int or max_combinations < 1:
            raise ValueError("max_combinations must be a positive integer")
        if not isinstance(candidates, Mapping):
            raise TypeError("candidates must be a mapping of field name -> values")
        if costs is not None and not (isinstance(costs, Mapping) or callable(costs)):
            raise TypeError("costs must be a mapping, a callable, or None")
        if not isinstance(locked, (str, Sequence, set, frozenset)):
            raise TypeError("locked must be a collection of field names")

        self.exclusive = bool(exclusive)
        self.on_rule_error = on_rule_error
        self.max_combinations = max_combinations
        self.default_cost = _as_cost(default_cost, "default_cost", None)
        self.evaluator = EvalWithCompoundTypes() if evaluator is None else evaluator
        self.derived = _normalize_derived(derived)
        self.locked = frozenset((locked,) if isinstance(locked, str) else locked)
        self.candidates = {}
        self._candidate_costs: dict[str, dict[Any, Any]] = {}
        self._candidate_order: list[str] = []

        for field_name, values in candidates.items():
            if not isinstance(field_name, str):
                raise TypeError("candidate field names must be strings")
            if isinstance(values, Mapping):
                # Mapping form: candidate value -> its change cost.
                self._candidate_costs[field_name] = dict(values)
                values = tuple(values)
            elif isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
                raise TypeError(f"candidates for {field_name!r} must be an iterable")
            values = tuple(values)
            self.candidates[field_name] = values
            self._candidate_order.append(field_name)

        derived_names = set(self.derived)
        derived_locked = derived_names.intersection(self.candidates)
        if derived_locked:
            raise ValueError(
                "derived columns cannot also be editable fields: "
                + ", ".join(sorted(derived_locked))
            )
        for field_name in self.locked:
            if not isinstance(field_name, str):
                raise TypeError("locked field names must be strings")
        for name in derived_names:
            if name in self.locked:
                raise ValueError(f"derived column {name!r} cannot be locked")

        self.costs = costs
        self.rules: dict[str, tuple[tuple, ...]] = _normalize_rules(rules, self.evaluator)
        if not self.rules:
            raise ValueError("at least one classification rule is required")

    # ----- public helpers -------------------------------------------------

    def rebuild(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Return a copy of ``record`` with all derived columns recomputed."""
        if not isinstance(record, Mapping):
            raise TypeError("record must be a mapping")
        rebuilt = dict(record)
        env = dict(rebuilt)
        for name, spec in self.derived.items():
            value = self._evaluate_spec(spec, env, field=name)
            rebuilt[name] = value
            env[name] = value
        return rebuilt

    def categories(self, record: Mapping[str, Any]) -> tuple[str, ...]:
        """Return every category whose rules match the record, in rule order.

        Derived columns are recomputed first.  A rule expression that cannot
        be evaluated counts as non-matching when ``on_rule_error='ignore'``.
        """
        rebuilt = self.rebuild(record)
        env = dict(rebuilt)
        matched = []
        for category, predicates in self.rules.items():
            if all(self._predicate_matches(predicate, env) for predicate in predicates):
                matched.append(category)
        return tuple(matched)

    def classify(self, record: Mapping[str, Any]) -> str | None:
        """Return the record's single category, or ``None`` if ambiguous."""
        categories = self.categories(record)
        return categories[0] if len(categories) == 1 else None

    def solve(self, record: Mapping[str, Any], target: str) -> FixPlan:
        """Return all lowest-cost edits that make ``record`` match ``target``.

        The input record is never mutated.  ``FixPlan.status`` is
        ``'solved'`` when one or more solutions exist, otherwise
        ``'no_solution'``.
        """
        if not isinstance(record, Mapping):
            raise TypeError("record must be a mapping")
        if target not in self.rules:
            raise UnknownCategory(target)

        original = dict(record)
        initial_categories = self.categories(original)
        domains = [
            self._build_domain(field_name, original)
            for field_name in self._candidate_order
            if field_name not in self.locked
        ]

        best_cost: Fraction | None = None
        solutions: list[FixSolution] = []
        searched = 0
        selected: list[tuple[str, Any]] = []

        def visit(index: int, running_cost: Fraction):
            nonlocal best_cost, searched

            if best_cost is not None and running_cost > best_cost:
                return
            if index == len(domains):
                searched += 1
                if searched > self.max_combinations:
                    raise PlanningLimitExceeded(
                        "Classification candidate space exceeds "
                        f"max_combinations={self.max_combinations}"
                    )
                rebuilt = self._rebuild_with_changes(original, selected)
                if not self._matches_target(rebuilt, target):
                    return
                if best_cost is None or running_cost < best_cost:
                    best_cost = running_cost
                    solutions.clear()
                if running_cost == best_cost:
                    solutions.append(
                        FixSolution(
                            changes=dict(selected),
                            record=rebuilt,
                            cost=running_cost,
                        )
                    )
                return

            field_name, options = domains[index]
            for value, cost, changed in options:
                if changed:
                    selected.append((field_name, value))
                visit(index + 1, running_cost + cost)
                if changed:
                    selected.pop()

        visit(0, Fraction(0))

        if not solutions:
            return FixPlan(
                target=target,
                solutions=(),
                cost=None,
                searched=searched,
                initial_categories=initial_categories,
                status="no_solution",
            )
        return FixPlan(
            target=target,
            solutions=tuple(solutions),
            cost=best_cost,
            searched=searched,
            initial_categories=initial_categories,
            status="solved",
        )

    # ----- internals ------------------------------------------------------

    def _build_domain(self, field_name: str, original: dict[str, Any]):
        values = self.candidates[field_name]
        present = field_name in original
        original_value = original[field_name] if present else _PLAN_ABSENT

        options = [(original_value, Fraction(0), False)]
        for value in values:
            if present and _values_equal(value, original_value):
                continue
            if any(_values_equal(value, existing) for existing, _, _ in options):
                continue
            options.append((value, self._field_cost(field_name, value), True))
        return field_name, options

    def _field_cost(self, field_name: str, value: Any) -> Fraction:
        candidate_costs = self._candidate_costs.get(field_name)
        if candidate_costs is not None:
            for key, raw_cost in candidate_costs.items():
                if _values_equal(key, value):
                    return _as_cost(raw_cost, field_name, value)
        if self.costs is None:
            return self.default_cost
        if callable(self.costs):
            return _as_cost(self.costs(field_name, value), field_name, value)
        if field_name not in self.costs:
            return self.default_cost
        spec = self.costs[field_name]
        if isinstance(spec, Mapping):
            for key, raw_cost in spec.items():
                if _values_equal(key, value):
                    return _as_cost(raw_cost, field_name, value)
            return self.default_cost
        return _as_cost(spec, field_name, value)

    def _rebuild_with_changes(
        self, original: dict[str, Any], selected: list[tuple[str, Any]]
    ) -> dict[str, Any]:
        changed = dict(original)
        for field_name, value in selected:
            if value is _PLAN_ABSENT:
                changed.pop(field_name, None)
            else:
                changed[field_name] = value
        return self.rebuild(changed)

    def _matches_target(self, record: Mapping[str, Any], target: str) -> bool:
        env = dict(record)
        matched = [
            category
            for category, predicates in self.rules.items()
            if all(self._predicate_matches(predicate, env) for predicate in predicates)
        ]
        if target not in matched:
            return False
        return not self.exclusive or matched == [target]

    def _predicate_matches(self, predicate, env: dict[str, Any]) -> bool:
        kind, payload = predicate
        if kind == "call":
            try:
                return bool(payload(env))
            except Exception:
                if self.on_rule_error == "raise":
                    raise
                return False
        try:
            self.evaluator.names = env
            return bool(self.evaluator.eval(payload, previously_parsed=payload))
        except Exception:
            if self.on_rule_error == "raise":
                raise
            return False

    def _evaluate_spec(self, spec, env: dict[str, Any], field: str):
        kind, payload = spec
        if kind == "call":
            return payload(env)
        self.evaluator.names = env
        return self.evaluator.eval(payload, previously_parsed=payload)


def _values_equal(left: Any, right: Any) -> bool:
    if left is right:
        return True
    if left is _PLAN_ABSENT or right is _PLAN_ABSENT:
        return False
    try:
        result = left == right
    except Exception:
        return False
    try:
        return bool(result)
    except Exception:
        return False


def _as_cost(raw: Any, field_name: str, value: Any) -> Fraction:
    label = f"cost for {field_name!r}"
    if value is not None:
        label += f" value {value!r}"
    if isinstance(raw, bool) or not isinstance(raw, (int, float, Decimal, Fraction)):
        raise TypeError(f"{label} must be a number, got {type(raw).__name__}")
    if isinstance(raw, float):
        if not math.isfinite(raw):
            raise ValueError(f"{label} must be finite")
        result = Fraction(Decimal(str(raw)))
    elif isinstance(raw, Decimal):
        if not raw.is_finite():
            raise ValueError(f"{label} must be finite")
        result = Fraction(raw)
    else:
        result = Fraction(raw)
    if result < 0:
        raise ValueError(f"{label} must not be negative")
    return result


def _normalize_derived(derived) -> dict[str, tuple]:
    if derived is None:
        return {}
    if not isinstance(derived, Mapping):
        raise TypeError("derived must be a mapping of name -> expression")
    result = {}
    for name, spec in derived.items():
        if not isinstance(name, str):
            raise TypeError("derived column names must be strings")
        result[name] = _compile_spec(spec, f"derived column {name!r}")
    return result


def _normalize_rules(rules, evaluator) -> dict[str, tuple[tuple, ...]]:
    if isinstance(rules, Mapping):
        items = list(rules.items())
    elif isinstance(rules, Iterable):
        items = list(rules)
    else:
        raise TypeError("rules must be a mapping or an iterable of (category, rule) pairs")

    normalized = {}
    for item in items:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise TypeError("each rule must be a (category, expression) pair")
        category, spec = item
        if not isinstance(category, str):
            raise TypeError("category names must be strings")
        if category in normalized:
            raise ValueError(f"duplicate category: {category!r}")
        if _is_compiled_spec(spec):
            specs = [spec]
        elif isinstance(spec, (str, ast.AST)) or callable(spec):
            specs = [spec]
        elif isinstance(spec, Iterable):
            specs = list(spec)
        else:
            raise TypeError(f"rule for {category!r} must be an expression or iterable")
        if not specs:
            raise ValueError(f"rule for {category!r} must contain at least one expression")
        normalized[category] = tuple(_compile_spec(one, f"rule for {category!r}") for one in specs)
    return normalized


def _is_compiled_spec(spec) -> bool:
    return isinstance(spec, tuple) and len(spec) == 2 and spec[0] in ("expr", "call")


def _compile_spec(spec, label: str) -> tuple:
    if _is_compiled_spec(spec):
        return spec
    if callable(spec):
        return ("call", spec)
    if not isinstance(spec, (str, ast.AST)):
        raise TypeError(f"{label} must be an expression string or callable")
    node: ast.AST
    if isinstance(spec, str):
        try:
            parsed = ast.parse(spec.strip())
        except SyntaxError as exc:
            raise InvalidExpression(f"{label} has invalid syntax: {exc.msg}") from exc
        if not parsed.body:
            raise InvalidExpression(f"{label} is empty")
        if len(parsed.body) > 1:
            raise InvalidExpression(f"{label} must be a single expression")
        node = parsed.body[0]
    elif isinstance(spec, ast.Expression):
        node = spec.body
    elif isinstance(spec, ast.Module):
        if not spec.body:
            raise InvalidExpression(f"{label} is empty")
        if len(spec.body) > 1:
            raise InvalidExpression(f"{label} must be a single expression")
        node = spec.body[0]
    else:
        node = spec
    if isinstance(node, ast.Expr):
        node = node.value
    if not isinstance(node, ast.expr):
        raise InvalidExpression(f"{label} must be a single expression")
    return ("expr", node)
