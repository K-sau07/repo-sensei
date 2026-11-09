"""Org isolation for every table, found in the database itself (10.10).

The schema is read from ``pg_catalog`` after the migrations ran, not from a list kept here: every
table with an ``org_id`` column must have row-level security enabled *and forced* with the
``org_isolation`` policy, or be named in :data:`RLS_EXEMPT` with the reason its migration gives.
Every table without ``org_id`` must be named in :data:`GLOBAL_TABLES`. So a new table that forgets
its policy, or a new table nobody classified, fails here.

Then, for every org-scoped table, a row is seeded in two orgs and, as ``sensei_app`` (the role the
API and workers' org-scoped work use, RLS enforced) in org A's context, org B's row is neither
read, changed, removed, nor can one be added for org B or moved there. Rows are seeded generically
from the catalog: each NOT NULL column without a default gets a value of its type (an enum's first
label; a ``CHECK (col = ANY (ARRAY[...]))`` column the first value listed), and each NOT NULL
foreign key a parent row seeded the same way in the same org. Every table seeds generically, so
there are no hand-written seeds; the policies beside ``org_isolation`` (share links, invitation
tokens, API key prefixes, own memberships, own account events, staff reads) are each probed
across orgs below with the same seeder and the column they key on set.
"""

from __future__ import annotations

import re
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy import CursorResult, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from sensei_core.db.tenancy import (
    APP_ROLE,
    ORG_SETTING,
    STAFF_READ_TABLES,
    enable_org_rls_sql,
    set_api_key_context,
    set_invite_context,
    set_org_context,
    set_share_context,
    set_staff_context,
    set_user_context,
)
from sensei_core.models import Base

pytestmark = pytest.mark.integration

# Tables with an ``org_id`` column that are deliberately not behind org row-level security.
RLS_EXEMPT: dict[str, str] = {
    # 0008: dead letters, a system table. ``org_id`` is nullable (set null when the org goes);
    # the app role may only INSERT (insert_only_sql), workers and staff read it as sensei_system.
    # Checked below: the app role really has no SELECT, UPDATE or DELETE on it.
    "job_failures": "insert-only system inbox",
    # 0040: OAuth 2.1 credentials, "like browser sessions: only hashes of the secrets, found by
    # hash, not behind org row-level security". A grant, device code or token is looked up by
    # the hash of a secret the client presents before any org is known; ``org_id`` records the
    # org the user's consent named. Checked below: each is found by a unique hash column.
    "oauth_device_codes": "OAuth credential found by hash",
    "oauth_grants": "OAuth credential found by hash",
    "oauth_issued_tokens": "OAuth credential found by hash",
}
# The unique hash column each OAuth exemption is found by (what keeps it unguessable).
OAUTH_LOOKUP = {
    "oauth_device_codes": "device_code_hash",
    "oauth_grants": "request_hash",
    "oauth_issued_tokens": "token_hash",
}

# Tables with no ``org_id``: global by design, each for the reason given.
GLOBAL_TABLES: dict[str, str] = {
    "alembic_version": "migration bookkeeping; the app role has no privileges on it",
    "users": "people exist across orgs (GitHub identities)",
    "orgs": "the tenants themselves; reached by slug only through the caller's membership",
    "sessions": "browser sessions, found by token hash (0003)",
    "oauth_tokens": "a user's own encrypted GitHub tokens (0003)",
    "oauth_clients": "OAuth clients registered for everyone (0040)",
    "notification_preferences": "a user's own preferences, across orgs (0051)",
    "eval_runs": "system table the app role may only read (0017, read_only_sql)",
    "webhook_deliveries": "system inbox the app role may only insert into (0007)",
    "public_embedding_cache": "embeddings of public code, shared; append-only (0053)",
}

# Policies allowed beside ``org_isolation`` (all permissive, so each widens what is visible):
# (table, policy) -> the command it covers ('r' SELECT, '*' ALL).
EXTRA_POLICIES: dict[tuple[str, str], str] = {
    ("api_keys", "key_by_prefix"): "r",
    ("conversations", "share_by_token"): "r",
    ("diagrams", "share_by_token"): "r",
    ("invitations", "invite_by_token"): "r",
    ("memberships", "own_memberships"): "r",
    ("audit_log", "own_account_events"): "*",  # only rows with org_id NULL (checked below)
    **{(table, "staff_read"): "r" for table in STAFF_READ_TABLES},
}

