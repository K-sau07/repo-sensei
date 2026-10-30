"""JPA entities, Spring Data repositories and the code that reads and writes their tables (3.20):
a small lending library of our own (entities with explicit and default names, a mapped
superclass, an embeddable, relationships; repositories with derived, inherited and ``@Query``
methods, one through a generic base interface; a service calling them and an
``EntityManager``; a MongoDB document; Flyway migrations), every node and edge checked, then the
links stage running it and a trace from the service down to the tables."""

from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from sensei_core.models import EdgeType, NodeKind, Provenance
from sensei_engine.graph import java
from sensei_engine.graph.confidence import confidence_of
from sensei_engine.graph.project import Definition, Project
from sensei_engine.graph.snapshot import Edge, GraphSnapshot, Node, lane
from sensei_engine.graph.trace import table_model, trace_feature
from sensei_engine.graph.writer import LINKS, GraphEdge, GraphNode
from sensei_engine.link import jpa
from sensei_engine.link.jpa import FileJpa, JpaGraph, elements, physical, string_expression
from sensei_engine.link.sql import ddl, tables
from sensei_engine.link.stage import LinkRequest, extract_request, links_language, resolve_links
from sensei_engine.parse.parser import parse_file
from sensei_engine.symbols.extract import extract_symbols

ROOT = "src/main/java/com/example/library"

