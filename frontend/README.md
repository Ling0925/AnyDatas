# AnyDatas Web

Vue 3 + TypeScript desktop client for the AnyDatas Rust API.

```bash
pnpm install
pnpm dev
```

The Vite development server listens on `127.0.0.1:5173` and proxies `/api` to the Rust service at `127.0.0.1:8080`.

```bash
pnpm build
```

The production bundle is written to `dist/` and is served by `anydatas-api` in the single-container deployment.