# Foreign keys between org-scoped tables that reference the parent by ``id`` alone instead of
# ``(id, org_id)``, so the database doesn't stop a row pointing at another org's row (the
# integrity check doesn't go through RLS). Each column is written by the server, never taken
# from a request, so this is hardening rather than a leak; listed so a new one is a choice.
UNPINNED_FKS: set[tuple[str, str]] = {
    ("conversations", "snapshot_id"),
    ("diagrams", "snapshot_id"),
    ("fix_runs", "run_id"),
    ("index_jobs", "snapshot_id"),
    ("messages", "snapshot_id"),
    ("repo_refs", "snapshot_id"),
    ("repos", "installation_id"),
    ("snapshots", "overlay_of"),
}

_RELATIONS = """
SELECT c.relname, c.relkind::text, c.relispartition, c.relrowsecurity, c.relforcerowsecurity,
       pg_get_userbyid(c.relowner) AS owner,
       EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attname = 'org_id'
               AND NOT a.attisdropped) AS has_org
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
ORDER BY c.relname
"""
_POLICIES = """
SELECT p.polname, p.polcmd::text, p.polpermissive,
       pg_get_expr(p.polqual, p.polrelid), pg_get_expr(p.polwithcheck, p.polrelid)
FROM pg_policy p WHERE p.polrelid = CAST(:table AS regclass)
"""


@dataclass(frozen=True)
class Relation:
    name: str
    kind: str
    partition: bool
    rls: bool
    forced: bool
    owner: str
    has_org: bool


async def _relations(db: AsyncSession) -> dict[str, Relation]:
    rows = (await db.execute(text(_RELATIONS))).all()
    return {row[0]: Relation(*row) for row in rows}


Policy = tuple[str, str, bool, str, str | None]  # name, command, permissive, USING, CHECK


async def _policies(db: AsyncSession, table: str) -> list[Policy]:
    rows = (await db.execute(text(_POLICIES), {"table": f'public."{table}"'})).all()
    return [cast("Policy", tuple(row)) for row in rows]


@pytest.fixture
async def app_db(db_session: AsyncSession) -> AsyncSession:
    """The app-role session (rolled back), checked to really be ``sensei_app``."""
    assert await db_session.scalar(text("SELECT current_user")) == APP_ROLE
    return db_session


# ----------------------------------------------------------------------------- the schema


async def test_every_table_is_classified(app_db: AsyncSession) -> None:
    relations = await _relations(app_db)
    unclassified = [
        name for name, rel in relations.items() if not rel.has_org and name not in GLOBAL_TABLES
    ]
    assert not unclassified, (
        f"tables without org_id that nobody classified: {unclassified}. Give them org_id and "
        "enable_org_rls_sql, or add them to GLOBAL_TABLES with the reason."
    )
    stale = [name for name in [*GLOBAL_TABLES, *RLS_EXEMPT] if name not in relations]
    assert not stale, f"classified tables that no longer exist: {stale}"
    misfiled = [name for name in GLOBAL_TABLES if relations[name].has_org]
    assert not misfiled, f"tables in GLOBAL_TABLES that have org_id: {misfiled}"


async def test_every_org_table_has_forced_rls_and_the_org_policy(
    app_db: AsyncSession, owner_session: AsyncSession
) -> None:
    # The policy expression exactly as the helper makes it, read back from Postgres.
    await owner_session.execute(text("CREATE TABLE _rls_expected (org_id uuid)"))
    for statement in enable_org_rls_sql("_rls_expected"):
        await owner_session.execute(text(statement))
    [(_, _, _, expected_qual, expected_check)] = await _policies(owner_session, "_rls_expected")

    relations = await _relations(app_db)
    org_tables = [r for r in relations.values() if r.has_org and r.name not in RLS_EXEMPT]
    assert len(org_tables) > 40  # the discovery found the schema, not an empty database
    problems: list[str] = []
    for rel in org_tables:
        if not (rel.rls and rel.forced):
            problems.append(f"{rel.name}: rls={rel.rls} forced={rel.forced}")
        if rel.owner == APP_ROLE:
            problems.append(f"{rel.name}: owned by {APP_ROLE}")
        policies = {
            name: (cmd, permissive, qual, check)
            for name, cmd, permissive, qual, check in await _policies(app_db, rel.name)
        }
        org = policies.pop("org_isolation", None)
        if org != ("*", True, expected_qual, expected_check):
            problems.append(f"{rel.name}: org_isolation is {org}")
        base = rel.name
        for name, (cmd, permissive, qual, check) in policies.items():
            allowed = EXTRA_POLICIES.get((base, name))
            if allowed is None or cmd != allowed or not permissive:
                problems.append(f"{rel.name}: unexpected policy {name} ({cmd}): {qual} / {check}")
            elif cmd == "*" and not (
                "org_id IS NULL" in qual and check is not None and "org_id IS NULL" in check
            ):
                problems.append(f"{rel.name}: {name} writes beyond rows without an org: {qual}")
    assert not problems, "\n".join(problems)


