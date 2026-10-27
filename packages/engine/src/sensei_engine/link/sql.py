"""The tables a raw SQL string reads and writes, parsed with sqlglot (3.11).

A string is parsed as SQL only when it looks like a statement (it starts with a statement's
keyword), and at most :data:`MAX_SQL_CHARS` of it. A statement that doesn't parse is kept with
its error and no tables. ``SELECT`` reads every table it names; ``INSERT``, ``UPDATE``,
``DELETE`` and ``MERGE`` write their target and read the rest; DDL (``CREATE``, ``ALTER``,
``DROP``, ``TRUNCATE``) writes the tables it changes; DDL of anything else (a sequence, an index)
names none. Common table expressions are names of the query, not
tables, so they are left out.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

MAX_SQL_CHARS = 20_000
MAX_TABLES = 50

_STATEMENT_RE = re.compile(
    r"\s*(?:\(\s*)?(select|with|insert|update|delete|merge|create|alter|drop|truncate)\b",
    re.IGNORECASE,
)
# sqlglot warns (on the root logger's handlers) about every statement it keeps as a bare command.
logging.getLogger("sqlglot").setLevel(logging.ERROR)

_WRITES = (exp.Insert, exp.Update, exp.Delete, exp.Merge)
_DDL = (exp.Create, exp.Alter, exp.Drop, exp.TruncateTable)


@dataclass(frozen=True, slots=True)
class SqlTables:
    kind: str  # the statement's: "select", "insert", ... ("" when it didn't parse)
    reads: tuple[str, ...]  # table names, schema-qualified when the SQL qualifies them
    writes: tuple[str, ...]
    error: str | None = None


def looks_like_sql(text: str) -> bool:
    return bool(_STATEMENT_RE.match(text))


def _name(table: exp.Table) -> str:
    parts = [part for part in (table.catalog, table.db, table.name) if part]
    return ".".join(parts)


def _targets(statement: exp.Expr) -> list[exp.Table]:
    """The tables a write or DDL statement changes."""
    if isinstance(statement, exp.Drop | exp.TruncateTable):  # DROP TABLE a, b / TRUNCATE a, b
        found: list[object] = [*(statement.args.get("tables") or []), *statement.expressions]
        return [table for table in found if isinstance(table, exp.Table)]
    target = statement.this if isinstance(statement, _WRITES + _DDL) else None
    if isinstance(target, exp.Schema):  # INSERT INTO t (a, b) / CREATE TABLE t (...)
        target = target.this
    return [target] if isinstance(target, exp.Table) else []


def tables(sql: str) -> SqlTables:
    """What ``sql`` (one statement, or the first of several) reads and writes."""
    try:
        parsed = [s for s in sqlglot.parse(sql[:MAX_SQL_CHARS]) if s is not None]
    except SqlglotError as exc:
        return SqlTables(kind="", reads=(), writes=(), error=str(exc).splitlines()[0][:200])
    if not parsed:
        return SqlTables(kind="", reads=(), writes=(), error="no statement")
    statement = parsed[0]
    kind = "truncate" if isinstance(statement, exp.TruncateTable) else statement.key.lower()
    if isinstance(statement, exp.Select | exp.Union | exp.Intersect | exp.Except):
        kind = "select"
    if (
        isinstance(statement, _DDL)
        and str(statement.args.get("kind") or "TABLE").upper() != "TABLE"
    ):
        return SqlTables(kind=kind, reads=(), writes=())  # a sequence, an index, an extension
    ctes = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
    targets = [id(table) for table in _targets(statement)]
    writes: list[str] = []
    reads: list[str] = []
    for table in statement.find_all(exp.Table):
        name = _name(table)
        if not name or (not table.db and name.lower() in ctes):
            continue
        bucket = writes if id(table) in targets else reads
        if name not in bucket:
            bucket.append(name)
    return SqlTables(kind=kind, reads=tuple(reads[:MAX_TABLES]), writes=tuple(writes[:MAX_TABLES]))
