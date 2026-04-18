# Changelog

All notable Obbystreams changes are tracked here.

## Unreleased

### Added

- Expanded live dashboard SEO metadata with canonical URL, Open Graph, Twitter Card, JSON-LD, manifest, favicon, robots, sitemap, and social preview assets.
- Expanded GitHub Pages SEO config with canonical site URL, base URL, page descriptions, social image, Jekyll SEO tag, Jekyll sitemap, and robots output.

## 0.2.1 - 2026-04-18

### Fixed

- Restored the full stream health scorer, assessment window, evidence scoring, and confirmed failure logic from the deployed Obbystreams backend.
- Restored transcoder support for `--ffmpeg-log-dir`, assessment thresholds, failure ramp settings, and strict GPU mode flags.
- Restored the expanded example YAML keys used by the dashboard and transcoder wrapper.

### Added

- Regression coverage for health scoring, strict GPU mode, NVIDIA telemetry parsing, and transcoder command generation.

## 0.2.0 - 2026-04-18

### Added

- React 19, Vite, TypeScript, Tailwind CSS, and Video.js frontend for the dashboard.
- Custom live-player controls with play, pause, mute, volume, live-edge, reload, and fullscreen behavior.
- Purple-accent visual system for the dashboard, replacing the previous green-heavy theme.
- Responsive control-room layout for stream actions, HLS health, process telemetry, GPU telemetry, ArangoDB status, links, logs, and events.
- Guarded `/api/nvidia-smi` endpoint for cached NVIDIA GPU telemetry.
- Frontend build validation in CI.
- GitHub Pages documentation source under `docs/`.
- Release notes for tagged releases under `docs/releases/`.
- Release artifacts for source, built static files, install bundles, and SHA-256 checksums.
- Expanded issue templates and pull request template.

### Changed

- Merged the Obbystreams frontend redesign back into `main`.
- Updated documentation across README, installation, contribution, security, changelog, release, API, operations, frontend, and troubleshooting surfaces.
- Updated package versions to `0.2.0`.
- Improved release workflow coverage so tagged releases build and publish frontend assets.

### Operational Notes

- The Starlette backend still serves the built frontend from `static/`.
- Production installs should run `npm ci && npm run build` before copying files or use the release install bundle.
- GitHub Pages and the GitHub wiki are intended to mirror the operator documentation.

## 0.1.0 - 2026-04-17

### Added

- Initial Obbystreams dashboard.
- Starlette backend for stream process management.
- Static dark dashboard frontend.
- ArangoDB persistence for events, links, metrics, configs, and snapshots.
- HLS health checks and process telemetry.
- Example nginx and systemd deployment files.
- CI, CodeQL, Dependabot, issue templates, pull request template, security policy, changelog, license, and CODEOWNERS.