async def test_rls_tables_all_carry_org_id(app_db: AsyncSession) -> None:
    relations = await _relations(app_db)
    assert [r.name for r in relations.values() if r.rls and not r.has_org] == []


async def test_the_exemptions_hold_what_makes_them_safe(app_db: AsyncSession) -> None:
    relations = await _relations(app_db)
    for name in RLS_EXEMPT:
        assert relations[name].has_org, name
    privileges = {
        p: await app_db.scalar(
            text("SELECT has_table_privilege(current_user, 'job_failures', :p)"), {"p": p}
        )
        for p in ("SELECT", "INSERT", "UPDATE", "DELETE")
    }
    assert privileges == {"SELECT": False, "INSERT": True, "UPDATE": False, "DELETE": False}
    for table, column in OAUTH_LOOKUP.items():
        unique = await app_db.scalar(
            text(
                "SELECT count(*) FROM pg_constraint c JOIN pg_attribute a"
                " ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)"
                " WHERE c.conrelid = CAST(:t AS regclass) AND c.contype = 'u'"
                " AND a.attname = :c AND array_length(c.conkey, 1) = 1"
            ),
            {"t": table, "c": column},
        )
        assert unique == 1, (table, column)


async def test_the_app_role_cannot_bypass_rls(app_db: AsyncSession) -> None:
    row = (
        await app_db.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        )
    ).one()
    assert tuple(row) == (False, False)


async def test_foreign_keys_between_org_tables_carry_org_id(app_db: AsyncSession) -> None:
    rows = (
        await app_db.execute(
            text(
                "SELECT c.conrelid::regclass::text, c.confrelid::regclass::text,"
                " ARRAY(SELECT attname FROM pg_attribute WHERE attrelid = c.conrelid"
                "       AND attnum = ANY (c.conkey) ORDER BY attnum)::text[]"
                " FROM pg_constraint c JOIN pg_class r ON r.oid = c.conrelid"
                " WHERE c.contype = 'f' AND c.connamespace = 'public'::regnamespace"
                " AND NOT r.relispartition"
            )
        )
    ).all()
    relations = await _relations(app_db)
    scoped = {n for n, r in relations.items() if r.has_org and n not in RLS_EXEMPT}
    unpinned = {
        (child, cols[0])
        for child, parent, cols in rows
        if child in scoped and parent in scoped and "org_id" not in cols
    }
    assert unpinned == UNPINNED_FKS


# ----------------------------------------------------------------------------- seeding

_ENUM_CHECK = re.compile(r"\(\((\w+)\)::text = ANY \(\(ARRAY\['([^']*)'")
_LENGTH = re.compile(r"\((\d+)(?:,\d+)?\)")

Key = str  # a row's primary key, as text (``ROW(pk...)::text``; the id itself for ``id``)


@dataclass
class Column:
    name: str
    type: str  # format_type(): what to cast to
    base: str  # the type's own name (an array's element's for arrays)
    kind: str  # pg_type.typtype: b base, e enum, ...
    category: str  # pg_type.typcategory: A for arrays
    labels: list[str]


@dataclass
class TableInfo:
    name: str
    key: str  # SQL for the row's primary key as text
    required: list[Column]  # NOT NULL, no default, not generated
    columns: dict[str, Column]
    fks: dict[str, str]  # column -> parent table (by its id; ``(x_id, org_id)`` the same org's)
    choices: dict[str, str]  # column -> a value its CHECK allows


