# Obbystreams / ObbyWatcher Design Spec

## Codebase Ownership

`s.obby.ca` is the Obbystreams cockpit. It manages the private official source currently being transcoded, starts and restarts ffmpeg, writes stream configuration, probes configured sources, and also hosts a separate public-source inventory for pasted internet stream URLs that must be proxied for browser playback.

`fight.nswfiles.com` is the ObbyWatcher public client. It renders the viewer/player experience, source/server switching UI, viewer heartbeats, chat and viewer-facing diagnostics. It must not write cockpit config, expose source headers, or administer ffmpeg.

## Current State

Obbystreams contains the Python cockpit API, the `bin/obbystreams` transcoder wrapper, HLS/DASH output management, source scraping helpers, Arango telemetry, health scoring, and a React cockpit UI.

ObbyWatcher contains the public static React app that consumes cockpit public status/source APIs, watches the fight stream, reports viewer counts, and switches between the official managed stream and separate pasted public playback sources.

Before the source model cleanup, the cockpit treated upstreams primarily as ordered URL strings. The Up/Down controls implied that ordering was a meaningful operator workflow, but the real need is to switch immediately to one known source and recover sour-signal links when they rot.

## Intended Behavior

The cockpit owns configured sources as structured records:

- `id`: stable internal source identifier.
- `label`: operator-facing label.
- `type`: `hls`, `soursignal`, `page`, or `external`.
- `url`: source page or playlist URL.
- `enabled`: whether this private source participates in cockpit-managed ffmpeg fallback.
- `headers`: private per-source request headers used by the proxy/ffmpeg, never exposed publicly.

The preferred source is the first enabled configured source. Pressing `Switch` moves that source to the preferred slot and restarts the managed stream if it is running. Legacy `links` remain as a compatibility projection for older callers and CLI args.

Sour-signal sources are recoverable. The cockpit keeps their stable source ID/label, attempts to scrape a replacement HLS URL, updates the source, and restarts the stream when appropriate.

Public pasted sources are top-level `public_sources`, not `stream.sources`. They are third-party internet stream URLs pasted for viewers to watch through the public client. They are not ffmpeg inputs for the official managed stream. They are exposed through `/api/public-streams` with `playback_url` values that point at `/api/proxy-hls`, so browser CORS never depends on the third-party origin. Some records require `headers` such as `Referer`, `Origin`, or browser user-agent values; the proxy applies those headers server-side and rewrites nested playlists back through itself. The current source inventory is documented in `public_srcs.md`.

The proxy path must be treated as a fan-out service. It uses backend coalescing,
bounded fresh/stale cache, pooled upstream HTTP connections, curl fallback for
hostile origins, browser cache headers, and a dedicated nginx `/api/proxy-hls`
buffered location. Do not route public HLS playback through the generic
unbuffered API proxy settings.

The watcher consumes only public and redacted endpoints:

- `GET /api/public-configured-sources`
- `GET /api/public-streams`
- `GET /api/live`
- `GET /hls/ufc.m3u8`
- `GET /api/public-source` as a legacy optional supplement only
- `POST /api/viewers`

## Feature Placement

Obbystreams should contain:

- Source CRUD, switching, sour-signal recovery, source health checks.
- ffmpeg process lifecycle and restart logic.
- HLS/DASH output configuration.
- Private source headers and source manifest generation for the CLI.
- Separate `public_sources` management and proxied public playback URLs.
- Public read-only watcher APIs for managed Server 1 status/viewer telemetry with redacted fields.
- Operator-only cockpit UI for managing sources and runtime health.

ObbyWatcher should contain:

- Public player UI and source/server selection.
- Viewer heartbeats and viewer-count display.
- SSE/polling subscription to cockpit public status.
- Client-side failover between pasted public sources.
- Public fallback display and user-facing diagnostics.

## Tradeoffs

The cockpit still preserves `links` because the transcoder and older integrations already understand it. That avoids a breaking migration while making `sources` authoritative.

Source headers are stored in cockpit config and passed through a temporary source manifest to the CLI. This keeps secrets out of public APIs but means the cockpit must write the manifest before starting ffmpeg.

The public cockpit API returns a synthetic `server-1` managed HLS source for the official stream, and `/api/public-streams` returns the separate pasted public source inventory. This keeps private official source headers and sour-signal management separate while still letting `s.obby.ca` proxy public streams for CORS.

Health probes are intentionally lightweight. Direct HLS sources are fetched through the proxy path; page and sour-signal sources are checked as reachable/recoverable pages because they are not necessarily HLS playlists themselves.

## Verification Expectations

For Obbystreams changes, run:

```sh
uv run python -m py_compile app.py bin/obbystreams
uv run pytest -q
npm run typecheck
npm run build
```

For ObbyWatcher changes, run:

```sh
npm run build
npm test
npm run test:e2e
```

After deployment, smoke-test:

- `https://s.obby.ca/api/public-configured-sources`
- `https://s.obby.ca/api/live`
- `https://s.obby.ca/api/viewers`
- `https://fight.nswfiles.com/`
- Cockpit source `Switch` and sour-signal `Recover` actions.