FILES = {
    f"{ROOT}/domain/BaseEntity.java": """\
package com.example.library.domain;

import jakarta.persistence.*;
import java.time.Instant;

@MappedSuperclass
public abstract class BaseEntity {
    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    private Instant createdAt;
}
""",
    f"{ROOT}/domain/Book.java": """\
package com.example.library.domain;

import jakarta.persistence.*;
import java.util.*;

@Entity
@Table(name = "books", schema = "lending")
public class Book extends BaseEntity {
    @Column(name = "isbn_code", nullable = false)
    private String isbn;

    private String title;

    @Transient
    private String cachedTitle;

    private transient int views;

    private static final int MAX = 3;

    @ManyToOne
    @JoinColumn(name = "writer_id")
    private Author author;

    @ManyToOne
    private Shelf shelf;

    @OneToMany(mappedBy = "book")
    private List<BookLoan> loans;

    @ManyToMany
    @JoinTable(
        name = "book_tags",
        joinColumns = @JoinColumn(name = "book_id"),
        inverseJoinColumns = @JoinColumn(name = "tag_id")
    )
    private Set<Tag> tags;
}
""",
    f"{ROOT}/domain/Author.java": """\
package com.example.library.domain;

import jakarta.persistence.Embedded;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.OneToOne;

@Entity
public class Author {
    @Id
    private Long authorId;

    private String penName;

    @Embedded
    private Address address;

    @OneToOne(mappedBy = "author")
    private Biography biography;
}
""",
    f"{ROOT}/domain/Address.java": """\
package com.example.library.domain;

import jakarta.persistence.Embeddable;

@Embeddable
public class Address {
    private String street;
    private String postCode;
}
""",
    f"{ROOT}/domain/Biography.java": """\
package com.example.library.domain;

import jakarta.persistence.*;

@Entity(name = "Bio")
public class Biography extends BaseEntity {
    @OneToOne
    private Author author;

    @Lob
    private String body;
}
""",
    f"{ROOT}/domain/BookLoan.java": """\
package com.example.library.domain;

import jakarta.persistence.*;

@Entity
public class BookLoan extends BaseEntity {
    @ManyToOne
    private Book book;

    private String borrowerName;
}
""",
    f"{ROOT}/domain/Shelf.java": """\
package com.example.library.domain;

import javax.persistence.*;

@Entity
@Table(name = "shelves")
public class Shelf {
    @Id
    @Column(name = "shelf_code")
    private String code;
}
""",
    f"{ROOT}/domain/Tag.java": """\
package com.example.library.domain;

import jakarta.persistence.*;

@Entity
public class Tag extends BaseEntity {
    private String label;
}
""",
    f"{ROOT}/domain/Review.java": """\
package com.example.library.domain;

import org.springframework.data.annotation.Id;
import org.springframework.data.mongodb.core.mapping.Document;
import org.springframework.data.mongodb.core.mapping.Field;

@Document(collection = "reviews")
public class Review {
    @Id
    private String id;

    @Field("stars")
    private int rating;
}
""",
    f"{ROOT}/domain/Draft.java": """\
package com.example.library.domain;

public class Draft {
    private String text;
}
""",
    f"{ROOT}/repository/BookRepository.java": """\
package com.example.library.repository;

import com.example.library.domain.Author;
import com.example.library.domain.Book;
import java.util.List;
import java.util.Optional;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;

public interface BookRepository extends JpaRepository<Book, Long> {
    Optional<Book> findByIsbn(String isbn);

    long countByShelfCode(String code);

    void deleteByAuthor(Author author);

    List<Book> findAll();

    @Query("select b from Book b join b.author a where a.penName = :name")
    List<Book> byPenName(String name);

    @Query(value = "SELECT * FROM books WHERE title LIKE ?1", nativeQuery = true)
    List<Book> search(String pattern);

    @Modifying
    @Query("update Book b set b.title = :title where b.id = :id")
    int retitle(Long id, String title);

    @Modifying
    @Query(value = "DELETE FROM book_tags " + "WHERE book_id = ?1", nativeQuery = true)
    void untag(Long id);

    @Query(\"\"\"
        select count(l) from BookLoan l
        where l.book.id = :id
        \"\"\")
    long loans(Long id);

    List<Book> popular();
}
""",
    f"{ROOT}/repository/BaseRepository.java": """\
package com.example.library.repository;

import org.springframework.data.repository.CrudRepository;
import org.springframework.data.repository.NoRepositoryBean;

@NoRepositoryBean
public interface BaseRepository<T, ID> extends CrudRepository<T, ID> {
}
""",
    f"{ROOT}/repository/AuthorRepository.java": """\
package com.example.library.repository;

import com.example.library.domain.Author;

public interface AuthorRepository extends BaseRepository<Author, Long> {
    Author findFirstByPenName(String penName);
}
""",
    f"{ROOT}/repository/LoanRepository.java": """\
package com.example.library.repository;

import com.example.library.domain.BookLoan;
import org.springframework.data.jpa.repository.JpaRepository;

public interface LoanRepository extends JpaRepository<BookLoan, Long> {
}
""",
    f"{ROOT}/repository/ReviewRepository.java": """\
package com.example.library.repository;

import com.example.library.domain.Review;
import org.springframework.data.mongodb.repository.MongoRepository;

public interface ReviewRepository extends MongoRepository<Review, String> {
    long countByRating(int rating);
}
""",
    f"{ROOT}/repository/DraftRepository.java": """\
package com.example.library.repository;

import com.example.library.domain.Draft;
import org.springframework.data.repository.Repository;

public interface DraftRepository extends Repository<Draft, Long> {
}
""",
    f"{ROOT}/service/LoanService.java": """\
package com.example.library.service;

import com.example.library.domain.Author;
import com.example.library.domain.Book;
import com.example.library.domain.BookLoan;
import com.example.library.repository.AuthorRepository;
import com.example.library.repository.BookRepository;
import com.example.library.repository.LoanRepository;
import jakarta.persistence.EntityManager;
import jakarta.persistence.PersistenceContext;
import java.util.List;
import org.springframework.stereotype.Service;

@Service
public class LoanService {
    private final BookRepository books;
    private final LoanRepository loans;
    private final AuthorRepository authors;

    @PersistenceContext
    private EntityManager em;

    public LoanService(BookRepository books, LoanRepository loans, AuthorRepository authors) {
        this.books = books;
        this.loans = loans;
        this.authors = authors;
    }

    public BookLoan lend(String isbn, String borrower) {
        Book book = books.findByIsbn(isbn).orElseThrow();
        BookLoan loan = new BookLoan();
        return loans.save(loan);
    }

    public List<Author> writers() {
        return (List<Author>) authors.findAll();
    }

    public void archive(BookLoan loan, Long id) {
        em.persist(loan);
        em.find(Author.class, id);
        em.createQuery("select s from Shelf s").getResultList();
        em.createNativeQuery("DELETE FROM audit_log").executeUpdate();
    }
}
""",
    "src/main/resources/db/migration/V1__init.sql": """\
-- the first schema
CREATE TABLE lending.books (id BIGSERIAL PRIMARY KEY, isbn_code VARCHAR(20) NOT NULL);
CREATE TABLE author (author_id BIGINT PRIMARY KEY, pen_name TEXT);
CREATE TABLE book_loan (id BIGINT PRIMARY KEY, book_id BIGINT);
CREATE TABLE IF NOT EXISTS "book_tags" (book_id BIGINT, tag_id BIGINT);
CREATE TABLE shelves_old (shelf_code TEXT);
CREATE TABLE bio (id BIGINT);
CREATE TABLE scratch (id INT);
""",
    "src/main/resources/db/migration/V2__shelves.sql": """\
ALTER TABLE shelves_old RENAME TO shelves;
DROP TABLE scratch;
CREATE TABLE audit_log (id BIGINT, note TEXT);
""",
    "src/main/resources/application.properties": "spring.jpa.open-in-view=false\n",
    "src/main/resources/logback.xml": "<configuration/>\n",
}


def _key(rest: str) -> str:
    return f"java {ROOT}/{rest}"


