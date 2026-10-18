# Postgres 18 with pgvector (PGDG apt), pg_trgm (contrib) and pg_textsearch (built from source).
ARG PG_IMAGE=postgres:18.6
ARG PG_MAJOR=18
# pg_textsearch v1.5.1, pinned to the release commit rather than the (movable) tag.
ARG PG_TEXTSEARCH_COMMIT=fa142d049d08eac45a263f5abc250c8877ae076e

FROM ${PG_IMAGE} AS textsearch-build
ARG PG_MAJOR
ARG PG_TEXTSEARCH_COMMIT
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential git ca-certificates "postgresql-server-dev-${PG_MAJOR}" \
 && git clone https://github.com/timescale/pg_textsearch /tmp/pg_textsearch \
 && git -C /tmp/pg_textsearch checkout --detach "${PG_TEXTSEARCH_COMMIT}" \
 && make -C /tmp/pg_textsearch \
 && make -C /tmp/pg_textsearch install DESTDIR=/out

FROM ${PG_IMAGE}
ARG PG_MAJOR
RUN apt-get update \
 && apt-get install -y --no-install-recommends "postgresql-${PG_MAJOR}-pgvector" \
 && rm -rf /var/lib/apt/lists/*
COPY --from=textsearch-build /out/ /
COPY postgresql.conf /etc/postgresql/postgresql.conf
COPY init/ /docker-entrypoint-initdb.d/
CMD ["postgres", "-c", "config_file=/etc/postgresql/postgresql.conf"]
