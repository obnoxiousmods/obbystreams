# Obbystreams

Obbystreams is a Starlette dashboard for managing a live HLS stream produced by the `ufc` transcoder. It gives you a dark broadcast-ops control surface for starting/stopping the stream, managing fallback links, viewing HLS health, reading recent process logs, and persisting events/metrics to ArangoDB.

The web UI is intentionally separate from the transcoder CLI. The service starts the configured stream command, captures its output, tracks process/HLS metrics, and stores operational history in ArangoDB.

## Features

- Start, stop, and restart the managed stream from the browser.
- Add, remove, and reorder stream links.
- Start with `kill_existing: true` so an old `ufc`/`obbystreams` process can be killed before a new managed stream starts.
- View HLS metrics: playlist existence, playlist age, segment count, and bytes on disk.
- View process metrics: managed PID, CPU, RSS, and child process data.
- See recent stream events and captured CLI/ffmpeg logs.
- Persist events, link changes, metrics, configs, and snapshots to ArangoDB.
- Configurable through `/etc/obbystreams/obbystreams.yaml`.
- Runs behind nginx at `s.obby.ca` by default.

## Layout

```text
/opt/obbystreams/                  # installed app
/etc/obbystreams/obbystreams.yaml  # live config with secrets
/usr/bin/obbystreams               # stream command wrapper
/etc/systemd/system/obbystreams.service
/etc/nginx/sites-available/s.obby.ca
```

## Configuration

Start from `examples/obbystreams.example.yaml`:

```yaml
server:
  host: 127.0.0.1
  port: 8767

dashboard:
  password: "change-me"
  session_token: "change-me-to-a-long-random-token"

stream:
  command: /usr/bin/obbystreams
  encoder: auto
  output_dir: /var/www/live.obnoxious.lol/stream
  ffmpeg_log_dir: ffmpegLogs
  public_hls_url: https://live.obnoxious.lol/stream/ufc.m3u8
  bitrate: 6M
  audio_bitrate: 192k
  min_assessment_seconds: 15
  health_sample_interval: 2
  success_score_threshold: 180
  failure_score_threshold: -120
  confirmed_failure_samples: 2
  failure_ramp_seconds: 60
  links: []

arangodb:
  enabled: true
  url: http://127.0.0.1:8529
  database: obbystreams
  username: obbystreams_app
  password: "change-me"
```

## ArangoDB Bootstrap

Use a root ArangoDB credential once to create a scoped database user:

```bash
python3 tools/bootstrap_arango.py \
  --root-password 'your-root-password' \
  --app-password 'long-random-app-password'
```

The app only needs the scoped `obbystreams_app` account after bootstrap.

## Manual Development

```bash
uv sync --dev
export OBBYSTREAMS_CONFIG=/etc/obbystreams/obbystreams.yaml
uv run uvicorn app:app --host 127.0.0.1 --port 8767 --reload
```

Then open `http://127.0.0.1:8767`.

## API

- `POST /api/auth/login`
- `GET /api/health` (readiness/liveness for monitoring)
- `GET /api/status`
- `GET /api/config`
- `PUT /api/config`
- `POST /api/links`
- `POST /api/links/remove`
- `POST /api/stream/start`
- `POST /api/stream/stop`
- `POST /api/stream/restart`
- `GET /api/arango`

Authenticated API calls use `x-obbystreams-token` or the `obbystreams_token` cookie.

`/api/health` is intentionally unauthenticated so systemd/nginx/monitoring checks can probe readiness.

Stream health is scored over time. A managed stream gets at least `min_assessment_seconds` of runtime evidence before the dashboard or transcoder confirms failure. Fresh HLS output, segment growth, media sequence movement, bytes written, and ffmpeg progress add strong positive score; stale/missing HLS, repeated ffmpeg errors, and no progress add negative score that ramps over `failure_ramp_seconds`.

The dashboard encoder toggle writes `encoder: auto`, `encoder: gpu-only`, or `encoder: cpu`. `gpu-only` refuses to fall back to CPU if no GPU encoder/device is available; `cpu` always uses libx264.

Every ffmpeg child process writes a durable log file under `ffmpeg_log_dir` (default `ffmpegLogs` relative to the service working directory). Each file includes the command, selected link, encoder, ffmpeg progress output, stderr, exit code, HLS snapshot, and stream assessment summary.

## Production Notes

- Run the web service as an unprivileged user that can write the HLS output directory.
- Keep `/etc/obbystreams/obbystreams.yaml` mode `640` or tighter because it contains dashboard and ArangoDB credentials.
- Keep the actual transcoder as `/usr/bin/ufc`; `/usr/bin/obbystreams` is a wrapper for product naming.
- nginx should proxy only to `127.0.0.1:8767`.

## Repository Features

The repo includes:

- uv dependency management with `pyproject.toml` and `uv.lock`.
- React/Vite frontend source under `frontend/`, with production assets built into `static/`.
- CI for Ruff, mypy, Python compile checks, frontend typecheck/lint/build, and example YAML validation.
- CodeQL scanning.
- Dependabot for GitHub Actions, Python, and npm dependencies.
- Release workflow for tagged source archives and checksums.
- Issue templates, pull request template, security policy, changelog, license, and CODEOWNERS.

## Frontend Development

The production service still serves static files from `static/`; React is a build-time dependency only.

```bash
npm ci
npm run typecheck
npm run lint
npm run build
```

For local UI development, run the Starlette app on `127.0.0.1:8767`, then start Vite:

```bash
uv run uvicorn app:app --host 127.0.0.1 --port 8767 --reload
npm run dev
```

Vite proxies `/api` and `/hls` to the Starlette backend.
