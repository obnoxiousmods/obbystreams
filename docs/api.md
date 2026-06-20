---
layout: page
title: Obbystreams API Reference
description: Authentication, health checks, stream actions, HLS proxy behavior, ArangoDB status, and NVIDIA SMI telemetry endpoints for Obbystreams.
---

# API

The API is JSON over HTTP. Guarded routes require either:

- `x-obbystreams-token: <token>`
- `obbystreams_token=<token>` cookie

Tokens come from `dashboard.session_token` in the YAML config.

## Login

```http
POST /api/auth/login
content-type: application/json

{"password":"dashboard-password"}
```

Success:

```json
{"ok":true,"token":"configured-session-token"}
```

The response also sets `obbystreams_token`.

## Health

```http
GET /api/health
```

No auth is required.

Successful ready response:

```json
{
  "ok": true,
  "ready": true,
  "checks": {
    "managed_process": true,
    "links_configured": true,
    "playlist_ready": true,
    "playlist_fresh": true
  },
  "health": {
    "state": "healthy",
    "level": "ok",
    "message": "Stream is producing fresh HLS output."
  }
}
```

The endpoint returns `503` when checks fail.

## Status

```http
GET /api/status
x-obbystreams-token: <token>
```

Returns:

- sanitized config
- managed process metrics
- existing unmanaged stream processes
- HLS metrics
- health assessment
- recent events
- recent logs
- recent errors
- runtime counters

## Config

Read sanitized config:

```http
GET /api/config
x-obbystreams-token: <token>
```

Update stream config:

```http
PUT /api/config
content-type: application/json
x-obbystreams-token: <token>

{
  "encoder": "auto",
  "bitrate": "6M",
  "audio_bitrate": "192k",
  "public_hls_url": "https://live.example/stream/ufc.m3u8",
  "links": ["https://example.com/live.m3u8"]
}
```

Accepted keys:

- `links`
- `encoder`
- `bitrate`
- `audio_bitrate`
- `output_dir`
- `public_hls_url`
- `auto_recover`
- `auto_restart_on_exit`
- `watchdog_restart_cooldown`
- `startup_grace_seconds`
- `playlist_stale_seconds`

Some changes restart a running stream automatically.

## Links

`links` are now a compatibility view over structured stream `sources`. New cockpit features should use source endpoints.

Add:

```http
POST /api/links
content-type: application/json
x-obbystreams-token: <token>

{"url":"https://example.com/live.m3u8"}
```

Remove:

```http
POST /api/links/remove
content-type: application/json
x-obbystreams-token: <token>

{"url":"https://example.com/live.m3u8"}
```

Links must be HTTP(S), are deduplicated, and are normalized before being written to config.

## Sources

Sources are guarded cockpit records with stable IDs, labels, type, URL, enabled state, and optional private headers. Headers are used by the proxy/ffmpeg path and are not returned by public endpoints.

List configured source status:

```http
GET /api/sources
x-obbystreams-token: <token>
```

Switch the running cockpit preference to a source:

```http
POST /api/sources/activate
content-type: application/json
x-obbystreams-token: <token>

{"id":"source-2"}
```

Recover a sour-signal source by scraping a replacement playlist:

```http
POST /api/sources/recover-soursignal
content-type: application/json
x-obbystreams-token: <token>

{"id":"sour-signal-main"}
```

Public watcher endpoints:

```http
GET /api/public-configured-sources
GET /api/public-streams
GET /api/live
GET /api/viewers
POST /api/viewers
```

`/api/public-configured-sources` returns the synthetic `server-1` managed source for the official ffmpeg output.

`/api/public-streams` returns separately managed pasted public internet streams. Each item includes `url` plus a CORS-safe `playback_url` that points through `/api/proxy-hls`. These records are not official ffmpeg sources and do not affect `stream.sources`.

## Stream Actions

Start:

```http
POST /api/stream/start
content-type: application/json
x-obbystreams-token: <token>

{"kill_existing":true}
```

Start with one-off links:

```json
{"kill_existing":true,"links":["https://example.com/live.m3u8"]}
```

Stop:

```http
POST /api/stream/stop
x-obbystreams-token: <token>
```

Restart:

```http
POST /api/stream/restart
content-type: application/json
x-obbystreams-token: <token>

{"kill_existing":true}
```

## ArangoDB

```http
GET /api/arango
x-obbystreams-token: <token>
```

Returns whether the configured ArangoDB endpoint is reachable from the app.

## NVIDIA SMI

```http
GET /api/nvidia-smi
x-obbystreams-token: <token>
```

Returns cached NVIDIA GPU telemetry. The collector runs at most once every five seconds and reports:

- GPU name, UUID, driver, pstate, clocks, temperature, utilization, memory, and power.
- NVENC encoder session count, average FPS, and average latency when supported.
- Compute and `pmon` process rows.
- Whether an ffmpeg/NVENC process appears active.
- Command summaries for failed optional `nvidia-smi` queries.

If `nvidia-smi` is unavailable, the route still returns `ok: true` with `available: false` so the frontend can display degraded GPU telemetry without breaking the dashboard.

## HLS Proxy

```http
GET /hls/ufc.m3u8
GET /hls/ufc123.ts
```

The proxy:

1. Serves files from `stream.output_dir` when present.
2. Rewrites relative playlist segment paths to `/hls/*`.
3. Falls back to `stream.public_hls_url`.
4. Falls back to the hardcoded fight stream URL used by the existing deployment.

The HLS proxy is intentionally unauthenticated so the browser player can load media once the dashboard shell is open.
