---
layout: page
title: Install Obbystreams
description: Production installation steps for Obbystreams with uv, npm, systemd, nginx, ArangoDB, and HLS output directories.
---

# Installation

This page mirrors the root `INSTALL.md` with operational context for GitHub Pages.

## Prerequisites

- Linux host with systemd.
- nginx for TLS and reverse proxying.
- Python 3.11 or newer.
- Node 20 or newer for frontend builds.
- `uv` for Python dependency management.
- ArangoDB if persistence is enabled.
- ffmpeg and a working `ufc` or `obbystreams` transcoder command.
- A service user with write access to the HLS output directory.

## Directory Layout

```bash
sudo mkdir -p /opt/obbystreams /etc/obbystreams /var/www/live.obnoxious.lol/stream
sudo chown -R joey:nobody /opt/obbystreams /var/www/live.obnoxious.lol/stream
sudo chmod 775 /var/www/live.obnoxious.lol/stream
```

Use your actual service user and group if they differ from `joey:nobody`.

## Build Frontend

```bash
npm ci
npm run typecheck
npm run lint
npm run build
```

The backend serves the generated files from `static/`. Do not edit files in `static/assets/` manually.

## Copy Application

```bash
sudo rsync -a \
  app.py bin static tools examples systemd nginx docs \
  pyproject.toml uv.lock package.json package-lock.json \
  /opt/obbystreams/

cd /opt/obbystreams
sudo chown -R joey:nobody /opt/obbystreams
sudo -u joey /home/joey/.local/bin/uv sync --no-dev --frozen
sudo cp /opt/obbystreams/bin/obbystreams /usr/bin/obbystreams
sudo chmod 755 /usr/bin/obbystreams
```

## Configure

```bash
sudo cp /opt/obbystreams/examples/obbystreams.example.yaml /etc/obbystreams/obbystreams.yaml
sudo chown joey:nobody /etc/obbystreams/obbystreams.yaml
sudo chmod 640 /etc/obbystreams/obbystreams.yaml
```

Edit the live YAML and set real dashboard, stream, and ArangoDB values. Use `openssl rand -hex 32` for token material.

## Bootstrap ArangoDB

```bash
python3 /opt/obbystreams/tools/bootstrap_arango.py \
  --root-password 'root-password' \
  --app-password 'same-password-as-yaml'
```

Use the root password once, then operate with the scoped `obbystreams_app` account.

## Install systemd

```bash
sudo cp /opt/obbystreams/systemd/obbystreams.service /etc/systemd/system/obbystreams.service
sudo systemctl daemon-reload
sudo systemctl enable --now obbystreams.service
sudo systemctl status obbystreams.service --no-pager
```

## Install nginx

```bash
sudo cp /opt/obbystreams/nginx/s.obby.ca /etc/nginx/sites-available/s.obby.ca
sudo ln -sf /etc/nginx/sites-available/s.obby.ca /etc/nginx/sites-enabled/s.obby.ca
sudo nginx -t
sudo systemctl reload nginx
```

Update certificate paths in `nginx/s.obby.ca` if your host does not use the bundled Let's Encrypt layout.

## Verify

```bash
curl -i http://127.0.0.1:8767/api/health
curl -I https://s.obby.ca/
journalctl -u obbystreams.service --no-pager -n 80
```

If `/api/health` returns `503`, the app is reachable but the managed stream is not fully ready. That is a service state problem, not necessarily an HTTP routing problem.

## Rollback

Keep the previous release artifact or git checkout available. To roll back application code:

```bash
sudo systemctl stop obbystreams.service
sudo rsync -a /path/to/previous/obbystreams/ /opt/obbystreams/
cd /opt/obbystreams
sudo -u joey /home/joey/.local/bin/uv sync --no-dev --frozen
sudo systemctl start obbystreams.service
```

Back up `/etc/obbystreams/obbystreams.yaml` before config changes. Application rollback does not automatically roll back live config.
