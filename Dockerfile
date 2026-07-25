FROM node:24-bookworm-slim AS frontend-builder

WORKDIR /build/frontend
RUN npm install --global pnpm@11.10.0

COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

FROM rust:1.97-bookworm AS backend-builder

WORKDIR /build/backend
ENV CARGO_BUILD_JOBS=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY backend/Cargo.toml backend/Cargo.lock ./
COPY backend/migrations ./migrations
COPY backend/src ./src
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/build/backend/target \
    cargo build --release --locked \
    && cp target/release/anydatas-api /build/anydatas-api

FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
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