def _scanned(files: Mapping[str, str]) -> list[FileJpa]:
    found: list[FileJpa] = []
    for path, content in sorted(files.items()):
        if path.endswith(".java"):
            result = jpa.scan(parse_file(path, content.encode()))
            assert result is not None, path
            found.append(result)
        else:
            found.append(jpa.scan_text(path, content.encode()))
    return found


def _symbols(files: Mapping[str, str]) -> set[str]:
    return {
        s.symbol_key
        for path, content in files.items()
        if path.endswith(".java")
        for s in extract_symbols(parse_file(path, content.encode()), "java")
    }


@pytest.fixture(scope="module")
def library() -> JpaGraph:
    graph = jpa.link(_scanned(FILES))
    LINKS.check(graph.nodes, graph.edges)
    keys = _symbols(FILES) | {n.key for n in graph.nodes}
    assert [e for e in graph.edges if e.src not in keys or e.dst not in keys] == []
    return graph


def _nodes(graph: JpaGraph, kind: NodeKind) -> dict[str, GraphNode]:
    return {n.key: n for n in graph.nodes if n.kind is kind}


def _maps(graph: JpaGraph, src: str) -> set[str]:
    return {e.dst for e in graph.edges if e.type is EdgeType.MAPS_TO and e.src == src}


def _access(graph: JpaGraph, src: str) -> set[tuple[str, str, str, float]]:
    """``src``'s (type, table, provenance, confidence)."""
    return {
        (
            e.type.value,
            e.dst.removeprefix("table "),
            e.provenance.value,
            round(e.confidence if e.confidence is not None else 0, 3),
        )
        for e in graph.edges
        if e.src == src and e.type in (EdgeType.READS, EdgeType.WRITES)
    }


def test_spring_names_tables_and_columns() -> None:
    assert physical("OrderLine") == "order_line"
    assert physical("BookLoan") == "book_loan"
    assert physical("createdAt") == "created_at"
    assert physical("HTTPServer") == "httpserver"  # no lower-case letter before the S
    assert physical("addressLine2Text") == "address_line2_text"
    assert physical("v2Api") == "v2_api"
    assert physical("my.Table") == "my_table"
    assert physical("`Quoted`") == "Quoted"
    assert physical("BookLoan", "standard") == "BookLoan"


def test_annotation_arguments_and_java_strings() -> None:
    found = elements('(value = "a, b", nativeQuery = true, hints = {@H(x = "1"), @H(y = "2")})')
    assert found == {
        "value": '"a, b"',
        "nativeQuery": "true",
        "hints": '{@H(x = "1"), @H(y = "2")}',
    }
    assert elements('("users")') == {"value": '"users"'}
    assert elements(None) == {}
    assert string_expression('"select a " + "from A a"') == "select a from A a"
    assert string_expression(r'"say \"hi\""') == 'say "hi"'
    assert string_expression('"a" + TABLE') is None
    assert string_expression('"""\n    select x\n    from y\n    """') == "select x\nfrom y\n"
    assert string_expression('""" select x from y """') == "select x from y "  # one line


def test_entities_tables_and_their_naming(library: JpaGraph) -> None:
    models = _nodes(library, NodeKind.ORM_MODEL)
    tables = _nodes(library, NodeKind.TABLE)
    named = {
        m.name: (m.attrs["table"], m.attrs["entity"], m.attrs["framework"]) for m in models.values()
    }
    assert named == {
        "Book": ("books", "Book", "jpa"),  # @Table(name)
        "Author": ("author", "Author", "jpa"),  # the class name
        "Biography": ("bio", "Bio", "jpa"),  # @Entity(name), through the naming strategy
        "BookLoan": ("book_loan", "BookLoan", "jpa"),  # camelCase to snake_case
        "Shelf": ("shelves", "Shelf", "jpa"),  # javax.persistence too
        "Tag": ("tag", "Tag", "jpa"),
        "Review": ("reviews", "Review", "mongo"),
    }  # no BaseEntity (a mapped superclass), Address (embeddable) or Draft (not an entity)
    assert tables["table books"].attrs["schema"] == "lending"
    assert tables["table reviews"].attrs["store"] == "mongo"
    book = _key("domain/Book.java/Book#")
    assert _maps(library, book) == {f"orm_model {book}"}
    assert _maps(library, f"orm_model {book}") == {"table books"}
    named_by = {
        e.src: e.attrs["named"]
        for e in library.edges
        if e.type is EdgeType.MAPS_TO and e.src.startswith("orm_model ")
    }
    assert named_by[f"orm_model {book}"] == "explicit"
    assert named_by[f"orm_model {_key('domain/Author.java/Author#')}"] == "class name"
    assert named_by[f"orm_model {_key('domain/Biography.java/Biography#')}"] == "entity name"
    assert all(e.provenance is Provenance.CONTRACT for e in library.edges if e.src == book)


