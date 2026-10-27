"""ORM models, tables and query sites to graph facts: a small repository with every pattern,
then the fastapi-template, where every handler and CRUD function that touches the database is
checked against a hand-made list."""

from __future__ import annotations

import tarfile
import textwrap
from collections.abc import Mapping
from pathlib import Path

import pytest

from sensei_core.models import EdgeType, NodeKind, Provenance
from sensei_engine.graph.writer import LINKS, GraphEdge
from sensei_engine.link.orm import FileOrm, OrmGraph, link, scan
from sensei_engine.parse.parser import parse_file
from sensei_engine.symbols.extract import extract_symbols

FIXTURE = Path(__file__).parent / "fixtures" / "fastapi-template.tar.gz"

REPO = {
    "app/__init__.py": "",
    "app/models/__init__.py": "from .user import User, tags\n",
    "app/models/user.py": """
        from sqlalchemy import Column, Integer, String, Table
        from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

        class Base(DeclarativeBase):
            pass

        class User(Base):
            __tablename__ = "users"
            id: Mapped[int] = mapped_column(primary_key=True)
            email: Mapped[str] = mapped_column("email_address", String(255))
            nickname = Column(String)
            age: Mapped[int | None]
            posts: Mapped[list["Post"]] = relationship(back_populates="author")
            _cache = None

        tags = Table("tags", Base.metadata, Column("id", Integer), Column("label", String))
    """,
    "app/models/post.py": """
        from sqlmodel import Field, SQLModel

        class PostBase(SQLModel):
            title: str = Field(max_length=200)
            body: str

        class Post(PostBase, table=True):
            __tablename__ = "posts"
            id: int | None = Field(default=None, primary_key=True)
            author_id: int = Field(foreign_key="users.id")

        class PostPublic(PostBase):
            id: int

        class Comment(SQLModel, table=True):
            id: int = Field(primary_key=True)
    """,
    "app/legacy.py": """
        from app.models.user import Base

        class User(Base):
            __tablename__ = "legacy_users"
    """,
    "app/services.py": """
        from typing import Annotated

        from sqlalchemy import delete, func, insert, select, text
        from sqlmodel import Session

        from app.models import User
        from app.models.post import Post
        from app.models.user import tags
        from . import models as m
        from .models.post import Comment

        PostDep = Annotated[Post, "a dependency"]


        def list_users(session: Session):
            return session.scalars(select(User).where(User.age > 1)).all()


        def count_posts(session: Session) -> int:
            return session.exec(select(func.count()).select_from(Post)).one()


        def new_post(session: Session, title: str) -> Post:
            post = Post(title=title, body="")
            session.add(post)
            return post


        def find_post(session: Session, id: int) -> Post | None:
            return session.get(Post, id)


        def touch(session: Session, id: int) -> None:
            post = find_post(session, id)
            session.add(post)


        def remove(session: Session, post: PostDep) -> None:
            session.delete(post)


        def purge(session: Session) -> None:
            session.execute(delete(Comment))
            session.execute(insert(tags))


        def raw(session: Session, conn, table: str) -> None:
            session.execute(text("SELECT * FROM users JOIN posts ON posts.author_id = users.id"))
            session.execute(text(f"SELECT * FROM {table}"))
            session.execute("DELETE FROM audit_log WHERE id = \\'x\\'")
            conn.exec_driver_sql("UPDATE unknown_table SET a = 1")


        def unrelated(things, x) -> None:
            things.add(x)
            session = None
            session.add(x)


        def via_module(session: Session):
            return session.query(m.User).all()
    """,
    "app/alembic/versions/001_init.py": """
        import sqlalchemy as sa
        from alembic import op

        def upgrade():
            op.create_table(
                "users", sa.Column("id", sa.Integer()), sa.Column("email_address", sa.String())
            )
            op.create_table("posts", sa.Column("id", sa.Integer()))
            op.create_table("tags_old", sa.Column("id", sa.Integer()))
            op.rename_table("tags_old", "tags")
            op.create_table(
                "audit_log", sa.Column("id", sa.Integer()), sa.Column("event", sa.Text())
            )
            op.create_table("legacy_users")
    """,
}


