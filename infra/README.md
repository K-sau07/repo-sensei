# Local infrastructure

`make up` starts everything with `infra/docker-compose.yml`; `make down` stops it (data is kept);
`make reset-db` deletes the Postgres volume after confirmation.

| Service            | Port  | Notes                                                                               |
| ------------------ | ----- | ----------------------------------------------------------------------------------- |
| Postgres 18        | 55432 | `pgvector`, `pg_trgm`, `pg_textsearch` (BM25); databases `sensei` and `sensei_test` |
| Redis 7            | 56379 | append-only persistence                                                             |
| SeaweedFS (S3 API) | 9000  | buckets `sensei-snapshots`, `sensei-artifacts`; production uses S3/R2               |

Ports and credentials come from `.env` (see `.env.example`).

The Postgres image is built from `infra/postgres/Dockerfile`: pgvector comes from the PGDG apt
repository and `pg_textsearch` is compiled from a pinned release tag. Init scripts in
`infra/postgres/init/` only run when the data volume is empty, so run `make reset-db` after
changing them.

Run `uv run python scripts/check_infra.py` (or `make infra-check`) to verify the stack.
