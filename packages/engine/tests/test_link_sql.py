"""Raw SQL to the tables it reads and writes (sqlglot)."""

from __future__ import annotations

import pytest

from sensei_engine.link.sql import MAX_SQL_CHARS, looks_like_sql, tables


@pytest.mark.parametrize(
    ("sql", "kind", "reads", "writes"),
    [
        ("SELECT a FROM users u JOIN items i ON i.owner = u.id", "select", ("users", "items"), ()),
        ("select * from item where id = :id", "select", ("item",), ()),
        ("WITH recent AS (SELECT * FROM orders) SELECT * FROM recent", "select", ("orders",), ()),
        ("SELECT * FROM a UNION SELECT * FROM b", "select", ("a", "b"), ()),
        ("INSERT INTO items (a, b) SELECT a, b FROM staging", "insert", ("staging",), ("items",)),
        (
            "UPDATE public.users SET a = 1 WHERE id IN (SELECT id FROM bans)",
            "update",
            ("bans",),
            ("public.users",),
        ),
        ('UPDATE "user" SET new_id = uuid_generate_v4()', "update", (), ("user",)),
        ("DELETE FROM sessions WHERE expires < now()", "delete", (), ("sessions",)),
        (
            "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET a = s.a",
            "merge",
            ("s",),
            ("t",),
        ),
        ("CREATE TABLE audit (id int)", "create", (), ("audit",)),
        ("ALTER TABLE users ADD COLUMN age int", "alter", (), ("users",)),
        ("DROP TABLE old_a, old_b", "drop", (), ("old_a", "old_b")),
        ("TRUNCATE logs", "truncate", (), ("logs",)),
        # Not tables: a sequence, an index.
        ("CREATE SEQUENCE IF NOT EXISTS s AS INTEGER OWNED BY users.id", "create", (), ()),
        ("CREATE INDEX ix_users_email ON users (email)", "create", (), ()),
        ("SELECT setval('user_id_seq', 1)", "select", (), ()),
    ],
)
def test_statements_read_and_write_their_tables(
    sql: str, kind: str, reads: tuple[str, ...], writes: tuple[str, ...]
) -> None:
    found = tables(sql)
    assert (found.kind, found.reads, found.writes, found.error) == (kind, reads, writes, None)


def test_a_statement_that_does_not_parse_keeps_its_error() -> None:
    found = tables("SELECT (a FROM")
    assert found.error
    assert found.reads == found.writes == ()


def test_only_statements_are_parsed() -> None:
    assert looks_like_sql("  select 1")
    assert looks_like_sql("(SELECT 1)")
    assert looks_like_sql("WITH x AS (SELECT 1) SELECT * FROM x")
    assert not looks_like_sql("selection of items")
    assert not looks_like_sql("Hello, world")
    assert not looks_like_sql("")


def test_a_huge_statement_is_cut_before_parsing() -> None:
    sql = "SELECT * FROM big WHERE a IN (" + ", ".join(["1"] * MAX_SQL_CHARS) + ")"
    found = tables(sql)
    assert found.error  # cut mid-list: parsed, failed, and quickly
