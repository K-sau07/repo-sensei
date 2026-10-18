# Postgres 18 with pgvector (PGDG apt), pg_trgm (contrib) and pg_textsearch (built from source).
ARG PG_MAJOR=18
ARG PG_TEXTSEARCH_VERSION=v1.5.1

FROM postgres:${PG_MAJOR} AS textsearch-build
ARG PG_MAJOR
ARG PG_TEXTSEARCH_VERSION
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential git ca-certificates "postgresql-server-dev-${PG_MAJOR}" \
 && git clone --depth 1 --branch "${PG_TEXTSEARCH_VERSION}" \
      https://github.com/timescale/pg_textsearch /tmp/pg_textsearch \
 && make -C /tmp/pg_textsearch \
 && make -C /tmp/pg_textsearch install DESTDIR=/out

FROM postgres:${PG_MAJOR}
ARG PG_MAJOR
RUN apt-get update \
 && apt-get install -y --no-install-recommends "postgresql-${PG_MAJOR}-pgvector" \
 && rm -rf /var/lib/apt/lists/*
COPY --from=textsearch-build /out/ /
COPY postgresql.conf /etc/postgresql/postgresql.conf
COPY init/ /docker-entrypoint-initdb.d/
CMD ["postgres", "-c", "config_file=/etc/postgresql/postgresql.conf"]
