# Test fixtures

## `fastapi-template.tar.gz`

[fastapi/full-stack-fastapi-template](https://github.com/fastapi/full-stack-fastapi-template)
(MIT licence, included in the archive as `LICENSE`) at commit
`b1125d8839482cb7bbae603ddc6d0081bf87532e` (2026-10-15).

A small, realistic monorepo for the discovery tests: a FastAPI backend with SQLModel and
Alembic migrations, a React frontend with a generated API client, an email-template package,
Compose files, nested `.gitignore` files and symbolic links.

Built by `build_fastapi_template.py` from GitHub's tarball of that commit (sha256
`95ec61b80d51c427c96c5004a987f6240edceddefdd8e5b70b1e5750d4393a94` when it was made). The nine
PNG screenshots are replaced with a 1×1 PNG to keep the fixture under 300 KB; every other file
is byte-for-byte the commit. Entries sit under `fastapi-template/`.

## `imports/fastapi-template.tsv`

What every JavaScript and TypeScript import of `fastapi-template.tar.gz` names (1,013 of them),
as the TypeScript compiler (6.0.3) answers it, for the import resolution test (3.3): one row per
binding (`importer`, `line`, `source`, `local`), then `kind` (`symbol`, `module`, `external`,
`file`), the `target` (a file or a package) and the definition's `name`.

Built by `build_import_labels.py`, which lists the statements from the parse captures and asks
`import_labels_oracle.cjs` (`ts.resolveModuleName` with each file's tsconfig, and the checker's
aliased symbols through barrels) what they name. Nothing is installed: a bare specifier is
labelled a package when the importer's `package.json` declares it, or Node lists it as a
built-in; a stylesheet or image import is labelled with its file, which must exist. The
resolver under test is never asked. A sample of rows was checked by hand against the sources.

## `imports/fastapi-template-code.tsv`

What every call, `new`, JSX element and prop given a name in the fixture's JavaScript and
TypeScript refers to, by the TypeScript compiler (6.0.3), for the call and React edge test
(3.5): the file and line of the use, the name used, and the declaring file and name, for the
1,039 uses whose declaration is in the repository. Built by `build_code_labels.py`, which has
`code_labels_oracle.cjs` walk every file and ask the checker (through imports and barrels:
`getAliasedSymbol`). The resolver under test is never asked.