_COLUMNS = """
SELECT a.attname, format_type(a.atttypid, a.atttypmod), COALESCE(e.typname, t.typname),
       COALESCE(e.typtype, t.typtype)::text, t.typcategory::text,
       a.attnotnull AND NOT a.atthasdef AND a.attgenerated = '' AND a.attidentity = '',
       ARRAY(SELECT enumlabel FROM pg_enum WHERE enumtypid = COALESCE(e.oid, t.oid)
             ORDER BY enumsortorder)::text[],
       COALESCE(a.attnum = ANY (i.indkey), false)
FROM pg_attribute a JOIN pg_type t ON t.oid = a.atttypid
LEFT JOIN pg_type e ON e.oid = t.typelem AND t.typcategory = 'A'
LEFT JOIN pg_index i ON i.indrelid = a.attrelid AND i.indisprimary
WHERE a.attrelid = CAST(:t AS regclass) AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum
"""
_CONSTRAINTS = """
SELECT c.contype::text, c.confrelid::regclass::text,
       ARRAY(SELECT attname FROM unnest(c.conkey) WITH ORDINALITY k(n, i)
             JOIN pg_attribute ON attrelid = c.conrelid AND attnum = k.n ORDER BY k.i)::text[],
       pg_get_constraintdef(c.oid)
FROM pg_constraint c WHERE c.conrelid = CAST(:t AS regclass) AND c.contype IN ('f', 'c')
"""


@dataclass
class Seeder:
    """Inserts a valid row into any table from what the catalog says (as the current role)."""

    db: AsyncSession
    tables: dict[str, TableInfo] = field(default_factory=dict[str, TableInfo])
    made: dict[tuple[str, uuid.UUID | None], Key] = field(
        default_factory=dict[tuple[str, uuid.UUID | None], Key]
    )

    async def info(self, table: str) -> TableInfo:
        if table in self.tables:
            return self.tables[table]
        columns: dict[str, Column] = {}
        required: list[Column] = []
        pk: list[str] = []
        for name, typ, base, kind, category, needed, labels, in_pk in (
            await self.db.execute(text(_COLUMNS), {"t": table})
        ).all():
            columns[name] = column = Column(name, typ, base, kind, category, list(labels))
            if needed:
                required.append(column)
            if in_pk:
                pk.append(name)
        fks: dict[str, str] = {}
        choices: dict[str, str] = {}
        for contype, parent, cols, definition in (
            await self.db.execute(text(_CONSTRAINTS), {"t": table})
        ).all():
            if contype == "c":
                for match in _ENUM_CHECK.finditer(definition):
                    choices.setdefault(match.group(1), match.group(2))
                continue
            own = [c for c in cols if c != "org_id"]  # (x_id, org_id) -> (id, org_id)
            if len(own) == 1:
                fks[own[0]] = parent
        assert pk, f"{table} has no primary key"
        quoted = ", ".join(f'"{c}"' for c in pk)
        key = f'"{pk[0]}"::text' if len(pk) == 1 else f"ROW({quoted})::text"
        info = TableInfo(table, key, required, columns, fks, choices)
        self.tables[table] = info
        return info

    def value(self, info: TableInfo, column: Column) -> str:  # noqa: PLR0912 - one per type
        """A text literal of ``column``'s type (cast by Postgres), valid for its checks."""
        if column.name in info.choices:
            return info.choices[column.name]
        if column.category == "A":
            return "{}"
        if column.kind == "e":
            return column.labels[0]
        base = column.base
        length = _LENGTH.search(column.type)
        if base in ("varchar", "text", "citext", "bpchar"):
            size = int(length.group(1)) if length and base != "text" else 24
            return ("z" + secrets.token_hex(16))[:size]
        if base == "uuid":
            return str(uuid.uuid4())
        if base in ("int2", "int4"):
            return "1"
        if base == "int8":
            return str(secrets.randbelow(10**12) + 10**6)
        if base in ("float4", "float8", "numeric"):
            return "0.5"
        if base == "bool":
            return "false"
        if base in ("timestamptz", "timestamp", "date"):
            return datetime.now(UTC).isoformat()
        if base in ("jsonb", "json"):
            return "{}"
        if base == "bytea":
            return "\\x" + secrets.token_hex(32)
        if base in ("vector", "halfvec"):  # paired with a ``dims`` int, 1 like every int here
            dims = int(length.group(1)) if length else 1
            return "[" + ",".join(["0.5"] * dims) + "]"
        msg = f"no generic value for {info.name}.{column.name} ({column.type})"
        raise NotImplementedError(msg)

    async def values(
        self, table: str, org: uuid.UUID | None, overrides: dict[str, object] | None = None
    ) -> dict[str, str]:
        """Values for a new row of ``table`` in ``org``, seeding the parents it needs."""
        info = await self.info(table)
        overrides = overrides or {}
        out: dict[str, str] = {}
        if "org_id" in info.columns and org is not None:
            out["org_id"] = str(org)
        for column in info.required:
            if column.name in out or column.name in overrides:
                continue
            if column.name in info.fks:
                out[column.name] = await self.parent(info.fks[column.name], org)
            else:
                out[column.name] = self.value(info, column)
        for name, value in overrides.items():
            out[name] = "\\x" + value.hex() if isinstance(value, bytes) else str(value)
        return out

    async def parent(self, table: str, org: uuid.UUID | None) -> Key:
        """One row of ``table`` per org (or one, for global tables), made on first use."""
        info = await self.info(table)
        made = (table, org if "org_id" in info.columns else None)
        if made not in self.made:
            self.made[made] = await self.row(table, org)
        return self.made[made]

    async def insert_sql(self, table: str, values: dict[str, str]) -> tuple[str, dict[str, Any]]:
        info = await self.info(table)
        names = list(values)
        casts = ", ".join(
            f"CAST(CAST(:p{i} AS text) AS {info.columns[n].type})" for i, n in enumerate(names)
        )
        columns = ", ".join(f'"{n}"' for n in names)
        sql = f'INSERT INTO "{table}" ({columns}) VALUES ({casts}) RETURNING {info.key}'
        return sql, {f"p{i}": values[n] for i, n in enumerate(names)}

    async def insert(self, table: str, values: dict[str, str]) -> Key:
        sql, params = await self.insert_sql(table, values)
        return cast("Key", (await self.db.execute(text(sql), params)).scalar_one())

    async def row(self, table: str, org: uuid.UUID | None, **overrides: object) -> Key:
        """A new row of ``table`` in ``org`` (written in that org's context)."""
        values = await self.values(table, org, overrides)
        if org is not None:
            await set_org_context(self.db, org)
        return await self.insert(table, values)

    async def keys(self, table: str, keys: list[Key]) -> set[Key]:
        """Which of ``keys`` the current transaction can see in ``table``."""
        key = (await self.info(table)).key
        sql = f'SELECT {key} FROM "{table}" WHERE {key} = ANY (:keys)'
        return set(await self.db.scalars(text(sql), {"keys": keys}))

    async def where(self, table: str) -> str:
        """``WHERE`` for one row by ``:key``."""
        return f"WHERE {(await self.info(table)).key} = :key"


