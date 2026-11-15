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
import operator as op
import os
import sys
import types
import warnings
from dataclasses import dataclass, field
from fractions import Fraction
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
# Static unit / dimension checking
#
# The evaluator itself deliberately knows nothing about units: at runtime a
# quantity is just a number.  The checker below is a separate, static pass over
# the same AST.  It never evaluates a field or a function, so it can be run
# before a derived metric is published, even when no real trade values are
# available.

_MISSING_ALIASES = {
    "missing",
    "none",
    "null",
    "nil",
    "nan",
    "n/a",
    "na",
    "缺值",
    "缺失",
    "空值",
    "无值",
}

_DIMENSIONLESS_ALIASES = {
    "",
    "1",
    "dimensionless",
    "unitless",
    "scalar",
    "bool",
    "boolean",
    "无量纲",
    "纯数",
    "倍数",
    "倍",
    "比例",
    "百分比",
    "%",
    "pct",
    "percent",
}

# (dimension, canonical label).  The label supplied by the caller is retained;
# these aliases only decide which dimension names are compatible with one
# another (CNY vs USD, 件 vs item, etc).
_UNIT_ALIASES = {
    # money / amount
    "cny": ("currency", "CNY"),
    "rmb": ("currency", "CNY"),
    "usd": ("currency", "USD"),
    "eur": ("currency", "EUR"),
    "jpy": ("currency", "JPY"),
    "gbp": ("currency", "GBP"),
    "hkd": ("currency", "HKD"),
    "元": ("currency", "元"),
    "块": ("currency", "元"),
    "人民币": ("currency", "元"),
    "美元": ("currency", "美元"),
    "欧元": ("currency", "欧元"),
    "日元": ("currency", "日元"),
    "港币": ("currency", "港币"),
    "金额": ("currency", "金额"),
    "交易金额": ("currency", "金额"),
    "成交金额": ("currency", "金额"),
    "amount": ("currency", "amount"),
    "amt": ("currency", "amount"),
    "money": ("currency", "money"),
    "currency": ("currency", "currency"),
    "value": ("currency", "value"),
    "万元": ("currency", "万元"),
    "千元": ("currency", "千元"),
    # counts / quantities
    "件": ("count", "件"),
    "件数": ("count", "件"),
    "个": ("count", "个"),
    "笔": ("count", "笔"),
    "笔数": ("count", "笔"),
    "条": ("count", "条"),
    "张": ("count", "张"),
    "单": ("count", "单"),
    "单数": ("count", "单"),
    "订单": ("count", "订单"),
    "订单数": ("count", "订单"),
    "交易": ("count", "交易"),
    "交易数": ("count", "交易"),
    "数量": ("count", "数量"),
    "count": ("count", "count"),
    "quantity": ("count", "quantity"),
    "qty": ("count", "qty"),
    "item": ("count", "item"),
    "items": ("count", "item"),
    "unit": ("count", "item"),
    "units": ("count", "item"),
    "pcs": ("count", "pcs"),
    "pc": ("count", "pc"),
    # people
    "人": ("person", "人"),
    "人数": ("person", "人"),
    "person": ("person", "person"),
    "people": ("person", "person"),
    # SI-ish bases
    "m": ("length", "m"),
    "meter": ("length", "m"),
    "metre": ("length", "m"),
    "米": ("length", "米"),
    "km": ("length", "km"),
    "cm": ("length", "cm"),
    "mm": ("length", "mm"),
    "kg": ("mass", "kg"),
    "g": ("mass", "g"),
    "gram": ("mass", "g"),
    "克": ("mass", "克"),
    "千克": ("mass", "kg"),
    "公斤": ("mass", "kg"),
    "吨": ("mass", "吨"),
    "t": ("mass", "t"),
    "s": ("time", "s"),
    "sec": ("time", "s"),
    "second": ("time", "s"),
    "秒": ("time", "秒"),
    "min": ("time", "min"),
    "minute": ("time", "min"),
    "分钟": ("time", "分钟"),
    "h": ("time", "h"),
    "hr": ("time", "h"),
    "hour": ("time", "h"),
    "小时": ("time", "小时"),
    "d": ("time", "d"),
    "day": ("time", "d"),
    "天": ("time", "天"),
    "周": ("time", "周"),
    "月": ("time", "月"),
    "年": ("time", "年"),
    "a": ("current", "A"),
    "ampere": ("current", "A"),
    "安": ("current", "安"),
    "k": ("temperature", "K"),
    "kelvin": ("temperature", "K"),
    "开": ("temperature", "开"),
    "mol": ("substance", "mol"),
    "摩尔": ("substance", "mol"),
    "cd": ("luminosity", "cd"),
    "candela": ("luminosity", "cd"),
    "rad": ("angle", "rad"),
    "radian": ("angle", "rad"),
    "弧度": ("angle", "弧度"),
    "byte": ("data", "byte"),
    "bytes": ("data", "byte"),
    "b": ("data", "B"),
    "kb": ("data", "KB"),
    "mb": ("data", "MB"),
    "gb": ("data", "GB"),
    "tb": ("data", "TB"),
    "字节": ("data", "字节"),
    "bit": ("data", "bit"),
    "bits": ("data", "bit"),
}

_UNIT_CACHE: dict[str, Unit] = {}


def _unit_alias(label):
    return _UNIT_ALIASES.get(label.strip().casefold(), _UNIT_ALIASES.get(label.strip()))


# Dimension names themselves are accepted in ``{dimension: exponent}`` maps.
_DIMENSION_LABELS = {
    "currency": "currency",
    "count": "count",
    "person": "person",
    "length": "length",
    "mass": "mass",
    "time": "time",
    "current": "current",
    "temperature": "temperature",
    "substance": "substance",
    "luminosity": "luminosity",
    "angle": "angle",
    "data": "data",
}


def _normalise_dimension(dimension):
    return dimension.strip().casefold()


def _dedupe_units(units):
    result = []
    for unit in units:
        if unit.is_missing:
            continue
        if not any(unit == existing for existing in result):
            result.append(unit)
    return tuple(result)


def _merge_labels(units):
    labels = {}
    for unit in units:
        for dimension, label in unit._labels:
            labels.setdefault(dimension, label)
    return tuple(sorted(labels.items()))