def test_columns_skip_transients_and_relationships(library: JpaGraph) -> None:
    columns = _nodes(library, NodeKind.COLUMN)
    by_table: dict[str, set[str]] = {}
    for node in columns.values():
        by_table.setdefault(node.attrs["table"], set()).add(node.name)
    assert by_table["table books"] == {
        # its own, with @Column's name and the owning sides' join columns...
        "isbn_code", "title", "writer_id", "shelf_shelf_code",
        # ... and its mapped superclass's
        "id", "created_at",
    }  # fmt: skip
    # @Embedded: the embeddable's fields; an inverse @OneToOne is no column
    assert by_table["table author"] == {"author_id", "pen_name", "street", "post_code"}
    assert by_table["table bio"] == {"id", "created_at", "author_author_id", "body"}
    assert by_table["table book_loan"] == {"id", "created_at", "book_id", "borrower_name"}
    assert by_table["table book_tags"] == {"book_id", "tag_id"}  # @JoinTable's
    assert by_table["table reviews"] == {"_id", "stars"}
    assert columns["column books.writer_id"].attrs["references"] == "table author"
    # field -MAPS_TO-> column, for the inherited and embedded fields too
    assert _maps(library, _key("domain/Book.java/Book#isbn.")) == {"column books.isbn_code"}
    assert _maps(library, _key("domain/BaseEntity.java/BaseEntity#createdAt.")) == {
        "column books.created_at", "column bio.created_at", "column book_loan.created_at",
        "column tag.created_at",
    }  # fmt: skip
    assert _maps(library, _key("domain/Address.java/Address#postCode.")) == {
        "column author.post_code"
    }
    for gone in ("cachedTitle", "views", "MAX", "loans", "tags"):
        assert _maps(library, _key(f"domain/Book.java/Book#{gone}.")) == set()


def test_repositories_map_to_their_entities_tables(library: JpaGraph) -> None:
    repos = {
        e.src.removeprefix(f"java {ROOT}/repository/"): e.dst
        for e in library.edges
        if e.type is EdgeType.MAPS_TO and e.attrs.get("via") == "repository"
    }
    assert repos == {
        "BookRepository.java/BookRepository#": "table books",
        "AuthorRepository.java/AuthorRepository#": "table author",  # through BaseRepository<T>
        "LoanRepository.java/LoanRepository#": "table book_loan",
        "ReviewRepository.java/ReviewRepository#": "table reviews",
    }  # not BaseRepository (generic) nor DraftRepository (no entity)
    assert library.report.repositories == 4


def test_repository_methods_read_and_write(library: JpaGraph) -> None:
    def method(name: str, repository: str = "BookRepository") -> set[tuple[str, str, str, float]]:
        return _access(library, _key(f"repository/{repository}.java/{repository}#{name}()."))

    assert method("findByIsbn") == {("reads", "books", "import_resolved", 0.9)}
    assert method("countByShelfCode") == {("reads", "books", "import_resolved", 0.9)}
    assert method("deleteByAuthor") == {("writes", "books", "import_resolved", 0.9)}
    assert method("findAll") == {("reads", "books", "import_resolved", 0.9)}  # redeclared
    # JPQL names entities: certain; a path join (b.author) is no entity
    assert method("byPenName") == {("reads", "books", "contract", 1.0)}
    assert method("retitle") == {("writes", "books", "contract", 1.0)}
    assert method("loans") == {("reads", "book_loan", "contract", 1.0)}  # a text block
    # native SQL: tables, through a SqlStatement as orm.py's raw SQL
    assert method("search") == {("reads", "books", "contract", 1.0)}
    assert method("untag") == {("writes", "book_tags", "contract", 1.0)}
    assert method("popular") == set()  # no query and no derived name: a fragment's
    assert method("findFirstByPenName", "AuthorRepository") == {
        ("reads", "author", "import_resolved", 0.9)
    }
    assert method("countByRating", "ReviewRepository") == {
        ("reads", "reviews", "import_resolved", 0.9)
    }
    statements = _nodes(library, NodeKind.SQL_STATEMENT)
    assert {s.attrs["kind"] for s in statements.values()} == {"select", "delete"}
    untag = next(s for s in statements.values() if s.attrs["kind"] == "delete" and "tag" in s.name)
    assert untag.attrs["sql"] == "DELETE FROM book_tags WHERE book_id = ?"
    via = {e.attrs["via"] for e in library.edges if e.src == _key(
        "repository/BookRepository.java/BookRepository#findByIsbn()."
    )}  # fmt: skip
    assert via == {"derived"}
    report = library.report
    assert (report.derived, report.queries, report.native) == (5, 3, 2)


