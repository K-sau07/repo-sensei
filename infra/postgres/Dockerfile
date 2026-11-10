# Postgres 18 with pgvector (PGDG apt), pg_trgm (contrib) and pg_textsearch (built from source),
# and WAL-G for continuous archiving and point-in-time restores (sensei-postgres.sh: off until a
# backup bucket is configured).
ARG PG_IMAGE=postgres:18.6
ARG PG_MAJOR=18
# pg_textsearch v1.5.1, pinned to the release commit rather than the (movable) tag.
ARG PG_TEXTSEARCH_COMMIT=fa142d049d08eac45a263f5abc250c8877ae076e
# WAL-G v3.0.9's Ubuntu 24.04 builds (glibc 2.39: older than this image's), by their release
# checksums.
ARG WALG_VERSION=v3.0.9
ARG WALG_SHA256_AMD64=444ce216230c254edef803e098a59a1746806da3e0d55671d7b0f0082db14ef4
ARG WALG_SHA256_ARM64=cb37c5f8391bd8ef2efceeb9c6995059c0abf2fa74b9b717b42a16a7c93cfde1

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

FROM ${PG_IMAGE} AS walg
ARG TARGETARCH
ARG WALG_VERSION
ARG WALG_SHA256_AMD64
ARG WALG_SHA256_ARM64
RUN set -eu; \
    case "${TARGETARCH:-$(dpkg --print-architecture)}" in \
      amd64) arch=amd64; sum="${WALG_SHA256_AMD64}" ;; \
      arm64) arch=aarch64; sum="${WALG_SHA256_ARM64}" ;; \
      *) echo "no WAL-G build for ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    apt-get update; apt-get install -y --no-install-recommends ca-certificates curl; \
    file="wal-g-pg-24.04-${arch}.tar.gz"; \
    curl -sSfL -o "/tmp/${file}" "https://github.com/wal-g/wal-g/releases/download/${WALG_VERSION}/${file}"; \
    echo "${sum}  /tmp/${file}" | sha256sum -c -; \
    tar -xzf "/tmp/${file}" -C /tmp; \
    install -m 0755 "/tmp/wal-g-pg-24.04-${arch}" /usr/local/bin/wal-g

FROM ${PG_IMAGE}
ARG PG_MAJOR
RUN apt-get update \
 && apt-get install -y --no-install-recommends "postgresql-${PG_MAJOR}-pgvector" \
 && rm -rf /var/lib/apt/lists/*
COPY --from=textsearch-build /out/ /
COPY --from=walg /usr/local/bin/wal-g /usr/local/bin/wal-g
COPY postgresql.conf /etc/postgresql/postgresql.conf
COPY init/ /docker-entrypoint-initdb.d/
COPY sensei-postgres.sh /usr/local/bin/sensei-postgres
ENTRYPOINT ["sensei-postgres"]
CMD ["-c", "config_file=/etc/postgresql/postgresql.conf"]
