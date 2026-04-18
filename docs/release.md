---
layout: page
title: Obbystreams Release Process
description: Versioning, validation, GitHub Releases, release artifacts, GitHub Pages, and wiki publishing for Obbystreams.
---

# Release

Obbystreams publishes GitHub Release artifacts. It does not currently publish an npm package, Python package, or container image.

## Versioning

Update these files together:

- `pyproject.toml`
- `package.json`
- `package-lock.json`
- `CHANGELOG.md`
- `docs/releases/vX.Y.Z.md`

Use tags in the form `vX.Y.Z`.

## Local Validation

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

## Release Artifacts

The release workflow creates:

- `obbystreams-vX.Y.Z-source.tar.gz`
- `obbystreams-vX.Y.Z-static.tar.gz`
- `obbystreams-vX.Y.Z-install-bundle.tar.gz`
- `SHA256SUMS`

The install bundle includes the app, frontend build, docs, examples, service files, nginx config, lockfiles, and tool scripts needed for deployment.

## Publish Flow

```bash
git checkout main
git pull --ff-only origin main
git tag -a vX.Y.Z -m "Obbystreams vX.Y.Z"
git push origin main
git push origin vX.Y.Z
```

The `Release` workflow runs on the pushed tag and creates or updates the GitHub Release.

## GitHub Pages

The Pages workflow deploys `docs/` from `main`.

Expected URL:

```text
https://obnoxiousmods.github.io/obbystreams/
```

Pages should be configured for GitHub Actions builds.

## GitHub Wiki

The wiki should mirror the operator docs at a higher level:

- Home
- Quick Start
- Installation
- Configuration
- Operations
- API
- Frontend
- Releases
- Security
- Troubleshooting

Update the wiki when production instructions change enough that operators would otherwise reach for outdated commands.