def test_the_service_reaches_tables_through_inherited_methods(library: JpaGraph) -> None:
    service = "service/LoanService.java/LoanService#"
    # loans.save(loan): JpaRepository's own, linked through the receiver's type
    assert _access(library, _key(f"{service}lend().")) == {
        ("writes", "book_loan", "receiver_heuristic", 0.7)
    }
    # authors.findAll(): CrudRepository's, through the generic base interface
    assert _access(library, _key(f"{service}writers().")) == {
        ("reads", "author", "receiver_heuristic", 0.7)
    }
    lend = next(e for e in library.edges if e.src == _key(f"{service}lend()."))
    assert lend.attrs["via"] == "repository.save"
    assert lend.attrs["repository"] == _key("repository/LoanRepository.java/LoanRepository#")
    source = FILES[f"{ROOT}/service/LoanService.java"].splitlines()
    assert lend.evidence_line == next(i for i, t in enumerate(source, 1) if "loans.save" in t)
    # the EntityManager: persist(loan) by the variable's type, find(Author.class), JPQL, SQL
    assert _access(library, _key(f"{service}archive().")) == {
        ("writes", "book_loan", "receiver_heuristic", 0.7),
        ("reads", "author", "import_resolved", 0.9),
        ("reads", "shelves", "import_resolved", 0.9),
        ("writes", "audit_log", "name_unique", 0.9),  # SQL naming a table a migration makes
    }
    assert (library.report.inherited, library.report.entity_manager) == (2, 4)


def test_tables_are_checked_against_the_flyway_migrations(library: JpaGraph) -> None:
    tables = _nodes(library, NodeKind.TABLE)
    made = {n.name for n in tables.values() if n.attrs.get("in_migrations")}
    # V2 renames shelves_old to shelves, drops scratch and adds audit_log
    assert made == {"books", "author", "book_loan", "book_tags", "shelves", "bio", "audit_log"}
    assert library.report.mismatches == ["tag"]  # no migration makes it
    assert library.report.migrations == 7
    assert "in_migrations" not in tables["table reviews"].attrs  # a Mongo collection
    audit = tables["table audit_log"]
    assert audit.attrs["sources"][0] == "migration"
    assert audit.attrs["migration"] == "flyway"
    assert audit.path == "src/main/resources/db/migration/V2__shelves.sql"
    assert {n.name for n in _nodes(library, NodeKind.COLUMN).values()} >= {"note"}
    assert "table scratch" not in tables


def test_migration_scripts_and_changelogs() -> None:
    changes = ddl(
        "CREATE TABLE `orders` (id bigint, note text default 'a;b') ENGINE=InnoDB;\n"
        "CREATE FUNCTION f() RETURNS trigger AS $$ BEGIN; END; $$ LANGUAGE plpgsql;\n"
        "create index ix on orders (note);\n"
        "rename table orders to purchases;\n"
    )
    assert [(c.op, c.table, c.line, c.columns, c.renamed_to) for c in changes] == [
        ("create_table", "orders", 1, (("id", 1), ("note", 1)), None),
        ("rename_table", "orders", 4, (), "purchases"),
    ]
    xml = jpa.scan_text(
        "src/main/resources/db/changelog/db.changelog-master.xml",
        b"""<databaseChangeLog>
  <changeSet id="1" author="me">
    <createTable tableName="members">
      <column name="id" type="bigint"/>
      <column name="nick" type="text"/>
    </createTable>
    <renameTable oldTableName="members" newTableName="patrons"/>
    <dropTable tableName="legacy"/>
  </changeSet>
</databaseChangeLog>
""",
    )
    assert xml.tool == "liquibase"
    assert [(c.op, c.table, c.line, c.columns, c.renamed_to) for c in xml.migrations] == [
        ("create_table", "members", 3, (("id", 4), ("nick", 5)), None),
        ("rename_table", "members", 7, (), "patrons"),
        ("drop_table", "legacy", 8, (), None),
    ]
    yaml = jpa.scan_text(
        "db/changelog/changes.yaml",
        b"""databaseChangeLog:
  - changeSet:
      id: 1
      changes:
        - createTable:
            tableName: patrons
            columns:
              - column:
                  name: id
        - dropTable:
            tableName: legacy
""",
    )
    assert [(c.op, c.table, c.columns) for c in yaml.migrations] == [
        ("create_table", "patrons", (("id", 9),)),
        ("drop_table", "legacy", ()),
    ]
    assert jpa.text_file("db/migration/U2__undo.sql") is None
    assert jpa.text_file("src/main/resources/schema.sql") == "schema"
    assert jpa.text_file("src/test/resources/schema.sql") is None
    assert jpa.text_file(".github/workflows/ci.yml") is None