def _graph(files: Mapping[str, bytes]) -> tuple[OrmGraph, set[str]]:
    """The ORM graph of ``files``, and every symbol key the symbols stage gives them."""
    scanned: list[FileOrm] = []
    keys: set[str] = set()
    for path, source in sorted(files.items()):
        result = parse_file(path, source)
        symbols = extract_symbols(result, "python")
        keys |= {s.symbol_key for s in symbols}
        scanned.append(scan(result, symbols))
    return link(scanned), keys


def _ends_resolve(graph: OrmGraph, keys: set[str]) -> None:
    known = keys | {node.key for node in graph.nodes}
    missing = [e for e in graph.edges if e.src not in known or e.dst not in known]
    assert missing == []  # the writer would count every one of these as unresolved


def _hops(graph: OrmGraph, path: str = "") -> set[tuple[str, str, str]]:
    """(function, READS/WRITES, table) for the query sites in files under ``path``."""
    return {
        (e.src.split("/")[-1], e.type.value, e.dst.removeprefix("table "))
        for e in graph.edges
        if e.type in (EdgeType.READS, EdgeType.WRITES)
        and e.src.startswith(f"py {path}")
        and (e.evidence_path or "").startswith(path)
    }


@pytest.fixture(scope="module")
def repo() -> tuple[OrmGraph, set[str]]:
    files = {path: textwrap.dedent(text).lstrip().encode() for path, text in REPO.items()}
    return _graph(files)


def _edges(graph: OrmGraph, src: str, *types: EdgeType) -> list[GraphEdge]:
    return [e for e in graph.edges if e.src.endswith(src) and e.type in types]


def test_models_tables_and_columns(repo: tuple[OrmGraph, set[str]]) -> None:
    graph, keys = repo
    _ends_resolve(graph, keys)
    nodes = {n.key: n for n in graph.nodes}
    models = sorted(n.name for n in graph.nodes if n.kind is NodeKind.ORM_MODEL)
    assert models == ["Comment", "Post", "User", "User"]  # no base, schema or read model
    assert nodes["orm_model py app/models/post.py/Post#"].attrs["framework"] == "sqlmodel"
    assert nodes["orm_model py app/models/user.py/User#"].attrs["framework"] == "sqlalchemy"

    def columns(table: str) -> list[str]:
        return sorted(
            n.name
            for n in graph.nodes
            if n.kind is NodeKind.COLUMN and n.attrs["table"] == f"table {table}"
        )

    assert columns("users") == ["age", "email_address", "id", "nickname"]  # relationship left out
    assert columns("posts") == ["author_id", "body", "id", "title"]  # PostBase's included
    assert columns("comment") == ["id"]  # table=True without __tablename__: the class's name
    assert columns("tags") == ["id", "label"]  # a Table() object
    # A field maps to its column, an inherited one from the base that declares it.
    maps = {(e.src, e.dst) for e in graph.edges if e.type is EdgeType.MAPS_TO}
    assert ("py app/models/post.py/PostBase#title.", "column posts.title") in maps
    assert ("py app/models/user.py/User#email.", "column users.email_address") in maps
    assert ("orm_model py app/models/user.py/User#", "table users") in maps
    assert ("py app/models/post.py/Post#", "orm_model py app/models/post.py/Post#") in maps
    assert all(
        e.provenance is Provenance.CONTRACT for e in graph.edges if e.type is EdgeType.MAPS_TO
    )
    # Everything is the links stage's to write: the writer's own check passes.
    LINKS.check(graph.nodes, graph.edges)


