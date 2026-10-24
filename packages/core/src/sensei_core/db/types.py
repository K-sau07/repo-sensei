"""Column types for Postgres extensions."""

from __future__ import annotations

import math
import struct
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy.dialects.postgresql.base import ischema_names
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator, UserDefinedType

HALF_MAX = 65504.0  # the largest finite half-precision float


def vector_literal(values: Sequence[float]) -> str:
    """pgvector's text form: ``[0.1,0.2]`` (``repr`` keeps a float exact)."""
    return "[" + ",".join(repr(float(value)) for value in values) + "]"


def parse_vector(text: str) -> list[float]:
    inner = text.strip()[1:-1]
    return [float(part) for part in inner.split(",")] if inner else []


def half_precision(vector: Sequence[float]) -> list[float]:
    """``vector`` rounded to IEEE 754 half precision (to nearest, ties to even). Values sent
    already rounded are stored exactly, so every reader and writer agrees on them (pgvector's own
    rounding of a full float can differ in the last bit). Raises ValueError for a value that
    isn't finite or is beyond ±65504."""
    for value in vector:
        if not math.isfinite(value) or abs(value) > HALF_MAX:
            msg = f"{value!r} doesn't fit in half precision"
            raise ValueError(msg)
    packed = struct.pack(f"<{len(vector)}e", *vector)
    return list(struct.unpack(f"<{len(vector)}e", packed))


class _HalfVec(UserDefinedType[list[float]]):
    cache_ok = True

    def __init__(self, dims: int | None = None) -> None:
        self.dims = dims

    def get_col_spec(self, **_: Any) -> str:
        return f"halfvec({self.dims})" if self.dims else "halfvec"


# Reflection (alembic's compare) knows the type by name; without this it skips the column.
ischema_names["halfvec"] = cast("Any", _HalfVec)  # the table is typed for built-in types


class HalfVector(TypeDecorator[list[float]]):
    """pgvector's ``halfvec``: half-precision floats, half the size of ``vector`` (values round to
    about 3 significant digits, here on both ways: see :func:`half_precision`). ``dims`` fixes
    the length; without it any length is stored. The driver has no codec for it, so values cross
    as text."""

    impl = _HalfVec
    cache_ok = True

    def __init__(self, dims: int | None = None) -> None:
        super().__init__(dims)

    def process_bind_param(self, value: list[float] | None, dialect: Dialect) -> str | None:
        return None if value is None else vector_literal(half_precision(value))

    def process_result_value(self, value: Any, dialect: Dialect) -> list[float] | None:
        return None if value is None else half_precision(parse_vector(str(value)))