class Unit:
    """An immutable unit (a product of dimensions).

    A unit is stored as a mapping of dimension -> exponent.  ``Unit("CNY")``
    and ``Unit("USD")`` are therefore compatible (both are currency), while
    ``Unit("CNY")`` and ``Unit("件")`` are not.  ``Unit("CNY/件")`` is a
    compound unit with currency^1 * count^-1.

    ``Unit.parse(None)`` / ``Unit.parse("缺值")`` produce the special missing
    unit, which is allowed to merge with any unit without changing it.
    """

    __slots__ = ("_powers", "_labels", "missing", "unknown")

    def __init__(self, powers=(), labels=(), missing=False, unknown=False):
        if isinstance(powers, (str, int, float, complex)) or (
            powers is None and not labels and not missing and not unknown
        ):
            parsed = type(self).parse(powers)
            powers = parsed._powers
            labels = parsed._labels
            missing = parsed.missing
            unknown = parsed.unknown
        elif isinstance(powers, dict):
            parsed = type(self)._from_dimensions(powers)
            powers = parsed._powers
            labels = parsed._labels
        self._powers = tuple(sorted(powers))
        self._labels = tuple(sorted(labels))
        self.missing = bool(missing)
        self.unknown = bool(unknown)

    @classmethod
    def _from_dimensions(cls, dimensions):
        """Build a unit from a ``{dimension: exponent}`` mapping."""
        powers = []
        labels = []
        for dimension, exponent in dimensions.items():
            exponent = Fraction(exponent)
            if exponent == 0:
                continue
            dimension = str(dimension)
            alias = _unit_alias(dimension)
            if alias is not None:
                canonical_dimension, label = alias
            elif dimension.strip().casefold() in _DIMENSION_LABELS:
                canonical_dimension = dimension.strip().casefold()
                label = _DIMENSION_LABELS[canonical_dimension]
            else:
                canonical_dimension, label = "base:" + _normalise_dimension(dimension), dimension
            powers.append((canonical_dimension, exponent))
            labels.append((canonical_dimension, label))
        return cls(tuple(powers), tuple(labels))

    # -- constructors -------------------------------------------------
    @classmethod
    def parse(cls, value):
        if isinstance(value, Unit):
            return value
        if value is None:
            return cls.missing_unit()
        if isinstance(value, bool):
            return cls.dimensionless()
        if isinstance(value, (int, float, complex)):
            return cls.dimensionless()
        if not isinstance(value, str):
            return cls.unknown_unit()

        text = value.strip()
        if text in _UNIT_CACHE:
            return _UNIT_CACHE[text]

        folded = text.casefold()
        if folded in _MISSING_ALIASES:
            unit = cls.missing_unit()
        elif folded in _DIMENSIONLESS_ALIASES:
            unit = cls.dimensionless()
        else:
            try:
                unit = _UnitParser(text).parse()
            except (ValueError, ZeroDivisionError):
                unit = cls.unknown_unit()

        _UNIT_CACHE[text] = unit
        return unit

    @classmethod
    def dimensionless(cls):
        return cls()

    @classmethod
    def missing_unit(cls):
        if "<missing>" not in _UNIT_CACHE:
            _UNIT_CACHE["<missing>"] = cls(missing=True)
        return _UNIT_CACHE["<missing>"]

    @classmethod
    def unknown_unit(cls):
        if "<unknown>" not in _UNIT_CACHE:
            _UNIT_CACHE["<unknown>"] = cls(unknown=True)
        return _UNIT_CACHE["<unknown>"]

    @classmethod
    def boolean(cls):
        return cls.dimensionless()

    @classmethod
    def _from_dimension(cls, dimension, label, exponent=Fraction(1)):
        return cls(((dimension, exponent),), ((dimension, label),))

    # -- properties ---------------------------------------------------
    @property
    def dimensions(self):
        return dict(self._powers)

    @property
    def powers(self):
        return self._powers

    @property
    def label(self):
        return str(self)

    @property
    def is_missing(self):
        return self.missing

    @property
    def is_unknown(self):
        return self.unknown

    @property
    def is_dimensionless(self):
        return not (self.missing or self.unknown) and not self._powers

    @property
    def is_compound(self):
        return not (self.missing or self.unknown) and len(self._powers) > 1

    @property
    def is_currency(self):
        return self.dimensions.get("currency", 0) == 1 and len(self._powers) == 1

    @property
    def is_count(self):
        return self.dimensions.get("count", 0) == 1 and len(self._powers) == 1

    # -- compatibility / algebra --------------------------------------
    def compatible(self, other):
        if self.missing or other.missing or self.unknown or other.unknown:
            return True
        return self._powers == other._powers

    def equivalent(self, other):
        """True when both the dimensions and the display labels match."""
        if self.missing or other.missing:
            return self.missing and other.missing
        if self.unknown or other.unknown:
            return self.unknown and other.unknown
        return (
            self._powers == other._powers
            and self.label_for_all_dimensions() == other.label_for_all_dimensions()
        )

    def label_for_all_dimensions(self):
        labels = dict(self._labels)
        return tuple(
            (dimension, labels.get(dimension, dimension)) for dimension, _ in self._powers
        )

    def with_right_labels(self, other):
        """Keep dimensions, but display using the labels of ``other``."""
        return Unit(self._powers, _merge_labels((self, other)), self.missing, self.unknown)

    def _with_labels_from(self, other):
        if self.compatible(other):
            return self.with_right_labels(other)
        return self

    def multiply(self, other):
        if self.missing:
            return other
        if other.missing:
            return self
        if self.unknown or other.unknown:
            return Unit.unknown_unit()
        powers = dict(self._powers)
        for dimension, exponent in other._powers:
            powers[dimension] = powers.get(dimension, Fraction(0)) + exponent
        powers = {d: e for d, e in powers.items() if e != 0}
        labels = dict(_merge_labels((self, other)))
        return Unit(tuple(powers.items()), tuple(labels.items()))

    def divide(self, other):
        return self.multiply(other.power(Fraction(-1)))

    def power(self, exponent):
        if self.missing:
            return self
        if self.unknown:
            return self
        exponent = Fraction(exponent)
        if exponent == 0:
            return Unit.dimensionless()
        powers = {d: e * exponent for d, e in self._powers}
        return Unit(
            tuple((d, e) for d, e in powers.items() if e != 0),
            self._labels,
        )

    # -- dunder -------------------------------------------------------
    def __mul__(self, other):
        return self.multiply(Unit.parse(other))

    def __truediv__(self, other):
        return self.divide(Unit.parse(other))

    def __pow__(self, exponent):
        return self.power(exponent)

    def __eq__(self, other):
        if isinstance(other, str):
            other = Unit.parse(other)
        if not isinstance(other, Unit):
            return NotImplemented
        return self.equivalent(other)

    def __hash__(self):
        return hash((self.missing, self.unknown, self._powers))

    def __bool__(self):
        return not (self.missing or self.unknown)

    def __repr__(self):
        return f"Unit({str(self)!r})"

    def __str__(self):
        if self.missing:
            return "缺值"
        if self.unknown:
            return "unknown"
        if not self._powers:
            return "1"

        labels = dict(self._labels)
        numerator = []
        denominator = []
        for dimension, exponent in sorted(self._powers, key=_dimension_order):
            label = labels.get(dimension, dimension)
            if exponent > 0:
                numerator.append(_format_power(label, exponent))
            else:
                denominator.append(_format_power(label, -exponent))

        left = "·".join(numerator) if numerator else "1"
        if not denominator:
            return left
        right = "·".join(denominator)
        return f"{left}/{right}"


