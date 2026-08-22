---
layout: page
title: Obbystreams Frontend
description: React, Vite, Tailwind CSS, TypeScript, and custom Video.js controls for the Obbystreams live stream dashboard.
---

# Frontend

The frontend is a React 19 application built with Vite, TypeScript, Tailwind CSS, and Video.js. It is a build-time asset pipeline only; production serves static files from Starlette.

## Source Layout

```text
frontend/index.html
frontend/src/App.tsx      Dashboard composition and player controls
frontend/src/api.ts       Fetch helpers and auth handling
frontend/src/format.ts    UI formatting helpers
frontend/src/main.tsx     React entrypoint
frontend/src/styles.css   Font faces, tokens, component styling, breakpoints
frontend/src/types.ts     API payload types
frontend/src/fonts/*.woff2  Vendored Inter + JetBrains Mono variable subsets
```

Generated files:

```text
static/index.html
static/assets/*
```

Do not hand-edit generated assets.

## Development

Run the backend:

```bash
export OBBYSTREAMS_CONFIG=examples/obbystreams.example.yaml
uv run uvicorn app:app --reload --host 127.0.0.1 --port 8767
```

Run Vite:

```bash
npm ci
npm run dev
```

Open the Vite URL. API and HLS requests proxy to the backend.

## Production Build

```bash
npm run typecheck
npm run lint
npm run build
```

The build command runs TypeScript project build and Vite production build.

## Fonts

Inter (UI) and JetBrains Mono (URLs, PIDs, segment names, log feeds) are shipped
as vendored latin `wght`-axis woff2 files in `frontend/src/fonts/`, referenced by
**relative** `url()` from `styles.css`. Vite then treats them as asset imports,
content-hashes them into `static/assets/`, and rewrites the paths under
`base: "/static/"` — correct in both dev and production with no manual handling.

Two rules:

- **Never reference a font CDN.** `tools/responsive-check.mjs` fails the build if
  any request leaves localhost. Self-hosting is also why `tabular-nums` works at
  all — the previous setup declared `font-family: Inter` without shipping it, so
  clients fell back to arbitrary faces with no `tnum` feature.
- Keep the metrics-matched `"… Fallback"` `@font-face` rules. They stop
  `font-display: swap` from reflowing the page on first paint.

To refresh the files: `npm i -D @fontsource-variable/inter
@fontsource-variable/jetbrains-mono`, copy `files/*-latin-wght-normal.woff2` into
`frontend/src/fonts/`, then uninstall the packages. Vendoring keeps every later
build offline and reproducible.

## Layout And Breakpoints

Three regions, all 12-column grids: `stageRow` (player, vitals rail, GPU),
`sourcesRow` (the four source cards), `telemetryRow` (full-bleed telemetry).

- **Media queries are mobile-first `min-width` only**, on Tailwind's scale plus
  `3xl` (1600) / `4xl` (1920) / `5xl` (2560) from the `@theme` block. Do not add
  `max-width` blocks — mixing directions is what previously left a dead band at
  1040–1360. The one exception is `@media (width < 40rem)` for treatments that
  are genuinely small-screen-only, such as the dropdown bottom sheets, where
  undoing `position: fixed !important` upward would need a second `!important`.
- **Do not redefine `sm`/`md`/`lg`/`xl`.** `.statusStrip` and `.rateBadge` use
  those variants inline.
- **`sourcesRow` uses multicol, not grid**, because the four cards have very
  unequal heights and a grid strands whichever one wraps to a new row. This is
  safe only because none of those panels is sticky and each sets
  `break-inside: avoid`.
- **Size metric grids to their panel, not the viewport.** `.metricGrid` uses
  `repeat(auto-fit, minmax(min(150px, 100%), 1fr))`; the vitals rail (~390px) and
  full-bleed telemetry (~2060px) exist at the same viewport width, so a
  breakpoint-driven column count breaks labels and values mid-word in the rail.
- Spacing comes from `--gutter` / `--pad-panel` / `--pad-panel-y` and type from
  the fluid `--text-*` scale. Prefer those over one-off values, and use the
  surface/ink/status tokens rather than literal hexes.

## UI Principles

- The first screen is the actual control surface, not a marketing landing page.
- The dashboard should stay readable under stream stress: state, action, health, and logs are all visible without hunting.
- Purple is the accent color. Green is reserved for positive state, not general theming.
- Controls must remain stable as state changes.
- Player controls must be usable with mouse, keyboard, and touch.
- Long URLs and process commands must wrap or truncate without breaking layout.
  Use `<UrlChip>` for URLs. Avoid `overflow-wrap: anywhere`: besides shredding
  addresses across three lines, it shrinks min-content width, which propagates
  out and can widen the whole document.
- Metric labels are small, dim and uppercase; values are large and near-white.
  Keep that inversion — it is what makes a wall of tiles scannable.
- Panels that are diagnostic rather than always-on should be collapsible, with
  state persisted in `localStorage["obbystreams_ui_v1"]`.

## Video.js Player

The live player uses Video.js for HLS playback and custom React controls for dashboard consistency.

Controls cover:

- play and pause
- mute and volume
- reload
- live-edge indication
- fullscreen
- player state and retry messages

The player prefers `/hls/ufc.m3u8` when the managed process is running and the playlist is ready.

## Authentication

The frontend stores the configured session token under `obbystreams_token` after login and sends it to guarded APIs. If the backend returns unauthorized, the UI clears local auth state and returns to the login screen.

## Accessibility And Responsiveness

Keep interactive controls as real buttons and inputs. Check both desktop and mobile widths before publishing. Use stable grid and panel sizing so logs, metrics, and player state changes do not shift the page unexpectedly.

`npm run test:responsive` is the gate. It spins its own Vite dev server, mocks the
API, and checks eleven viewports from 320 to 3440 for:

- horizontal overflow and elements escaping the viewport
- tap targets under 34px (a control wrapped in a `<label>` is exempt if the label
  itself clears it — so size the label, not the native checkbox)
- dropdown panels escaping the viewport
- **full-page height** against a per-width budget in `HEIGHT_BUDGET`
- **dead column space** — a side-by-side child stopping >900px above the bottom of
  its own grid row
- **any request leaving localhost**, which keeps fonts self-hosted

Ratchet `HEIGHT_BUDGET` down whenever the layout tightens; that ratchet is what
stops the page silently re-inflating. If a change makes the source cards or
telemetry render differently, update `mockStatus()` too — the fixture has to
actually produce source cards, or the badge row, `UrlChip` and per-source menu go
unchecked.

Deploying a frontend change never needs a service restart, and must not get one:
restarting `obbystreams` kills the live ffmpeg encode and drops every viewer.
Because `emptyOutDir` briefly removes `static/`, prefer a staged swap during a
live card:

```bash
npx vite build --outDir ../.static-next --emptyOutDir
mv static static.prev && mv .static-next static   # StaticFiles resolves per request
```