def test_query_sites_read_and_write_tables(repo: tuple[OrmGraph, set[str]]) -> None:
    graph, _ = repo
    by_site = {
        (e.src.split("/")[-1], e.type.value, e.dst): (e.provenance, round(e.confidence or 0, 3))
        for e in graph.edges
        if e.type in (EdgeType.READS, EdgeType.WRITES)
        and not e.src.startswith("sql_statement")
        and not e.attrs.get("sql")
    }
    imported, receiver = (Provenance.IMPORT_RESOLVED, 0.9), (Provenance.RECEIVER_HEURISTIC, 0.7)
    ambiguous = (Provenance.NAME_AMBIGUOUS, 0.1)  # two classes named User
    assert by_site == {
        # Imported from the package that re-exports it: only the name says which User.
        ("list_users().", "reads", "table users"): ambiguous,
        ("list_users().", "reads", "table legacy_users"): ambiguous,
        ("count_posts().", "reads", "table posts"): imported,
        ("new_post().", "writes", "table posts"): receiver,  # post = Post(...)
        ("find_post().", "reads", "table posts"): imported,  # session.get(Post, id)
        ("touch().", "writes", "table posts"): receiver,  # what find_post() returns
        ("remove().", "writes", "table posts"): receiver,  # post: PostDep = Annotated[Post, ...]
        ("purge().", "writes", "table comment"): imported,
        ("purge().", "writes", "table tags"): imported,  # a Table() object, by its variable
        ("via_module().", "reads", "table users"): ambiguous,  # m.User: the package again
        ("via_module().", "reads", "table legacy_users"): ambiguous,
    }
    assert not _edges(graph, "unrelated().", EdgeType.WRITES)  # things.add, an unknown x
    report = graph.report
    assert report.accesses == 9
    assert report.models == 4


def test_raw_sql_becomes_statements_and_table_edges(repo: tuple[OrmGraph, set[str]]) -> None:
    graph, _ = repo
    statements = sorted(
        (n.start_line, n.attrs["kind"], n.attrs["sql"])
        for n in graph.nodes
        if n.kind is NodeKind.SQL_STATEMENT
    )
    assert [kind for _, kind, _ in statements] == ["select", "delete", "update"]  # no f-string
    assert statements[1][2] == "DELETE FROM audit_log WHERE id = 'x'"  # escapes decoded
    raw = {
        (e.src.split("/")[-1], e.type.value, e.dst): round(e.confidence or 0, 2)
        for e in graph.edges
        if e.attrs.get("sql") and not e.src.startswith("sql_statement")
    }
    assert raw == {
        ("raw().", "reads", "table users"): 0.9,  # tables the models declare
        ("raw().", "reads", "table posts"): 0.9,
        ("raw().", "writes", "table audit_log"): 0.9,  # one the migrations create
        ("raw().", "writes", "table unknown_table"): 0.4,  # only the SQL names it
    }
    unknown = next(n for n in graph.nodes if n.key == "table unknown_table")
    assert unknown.path is None
    assert unknown.attrs["sources"] == ["sql"]
    functions = {n.attrs["function"] for n in graph.nodes if n.kind is NodeKind.SQL_STATEMENT}
    assert functions == {"py app/services.py/raw()."}
    assert graph.report.statements == 3


def test_tables_are_checked_against_the_migrations(repo: tuple[OrmGraph, set[str]]) -> None:
    graph, _ = repo
    nodes = {n.key: n for n in graph.nodes}
    assert graph.report.mismatches == ["comment"]  # a model whose table no migration makes
    assert nodes["table comment"].attrs["in_migrations"] is False
    assert nodes["table users"].attrs["in_migrations"] is True
    assert nodes["table tags"].attrs["in_migrations"] is True  # renamed into existence
    assert "table tags_old" not in nodes  # renamed away
    audit = nodes["table audit_log"]  # only the migrations know it
    assert audit.path == "app/alembic/versions/001_init.py"
    assert audit.attrs["sources"] == ["migration", "sql"]
    assert "column audit_log.event" in nodes


def test_a_file_without_models_or_queries_adds_nothing() -> None:
    graph, _ = _graph({"util.py": b"def add(a, b):\n    return a + b\n"})
    assert (graph.nodes, graph.edges) == ([], [])