def _dimension_order(item):
    dimension, exponent = item
    priority = {"currency": 0, "count": 1, "person": 2, "time": 3, "length": 4, "mass": 5}
    return (priority.get(dimension, 10), dimension, exponent)


def _format_power(label, exponent):
    exponent = Fraction(exponent)
    if exponent == 1:
        return label
    if exponent.denominator == 1:
        return f"{label}^{exponent.numerator}"
    return f"{label}^{exponent.numerator}/{exponent.denominator}"


class _UnitParser:
    """Small recursive-descent parser for unit expressions."""

    _OPERATORS = {"*", "·", "×", "/", "÷", "^"}

    def __init__(self, text):
        self.tokens = self._tokenise(text)
        self.index = 0

    @staticmethod
    def _tokenise(text):
        tokens = []
        index = 0
        while index < len(text):
            char = text[index]
            if char.isspace():
                index += 1
                continue
            if text.startswith("**", index):
                tokens.append("^")
                index += 2
                continue
            if char in "*/^()·×÷":
                tokens.append(char)
                index += 1
                continue
            start = index
            while index < len(text):
                if text.startswith("**", index):
                    break
                if text[index] in "*/^()·×÷" or text[index].isspace():
                    break
                index += 1
            tokens.append(text[start:index])
        return tokens

    def _peek(self):
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def _take(self):
        token = self._peek()
        self.index += 1
        return token

    def parse(self):
        unit = self._parse_expression()
        if self._peek() is not None:
            raise ValueError(f"Unexpected token {self._peek()!r}")
        return unit

    def _parse_expression(self):
        unit = self._parse_factor()
        while True:
            token = self._peek()
            if token in ("*", "·", "×"):
                self._take()
                unit = unit.multiply(self._parse_factor())
            elif token in ("/", "÷") or (isinstance(token, str) and token.casefold() == "per"):
                self._take()
                unit = unit.divide(self._parse_factor())
            else:
                return unit

    def _parse_factor(self):
        token = self._take()
        if token is None:
            raise ValueError("Unexpected end of unit")
        if token == "(":
            unit = self._parse_expression()
            if self._take() != ")":
                raise ValueError("Unclosed parenthesis in unit")
        elif token in self._OPERATORS or token == ")":
            raise ValueError(f"Unexpected token {token!r}")
        elif self._is_number(token):
            unit = Unit.dimensionless()
        else:
            alias = _unit_alias(token)
            if alias is None:
                dimension = "base:" + _normalise_dimension(token)
                unit = Unit._from_dimension(dimension, token)
            else:
                unit = Unit._from_dimension(alias[0], alias[1])

        if self._peek() == "^":
            self._take()
            if self._peek() == "(":
                self._take()
                numerator_token = self._take()
                if not self._is_number(numerator_token):
                    raise ValueError("Unit exponent must be a number")
                if self._peek() == "/":
                    self._take()
                    denominator_token = self._take()
                    if not self._is_number(denominator_token):
                        raise ValueError("Unit exponent must be a number")
                    exponent = Fraction(numerator_token) / Fraction(denominator_token)
                else:
                    exponent = Fraction(numerator_token)
                if self._take() != ")":
                    raise ValueError("Unclosed exponent parenthesis in unit")
                unit = unit.power(exponent)
            else:
                exponent_token = self._take()
                if exponent_token is None or not self._is_number(exponent_token):
                    raise ValueError("Unit exponent must be a number")
                unit = unit.power(Fraction(exponent_token))
        return unit

    @staticmethod
    def _is_number(token):
        try:
            Fraction(token)
        except (ValueError, ZeroDivisionError):
            return False
        return True


class UnitFunction:
    """Unit signature for a function used by an expression.

    ``UnitFunction("CNY")`` says that the function returns an amount, but says
    nothing about its parameters.  ``UnitFunction("CNY", ["CNY", "件"])`` also
    requires the first argument to be an amount and the second a count.
    """

    __slots__ = ("returns", "parameters", "parameter_names", "variadic")

    def __init__(
        self,
        returns,
        parameters=(),
        parameter_names=(),
        variadic=None,
    ):
        self.returns = Unit.parse(returns)
        self.parameters = tuple(Unit.parse(item) for item in parameters)
        self.parameter_names = tuple(parameter_names)
        self.variadic = Unit.parse(variadic) if variadic is not None else None
        if self.parameter_names and len(self.parameter_names) != len(self.parameters):
            raise ValueError("parameter_names must match parameters")

    @property
    def return_unit(self):
        return self.returns

    @property
    def params(self):
        return self.parameters

    def expected(self, index):
        if index < len(self.parameters):
            return self.parameters[index]
        return self.variadic

    def expected_keyword(self, name):
        if name in self.parameter_names:
            return self.parameters[self.parameter_names.index(name)]
        return self.variadic

    def __repr__(self):
        return (
            f"UnitFunction({str(self.returns)!r}, "
            f"parameters={[str(p) for p in self.parameters]!r})"
        )


FunctionSignature = UnitFunction


@dataclass(frozen=True)
class UnitConflict:
    """A location where one expression requires incompatible units."""

    code: str
    message: str
    line: int
    column: int
    left: Unit
    right: Unit
    node: ast.AST | None = field(default=None, compare=False, repr=False)
    end_line: int | None = None
    end_column: int | None = None
    source: str | None = None
    left_line: int | None = None
    left_column: int | None = None
    left_source: str | None = None
    right_line: int | None = None
    right_column: int | None = None
    right_source: str | None = None

    @property
    def position(self):
        return (self.line, self.column)

    @property
    def units(self):
        return (self.left, self.right)

    @property
    def left_position(self):
        if self.left_line is None:
            return None
        return (self.left_line, self.left_column)

    @property
    def right_position(self):
        if self.right_line is None:
            return None
        return (self.right_line, self.right_column)

    def __str__(self):
        return self.message


@dataclass(frozen=True)
class ExcludedBranch:
    """A branch that cannot execute, with the reason it was excluded."""

    code: str
    reason: str
    line: int
    column: int
    node: ast.AST | None = field(default=None, compare=False, repr=False)
    end_line: int | None = None
    end_column: int | None = None
    source: str | None = None

    @property
    def position(self):
        return (self.line, self.column)

    def __str__(self):
        return self.reason