def test_a_config_names_the_physical_naming_strategy() -> None:
    files = {
        f"{ROOT}/domain/Shelf.java": FILES[f"{ROOT}/domain/Shelf.java"],
        f"{ROOT}/domain/BookLoan.java": FILES[f"{ROOT}/domain/BookLoan.java"].replace(
            " extends BaseEntity", ""
        ),
        "src/main/resources/application.yml": """\
spring:
  jpa:
    hibernate:
      naming:
        physical-strategy: org.hibernate.boot.model.naming.PhysicalNamingStrategyStandardImpl
""",
    }
    graph = jpa.link(_scanned(files))
    assert graph.report.naming == "standard"
    tables = {n.name for n in graph.nodes if n.kind is NodeKind.TABLE}
    assert tables == {"shelves", "BookLoan"}
    columns = {n.name for n in graph.nodes if n.kind is NodeKind.COLUMN}
    assert columns == {"shelf_code", "book_id", "borrowerName"}


def test_single_table_inheritance_and_wildcard_imports() -> None:
    files = {
        f"{ROOT}/domain/Item.java": """\
package com.example.library.domain;

import jakarta.persistence.*;
import java.util.*;

@Entity
public class Item {
    @Id
    private Long id;
}
""",
        f"{ROOT}/domain/Magazine.java": """\
package com.example.library.domain;

import jakarta.persistence.*;
import java.util.*;

@Entity
public class Magazine extends Item {
    private int issueNumber;
}
""",
        f"{ROOT}/other/Entity.java": """\
package com.example.library.other;

public @interface Entity {
}
""",
        f"{ROOT}/other/Note.java": """\
package com.example.library.other;

@Entity
public class Note {
    private String text;
}
""",
    }
    graph = jpa.link(_scanned(files))
    models = {n.name: n.attrs["table"] for n in graph.nodes if n.kind is NodeKind.ORM_MODEL}
    # two on-demand imports leave Entity to the persistence one; Note's is its package's own
    assert models == {"Item": "item", "Magazine": "item"}
    columns = {n.key for n in graph.nodes if n.kind is NodeKind.COLUMN}
    assert columns == {"column item.id", "column item.issue_number"}


def test_python_tables_are_shared_and_other_files_add_nothing() -> None:
    orm_table = GraphNode(
        "table books", NodeKind.TABLE, "books", "app/models.py", 3, 3, lang="sql",
        attrs={"sources": ["model"]}, owner="app/models.py",
    )  # fmt: skip
    graph = jpa.link(_scanned({f"{ROOT}/domain/Shelf.java": FILES[f"{ROOT}/domain/Shelf.java"]}))
    assert graph.report.entities == 1
    graph = jpa.link(_scanned(FILES), [orm_table])
    books = next(n for n in graph.nodes if n.key == "table books")
    assert books.path == "app/models.py"
    assert books.attrs["sources"][:2] == ["model", "entity"]
    assert len({n.key for n in graph.nodes}) == len(graph.nodes)
    empty = jpa.link([jpa.scan_text("src/main/resources/logback.xml", b"<configuration/>")])
    assert (empty.nodes, empty.edges) == ([], [])


def _write(root: Path, files: Mapping[str, str]) -> None:
    for path, content in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content)


def _graph_of(
    definitions: Mapping[str, tuple[Definition, ...]],
    nodes: list[GraphNode],
    edges: list[GraphEdge],
) -> GraphSnapshot:
    held: dict[str, Node] = {}
    for path, found in definitions.items():
        for d in found:
            held[d.symbol_key] = Node(
                d.symbol_key, d.kind, d.name, d.qualified_name, path, d.start_line, d.end_line,
                "java", lane(d.kind, path, None),
            )  # fmt: skip
    for n in nodes:
        held.setdefault(
            n.key,
            Node(
                n.key, n.kind.value, n.name, n.qualified_name or n.name, n.path, n.start_line,
                n.end_line, n.lang, lane(n.kind.value, n.path, None),
            ),
        )  # fmt: skip
    order = sorted(held)
    at = {key: i for i, key in enumerate(order)}
    kept: list[tuple[int, int, Edge]] = []
    for e in edges:
        if e.src in at and e.dst in at:
            confidence = e.confidence if e.confidence is not None else confidence_of(e.provenance)
            edge = Edge(e.type, confidence, e.provenance, e.evidence_path, e.evidence_line)
            kept.append((at[e.src], at[e.dst], edge))
    return GraphSnapshot([held[key] for key in order], kept)


