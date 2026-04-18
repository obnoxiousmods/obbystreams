# Obbystreams

Obbystreams is a production dashboard for running a resilient HLS stream. It wraps the `ufc` transcoder, controls start/stop/restart from the browser, watches playlist and process health, persists operational history to ArangoDB, and serves a React/Tailwind control room at `s.obby.ca`.

The project is intentionally small: one Starlette service, one Vite-built frontend, one managed transcoder process, one YAML configuration file, and optional ArangoDB persistence. The service is designed to sit behind nginx on `127.0.0.1`, while nginx handles TLS and public traffic.

## Highlights

- React 19 + Vite + Tailwind CSS frontend with a purple accent system, responsive control panels, and custom Video.js live-stream controls.
- Start, stop, and restart the managed stream from the browser.
- Add, remove, deduplicate, and reorder HLS input links.
- Kill existing unmanaged `ufc` or `obbystreams` processes before launching a new managed stream.
- Track HLS freshness, segment count, playlist readiness, target duration, media sequence, and written bytes.
- Track managed process PID, runtime, CPU, RSS, child processes, exits, and watchdog restarts.
- Proxy `/hls/*` through the dashboard so the player can use local output first and configured upstream output as fallback.
- Persist events, logs, metrics, configs, links, and snapshots to ArangoDB when enabled.
- Expose `/api/health` for systemd, nginx, uptime checks, and external monitoring.
- Ship production examples for systemd, nginx, ArangoDB bootstrap, GitHub Actions CI, GitHub Releases, GitHub Pages, issue templates, and release notes.

## Repository Layout

```text
app.py                         Starlette backend and stream manager
bin/obbystreams                Resilient HLS transcoder wrapper
frontend/                      React, TypeScript, Video.js, Tailwind UI source
static/                        Built frontend served by Starlette
examples/obbystreams.example.yaml
tools/bootstrap_arango.py      Scoped ArangoDB user/database bootstrap
systemd/obbystreams.service    Production service unit
nginx/s.obby.ca                Production reverse proxy example
docs/                          GitHub Pages documentation source
```

## Quick Start

Install Python dependencies, build the frontend, and run the backend locally:

```bash
uv sync --dev
npm ci
npm run build
export OBBYSTREAMS_CONFIG=examples/obbystreams.example.yaml
uv run uvicorn app:app --host 127.0.0.1 --port 8767 --reload
```

Open `http://127.0.0.1:8767`.

For active frontend development, keep the Starlette app running and start Vite in another shell:

```bash
npm run dev
```

Vite proxies `/api` and `/hls` to the backend on `127.0.0.1:8767`.

## Production Model

Default production paths:

```text
/opt/obbystreams/                  Installed application
/etc/obbystreams/obbystreams.yaml  Live config with credentials
/usr/bin/obbystreams               Transcoder command wrapper
/var/www/live.obnoxious.lol/stream HLS output directory
/etc/systemd/system/obbystreams.service
/etc/nginx/sites-available/s.obby.ca
```

The service should run as an unprivileged user with write access to the HLS output directory and read access to `/etc/obbystreams/obbystreams.yaml`. nginx should proxy public traffic to `http://127.0.0.1:8767`.

## Configuration

Start from [examples/obbystreams.example.yaml](examples/obbystreams.example.yaml):

```yaml
server:
  host: 127.0.0.1
  port: 8767
  workers: 1

dashboard:
  password: "change-me"
  session_token: "change-me-to-a-long-random-token"

stream:
  command: /usr/bin/obbystreams
  encoder: auto
  output_dir: /var/www/live.obnoxious.lol/stream
  public_hls_url: https://live.obnoxious.lol/stream/ufc.m3u8
  auto_recover: true
  auto_restart_on_exit: true
  watchdog_restart_cooldown: 20
  startup_grace_seconds: 25
  playlist_stale_seconds: 25
  bitrate: 6M
  audio_bitrate: 192k
  restart_delay: 2
  max_restart_delay: 120
  rate_limit_delay: 180
  stop_after_failed_rounds: 2
  links:
    - https://example.com/primary/live.m3u8
    - https://example.com/backup/live.m3u8

arangodb:
  enabled: true
  url: http://127.0.0.1:8529
  database: obbystreams
  username: obbystreams_app
  password: "change-me"
```

Use long random values for `dashboard.password`, `dashboard.session_token`, and `arangodb.password`. Keep the live YAML mode `640` or tighter.

## API Summary

Authenticated endpoints accept either the `x-obbystreams-token` header or the `obbystreams_token` cookie.

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `GET` | `/api/health` | No | Readiness and liveness checks |
| `POST` | `/api/auth/login` | Password | Create dashboard token cookie |
| `GET` | `/api/status` | Yes | Current config, process, HLS, logs, events, runtime |
| `GET` | `/api/config` | Yes | Sanitized runtime config |
| `PUT` | `/api/config` | Yes | Update stream config and restart when required |
| `POST` | `/api/links` | Yes | Add a stream link |
| `POST` | `/api/links/remove` | Yes | Remove a stream link |
| `POST` | `/api/stream/start` | Yes | Start managed stream |
| `POST` | `/api/stream/stop` | Yes | Stop managed stream |
| `POST` | `/api/stream/restart` | Yes | Restart managed stream |
| `GET` | `/api/arango` | Yes | ArangoDB connectivity status |
| `GET` | `/api/nvidia-smi` | Yes | NVIDIA GPU telemetry and ffmpeg/NVENC visibility |
| `GET` | `/hls/{path}` | No | Local-first HLS proxy |

Full API details live in [docs/api.md](docs/api.md).

## Documentation

- [Installation](INSTALL.md)
- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)
- [Changelog](CHANGELOG.md)
- [GitHub Pages docs](docs/index.md)
- [Release process](docs/release.md)
- [Troubleshooting](docs/troubleshooting.md)

After GitHub Pages is enabled for this repository, the published docs are available at `https://obnoxiousmods.github.io/obbystreams/`.

## Releases And Packages

Tagged releases publish release assets rather than a package registry image:

- `obbystreams-vX.Y.Z-source.tar.gz` contains the repository source at the tag.
- `obbystreams-vX.Y.Z-static.tar.gz` contains the built frontend served by Starlette.
- `obbystreams-vX.Y.Z-install-bundle.tar.gz` contains the deployable application files, examples, service files, and docs.
- `SHA256SUMS` contains checksums for release artifacts.

The release workflow runs on `v*.*.*` tags. See [docs/release.md](docs/release.md) for the checklist.

## Validation

Run these before pushing changes:

```bash
npm run typecheck
npm run lint
npm run build
npm audit --audit-level=moderate
uv run pytest
uv run ruff check .
uv run mypy app.py
uv run python -m py_compile app.py tools/bootstrap_arango.py
```

## License

MIT. See [LICENSE](LICENSE).
