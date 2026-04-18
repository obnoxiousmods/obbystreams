# Contributing

Obbystreams is a small production service, so changes should keep the runtime model understandable: Starlette owns the API and process supervision, React owns the browser experience, `bin/obbystreams` owns transcoding, and YAML owns local configuration.

## Local Setup

Backend:

```bash
uv sync --dev
export OBBYSTREAMS_CONFIG=examples/obbystreams.example.yaml
uv run uvicorn app:app --reload --host 127.0.0.1 --port 8767
```

Frontend:

```bash
npm ci
npm run dev
```

The Vite dev server proxies `/api` and `/hls` to `127.0.0.1:8767`.

## Validation

Run the full local suite before opening a pull request:

```bash
npm run typecheck
npm run lint
npm run build
npm audit --audit-level=moderate
uv run pytest
uv run ruff check .
uv run mypy app.py
uv run python -m py_compile app.py tools/bootstrap_arango.py
```

Use focused checks during development, but include the full command list in PR validation notes when possible.

## Backend Guidelines

- Keep API responses JSON object shaped with `ok` and explicit error strings.
- Keep authenticated routes behind `guarded(...)` unless the route is intentionally public.
- Keep `/api/health` unauthenticated so service monitors can use it.
- Treat `CONFIG_PATH` as the source of truth for runtime config.
- Keep config writes normalized with `normalize_config`.
- Restart a running stream only when changed settings affect the transcoder.
- Avoid blocking the event loop. Use `asyncio.to_thread` or subprocess APIs intentionally.
- Do not log dashboard passwords, session tokens, or ArangoDB passwords.

## Frontend Guidelines

- Build user-facing UI in `frontend/src/`; never edit generated `static/assets/*` by hand.
- Preserve the production path where Starlette serves `static/index.html`.
- Keep the Video.js player controlled through React state and stable refs.
- Keep custom controls keyboard accessible and usable on mobile.
- Prefer existing formatting and API helpers in `frontend/src/api.ts`, `frontend/src/format.ts`, and `frontend/src/types.ts`.
- Use Tailwind and the local design tokens already in `frontend/src/styles.css`; do not introduce a second component framework.
- Use purple as the accent color and avoid reverting to green status-heavy theming.

## Documentation Guidelines

- Update `README.md` for high-level behavior changes.
- Update `INSTALL.md` when deployment steps, paths, services, or required tools change.
- Update `docs/` when API, config, operations, frontend, release, or troubleshooting behavior changes.
- Update `CHANGELOG.md` for user-visible changes.
- Keep examples copy-pasteable and avoid committing secrets.

## Pull Request Expectations

Every pull request should include:

- What changed and why.
- Any config or migration impact.
- Screenshots for meaningful frontend changes.
- Validation commands that were run.
- Release notes impact, if the change is user-visible.

Prefer small, direct pull requests. Large redesign or release work is acceptable when the scope is intentionally tied together, but the PR should still separate frontend, backend, docs, and release notes clearly in its summary.

## Release Checklist

1. Update versions in `pyproject.toml`, `package.json`, and `package-lock.json`.
2. Update `CHANGELOG.md`.
3. Add release notes under `docs/releases/`.
4. Run the full validation suite.
5. Commit and push `main`.
6. Tag with `vX.Y.Z`.
7. Confirm the Release workflow uploads source, static, install bundle, and checksum assets.
8. Confirm GitHub Pages deploys.
9. Update the GitHub wiki when operator guidance changes.