@dataclass(frozen=True)
class UnknownName:
    """A field/function name without a declared unit."""

    kind: str
    name: str
    line: int
    column: int
    node: ast.AST | None = field(default=None, compare=False, repr=False)

    @property
    def position(self):
        return (self.line, self.column)

    def __str__(self):
        return f"{self.kind} '{self.name}' has no declared unit"


@dataclass
class UnitReport:
    """Result of a static unit check."""

    expression: str
    result_unit: Unit
    possible_units: tuple[Unit, ...] = ()
    conflicts: list[UnitConflict] = field(default_factory=list)
    excluded_branches: list[ExcludedBranch] = field(default_factory=list)
    unknown_names: list[UnknownName] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    may_be_missing: bool = False
    parse_error: str | None = None

    @property
    def ok(self):
        return not self.conflicts and self.parse_error is None

    @property
    def valid(self):
        return self.ok

    @property
    def is_valid(self):
        return self.ok

    @property
    def unit(self):
        return self.result_unit

    @property
    def result(self):
        return self.result_unit

    @property
    def result_units(self):
        return self.possible_units

    @property
    def errors(self):
        return self.conflicts

    @property
    def excluded(self):
        return self.excluded_branches

    @property
    def viable_units(self):
        return self.possible_units

    @property
    def can_be_missing(self):
        return self.may_be_missing

    @property
    def missing_branch(self):
        return self.may_be_missing

    def raise_for_conflicts(self):
        if self.parse_error is not None:
            raise InvalidExpression(self.parse_error)
        if self.conflicts:
            raise IncompatibleUnits(self)
        return self.result_unit

    def __bool__(self):
        return self.ok

    def __str__(self):
        lines = [f"result unit: {self.result_unit}"]
        if self.may_be_missing:
            lines.append("may be missing (缺值)")
        if self.possible_units and (
            len(self.possible_units) > 1 or self.possible_units[0] != self.result_unit
        ):
            lines.append("possible units: " + ", ".join(str(u) for u in self.possible_units))
        if self.conflicts:
            lines.append("conflicts:")
            lines.extend(f"  {conflict.message}" for conflict in self.conflicts)
        if self.excluded_branches:
            lines.append("excluded branches:")
            lines.extend(f"  {item.reason}" for item in self.excluded_branches)
        if self.unknown_names:
            lines.append("undeclared names:")
            lines.extend(f"  {item}" for item in self.unknown_names)
        if self.notes:
            lines.append("notes:")
            lines.extend(f"  {note}" for note in self.notes)
        if self.parse_error is not None:
            lines.append(f"parse error: {self.parse_error}")
        return "\n".join(lines)


class IncompatibleUnits(InvalidExpression):
    """Raised by ``UnitReport.raise_for_conflicts`` when units conflict."""

    def __init__(self, report):
        self.report = report
        super().__init__(str(report))


@dataclass
class _Inferred:
    """Units an expression may evaluate to.

    ``units`` deliberately excludes the missing unit.  A missing value is
    tracked separately, because it is a branch-level possibility and does not
    itself carry a unit.  ``neutral`` marks a bare numeric literal, which acts
    as a scalar multiplier/threshold and adopts the unit of the other operand
    rather than forcing a dimensionless conflict.
    """

    units: tuple[Unit, ...] = ()
    may_be_missing: bool = False
    neutral: bool = False

    @classmethod
    def missing(cls):
        return cls((), True)

    @classmethod
    def scalar(cls):
        return cls((), False, True)

    @classmethod
    def of(cls, unit):
        if unit.is_missing:
            return cls.missing()
        return cls((unit,), False)

    @property
    def has_units(self):
        return bool(self.units)

    @property
    def primary(self):
        return _preferred_unit(self.units)


def _preferred_unit(units):
    concrete = [unit for unit in units if not unit.unknown]
    if not concrete:
        return units[0] if units else Unit.unknown_unit()
    preferred = concrete[0]
    labels = _merge_labels(concrete)
    return Unit(preferred._powers, labels)


