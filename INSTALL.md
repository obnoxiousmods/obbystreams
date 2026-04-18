# Installation

This guide installs Obbystreams as a Starlette service behind nginx with the React frontend prebuilt into `static/`. It assumes a Linux host with systemd, nginx, Python 3.11 or newer, Node 20 or newer, `uv`, ArangoDB, ffmpeg, and a working `ufc`/`obbystreams` transcoder command.

## 1. Prepare The Host

Create the application, configuration, and HLS output directories:

```bash
sudo mkdir -p /opt/obbystreams /etc/obbystreams /var/www/live.obnoxious.lol/stream
sudo chown -R joey:nobody /opt/obbystreams /var/www/live.obnoxious.lol/stream
sudo chmod 775 /var/www/live.obnoxious.lol/stream
```

Adjust `joey:nobody` to the service user and group used on your host.

Install runtime tools:

```bash
sudo apt-get update
sudo apt-get install -y nginx ffmpeg python3 python3-venv
```

Install `uv` if it is not already available:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 2. Build Frontend Assets

Build the production React/Tailwind bundle before copying files:

```bash
npm ci
npm run typecheck
npm run lint
npm run build
```

The build writes hashed assets into `static/assets/` and updates `static/index.html`.

## 3. Install Application Files

Copy the application into `/opt/obbystreams`:

```bash
sudo rsync -a \
  app.py bin static tools examples systemd nginx docs \
  pyproject.toml uv.lock package.json package-lock.json \
  /opt/obbystreams/

cd /opt/obbystreams
sudo chown -R joey:nobody /opt/obbystreams
sudo -u joey /home/joey/.local/bin/uv sync --no-dev --frozen
```

Install the transcoder wrapper:

```bash
sudo cp /opt/obbystreams/bin/obbystreams /usr/bin/obbystreams
sudo chmod 755 /usr/bin/obbystreams
```

If your real transcoder command is `/usr/bin/ufc`, keep that installed as well. Obbystreams detects and manages both `ufc` and `obbystreams` process names.

## 4. Configure Obbystreams

Create the live config:

```bash
sudo cp /opt/obbystreams/examples/obbystreams.example.yaml /etc/obbystreams/obbystreams.yaml
sudo chown joey:nobody /etc/obbystreams/obbystreams.yaml
sudo chmod 640 /etc/obbystreams/obbystreams.yaml
```

Edit `/etc/obbystreams/obbystreams.yaml`:

```yaml
dashboard:
  password: "use-a-real-dashboard-password"
  session_token: "use-a-long-random-session-token"

stream:
  command: /usr/bin/obbystreams
  encoder: auto
  output_dir: /var/www/live.obnoxious.lol/stream
  ffmpeg_log_dir: ffmpegLogs
  public_hls_url: https://live.obnoxious.lol/stream/ufc.m3u8
  min_assessment_seconds: 15
  success_score_threshold: 180
  failure_score_threshold: -120
  confirmed_failure_samples: 2
  failure_ramp_seconds: 60
  links:
    - https://your-primary-source.example/live.m3u8
    - https://your-backup-source.example/live.m3u8

arangodb:
  enabled: true
  url: http://127.0.0.1:8529
  database: obbystreams
  username: obbystreams_app
  password: "use-a-real-arango-password"
```

Generate random secret material with a tool such as:

```bash
openssl rand -hex 32
```

## 5. Bootstrap ArangoDB

Run the bootstrap script once with an ArangoDB root credential:

```bash
python3 /opt/obbystreams/tools/bootstrap_arango.py \
  --root-password 'root-password' \
  --app-password 'same-password-as-yaml'
```

The application only needs the scoped `obbystreams_app` user after bootstrap. Keep root credentials out of `/etc/obbystreams/obbystreams.yaml`.

## 6. Install systemd Service

```bash
sudo cp /opt/obbystreams/systemd/obbystreams.service /etc/systemd/system/obbystreams.service
sudo systemctl daemon-reload
sudo systemctl enable --now obbystreams.service
sudo systemctl status obbystreams.service --no-pager
```

Inspect logs:

```bash
journalctl -u obbystreams.service -f
```

## 7. Install nginx

```bash
sudo cp /opt/obbystreams/nginx/s.obby.ca /etc/nginx/sites-available/s.obby.ca
sudo ln -sf /etc/nginx/sites-available/s.obby.ca /etc/nginx/sites-enabled/s.obby.ca
sudo nginx -t
sudo systemctl reload nginx
```

The bundled vhost expects certificates under `/etc/letsencrypt/live/obby.ca/`. Update paths before reloading nginx if your certificate layout differs.

## 8. Verify The Install

Local backend:

```bash
curl -i http://127.0.0.1:8767/api/health
```

Public dashboard:

```bash
curl -I https://s.obby.ca/
```

Authenticated status check:

```bash
curl -sS https://s.obby.ca/api/auth/login \
  -H 'content-type: application/json' \
  -d '{"password":"your-dashboard-password"}'
```

Use the returned token as `x-obbystreams-token` for API calls, or log in through the browser.

## 9. Update Existing Installs

Build and copy the new files:

```bash
npm ci
npm run build
sudo rsync -a app.py bin static tools examples systemd nginx docs pyproject.toml uv.lock package.json package-lock.json /opt/obbystreams/
cd /opt/obbystreams
sudo -u joey /home/joey/.local/bin/uv sync --no-dev --frozen
sudo systemctl restart obbystreams.service
```

If the config schema changed, merge new keys from `examples/obbystreams.example.yaml` into `/etc/obbystreams/obbystreams.yaml` before restarting.

## 10. Rollback

Use the previous release artifact or git tag:

```bash
sudo systemctl stop obbystreams.service
sudo rsync -a /path/to/previous/obbystreams/ /opt/obbystreams/
sudo -u joey /home/joey/.local/bin/uv sync --no-dev --frozen
sudo systemctl start obbystreams.service
```

Rollback does not automatically revert `/etc/obbystreams/obbystreams.yaml`. Keep a backup of the live config before production changes.
