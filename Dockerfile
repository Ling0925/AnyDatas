FROM node:24-bookworm-slim AS frontend-builder

WORKDIR /build/frontend
RUN npm install --global pnpm@11.10.0

COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

FROM rust:1.97-bookworm AS backend-builder

WORKDIR /build/backend
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential pkg-config python3 \
    && rm -rf /var/lib/apt/lists/*

COPY scripts/with-duckdb-prebuilt.py /build/scripts/with-duckdb-prebuilt.py
COPY backend/Cargo.toml backend/Cargo.lock backend/build.rs ./
COPY backend/migrations ./migrations
COPY backend/src ./src
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/build/backend/target \
    --mount=type=cache,target=/build/.cache/duckdb-prebuilt,sharing=locked,id=anydatas-duckdb-prebuilt-linux-x64-v1 \
    python3 /build/scripts/with-duckdb-prebuilt.py -- cargo build --release --locked \
    && cp target/release/anydatas-api /build/anydatas-api \
    && ldd /build/anydatas-api > /tmp/anydatas-api.ldd \
    && if grep -i duckdb /tmp/anydatas-api.ldd; then \
         echo "anydatas-api must not depend on a dynamic DuckDB library" >&2; \
         exit 1; \
       fi

FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl libstdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin anydatas

WORKDIR /app
COPY --from=backend-builder /build/anydatas-api /usr/local/bin/anydatas-api
COPY --from=frontend-builder /build/frontend/dist /app/web

RUN mkdir -p /data/uploads \
    && chown -R anydatas:anydatas /data /app

ENV ANYDATAS_BIND=0.0.0.0:8080
ENV ANYDATAS_DATA_DIR=/data
ENV ANYDATAS_WEB_DIR=/app/web
ENV RUST_LOG=anydatas_api=info,tower_http=info

USER anydatas
EXPOSE 8080

CMD ["anydatas-api"]
