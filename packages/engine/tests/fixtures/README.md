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
