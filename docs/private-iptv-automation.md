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
- `private_iptv.connection_limit`: sour-signal upstream connection limit. Default is `2`.
- `private_iptv.reserve_spare_when_streaming`: default true, prevents scheduled probes from using the spare sour-signal slot while ffmpeg is healthy.
- `private_iptv.keep_stream_live_when_inactive`: default true, keeps the current official stream running 24/7 even when automation finds no fight-day candidate.

## Flow

> The loop is paused entirely while an operator Stop is in effect
> (`stream.operator_stopped`) — it neither refreshes sources nor auto-starts
> ffmpeg. Blacklisted rows (`source_blacklist`) are dropped before scoring/probe
> and never written into `stream.sources`. See
> [Persistent Stop &amp; Source Blacklist](blacklist-and-stop.md).

1. Fetch provider HTML when needed.
2. Extract the download link with `m3uDownloadBtn` or use configured `playlist_url`.
3. Fetch and parse the M3U playlist into `title`, attributes, and URL.
4. Score each row with metadata, keyword hits, reject terms, PPV/event grouping, and inferred date window.
5. Probe top candidates for playback evidence only when the private connection budget allows it.
6. Write accepted rows into `stream.sources` using the configured prefix.
7. Restart/start the managed ffmpeg stream when accepted source changes require it.
8. Disable auto-created private sources when inactive. Keep ffmpeg running by default so the official stream remains 24/7.

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

## Private Connection Budget

Sour-signal/private URLs allow only two concurrent upstream readers. The managed ffmpeg stream owns the first slot. While ffmpeg is healthy, scheduled automation reserves the remaining slot and runs metadata-only refreshes rather than fetching candidate playlists or media segments. The cockpit can force a deep probe for recovery work, but that should be treated as spending the spare upstream slot.

Source-health checks follow the same rule. Private source rows may show “probe paused” while the live stream is healthy; that is expected and protects the 24/7 stream from account-level “too many streams” failures.

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
