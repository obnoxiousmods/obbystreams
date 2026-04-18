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
frontend/src/styles.css   Tailwind import, tokens, and component styling
frontend/src/types.ts     API payload types
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

## UI Principles

- The first screen is the actual control surface, not a marketing landing page.
- The dashboard should stay readable under stream stress: state, action, health, and logs are all visible without hunting.
- Purple is the accent color. Green is reserved for positive state, not general theming.
- Controls must remain stable as state changes.
- Player controls must be usable with mouse, keyboard, and touch.
- Long URLs and process commands must wrap or truncate without breaking layout.

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
