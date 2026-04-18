---
layout: page
title: Obbystreams Operations
description: Day-to-day operating guide for Obbystreams stream actions, watchdog recovery, HLS health, GPU telemetry, logs, and deploys.
---

# Operations

This page is for day-to-day operation of the production dashboard.

## Dashboard Actions

Start:

- Validates configured or supplied links.
- Optionally kills existing unmanaged stream processes.
- Launches the configured `stream.command`.
- Captures stdout and stderr into recent logs.
- Records an event and process metadata.

Stop:

- Sends SIGTERM to the managed process group.
- Sends SIGKILL if the process does not exit within the timeout.
- Records the last exit code.

Restart:

- Stops the current managed process.
- Starts a new managed process using current config and links.

## Watchdog Behavior

The watchdog runs inside the Starlette service. When `stream.auto_recover` is true, it checks every few seconds for:

- managed process exit
- missing ffmpeg child after startup grace
- missing playlist after startup grace
- stale playlist older than `playlist_stale_seconds`

The watchdog respects `watchdog_restart_cooldown` to avoid rapid restart loops. If no links are configured, it skips restart and records a warning.

## HLS Health

The app reads the configured output directory and checks:

- `ufc.m3u8` existence
- playlist line count
- playlist age
- target duration
- media sequence
- segment names
- segment count
- total segment bytes
- last segment size
- media sequence movement
- bytes and segment deltas between scorer samples
- recent stream errors
- ffmpeg progress evidence

`/api/health` returns:

- `200` when a managed process is running, a playlist is ready, and the playlist is fresh.
- `503` when the stream is stopped, still starting, missing playlist output, or stale.

The stream health scorer waits at least `min_assessment_seconds` before confirming failure, then requires `confirmed_failure_samples` bad samples. This prevents a normal startup window or a quiet segment interval from being treated as a hard failure.

## Logs And Events

The status payload exposes recent events, logs, and errors from in-memory deques. ArangoDB stores durable operational records when enabled.

Use systemd logs for service-level failures:

```bash
journalctl -u obbystreams.service -f
```

Use the dashboard logs for stream-level failures from the transcoder and ffmpeg.

## GPU Telemetry

The dashboard polls `/api/nvidia-smi` every five seconds. The backend caches NVIDIA SMI collection for the same interval so the UI can stay fresh without spawning unnecessary GPU probes.

GPU telemetry is best-effort. Hosts without NVIDIA drivers return a structured degraded payload instead of failing the dashboard.

## Deploy Routine

1. Pull or unpack the new release.
2. Run `npm ci && npm run build` if building from source.
3. Copy files to `/opt/obbystreams`.
4. Run `uv sync --no-dev --frozen` as the service user.
5. Merge any new example config keys into the live config.
6. Run `sudo systemctl restart obbystreams.service`.
7. Check `/api/health`, the dashboard, and `journalctl`.

## Useful Commands

```bash
sudo systemctl status obbystreams.service --no-pager
journalctl -u obbystreams.service --no-pager -n 120
curl -i http://127.0.0.1:8767/api/health
curl -I https://s.obby.ca/
curl -sS http://127.0.0.1:8767/api/nvidia-smi -H 'x-obbystreams-token: TOKEN'
sudo nginx -t
ps aux | rg 'obbystreams|ufc|ffmpeg'
ls -lah /var/www/live.obnoxious.lol/stream
```

## Recovery Notes

- If the dashboard is up but health is `503`, inspect stream links and HLS output.
- If nginx returns `502`, inspect systemd status and confirm uvicorn is bound to `127.0.0.1:8767`.
- If the player is blank, check `/hls/ufc.m3u8` and browser network requests.
- If ArangoDB is offline, the dashboard should still operate but persistence will be degraded.
