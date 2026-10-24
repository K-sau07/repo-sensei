"""Python's own names, frozen so a rename plan doesn't depend on the interpreter that makes it.

Generated once with Python 3.13 (regenerate the same way when the supported version changes)::

    sorted({*keyword.kwlist, *keyword.softkwlist, *dir(builtins),
            *(name for t in (str, bytes, dict, list, set, tuple, int, float, object)
              for name in dir(t))})
"""

from __future__ import annotations

PYTHON_NAMES: frozenset[str] = frozenset(
    """
ArithmeticError AssertionError AttributeError BaseException BaseExceptionGroup BlockingIOError
BrokenPipeError BufferError BytesWarning ChildProcessError ConnectionAbortedError ConnectionError
ConnectionRefusedError ConnectionResetError DeprecationWarning EOFError Ellipsis EncodingWarning
EnvironmentError Exception ExceptionGroup False FileExistsError FileNotFoundError
FloatingPointError FutureWarning GeneratorExit IOError ImportError ImportWarning IndentationError
IndexError InterruptedError IsADirectoryError KeyError KeyboardInterrupt LookupError MemoryError
ModuleNotFoundError NameError None NotADirectoryError NotImplemented NotImplementedError OSError
OverflowError PendingDeprecationWarning PermissionError ProcessLookupError PythonFinalizationError
RecursionError ReferenceError ResourceWarning RuntimeError RuntimeWarning StopAsyncIteration
StopIteration SyntaxError SyntaxWarning SystemError SystemExit TabError TimeoutError True TypeError
UnboundLocalError UnicodeDecodeError UnicodeEncodeError UnicodeError UnicodeTranslateError
UnicodeWarning UserWarning ValueError Warning ZeroDivisionError _ _IncompleteInputError __abs__
__add__ __and__ __bool__ __buffer__ __build_class__ __bytes__ __ceil__ __class__ __class_getitem__
__contains__ __debug__ __delattr__ __delitem__ __dir__ __divmod__ __doc__ __eq__ __float__
__floor__ __floordiv__ __format__ __ge__ __getattribute__ __getformat__ __getitem__ __getnewargs__
__getstate__ __gt__ __hash__ __iadd__ __iand__ __import__ __imul__ __index__ __init__
__init_subclass__ __int__ __invert__ __ior__ __isub__ __iter__ __ixor__ __le__ __len__ __loader__
__lshift__ __lt__ __mod__ __mul__ __name__ __ne__ __neg__ __new__ __or__ __package__ __pos__
__pow__ __radd__ __rand__ __rdivmod__ __reduce__ __reduce_ex__ __repr__ __reversed__ __rfloordiv__
__rlshift__ __rmod__ __rmul__ __ror__ __round__ __rpow__ __rrshift__ __rshift__ __rsub__
__rtruediv__ __rxor__ __setattr__ __setitem__ __sizeof__ __spec__ __str__ __sub__ __subclasshook__
__truediv__ __trunc__ __xor__ abs add aiter all and anext any append as as_integer_ratio ascii
assert async await bin bit_count bit_length bool break breakpoint bytearray bytes callable
capitalize case casefold center chr class classmethod clear compile complex conjugate continue copy
copyright count credits decode def del delattr denominator dict difference difference_update dir
discard divmod elif else encode endswith enumerate eval except exec exit expandtabs extend filter
finally find float for format format_map from from_bytes fromhex fromkeys frozenset get getattr
global globals hasattr hash help hex id if imag import in index input insert int intersection
intersection_update is is_integer isalnum isalpha isascii isdecimal isdigit isdisjoint isidentifier
isinstance islower isnumeric isprintable isspace issubclass issubset issuperset istitle isupper
items iter join keys lambda len license list ljust locals lower lstrip maketrans map match max
memoryview min next nonlocal not numerator object oct open or ord partition pass pop popitem pow
print property quit raise range real remove removeprefix removesuffix replace repr return reverse
reversed rfind rindex rjust round rpartition rsplit rstrip set setattr setdefault slice sort sorted
split splitlines startswith staticmethod str strip sum super swapcase symmetric_difference
symmetric_difference_update title to_bytes translate try tuple type union update upper values vars
while with yield zfill zip
""".split()  # noqa: SIM905 - a word list reads better as text
)
