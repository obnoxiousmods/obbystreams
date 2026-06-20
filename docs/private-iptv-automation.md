---
layout: page
title: Private IPTV Automation
description: Design notes for automatic official source discovery from the private provider playlist.
---

# Private IPTV Automation

This feature manages the official/private ffmpeg input. It is not the public pasted-source system used by watcher clients.

## Purpose

The provider playlist contains many channels and event rows. On UFC/fight days, the cockpit should find the relevant private rows, validate that they are likely usable, and add them to `stream.sources` so the managed ffmpeg process can use them. On non-fight days or when provider rows are placeholders, the cockpit should disable auto-created private IPTV sources and stop the managed ffmpeg stream when configured.

## Inputs

- `private_iptv.provider_url`: authenticated HTML provider page.
- `private_iptv.playlist_url`: direct M3U playlist URL, preferred when known.
- `private_iptv.cookies`: provider auth cookies, kept only in live server config.
- `private_iptv.headers`: browser-like headers for provider requests.
- `private_iptv.keywords`: positive row-matching terms.
- `private_iptv.reject_keywords`: placeholder/stale/non-event terms.
- `private_iptv.require_date_window_match`: default true, prevents always-present generic UFC/PPV rows from keeping ffmpeg active on non-fight days.

## Flow

1. Fetch provider HTML when needed.
2. Extract the download link with `m3uDownloadBtn` or use configured `playlist_url`.
3. Fetch and parse the M3U playlist into `title`, attributes, and URL.
4. Score each row with metadata, keyword hits, reject terms, PPV/event grouping, and inferred date window.
5. Probe top candidates for playback evidence.
6. Write accepted rows into `stream.sources` using the configured prefix.
7. Restart/start the managed ffmpeg stream when accepted source changes require it.
8. Disable auto-created private sources and stop ffmpeg when inactive if configured.

## Bad-Valid Detection

Some upstreams answer with HTTP 200 but are not usable streams. Candidate probing rejects or penalizes:

- HTML block/error pages returned as playlist URLs.
- responses that are not HLS playlists.
- HLS playlists with no media URLs.
- master playlists whose first variant is dead or empty.
- media playlists whose latest segment cannot be read.
- tiny ended VOD windows that look unlike a live source.
- placeholder rows such as “No Scheduled Event”, 24/7 rows, replays, classics, preshows, and post-fight press conferences.

The scorer is intentionally heuristic. It does not claim perfect schedule knowledge; it makes defensible decisions from provider metadata and playback evidence. Tune `keywords`, `reject_keywords`, `min_score`, `date_window_hours`, and `require_date_window_match` when provider naming changes. For the current provider shape, requiring a current date-window match is important because generic UFC/PPV rows can remain in the playlist even when there is no active fight.

## Source Ownership

Accepted private IPTV rows are official cockpit sources:

```yaml
stream:
  sources:
    - id: private-iptv-ufc-ppv-main-card
      label: UFC PPV Main Card
      type: soursignal
      url: https://soursignal.com/...
      enabled: true
```

They are never `public_sources`. Public pasted sources remain 24/7 viewer options, are proxied by `/api/proxy-hls`, and are documented in `public_srcs.md`.

## Security

Do not commit live provider cookies, direct private playlist keys, or generated source manifests. Guarded config/status responses redact cookies and token-like headers, but the live YAML file and `/tmp/obbystreams-sources.json` still need filesystem protection.

## Verification

Run:

```sh
uv run python -m py_compile app.py bin/obbystreams
uv run pytest -q
npm run typecheck
npm run build
```

Then force a guarded refresh:

```sh
curl -fsS -X POST http://127.0.0.1:8767/api/private-iptv/refresh \
  -H 'x-obbystreams-token: TOKEN' \
  -H 'content-type: application/json' \
  --data '{}'
```

Check `/api/status` for `private_iptv.state`, `accepted_count`, and evidence rows.