async def _org(db: AsyncSession) -> uuid.UUID:
    slug = "iso-" + secrets.token_hex(6)
    sql = "INSERT INTO orgs (slug, name) VALUES (:slug, :name) RETURNING id"
    return cast("uuid.UUID", await db.scalar(text(sql), {"slug": slug, "name": slug}))


async def _no_org(db: AsyncSession) -> None:
    await db.execute(text("SELECT set_config(:n, '', true)"), {"n": ORG_SETTING})


# ----------------------------------------------------------------------------- probing

_REFUSED = ("row-level security", "permission denied")


async def _try(db: AsyncSession, sql: str, params: dict[str, Any]) -> str:
    """Runs ``sql`` in a savepoint: 'refused' if the database refused it, else its row count."""
    try:
        async with db.begin_nested():
            result = cast("CursorResult[Any]", await db.execute(text(sql), params))
            return f"{result.rowcount} rows"
    except DBAPIError as exc:
        if any(reason in str(exc.orig) for reason in _REFUSED):
            return "refused"
        raise


def _org_tables() -> list[str]:
    """Every model table with ``org_id`` (the database's own list is checked against it)."""
    return sorted(
        t.name
        for t in Base.metadata.tables.values()
        if "org_id" in t.columns and t.name not in RLS_EXEMPT
    )


async def test_the_models_name_every_org_table_the_database_has(app_db: AsyncSession) -> None:
    relations = await _relations(app_db)
    in_db = sorted(
        n for n, r in relations.items() if r.has_org and not r.partition and n not in RLS_EXEMPT
    )
    assert in_db == _org_tables()