def test_the_links_stage_runs_jpa_and_a_trace_reaches_the_tables(tmp_path: Path) -> None:
    """The pool's work for every file (Java, the migrations, the configs; the rest left
    alone), the stage's resolution with its details, and traces from the service: through the
    repository's derived method to its table, and straight to the table ``save`` writes."""
    _write(tmp_path, FILES)
    languages = {
        p: {"java": "java", "sql": "sql", "properties": "properties", "xml": "xml"}[
            p.rsplit(".", 1)[-1]
        ]
        for p in FILES
    }
    assert all(links_language(language) for language in languages.values())
    files = {
        path: extract_request(LinkRequest(str(tmp_path), path, language))
        for path, language in languages.items()
    }
    assert all(f.failure is None and f.jpa is not None for f in files.values())
    assert files["src/main/resources/logback.xml"].jpa == FileJpa("src/main/resources/logback.xml")
    definitions = {
        path: tuple(
            Definition(
                s.symbol_key, s.name, s.qualified_name, s.kind.value, s.start_line, s.end_line
            )
            for s in extract_symbols(parse_file(path, content.encode()), "java")
        )
        for path, content in FILES.items()
        if path.endswith(".java")
    }
    project = Project(frozenset(FILES), {}, definitions)
    build = resolve_links(project, files)
    LINKS.check(build.nodes, build.edges)
    details: dict[str, Any] = build.details["jpa"]
    assert (details["entities"], details["documents"], details["repositories"]) == (6, 1, 4)
    assert details["mismatches"] == ["tag"]
    facts = {p: f.jpa.java for p, f in files.items() if f.jpa is not None and f.jpa.java}
    code_nodes, code_edges, _ = java.resolve(project, facts)
    graph = _graph_of(definitions, [*code_nodes, *build.nodes], [*code_edges, *build.edges])
    service = _key("service/LoanService.java/LoanService#")
    result = trace_feature(graph, f"{service}lend().", to="table books")
    hops = [hop.node.symbol_key for hop in result.paths[0].hops]
    assert hops == [
        f"{service}lend().",
        _key("repository/BookRepository.java/BookRepository#findByIsbn()."),
        "table books",
    ]
    result = trace_feature(graph, f"{service}lend().", to="table book_loan")
    best = result.paths[0]
    assert [hop.node.symbol_key for hop in best.hops] == [f"{service}lend().", "table book_loan"]
    table = best.hops[-1].node
    model = table_model(graph, table)
    assert model is not None
    assert model.symbol_key == _key("domain/BookLoan.java/BookLoan#")


# ---------------------------------------------------------------- hardening (3.20 b)


def _entity(name: str, body: str, *, imports: str = "") -> str:
    return (
        f"package com.example.library.domain;\n\nimport jakarta.persistence.*;\n{imports}\n"
        f"@Entity\npublic class {name} {{\n    @Id\n    private Long id;\n{body}}}\n"
    )


def test_an_entity_referring_to_itself_or_round_a_cycle_has_its_join_columns() -> None:
    """An employee's manager is an employee; a team's lead's team is a team: the id column is
    read from ``@Id``, never by following the relations round."""
    files = {
        f"{ROOT}/domain/Employee.java": _entity(
            "Employee", "    @ManyToOne\n    private Employee manager;\n"
        ),
        f"{ROOT}/domain/Team.java": _entity("Team", "    @OneToOne\n    private Lead lead;\n"),
        f"{ROOT}/domain/Lead.java": _entity("Lead", "    @ManyToOne\n    private Team team;\n"),
    }
    graph = jpa.link(_scanned(files))
    columns = {n.key for n in graph.nodes if n.kind is NodeKind.COLUMN}
    assert {"column employee.manager_id", "column team.lead_id", "column lead.team_id"} <= columns


def test_a_join_tables_columns_are_named_wherever_name_is() -> None:
    body = (
        "    @ManyToMany\n"
        '    @JoinTable(name = "member_roles",\n'
        '        joinColumns = @JoinColumn(referencedColumnName = "id", name = "member_ref"),\n'
        '        inverseJoinColumns = {@JoinColumn(name = "role_ref")})\n'
        "    private java.util.Set<Role> roles;\n"
    )
    files = {
        f"{ROOT}/domain/Member.java": _entity("Member", body),
        f"{ROOT}/domain/Role.java": _entity("Role", ""),
    }
    graph = jpa.link(_scanned(files))
    columns = {n.key for n in graph.nodes if n.kind is NodeKind.COLUMN}
    assert {"column member_roles.member_ref", "column member_roles.role_ref"} <= columns