class UnitChecker:
    """Statically infer units for an expression without using real values.

    Parameters
    ----------
    names:
        Mapping of field name (or dotted path) to its unit.  Values may be
        ``Unit`` objects, unit strings, or a nested mapping.  ``None`` and
        ``"缺值"`` mean the field has no value.
    functions:
        Mapping of function name to a return unit, or to a ``UnitFunction``
        describing parameter units as well.
    values:
        Optional mapping of compile-time values for conditions.  These are
        values that are known independently of today's samples, not a sample
        data set.  Literal conditions (``False``, ``1 == 2``, ...) are folded
        without any help from this argument.
    strict:
        When true, undeclared names also count as errors (in addition to
        incompatible units).  Defaults to false.
    """

    _BINARY_CONSTANTS = {
        ast.Add: op.add,
        ast.Sub: op.sub,
        ast.Mult: op.mul,
        ast.Div: op.truediv,
        ast.FloorDiv: op.floordiv,
        ast.Mod: op.mod,
        ast.Pow: op.pow,
        ast.LShift: op.lshift,
        ast.RShift: op.rshift,
        ast.BitOr: op.or_,
        ast.BitAnd: op.and_,
        ast.BitXor: op.xor,
    }

    _COMPARE_CONSTANTS = {
        ast.Eq: op.eq,
        ast.NotEq: op.ne,
        ast.Lt: op.lt,
        ast.LtE: op.le,
        ast.Gt: op.gt,
        ast.GtE: op.ge,
        ast.Is: op.is_,
        ast.IsNot: op.is_not,
        ast.In: lambda left, right: op.contains(right, left),
        ast.NotIn: lambda left, right: not op.contains(right, left),
    }

    def __init__(self, names=None, functions=None, values=None, strict=False):
        self.names = names or {}
        self.functions = functions or {}
        self.values = values or {}
        self.strict = strict
        self.expression = ""
        self._conflicts = []
        self._excluded = []
        self._unknown = []
        self._notes = []
        self._seen_conflicts = set()
        self._seen_unknown = set()
        self._function_specs = {}
        for name, spec in self.functions.items():
            self._function_specs[name] = self._coerce_function(spec)

    # -- public API ---------------------------------------------------
    def check(self, expression, previously_parsed=None):
        """Check ``expression`` and return a :class:`UnitReport`."""
        self.expression = expression
        self._conflicts = []
        self._excluded = []
        self._unknown = []
        self._notes = []
        self._seen_conflicts = set()
        self._seen_unknown = set()

        try:
            node = previously_parsed or SimpleEval.parse(expression)
        except (SyntaxError, InvalidExpression) as error:
            self._notes.append(str(error))
            return UnitReport(
                expression,
                Unit.unknown_unit(),
                notes=list(self._notes),
                parse_error=str(error),
            )

        inferred = self._infer(node)
        if self._conflicts:
            result_unit = Unit.unknown_unit()
        elif inferred.has_units:
            result_unit = inferred.primary
        elif inferred.neutral:
            result_unit = Unit.dimensionless()
        else:
            result_unit = Unit.missing_unit()
        return UnitReport(
            expression=expression,
            result_unit=result_unit,
            possible_units=_dedupe_units(inferred.units),
            conflicts=list(self._conflicts),
            excluded_branches=list(self._excluded),
            unknown_names=list(self._unknown),
            notes=list(self._notes),
            may_be_missing=inferred.may_be_missing,
        )

    def check_ast(self, node, expression=""):
        """Check an already parsed AST node."""
        self.expression = expression
        return self.check(expression, previously_parsed=node)

    # -- helpers ------------------------------------------------------
    def _location(self, node):
        return (
            getattr(node, "lineno", 0),
            getattr(node, "col_offset", 0),
            getattr(node, "end_lineno", None),
            getattr(node, "end_col_offset", None),
        )

    def _source(self, node):
        if not self.expression:
            return None
        try:
            return ast.get_source_segment(self.expression, node)
        except (AttributeError, ValueError):
            return None

    def _add_conflict(self, node, left, right, code, message, left_node=None, right_node=None):
        line, column, end_line, end_column = self._location(node)
        key = (code, line, column)
        if key in self._seen_conflicts:
            return
        self._seen_conflicts.add(key)
        if left_node is not None and left is Unit.unknown_unit():
            left = self._infer(left_node).primary
        if right_node is not None and right is Unit.unknown_unit():
            right = self._infer(right_node).primary

        left_line = left_column = right_line = right_column = None
        left_source = right_source = None
        if left_node is not None:
            left_line, left_column, _, _ = self._location(left_node)
            left_source = self._source(left_node)
        if right_node is not None:
            right_line, right_column, _, _ = self._location(right_node)
            right_source = self._source(right_node)

        self._conflicts.append(
            UnitConflict(
                code=code,
                message=f"line {line}, column {column}: {message}",
                line=line,
                column=column,
                left=left,
                right=right,
                node=node,
                end_line=end_line,
                end_column=end_column,
                source=self._source(node),
                left_line=left_line,
                left_column=left_column,
                left_source=left_source,
                right_line=right_line,
                right_column=right_column,
                right_source=right_source,
            )
        )

    def _exclude(self, node, code, reason):
        line, column, end_line, end_column = self._location(node)
        self._excluded.append(
            ExcludedBranch(
                code=code,
                reason=f"line {line}, column {column}: {reason}",
                line=line,
                column=column,
                node=node,
                end_line=end_line,
                end_column=end_column,
                source=self._source(node),
            )
        )

    def _unknown_name(self, kind, name, node):
        line, column, _, _ = self._location(node)
        key = (kind, name, line, column)
        if key in self._seen_unknown:
            return
        self._seen_unknown.add(key)
        self._unknown.append(UnknownName(kind, name, line, column, node))
        if self.strict:
            self._add_conflict(
                node,
                Unit.unknown_unit(),
                Unit.unknown_unit(),
                "unknown-" + kind,
                f"{kind} '{name}' has no declared unit",
            )

    def _note(self, message):
        if message not in self._notes:
            self._notes.append(message)

    def _coerce_function(self, spec):
        if isinstance(spec, UnitFunction):
            return spec
        if isinstance(spec, dict):
            returns = (
                spec.get("returns")
                if "returns" in spec
                else spec.get("return", spec.get("unit", spec.get("result", spec.get("返回"))))
            )
            parameters = (
                spec.get("parameters")
                or spec.get("params")
                or spec.get("args")
                or spec.get("参数")
                or ()
            )
            names = spec.get("parameter_names") or spec.get("names") or ()
            variadic = spec.get("variadic") or spec.get("varargs")
            return UnitFunction(returns, parameters, names, variadic)
        if isinstance(spec, (tuple, list)) and len(spec) == 2:
            first, second = spec
            if isinstance(first, (tuple, list)):
                return UnitFunction(second, first)
            if isinstance(second, (tuple, list)):
                return UnitFunction(first, second)
        return UnitFunction(spec)

    def _coerce_declaration(self, value):
        if isinstance(value, Unit):
            return value
        if isinstance(value, dict):
            if value and all(
                isinstance(exponent, (int, float, Fraction)) and not isinstance(exponent, bool)
                for exponent in value.values()
            ):
                return Unit._from_dimensions(value)
            return Unit.unknown_unit()
        if isinstance(value, (list, tuple, set)):
            return Unit.unknown_unit()
        return Unit.parse(value)

    def _lookup_declaration(self, path):
        if not path:
            return None
        if path in self.names:
            return self._coerce_declaration(self.names[path])
        current = self.names
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return self._coerce_declaration(current)

    def _lookup_value(self, path):
        if path in self.values:
            return True, self.values[path]
        current = self.values
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                return False, None
            current = current[part]
        return True, current

    def _path(self, node):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = self._path(node.value)
            if base:
                return f"{base}.{node.attr}"
        return None

    # -- static values ------------------------------------------------
    def _constant(self, node):
        """Return ``(is_known, value)`` for a side-effect-free expression."""
        if isinstance(node, ast.Constant):
            return True, node.value
        if isinstance(node, ast.Name):
            return self._lookup_value(node.id)
        if isinstance(node, ast.Attribute):
            path = self._path(node)
            if path:
                return self._lookup_value(path)
            return False, None
        if isinstance(node, ast.UnaryOp):
            known, value = self._constant(node.operand)
            if not known:
                return False, None
            try:
                if isinstance(node.op, ast.Not):
                    return True, not value
                if isinstance(node.op, ast.USub):
                    return True, -value
                if isinstance(node.op, ast.UAdd):
                    return True, +value
                if isinstance(node.op, ast.Invert):
                    return True, ~value
            except (TypeError, ValueError):
                return False, None
            return False, None
        if isinstance(node, ast.BinOp):
            left_known, left = self._constant(node.left)
            right_known, right = self._constant(node.right)
            if not (left_known and right_known):
                return False, None
            function = self._BINARY_CONSTANTS.get(type(node.op))
            if function is None:
                return False, None
            try:
                if isinstance(node.op, ast.Pow) and abs(right) > 1000:
                    return False, None
                return True, function(left, right)
            except (ArithmeticError, TypeError, ValueError):
                return False, None
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                value = True
                for item in node.values:
                    known, value = self._constant(item)
                    if not known:
                        return False, None
                    if not value:
                        return True, value
                return True, value
            if isinstance(node.op, ast.Or):
                value = False
                for item in node.values:
                    known, value = self._constant(item)
                    if not known:
                        return False, None
                    if value:
                        return True, value
                return True, value
            return False, None
        if isinstance(node, ast.Compare):
            left_known, left = self._constant(node.left)
            if not left_known:
                return False, None
            result = True
            for operator, comparator in zip(node.ops, node.comparators):
                right_known, right = self._constant(comparator)
                if not right_known:
                    return False, None
                function = self._COMPARE_CONSTANTS.get(type(operator))
                if function is None:
                    return False, None
                try:
                    result = function(left, right)
                except (TypeError, ValueError):
                    return False, None
                if not result:
                    return True, False
                left = right
            return True, result
        if isinstance(node, ast.IfExp):
            test_known, test = self._constant(node.test)
            if test_known:
                return self._constant(node.body if test else node.orelse)
            body_known, body = self._constant(node.body)
            else_known, alternative = self._constant(node.orelse)
            if body_known and else_known and body == alternative:
                return True, body
            return False, None
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            values = []
            for element in node.elts:
                known, value = self._constant(element)
                if not known:
                    return False, None
                values.append(value)
            if isinstance(node, ast.Set):
                try:
                    return True, set(values)
                except TypeError:
                    return False, None
            if isinstance(node, ast.Tuple):
                return True, tuple(values)
            return True, list(values)
        if isinstance(node, ast.Dict):
            result = {}
            for key, value in zip(node.keys, node.values):
                if key is None:
                    known, value = self._constant(value)
                    if not known or not isinstance(value, dict):
                        return False, None
                    result.update(value)
                    continue
                key_known, key_value = self._constant(key)
                value_known, value_value = self._constant(value)
                if not (key_known and value_known):
                    return False, None
                result[key_value] = value_value
            return True, result
        if isinstance(node, ast.Subscript):
            base_known, base = self._constant(node.value)
            key_known, key = self._constant(node.slice)
            if not (base_known and key_known):
                return False, None
            try:
                return True, base[key]
            except (KeyError, IndexError, TypeError):
                return False, None
        return False, None

    def _is_zero(self, node):
        known, value = self._constant(node)
        if not known:
            return False
        try:
            return value == 0 and not isinstance(value, bool)
        except (TypeError, ValueError):
            return False

    # -- inference ----------------------------------------------------
    def _infer(self, node):
        if node is None:
            return _Inferred.missing()
        handler = getattr(self, "_infer_" + type(node).__name__.lower(), None)
        if handler is None:
            self._note(f"{type(node).__name__} nodes are not modelled for units")
            return _Inferred.of(Unit.unknown_unit())
        return handler(node)

    def _infer_expr(self, node):
        return self._infer(node.value)

    def _infer_constant(self, node):
        if node.value is None:
            return _Inferred.missing()
        value = node.value
        if isinstance(value, bool):
            return _Inferred.of(Unit.boolean())
        if isinstance(value, (int, float, complex)):
            return _Inferred.scalar()
        if isinstance(value, (str, bytes)):
            return _Inferred.of(Unit.dimensionless())
        return _Inferred.of(Unit.unknown_unit())

    def _infer_num(self, node):
        return _Inferred.scalar()

    def _infer_str(self, node):
        return _Inferred.of(Unit.dimensionless())

    def _infer_name(self, node):
        declaration = self._lookup_declaration(node.id)
        if declaration is not None:
            return _Inferred.of(declaration)
        known, value = self._lookup_value(node.id)
        if known:
            if isinstance(value, Unit):
                return _Inferred.of(value)
            if isinstance(value, bool):
                return _Inferred.of(Unit.boolean())
            if isinstance(value, (int, float, complex)):
                return _Inferred.scalar()
            if isinstance(value, str):
                return _Inferred.of(Unit.parse(value))
            return _Inferred.of(Unit.unknown_unit())
        if node.id in ("True", "False"):
            return _Inferred.of(Unit.boolean())
        if node.id == "None":
            return _Inferred.missing()
        self._unknown_name("name", node.id, node)
        return _Inferred.of(Unit.unknown_unit())

    def _infer_attribute(self, node):
        path = self._path(node)
        if path:
            declaration = self._lookup_declaration(path)
            if declaration is not None:
                return _Inferred.of(declaration)
            known, value = self._lookup_value(path)
            if known:
                if isinstance(value, Unit):
                    return _Inferred.of(value)
                if isinstance(value, bool):
                    return _Inferred.of(Unit.boolean())
                if isinstance(value, (int, float, complex)):
                    return _Inferred.scalar()
                if isinstance(value, str):
                    return _Inferred.of(Unit.parse(value))
                return _Inferred.of(Unit.unknown_unit())
        base = self._infer(node.value)
        if node.attr in ("real", "imag", "numerator", "denominator"):
            if node.attr == "numerator" and base.has_units:
                return base
            if node.attr == "denominator":
                return _Inferred.of(Unit.dimensionless())
            return base
        self._unknown_name("attribute", path or node.attr, node)
        return _Inferred.of(Unit.unknown_unit())

    def _infer_subscript(self, node):
        base = self._infer(node.value)
        self._infer(node.slice)
        return base

    def _infer_slice(self, node):
        if node.lower is not None:
            self._infer(node.lower)
        if node.upper is not None:
            self._infer(node.upper)
        if node.step is not None:
            self._infer(node.step)
        return _Inferred.of(Unit.dimensionless())

    def _infer_index(self, node):
        return self._infer(node.value)

    def _infer_unaryop(self, node):
        operand = self._infer(node.operand)
        if isinstance(node.op, ast.Not):
            return _Inferred.of(Unit.boolean())
        if isinstance(node.op, ast.Invert):
            self._require_dimensionless(node.operand, operand, "~")
            return _Inferred.of(Unit.dimensionless())
        return operand

    def _infer_binop(self, node):
        left = self._infer(node.left)
        right = self._infer(node.right)
        operator = node.op

        if isinstance(operator, (ast.Add, ast.Sub)):
            return self._arithmetic_additive(node, left, right, operator)
        if isinstance(operator, (ast.Mult, ast.Div, ast.FloorDiv)):
            return self._arithmetic_multiplicative(node, left, right, operator)
        if isinstance(operator, ast.Mod):
            return self._arithmetic_mod(node, left, right)
        if isinstance(operator, ast.Pow):
            return self._arithmetic_power(node, left, right)
        if isinstance(operator, (ast.LShift, ast.RShift)):
            self._require_dimensionless(node.left, left, "shift")
            if not right.has_units or all(u.is_dimensionless for u in right.units):
                return left
            self._add_conflict(
                node,
                left.primary,
                right.primary,
                "shift-operand",
                f"shift count must be dimensionless, got '{right.primary}'",
            )
            return left
        if isinstance(operator, (ast.BitAnd, ast.BitOr, ast.BitXor)):
            self._require_dimensionless(node.left, left, "bitwise operation")
            self._require_dimensionless(node.right, right, "bitwise operation")
            return _Inferred.of(Unit.dimensionless())
        self._note(f"operator {type(operator).__name__} is not modelled for units")
        return _Inferred.of(Unit.unknown_unit())

    def _arithmetic_additive(self, node, left, right, operator):
        name = "+" if isinstance(operator, ast.Add) else "-"
        may_missing = left.may_be_missing or right.may_be_missing

        if not left.has_units and not right.has_units:
            return _Inferred((), may_missing, left.neutral and right.neutral)
        if not right.has_units and left.has_units:
            return _Inferred(left.units, may_missing)
        if not left.has_units and right.has_units:
            return _Inferred(right.units, may_missing)

        left_zero = self._is_zero(node.left)
        right_zero = self._is_zero(node.right)
        if left_zero and not right_zero:
            return _Inferred(right.units, may_missing)
        if right_zero and not left_zero:
            return _Inferred(left.units, may_missing)

        result = []
        for left_unit in left.units:
            for right_unit in right.units:
                if left_unit.compatible(right_unit):
                    result.append(left_unit._with_labels_from(right_unit))
                else:
                    self._add_conflict(
                        node,
                        left_unit,
                        right_unit,
                        "additive-units",
                        f"operator '{name}' requires compatible units, "
                        f"got '{left_unit}' and '{right_unit}'",
                        node.left,
                        node.right,
                    )
                    result.extend((left_unit, right_unit))
        return _Inferred(_dedupe_units(result), may_missing)

    def _arithmetic_multiplicative(self, node, left, right, operator):
        may_missing = left.may_be_missing or right.may_be_missing
        is_division = not isinstance(operator, ast.Mult)

        if not left.has_units and not right.has_units:
            neutral = left.neutral and right.neutral
            return _Inferred((), may_missing, neutral)
        if not right.has_units and left.has_units:
            return _Inferred(left.units, may_missing)
        if not left.has_units and right.has_units:
            if is_division and right.units:
                return _Inferred(
                    _dedupe_units(unit.power(Fraction(-1)) for unit in right.units),
                    may_missing,
                )
            return _Inferred(right.units, may_missing)

        result = []
        for left_unit in left.units:
            for right_unit in right.units:
                if isinstance(operator, ast.Mult):
                    result.append(left_unit.multiply(right_unit))
                else:
                    result.append(left_unit.divide(right_unit))
        return _Inferred(_dedupe_units(result), may_missing)

    def _arithmetic_mod(self, node, left, right):
        may_missing = left.may_be_missing or right.may_be_missing
        if not left.has_units:
            return _Inferred((), may_missing)
        if not right.has_units:
            return _Inferred(left.units, may_missing)

        result = []
        for left_unit in left.units:
            for right_unit in right.units:
                if left_unit.compatible(right_unit):
                    result.append(left_unit._with_labels_from(right_unit))
                else:
                    self._add_conflict(
                        node,
                        left_unit,
                        right_unit,
                        "mod-units",
                        f"operator '%' requires compatible units, "
                        f"got '{left_unit}' and '{right_unit}'",
                        node.left,
                        node.right,
                    )
                    result.extend((left_unit, right_unit))
        return _Inferred(_dedupe_units(result), may_missing)

    def _arithmetic_power(self, node, left, right):
        may_missing = left.may_be_missing or right.may_be_missing

        if right.has_units and any(
            not unit.is_dimensionless and not unit.is_unknown for unit in right.units
        ):
            self._add_conflict(
                node,
                right.primary,
                left.primary,
                "exponent-unit",
                f"exponent of '**' must be dimensionless, got '{right.primary}'",
                node.right,
                node.left,
            )
            return _Inferred.of(Unit.unknown_unit())

        if not left.has_units:
            return _Inferred((), may_missing, left.neutral and right.neutral)

        known, exponent = self._constant(node.right)
        if not known:
            if all(unit.is_dimensionless for unit in left.units):
                return _Inferred.of(Unit.dimensionless())
            self._add_conflict(
                node,
                left.primary,
                right.primary,
                "variable-exponent",
                f"cannot infer a unit for '{left.primary}' raised to a variable exponent",
                node.left,
                node.right,
            )
            return _Inferred.of(Unit.unknown_unit())

        try:
            exponent = Fraction(exponent)
        except (TypeError, ValueError, ZeroDivisionError):
            self._note(f"non-numeric exponent {exponent!r} is not modelled for units")
            return _Inferred.of(Unit.unknown_unit())
        return _Inferred(_dedupe_units(unit.power(exponent) for unit in left.units), may_missing)

    def _require_dimensionless(self, node, inferred, description):
        for unit in inferred.units:
            if not unit.is_dimensionless:
                self._add_conflict(
                    node,
                    unit,
                    Unit.dimensionless(),
                    "dimensionless-required",
                    f"{description} requires a dimensionless operand, got '{unit}'",
                )

    def _infer_boolop(self, node):
        is_and = isinstance(node.op, ast.And)
        results = []
        result_nodes = []
        last_index = len(node.values) - 1
        for index, value in enumerate(node.values):
            known, constant = self._constant(value)
            inferred = self._infer(value)
            can_be_true = True
            can_be_false = True
            if known:
                can_be_true = bool(constant)
                can_be_false = not can_be_true

            # `and` returns the first falsy operand, or the last operand if all
            # are truthy.  `or` returns the first truthy operand, or the last
            # operand if all are falsy.  Therefore every operand that can be
            # decisive is a possible result; boolean gate values included.
            if index == last_index:
                results.append(inferred)
                result_nodes.append(value)
            elif is_and and can_be_false:
                results.append(inferred)
                result_nodes.append(value)
            elif (not is_and) and can_be_true:
                results.append(inferred)
                result_nodes.append(value)

            # If this operand is decisive, the later operands can never run.
            if is_and and not can_be_true:
                self._exclude_remaining(node.values[index + 1 :], "and", value)
                break
            if (not is_and) and not can_be_false:
                self._exclude_remaining(node.values[index + 1 :], "or", value)
                break

        return self._merge(
            results,
            node,
            "boolop-units",
            "and/or results",
            result_nodes=result_nodes,
        )

    def _exclude_remaining(self, nodes, operator, deciding_node):
        source = self._source(deciding_node)
        if source is None:
            source = type(deciding_node).__name__
        reason = (
            f"right operand is unreachable because the left operand of '{operator}' "
            f"is always {'false' if operator == 'and' else 'true'} (`{source}`)"
        )
        for node in nodes:
            self._exclude(node, operator + "-short-circuit", reason)

    def _infer_ifexp(self, node):
        test_unit = self._infer(node.test)
        known, test = self._constant(node.test)
        if known:
            truthy = bool(test)
            chosen = node.body if truthy else node.orelse
            excluded = node.orelse if truthy else node.body
            source = self._source(node.test) or type(node.test).__name__
            self._exclude(
                excluded,
                "if-always-true" if truthy else "if-always-false",
                f"branch is unreachable because condition `{source}` is always "
                f"{'true' if truthy else 'false'}",
            )
            result = self._infer(chosen)
            return _Inferred(result.units, result.may_be_missing or test_unit.may_be_missing)

        body = self._infer(node.body)
        alternative = self._infer(node.orelse)
        return self._merge(
            [body, alternative],
            node,
            "if-branch-units",
            "if branches",
            result_nodes=[node.body, node.orelse],
        )

    def _merge(
        self,
        results,
        node,
        code,
        description,
        left_node=None,
        right_node=None,
        result_nodes=None,
    ):
        units = []
        origins = []
        may_be_missing = False
        all_neutral = bool(results)
        for index, result in enumerate(results):
            if result.units:
                units.extend(result.units)
                for unit in result.units:
                    origins.append((unit, result_nodes[index] if result_nodes else None))
            may_be_missing = may_be_missing or result.may_be_missing
            all_neutral = all_neutral and result.neutral

        deduped = []
        origin_map = {}
        for unit, origin in origins:
            if any(unit.equivalent(existing) for existing in deduped):
                continue
            deduped.append(unit)
            origin_map[id(unit)] = origin
        units = deduped
        if len(units) > 1:
            incompatible = [
                (left, right)
                for index, left in enumerate(units)
                for right in units[index + 1 :]
                if not left.compatible(right)
            ]
            if incompatible:
                left, right = incompatible[0]
                if result_nodes:
                    left_node = origin_map.get(id(left), left_node)
                    right_node = origin_map.get(id(right), right_node)
                self._add_conflict(
                    node,
                    left,
                    right,
                    code,
                    f"{description} have incompatible units: '{left}' vs '{right}'",
                    left_node,
                    right_node,
                )

        return _Inferred(units, may_be_missing, all_neutral and not units)

    def _infer_compare(self, node):
        operands = [self._infer(node.left)]
        operands.extend(self._infer(item) for item in node.comparators)
        nodes = [node.left] + list(node.comparators)

        for index, operator in enumerate(node.ops):
            left = operands[index]
            right = operands[index + 1]
            if isinstance(operator, (ast.Is, ast.IsNot, ast.In, ast.NotIn)):
                continue
            if left.neutral or right.neutral:
                continue
            left_zero = self._is_zero(nodes[index])
            right_zero = self._is_zero(nodes[index + 1])
            if left_zero or right_zero:
                continue
            for left_unit in left.units:
                for right_unit in right.units:
                    if not left_unit.compatible(right_unit):
                        self._add_conflict(
                            node,
                            left_unit,
                            right_unit,
                            "comparison-units",
                            f"comparison requires compatible units, "
                            f"got '{left_unit}' and '{right_unit}'",
                            nodes[index],
                            nodes[index + 1],
                        )
        return _Inferred.of(Unit.boolean())

    def _infer_call(self, node):
        path = self._path(node.func)
        arguments = [self._infer(argument) for argument in node.args]
        for keyword in node.keywords:
            self._infer(keyword.value)

        if path is None:
            self._note("call target is not a simple name; return unit is unknown")
            return _Inferred.of(Unit.unknown_unit())

        spec = self._function_specs.get(path)
        if spec is None:
            self._unknown_name("function", path, node.func)
            return _Inferred.of(Unit.unknown_unit())

        for index, argument in enumerate(arguments):
            expected = spec.expected(index)
            if expected is None:
                continue
            self._check_argument(node.args[index], expected, argument, spec, index)

        if spec.parameter_names:
            for keyword in node.keywords:
                if keyword.arg is None:
                    continue
                expected = spec.expected_keyword(keyword.arg)
                if expected is not None:
                    self._check_argument(
                        keyword.value,
                        expected,
                        self._infer(keyword.value),
                        spec,
                        spec.parameter_names.index(keyword.arg)
                        if keyword.arg in spec.parameter_names
                        else -1,
                        keyword.arg,
                    )

        if spec.returns.is_missing:
            return _Inferred.missing()
        return _Inferred.of(spec.returns)

    def _check_argument(self, node, expected, inferred, spec, index, name=None):
        if expected.is_missing:
            return
        for unit in inferred.units:
            if not unit.compatible(expected):
                location = f"argument {index + 1}" if index >= 0 else f"argument '{name}'"
                self._add_conflict(
                    node,
                    expected,
                    unit,
                    "argument-units",
                    f"{location} expects '{expected}', got '{unit}'",
                )

    def _infer_joinedstr(self, node):
        return _Inferred.of(Unit.dimensionless())

    def _infer_formattedvalue(self, node):
        return self._infer(node.value)

    def _infer_keyword(self, node):
        return self._infer(node.value)

    def _infer_list(self, node):
        for element in node.elts:
            self._infer(element)
        return _Inferred.of(Unit.unknown_unit())

    def _infer_tuple(self, node):
        for element in node.elts:
            self._infer(element)
        return _Inferred.of(Unit.unknown_unit())

    def _infer_set(self, node):
        for element in node.elts:
            self._infer(element)
        return _Inferred.of(Unit.unknown_unit())

    def _infer_dict(self, node):
        for key, value in zip(node.keys, node.values):
            if key is not None:
                self._infer(key)
            self._infer(value)
        return _Inferred.of(Unit.unknown_unit())

    def _infer_comprehension(self, node):
        for generator in node.generators:
            self._infer(generator.iter)
            for condition in generator.ifs:
                self._infer(condition)
        if isinstance(node, ast.DictComp):
            self._infer(node.key)
            return self._infer(node.value)
        return self._infer(node.elt)

    def _infer_generatorexp(self, node):
        return self._infer_comprehension(node)

    def _infer_dictcomp(self, node):
        return self._infer_comprehension(node)

    def _infer_starred(self, node):
        return self._infer(node.value)

    def _infer_expression(self, node):
        return self._infer(node.body)

    def _infer_module(self, node):
        results = [self._infer(statement) for statement in node.body]
        return self._merge(results, node, "module-units", "module statements")

    def _infer_expr_statement(self, node):
        return self._infer(node.value)

    def _infer_nameconstant(self, node):
        if node.value is None:
            return _Inferred.missing()
        return _Inferred.scalar()


def check_units(expression, names=None, functions=None, values=None, strict=False):
    """Statically check the units of ``expression``.

    >>> check_units("amount / count", names={"amount": "CNY", "count": "件"})
    result unit: CNY/件
    """
    return UnitChecker(names=names, functions=functions, values=values, strict=strict).check(
        expression
    )


analyze_units = check_units
validate_units = check_units
check_expression_units = check_units
