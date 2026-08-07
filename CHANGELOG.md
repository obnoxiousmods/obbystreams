# Changelog

All notable Obbystreams changes are tracked here.

## Unreleased

### Added

- **Event-aware source discovery.** The auto-schedule now hands the tracked
  card's identity (fighter surnames, event number, ESPN's real per-segment
  start times) to the private-IPTV scraper, and discovery runs *before* the
  encode starts rather than after it. A feed must identify the tracked card to
  be selected; with none verified the cockpit stays armed with the stream
  **down** and retries every 60s rather than broadcasting an unidentified
  channel. Sources carry `event_id`/`discovered_at`, a previous card's feeds are
  purged at every arming and retired at stand-down, and `GET /api/schedule`
  exposes a `source_state` block (matched sources, match terms, rejected
  candidates with reasons, switch count) that the cockpit renders.
  Fixes the 2026-08-01 card, which armed on time and then streamed the previous
  week's channels for its entire duration while reporting healthy.
- Mid-card source switching with anti-flap guardrails
  (`switch_cooldown_seconds`, `switch_confirm_samples`, `max_switches_per_card`),
  so the stream follows early prelims → prelims → main card and fails off a dead
  or wrong feed without flapping during a fight.
- Public backup sources are re-scraped at pre-roll (their tokens last ~3h) and
  used as ingest links after `public_fallback_after_attempts` failed private
  sweeps.
- Stand-down backstop for a card ESPN never marks final (`stall_hours` +
  `stall_idle_minutes`), and boot-time pruning of `ffmpegLogs/`, which had grown
  to ~150k files / 13GB with no rotation.

- **Persistent operator Stop**: `POST /api/stream/stop` now persists
  `stream.operator_stopped`, keeping the managed ffmpeg AND both scrapers idle
  until an explicit Start/Restart — surviving supervisor ticks and full
  restarts. The cockpit shows a "STOPPED (manual)" banner and a Resume button.
- **Persistent source blacklist**: new `source_blacklist` config plus
  `GET/POST /api/blacklist` and `/api/blacklist/remove`. A blocked source
  (matched by URL, URL-without-query for token rotation, id, channel, or label)
  is filtered from every scraper cycle and every viewer-facing list. Cockpit
  gains Block buttons on both source lists and a Blacklist panel to unblock.
- Backend integration test harness (Starlette `TestClient`) covering the full
  route surface, plus dedicated operator-stop and blacklist unit tests; pytest
  now runs in CI.
- A Vitest + Testing Library suite for the cockpit frontend (previously had no
  test runner), and config-module tests for ObbyWatcher.
- Expanded live dashboard SEO metadata with canonical URL, Open Graph, Twitter Card, JSON-LD, manifest, favicon, robots, sitemap, and social preview assets.

### Changed

- **Type checking migrated from mypy to [ty](https://github.com/astral-sh/ty)**
  (Astral) at strict settings (`error-on-warning`); the ruff ruleset was
  broadened (C4, PIE, RET, RUF, ASYNC, PERF, ISC, TID, FLY, G, LOG, PLE). All
  tooling runs through uv.

### Fixed

- `start_managed_process` no longer resets the stream desired-state as a side
  effect, so a watchdog- or scraper-initiated start can never silently override
  a manual Stop.
- Closed a shutdown leak where the viewer-stats flush task was never cancelled,
  and retained strong references to fire-and-forget geo-lookup tasks.
- Expanded GitHub Pages SEO config with canonical site URL, base URL, page descriptions, social image, Jekyll SEO tag, Jekyll sitemap, and robots output.
- Replaced selector-style dashboard controls with custom React dropdown/listbox menus, including the encoder picker and live player overflow actions, while keeping native Video.js dropdowns disabled.
- Expanded responsive dashboard support with safe-area spacing, intrinsic grids, mobile player controls, phone bottom-sheet menus, viewport-aware dropdown placement, and a Playwright viewport screenshot checker.

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
