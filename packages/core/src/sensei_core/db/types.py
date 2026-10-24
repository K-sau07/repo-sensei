"""Column types for Postgres extensions."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator, UserDefinedType


def vector_literal(values: Sequence[float]) -> str:
    """pgvector's text form: ``[0.1,0.2]`` (``repr`` keeps a float exact)."""
    return "[" + ",".join(repr(float(value)) for value in values) + "]"


def parse_vector(text: str) -> list[float]:
    inner = text.strip()[1:-1]
    return [float(part) for part in inner.split(",")] if inner else []


class _HalfVec(UserDefinedType[list[float]]):
    cache_ok = True

    def __init__(self, dims: int | None = None) -> None:
        self.dims = dims

    def get_col_spec(self, **_: Any) -> str:
        return f"halfvec({self.dims})" if self.dims else "halfvec"


class HalfVector(TypeDecorator[list[float]]):
    """pgvector's ``halfvec``: half-precision floats, half the size of ``vector`` (values round to
    about 3 significant digits). ``dims`` fixes the length; without it any length is stored. The
    driver has no codec for it, so values cross as text."""

    impl = _HalfVec
    cache_ok = True

    def __init__(self, dims: int | None = None) -> None:
        super().__init__(dims)

    def process_bind_param(self, value: list[float] | None, dialect: Dialect) -> str | None:
        return None if value is None else vector_literal(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> list[float] | None:
        return None if value is None else parse_vector(str(value))