async def _cannot_write_into(
    seeder: Seeder, table: str, org_a: uuid.UUID, org_b: uuid.UUID
) -> None:
    """From org A's context, a new row for org B (with B's parents) is refused."""
    values = await seeder.values(table, org_b)  # its parents exist already: nothing new is made
    await set_org_context(seeder.db, org_a)
    sql, params = await seeder.insert_sql(table, values)
    assert await _try(seeder.db, sql, params) == "refused"


@pytest.mark.parametrize("table", _org_tables())
async def test_org_a_cannot_reach_org_b_rows(app_db: AsyncSession, table: str) -> None:
    db = app_db
    seeder = Seeder(db)
    org_a, org_b = await _org(db), await _org(db)
    mine = await seeder.row(table, org_a)
    theirs = await seeder.row(table, org_b)
    where = await seeder.where(table)

    await set_org_context(db, org_a)
    assert await seeder.keys(table, [mine, theirs]) == {mine}
    count = f'SELECT count(*) FROM "{table}" WHERE org_id = :b'
    assert await db.scalar(text(count), {"b": org_b}) == 0
    # Changing or removing B's row (by its key, or by B's org_id) matches nothing.
    for sql in (
        f'UPDATE "{table}" SET org_id = org_id {where}',
        f'UPDATE "{table}" SET org_id = org_id WHERE org_id = :b',
        f'DELETE FROM "{table}" {where}',
        f'DELETE FROM "{table}" WHERE org_id = :b',
    ):
        params = {"key": theirs} if ":key" in sql else {"b": org_b}
        assert await _try(db, sql, params) in ("0 rows", "refused"), sql
    # A new row for org B is refused from org A, and so is moving A's own row there.
    await _cannot_write_into(seeder, table, org_a, org_b)
    move = f'UPDATE "{table}" SET org_id = :b {where}'
    assert await _try(db, move, {"b": org_b, "key": mine}) == "refused"
    assert await seeder.keys(table, [mine, theirs]) == {mine}

    await _no_org(db)  # without an org, neither row exists
    assert await seeder.keys(table, [mine, theirs]) == set()
    await set_org_context(db, org_b)  # and B's row is still there, as it was
    assert await seeder.keys(table, [mine, theirs]) == {theirs}


async def test_a_partition_queried_directly_is_isolated_too(app_db: AsyncSession) -> None:
    db = app_db
    relations = await _relations(db)
    partitions = sorted(n for n, r in relations.items() if r.partition)
    assert partitions, "chunks is hash-partitioned"
    seeder = Seeder(db)
    org_a, org_b = await _org(db), await _org(db)
    await seeder.row("chunks", org_a)
    theirs = await seeder.row("chunks", org_b)
    where = await seeder.where("chunks")
    held = await db.scalar(
        text(f"SELECT tableoid::regclass::text FROM chunks {where}"),
        {"key": theirs},
    )
    assert held in partitions
    await set_org_context(db, org_a)
    for partition in partitions:
        assert await seeder.keys(partition, [theirs]) == set(), partition
        delete = f'DELETE FROM "{partition}" WHERE org_id = :b'
        assert await _try(db, delete, {"b": org_b}) == "0 rows", partition
    await set_org_context(db, org_b)
    assert await seeder.keys(str(held), [theirs]) == {theirs}


# ------------------------------------------------- the policies beside org_isolation, across orgs

Setter = Callable[[AsyncSession, Any], Awaitable[None]]


@pytest.mark.parametrize(
    ("table", "column", "secret", "setter"),
    [
        ("conversations", "share_token_hash", secrets.token_hex(32), set_share_context),
        ("diagrams", "share_token_hash", secrets.token_hex(32), set_share_context),
        ("invitations", "token_hash", secrets.token_bytes(32), set_invite_context),
        ("api_keys", "prefix", secrets.token_hex(4), set_api_key_context),
    ],
    ids=["share-conversation", "share-diagram", "invitation-token", "api-key-prefix"],
)
async def test_a_presented_secret_reads_its_one_row_and_writes_none(
    app_db: AsyncSession, table: str, column: str, secret: str | bytes, setter: Setter
) -> None:
    db = app_db
    seeder = Seeder(db)
    org_a, org_b = await _org(db), await _org(db)
    theirs = await seeder.row(table, org_b, **{column: secret})
    other = await seeder.row(table, org_b)  # the same org's row, whose secret isn't presented
    where = await seeder.where(table)

    await set_org_context(db, org_a)
    await setter(db, secret)
    assert await seeder.keys(table, [theirs, other]) == {theirs}
    for sql in (f'UPDATE "{table}" SET org_id = org_id {where}', f'DELETE FROM "{table}" {where}'):
        assert await _try(db, sql, {"key": theirs}) == "0 rows", sql
    await _cannot_write_into(seeder, table, org_a, org_b)
    await setter(db, secrets.token_bytes(32) if isinstance(secret, bytes) else "nope0000")
    assert await seeder.keys(table, [theirs, other]) == set()