def test_typescript_files_are_skipped() -> None:
    result = parse_file("web/a.ts", b"const select = (x: unknown) => x;\nselect(User);\n")
    assert scan(result, extract_symbols(result, "typescript")).empty


# ---------------------------------------------------------------- the fastapi-template

# Every function in backend/app that queries the database itself, and how (handlers that go
# through crud.* reach the tables over CALLS edges, 3.4).
TEMPLATE_HOPS = {
    ("get_current_user().", "reads", "user"),  # api/deps.py: session.get(User, ...)
    ("read_items().", "reads", "item"),
    ("read_item().", "reads", "item"),
    ("create_item().", "writes", "item"),  # routes/items.py, and crud.create_item
    ("update_item().", "reads", "item"),
    ("update_item().", "writes", "item"),
    ("delete_item().", "reads", "item"),
    ("delete_item().", "writes", "item"),
    ("create_user().", "writes", "user"),  # routes/private.py, and crud.create_user
    ("read_users().", "reads", "user"),
    ("update_user_me().", "writes", "user"),  # current_user: CurrentUser
    ("update_password_me().", "writes", "user"),
    ("read_user_by_id().", "reads", "user"),
    ("update_user().", "reads", "user"),  # routes/users.py: session.get
    ("update_user().", "writes", "user"),  # crud.update_user: db_user: User
    ("delete_user_me().", "writes", "user"),
    ("delete_user().", "reads", "user"),
    ("delete_user().", "writes", "item"),  # delete(Item).where(...)
    ("delete_user().", "writes", "user"),
    ("get_user_by_email().", "reads", "user"),
    ("authenticate().", "writes", "user"),  # db_user = get_user_by_email(...) -> User | None
    ("init_db().", "reads", "user"),  # core/db.py
}


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> tuple[OrmGraph, set[str]]:
    root = tmp_path_factory.mktemp("fixture")
    with tarfile.open(FIXTURE) as tar:
        tar.extractall(root, filter="data")
    repo = root / "fastapi-template"
    files = {str(path.relative_to(repo)): path.read_bytes() for path in sorted(repo.rglob("*.py"))}
    return _graph(files)


def test_the_template_every_handler_to_table_hop(template: tuple[OrmGraph, set[str]]) -> None:
    graph, keys = template
    _ends_resolve(graph, keys)
    LINKS.check(graph.nodes, graph.edges)
    hops = {
        (function, kind, table.removeprefix("table "))
        for function, kind, table in _hops(graph, "backend/app/")
        if not table.startswith("sql_statement")
    }
    app_hops = {hop for hop in hops if "alembic" not in hop[0]}
    assert {h for h in app_hops if h[0] not in ("upgrade().", "downgrade().")} == TEMPLATE_HOPS
    assert graph.report.unresolved == 0


def test_the_template_models_and_migrations(template: tuple[OrmGraph, set[str]]) -> None:
    graph, _ = template
    nodes = {n.key: n for n in graph.nodes}
    models = {n.name: n.attrs["table"] for n in graph.nodes if n.kind is NodeKind.ORM_MODEL}
    assert models == {"User": "user", "Item": "item"}
    columns = {k.removeprefix("column ") for k, n in nodes.items() if n.kind is NodeKind.COLUMN}
    assert columns == {
        "user.email",  # from UserBase
        "user.is_active",
        "user.is_superuser",
        "user.full_name",
        "user.id",
        "user.hashed_password",
        "user.created_at",
        "item.title",  # from ItemBase
        "item.description",
        "item.id",
        "item.created_at",
        "item.owner_id",
    }  # no user.items or item.owner: relationships
    assert graph.report.mismatches == []
    assert nodes["table user"].attrs["in_migrations"] is True
    assert graph.report.sql_errors == 0
    sequences = [k for k in nodes if k.endswith("_seq")]
    assert sequences == []  # the migrations' sequences aren't tables
