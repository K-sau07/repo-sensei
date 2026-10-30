"""The tables a raw SQL string reads and writes, parsed with sqlglot (3.11).

A string is parsed as SQL only when it looks like a statement (it starts with a statement's
keyword), and at most :data:`MAX_SQL_CHARS` of it. A statement that doesn't parse is kept with
its error and no tables. ``SELECT`` reads every table it names; ``INSERT``, ``UPDATE``,
``DELETE`` and ``MERGE`` write their target and read the rest; DDL (``CREATE``, ``ALTER``,
``DROP``, ``TRUNCATE``) writes the tables it changes; DDL of anything else (a sequence, an index)
names none. Common table expressions are names of the query, not
tables, so they are left out.

:func:`ddl` reads a migration script (Flyway's ``V1__init.sql``, a ``schema.sql``): the tables
each of its statements creates, renames and drops, by its head (any dialect: ``IF NOT EXISTS``,
quoted names, a schema), with the columns sqlglot reads of a ``CREATE TABLE`` when one of its
dialects parses it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

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
    except (RecursionError, ValueError):  # nested past the parser's depth, or text it can't read
        return SqlTables(kind="", reads=(), writes=(), error="too deeply nested to read")
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


# ---------------------------------------------------------------------- migrations

DdlOp = Literal["create_table", "rename_table", "drop_table"]
_NAME = r'((?:[`"\[]?[\w$]+[`"\]]?\.)*[`"\[]?[\w$]+[`"\]]?)'
_CREATE_RE = re.compile(
    r"\s*create\s+(?:or\s+replace\s+)?(?:(?:global\s+|local\s+)?(?:temporary|temp)\s+|unlogged\s+)?"
    rf"table\s+(?:if\s+not\s+exists\s+)?{_NAME}",
    re.IGNORECASE,
)
_RENAME_RE = re.compile(
    rf"\s*alter\s+table\s+(?:if\s+exists\s+)?(?:only\s+)?{_NAME}\s+rename\s+to\s+{_NAME}",
    re.IGNORECASE,
)
_RENAME_TABLE_RE = re.compile(rf"\s*rename\s+table\s+{_NAME}\s+to\s+{_NAME}", re.IGNORECASE)
_DROP_RE = re.compile(rf"\s*drop\s+table\s+(?:if\s+exists\s+)?{_NAME}", re.IGNORECASE)
_DIALECTS = (None, "postgres", "mysql")


@dataclass(frozen=True, slots=True)
class TableChange:
    op: DdlOp
    table: str  # as written, unquoted ("public.users")
    line: int  # 1-based, the statement's first
    columns: tuple[tuple[str, int], ...] = ()
    renamed_to: str | None = None


_DOLLAR_RE = re.compile(r"\$\w*\$")
# What only MySQL writes: a quoted name (`name`), a table's engine, an AUTO_INCREMENT column.
_MYSQL_RE = re.compile(r"`|\bENGINE\s*=|\bAUTO_INCREMENT\b", re.IGNORECASE)


def _unquoted(name: str) -> str:
    return ".".join(part.strip('`"[]') for part in name.split("."))


def _statements(script: str) -> list[tuple[str, int]]:
    """A script's statements and the line each starts on (comments blanked, lines kept; split
    at semicolons outside quotes and dollar-quoted bodies). One pass, so linear: a comment marker
    inside a string is the string's, and an unclosed comment or string runs to the end.

    A backslash is a character like any other, as the standard has it (Postgres's
    ``ESCAPE '\\'``), except in an ``E'…'`` string. It escapes the character after it
    (``'it\\'s'``) in every string of a script that reads as MySQL's (:data:`_MYSQL_RE`), or
    that the standard can't read: a string left unclosed, or closed by a backslash's quote right
    before a word (``'it\\'`` then ``s``)."""
    script = script[: MAX_SQL_CHARS * 10]
    escapes = _MYSQL_RE.search(script) is not None
    text, read = _uncommented(script, escapes=escapes)
    if not read and not escapes:
        again, read = _uncommented(script, escapes=True)
        if read:
            text, escapes = again, True
    found: list[tuple[str, int]] = []
    start = 0
    for end in _semicolons(text, escapes=escapes):
        found.append((text[start:end], start))
        start = end + 1
    found.append((text[start:], start))
    lines: list[tuple[str, int]] = []
    for statement, offset in found:
        lead = len(statement) - len(statement.lstrip())
        if statement.strip():
            lines.append((statement, text.count("\n", 0, offset + lead) + 1))
    return lines


def _opened(text: str, i: int, *, escapes: bool) -> tuple[str, bool] | None:
    """The string (or quoted name) opening at ``i``: its closing quote, and whether a backslash
    escapes in it; None when none opens there."""
    char = text[i]
    if char == "`":
        return char, False
    if char in "'\"":
        prefix = char == "'" and i > 0 and text[i - 1] in "Ee"
        estring = prefix and (i < 2 or not (text[i - 2].isalnum() or text[i - 2] == "_"))
        return char, escapes or estring
    if char == "$" and (dollar := _DOLLAR_RE.match(text, i)) is not None:
        return dollar[0], False
    return None


def _uncommented(text: str, *, escapes: bool) -> tuple[str, bool]:
    """``text`` with its comments (outside strings) blanked, line breaks kept; and whether its
    strings read as they should (see :func:`_statements`)."""
    out: list[str] = []
    quote, escaped, read, i, n = "", False, True, 0, len(text)
    while i < n:
        if quote:
            skip = _quoted_end(text, i, quote, escaped=escaped)
            if skip is None:  # unclosed: the rest is the string's
                out.append(text[i:])
                return "".join(out), False
            if not escaped and quote in "'\"" and text[skip - 2] == "\\" and skip < n:
                read = read and not (text[skip].isalnum() or text[skip] == "_")
            out.append(text[i:skip])
            i, quote = skip, ""
            continue
        if text.startswith("--", i) or text.startswith("/*", i):
            close = "\n" if text[i + 1] == "-" else "*/"
            end = text.find(close, i + 2)
            end = n if end < 0 else end + (0 if close == "\n" else 2)
            out.append(re.sub(r"[^\n]", " ", text[i:end]))
            i = end
            continue
        if (opened := _opened(text, i, escapes=escapes)) is not None:
            quote, escaped = opened
            out.append(quote)
            i += len(quote)
            continue
        out.append(text[i])
        i += 1
    return "".join(out), read


def _quoted_end(text: str, i: int, quote: str, *, escaped: bool) -> int | None:
    """Where the string ``quote`` opened before ``i`` ends (past its closing quote), or None
    when it doesn't; with ``escaped``, a backslash escapes the character after it."""
    n = len(text)
    while i < n:
        if escaped and text[i] == "\\":
            i += 2
            continue
        if text.startswith(quote, i):
            return i + len(quote)
        i += 1
    return None


def _semicolons(text: str, *, escapes: bool) -> list[int]:
    """Where ``text`` (comments blanked) has a semicolon outside its strings."""
    found: list[int] = []
    quote, escaped, i, n = "", False, 0, len(text)
    while i < n:
        if quote:
            end = _quoted_end(text, i, quote, escaped=escaped)
            if end is None:
                break
            i, quote = end, ""
            continue
        if (opened := _opened(text, i, escapes=escapes)) is not None:
            quote, escaped = opened
            i += len(quote)
            continue
        if text[i] == ";":
            found.append(i)
        i += 1
    return found


def _columns(statement: str, line: int) -> tuple[tuple[str, int], ...]:
    for dialect in _DIALECTS:
        try:
            parsed = sqlglot.parse_one(statement.strip()[:MAX_SQL_CHARS], read=dialect)
        except (SqlglotError, RecursionError, ValueError):
            continue
        if isinstance(parsed, exp.Create) and isinstance(parsed.this, exp.Schema):
            return tuple(
                (column.name, line)
                for column in parsed.this.expressions
                if isinstance(column, exp.ColumnDef) and column.name
            )
    return ()


def ddl(script: str) -> list[TableChange]:
    """The tables a migration script creates, renames and drops, in order (see the module)."""
    changes: list[TableChange] = []
    for statement, line in _statements(script):
        if (found := _CREATE_RE.match(statement)) is not None:
            columns = _columns(statement, line)
            changes.append(TableChange("create_table", _unquoted(found[1]), line, columns))
        elif (found := _RENAME_RE.match(statement) or _RENAME_TABLE_RE.match(statement)) is not (
            None
        ):
            old, new = _unquoted(found[1]), _unquoted(found[2])
            changes.append(TableChange("rename_table", old, line, renamed_to=new))
        elif (found := _DROP_RE.match(statement)) is not None:
            changes.append(TableChange("drop_table", _unquoted(found[1]), line))
    return changes