async def test_own_memberships_elsewhere_are_read_only(app_db: AsyncSession) -> None:
    db = app_db
    seeder = Seeder(db)
    org_a, org_b = await _org(db), await _org(db)
    me = await seeder.row("users", None)
    someone = await seeder.row("users", None)
    await seeder.row("memberships", org_a, user_id=me, role="viewer")
    mine_in_b = await seeder.row("memberships", org_b, user_id=me, role="viewer")
    theirs_in_b = await seeder.row("memberships", org_b, user_id=someone, role="owner")
    where = await seeder.where("memberships")

    await set_org_context(db, org_a)
    await set_user_context(db, uuid.UUID(me))
    assert await seeder.keys("memberships", [mine_in_b, theirs_in_b]) == {mine_in_b}
    promote = f"UPDATE memberships SET role = 'owner' {where}"
    assert await _try(db, promote, {"key": mine_in_b}) == "0 rows"
    remove = f"DELETE FROM memberships {where}"
    assert await _try(db, remove, {"key": theirs_in_b}) == "0 rows"
    join = "INSERT INTO memberships (org_id, user_id, role) VALUES (:b, :u, 'owner')"
    assert await _try(db, join, {"b": org_b, "u": uuid.UUID(someone)}) == "refused"
    await _no_org(db)  # before an org is chosen: still only one's own rows, still read-only
    assert await seeder.keys("memberships", [mine_in_b, theirs_in_b]) == {mine_in_b}
    assert await _try(db, promote, {"key": mine_in_b}) == "0 rows"


async def test_own_account_events_are_only_ones_own(app_db: AsyncSession) -> None:
    db = app_db
    seeder = Seeder(db)
    org_b = await _org(db)
    me = await seeder.row("users", None)
    someone = await seeder.row("users", None)

    def event(actor: Key) -> dict[str, str]:
        return {"actor_type": "user", "action": "auth.login", "actor_user_id": actor}

    await set_user_context(db, uuid.UUID(someone))
    theirs = await seeder.insert("audit_log", event(someone))  # no org: an account event
    in_b = await seeder.row("audit_log", org_b, actor_user_id=me)  # mine, but in org B
    await _no_org(db)
    await set_user_context(db, uuid.UUID(me))
    mine = await seeder.insert("audit_log", event(me))
    assert await seeder.keys("audit_log", [mine, theirs, in_b]) == {mine}
    forged, params = await seeder.insert_sql("audit_log", event(someone))
    assert await _try(db, forged, params) == "refused"
    into_b, params = await seeder.insert_sql("audit_log", {**event(me), "org_id": str(org_b)})
    assert await _try(db, into_b, params) == "refused"


@pytest.mark.parametrize("table", STAFF_READ_TABLES)
async def test_staff_read_every_org_and_write_none(app_db: AsyncSession, table: str) -> None:
    db = app_db
    seeder = Seeder(db)
    org_a, org_b = await _org(db), await _org(db)
    staff = await seeder.row("users", None, is_staff=True)
    plain = await seeder.row("users", None)
    theirs = await seeder.row(table, org_b)
    where = await seeder.where(table)

    await set_org_context(db, org_a)
    await set_staff_context(db, uuid.UUID(plain))  # named, but not staff: nothing more
    assert await seeder.keys(table, [theirs]) == set()
    await set_staff_context(db, uuid.UUID(staff))
    assert await seeder.keys(table, [theirs]) == {theirs}
    for sql in (f'UPDATE "{table}" SET org_id = org_id {where}', f'DELETE FROM "{table}" {where}'):
        assert await _try(db, sql, {"key": theirs}) in ("0 rows", "refused"), sql
    await _cannot_write_into(seeder, table, org_a, org_b)
