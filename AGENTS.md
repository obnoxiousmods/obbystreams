# Obbystreams Agent Notes

## Project Role

Obbystreams is the cockpit/control plane for `https://s.obby.ca/`.

Do not treat this repository as the public fight viewer. The public watcher/client is `https://fight.nswfiles.com/` and lives in `/home/joey/obbywatcher`.

## Responsibilities

- Manage the currently streamed source for `soursignal.com` and other upstreams.
- Start, stop, restart, and monitor the managed ffmpeg process.
- Keep structured source config authoritative; keep legacy `links` synchronized for compatibility.
- Support explicit source switching through `Switch`, not order-management UX.
- Recover sour-signal links by finding a replacement stream URL while preserving the source identity.
- Run private IPTV automation as an official-source feeder: parse the authenticated provider playlist, score UFC/fight-day rows, probe only when the private sour-signal connection budget allows it, and update `stream.sources`.
- Protect the sour-signal upstream limit: private URLs allow two concurrent readers, the managed ffmpeg stream owns the first slot, and scheduled automation/source-health checks must reserve the spare slot while the stream is healthy.
- Keep the official managed ffmpeg stream live 24/7 by default. Inactive private IPTV automation may disable auto-created candidate rows, but it must not stop ffmpeg unless `keep_stream_live_when_inactive` is explicitly false.
- Store and apply private per-source headers for proxy/ffmpeg use.
- Maintain a separate `public_sources` inventory for pasted public internet stream URLs, including per-public-source request headers when needed.
- Expose safe public managed Server 1 status, viewer telemetry, and proxied public-source playback for ObbyWatcher.

## Public Contract For ObbyWatcher

- `GET /api/public-configured-sources`
- `GET /api/live`
- `GET /api/viewers`
- `POST /api/viewers`
- `GET /api/public-streams`
- `GET /api/public-source`
- `GET /hls/ufc.m3u8`

Never expose private official source headers, dashboard credentials, Arango credentials, or raw cockpit config through public endpoints. Keep official ffmpeg sources in `stream.sources`; keep pasted public internet sources in top-level `public_sources`. See `public_srcs.md` before changing, deleting, or reclassifying public source records.

Private IPTV provider cookies and direct playlist keys belong only in live server config. Commit placeholders and docs, never live cookies.

The `/api/proxy-hls` path is a hot fan-out path. Preserve backend cache
coalescing, stale fallback, pooled upstream fetches, and the nginx buffered
location when changing public playback.

## Operator UI Guidance

The cockpit should be dense, operational, and source-focused. `Up`/`Down` link ordering controls are not useful here. Prefer source rows with health, type, viewer count, `Switch`, `Recover` where applicable, `Open`, and `Remove`.

## Verification

Run at minimum:

```sh
uv run python -m py_compile app.py bin/obbystreams
uv run pytest -q
npm run typecheck
npm run build
```

See `docs/system-design.md` for the full design/spec and ownership boundary.