def test_a_commented_or_profile_naming_strategy_is_not_the_apps() -> None:
    """``#``/``!`` lines never count, and the app's own config wins over a profile's or a
    test's (``application-local.properties`` sorts first, but applies only under its profile)."""
    standard = "org.hibernate.boot.model.naming.PhysicalNamingStrategyStandardImpl"
    key = "spring.jpa.hibernate.naming.physical-strategy"
    entity = {f"{ROOT}/domain/OrderLine.java": _entity("OrderLine", "    private String note;\n")}

    def naming(configs: Mapping[str, str]) -> str:
        return jpa.link(_scanned({**entity, **configs})).report.naming

    main = "src/main/resources/application"
    assert naming({f"{main}.properties": f"#{key}={standard}\n"}) == "spring"
    assert naming({f"{main}.properties": f"! {key}={standard}\n"}) == "spring"
    profiled = {f"{main}-local.properties": f"{key}={standard}\n", f"{main}.properties": "x=1\n"}
    assert naming(profiled) == "standard"  # the only one that names it
    spring_own = "org.springframework.boot.orm.jpa.hibernate.SpringPhysicalNamingStrategy"
    assert naming({**profiled, f"{main}.yml": f"{key}: {spring_own}\n"}) == "spring"


def test_migrations_keep_their_tables_past_quotes_comments_and_escapes() -> None:
    script = (
        "INSERT INTO settings VALUES ('--verbose', 'it\\'s', '/* not a comment');\n"
        "CREATE TABLE users (id int);\n"
        "-- a comment; with a semicolon\n"
        "/* and ; another */ CREATE TABLE roles (id int, note text DEFAULT 'a''b;c');\n"
        "CREATE FUNCTION f() RETURNS int AS $$ select 1; $$ LANGUAGE sql;\n"
        "CREATE TABLE grants (id int);\n"
    )
    assert [(c.table, c.line) for c in ddl(script)] == [("users", 2), ("roles", 4), ("grants", 6)]
    # An unclosed comment or string runs to the end, without rescanning it each time.
    assert ddl("CREATE TABLE a (id int); /* never closed ; CREATE TABLE b (id int);") == [
        ddl("CREATE TABLE a (id int);")[0]
    ]


def test_a_backslash_escapes_only_where_the_script_says_it_does() -> None:
    """Postgres's ``ESCAPE '\\'`` and ``replace(p, '\\', '/')`` close their strings, as the
    standard has it; an ``E'…'`` string's backslash escapes, and so does every one of a script
    that reads as MySQL's, even where the quotes would still pair up without."""
    postgres = (
        "CREATE TABLE a (id int);\n"
        "CREATE INDEX a_like ON a (name) WHERE name LIKE 'x\\_%' ESCAPE '\\';\n"
        "UPDATE a SET p = replace(p, '\\', '/');\n"
        "CREATE TABLE b (id int);\n"
        "INSERT INTO a VALUES (E'it\\'s; one');\n"
        "CREATE TABLE c (id int);\n"
    )
    assert [c.table for c in ddl(postgres)] == ["a", "b", "c"]
    # One string in MySQL's (backslash-escaped quote), never a table x.
    mysql = (
        "INSERT INTO `a` VALUES ('a\\'; CREATE TABLE x (id int); --');\nCREATE TABLE b (id int);\n"
    )
    assert [(c.table, c.line) for c in ddl(mysql)] == [("b", 2)]


def test_sql_nested_past_the_parsers_depth_is_unread_not_a_crash() -> None:
    deep = "SELECT * FROM t WHERE " + "(" * 80 + "a = 1" + ")" * 80
    found = tables(deep)
    assert (found.reads, found.writes, found.error) == ((), (), "too deeply nested to read")


def test_changelogs_are_read_in_linear_time() -> None:
    yaml = "databaseChangeLog:\n" + ("a:" + " " * 2000 + "x" + " " * 2000 + "'z\n") * 20
    xml = "<databaseChangeLog>" + '<createTable tableName="t">' * 5000
    started = time.monotonic()
    assert jpa.scan_text("db/changelog/master.yaml", yaml.encode()).migrations == ()
    assert jpa.scan_text("db/changelog/master.xml", xml.encode()).migrations == ()
    assert time.monotonic() - started < 2


def test_one_linkers_bug_costs_its_own_facts_not_the_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A linker that raises answers as it would for no files, and the details say it failed;
    the other linkers' reports are all still there."""
    project = Project(frozenset())
    calm = resolve_links(project, {})
    real = jpa.link
    tries: list[int] = []

    def flaky(files: Any, known: Any = ()) -> JpaGraph:
        tries.append(1)
        if len(tries) == 1:  # the stage's run; its fallback (no files) then works
            raise RecursionError("maximum recursion depth exceeded")
        return real(files, known)

    monkeypatch.setattr(jpa, "link", flaky)
    build = resolve_links(project, {})
    assert build.details["failed"] == {"jpa": "RecursionError: maximum recursion depth exceeded"}
    assert {k: v for k, v in build.details.items() if k != "failed"} == calm.details
    assert "failed" not in calm.details
