---
layout: page
title: Obbystreams Configuration
description: Configure dashboard auth, stream links, HLS output, health scoring, GPU mode, ffmpeg recovery, and ArangoDB persistence.
---

# Configuration

Obbystreams reads YAML from `OBBYSTREAMS_CONFIG`, defaulting to `/etc/obbystreams/obbystreams.yaml`.

The app normalizes the file on load and writes normalized YAML when the dashboard updates stream settings. Keep comments in a separate operator note if you need them permanently, because dashboard writes may not preserve YAML comments.

## server

```yaml
server:
  host: 127.0.0.1
  port: 8767
  workers: 1
```

`host` and `port` describe the intended bind address. The provided systemd unit starts uvicorn explicitly on `127.0.0.1:8767`.

## dashboard

```yaml
dashboard:
  password: "change-me"
  session_token: "change-me-to-a-long-random-token"
```

`password` is submitted to `POST /api/auth/login`.

`session_token` is returned on successful login and accepted by guarded routes through the `x-obbystreams-token` header or `obbystreams_token` cookie.

Do not leave `session_token` empty in production. An empty token makes guarded routes open.

## stream

```yaml
stream:
  command: /usr/bin/obbystreams
  encoder: auto
  output_dir: /var/www/live.obnoxious.lol/stream
  ffmpeg_log_dir: ffmpegLogs
  public_hls_url: https://live.obnoxious.lol/stream/ufc.m3u8
  auto_recover: true
  auto_restart_on_exit: true
  watchdog_restart_cooldown: 20
  startup_grace_seconds: 25
  playlist_stale_seconds: 25
  min_assessment_seconds: 15
  health_sample_interval: 2
  success_score_threshold: 180
  failure_score_threshold: -120
  confirmed_failure_samples: 2
  failure_ramp_seconds: 60
  bitrate: 6M
  audio_bitrate: 192k
  restart_delay: 2
  max_restart_delay: 120
  rate_limit_delay: 180
  stop_after_failed_rounds: 2
  links:
    - https://example.com/primary/live.m3u8
    - https://example.com/backup/live.m3u8
```

Important keys:

- `command`: executable launched by the dashboard when starting the managed stream.
- `encoder`: `auto`, `gpu-only`, or `cpu`.
- `output_dir`: directory where `ufc.m3u8` and segments are written.
- `ffmpeg_log_dir`: durable ffmpeg log directory used by the transcoder wrapper.
- `public_hls_url`: public HLS playlist used by the dashboard and HLS proxy fallback.
- `auto_recover`: enables watchdog restarts.
- `auto_restart_on_exit`: restarts the stream after unexpected process exit when links exist.
- `watchdog_restart_cooldown`: minimum seconds between watchdog restart actions.
- `startup_grace_seconds`: startup window before missing ffmpeg child or playlist output is considered unhealthy.
- `playlist_stale_seconds`: maximum playlist age before the health endpoint reports stale output.
- `min_assessment_seconds`: minimum runtime evidence before a failure can be confirmed.
- `health_sample_interval`: minimum interval between health scorer samples.
- `success_score_threshold`: score required to mark output healthy.
- `failure_score_threshold`: score low enough to count as bad evidence.
- `confirmed_failure_samples`: repeated bad samples required before confirmed failure.
- `failure_ramp_seconds`: time window used to ramp failure evidence.
- `bitrate` and `audio_bitrate`: forwarded to the transcoder command.
- `restart_delay`, `max_restart_delay`, `rate_limit_delay`, `stop_after_failed_rounds`: forwarded to the transcoder wrapper when configured.
- `links`: source HLS links used by the transcoder.

Changing links, encoder, bitrate, audio bitrate, output directory, public HLS URL, ffmpeg log directory, assessment thresholds, or transcoder restart parameters restarts a running managed stream so the new settings take effect.

## private_iptv

`private_iptv` is the automation lane for the official/private ffmpeg source. It fetches the provider page or direct M3U playlist, scores fight-day entries, probes likely playback candidates, writes accepted entries into `stream.sources`, and can stop the managed ffmpeg stream when no validated fight source is active.

It does not populate `public_sources`. Public pasted internet streams are a separate 24/7 viewer inventory.

```yaml
private_iptv:
  enabled: false
  provider_url: https://iptorrents.com/iptv
  playlist_url: https://tv123.me/iptv/ACCOUNT/PRIVATE_KEY/Default
  timezone: Canada/Pacific
  refresh_interval_seconds: 900
  max_candidates: 12
  min_score: 70
  probe_candidates: true
  probe_timeout_seconds: 10
  disable_stream_when_inactive: true
  auto_start_when_active: true
  auto_source_prefix: private-iptv
  headers:
    Referer: https://iptorrents.com/t
    User-Agent: Mozilla/5.0
  cookies:
    uid: "private"
    pass: "private"
  keywords:
    - ufc
    - mma
    - fight night
    - prelims
    - main card
  reject_keywords:
    - no event
    - no scheduled event
    - replay
    - classic
    - 24/7
  date_window_hours: 30
  require_date_window_match: true
```

Important keys:

- `enabled`: starts the scheduled automation loop.
- `provider_url`: authenticated HTML page used to discover the playlist download link when `playlist_url` is omitted.
- `playlist_url`: direct M3U playlist URL. Prefer setting this in production so refreshes do not depend on provider-page markup.
- `cookies`: private provider cookies. These are redacted from API/config responses and must not be committed.
- `keywords` and `reject_keywords`: scoring vocabulary for event rows. UFC/fight rows score up; placeholders like “No Scheduled Event”, 24/7 channels, replays, and stale preshows score down.
- `date_window_hours`: rows with an inferred date outside this window are penalized.
- `require_date_window_match`: requires at least one current dated event signal before accepting a row. Keep this enabled for providers that always list generic UFC/PPV channels.
- `probe_candidates`: fetches candidate URLs and rejects bad-but-HTTP-valid responses such as HTML block pages, empty playlists, dead variant playlists, unreadable segments, or tiny ended VOD windows.
- `disable_stream_when_inactive`: disables auto-created private IPTV sources and stops managed ffmpeg when no accepted source remains.
- `auto_start_when_active`: starts managed ffmpeg after automation finds accepted sources and the process is not already running.

Auto-created source IDs start with `auto_source_prefix`, default `private-iptv-`. Manual official sources are preserved.

## arangodb

```yaml
arangodb:
  enabled: true
  url: http://127.0.0.1:8529
  database: obbystreams
  username: obbystreams_app
  password: "change-me"
```

When enabled, the app queues writes for:

- `events`
- `links`
- `metrics`
- `configs`

ArangoDB write failures do not block the dashboard request path. Failures are tracked in runtime counters and recent errors.

## Config Safety

- Keep the file readable only by the service user or trusted group.
- Do not commit the live file.
- Prefer updating stream links through the dashboard once production is running.
- Back up the file before release rollouts.
