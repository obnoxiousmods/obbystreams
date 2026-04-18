---
layout: page
title: Obbystreams Troubleshooting
description: Diagnose Obbystreams nginx 502s, health 503s, blank players, ArangoDB errors, stale frontend assets, and HLS output problems.
---

# Troubleshooting

Start by separating HTTP reachability, dashboard auth, managed process state, HLS output, and ArangoDB persistence.

## Dashboard Returns 502

Check systemd:

```bash
sudo systemctl status obbystreams.service --no-pager
journalctl -u obbystreams.service --no-pager -n 120
```

Confirm uvicorn is listening:

```bash
ss -ltnp | rg '8767|uvicorn'
```

Check nginx:

```bash
sudo nginx -t
sudo systemctl status nginx --no-pager
```

## Health Returns 503

`503` means the app is reachable but the stream is not ready.

Check:

```bash
curl -sS http://127.0.0.1:8767/api/health
ps aux | rg 'obbystreams|ufc|ffmpeg'
ls -lah /var/www/live.obnoxious.lol/stream
```

Common causes:

- no links configured
- bad source link
- ffmpeg failed before playlist output
- output directory permissions
- stale playlist
- watchdog cooldown still in effect

## Player Is Blank

Check the playlist:

```bash
curl -i http://127.0.0.1:8767/hls/ufc.m3u8
curl -i https://s.obby.ca/hls/ufc.m3u8
```

In the browser, inspect network requests for `/hls/ufc.m3u8` and segment files. A playlist with no media segment lines is treated as not ready.

## Login Fails

Check `/etc/obbystreams/obbystreams.yaml`:

```bash
sudo grep -n 'password\\|session_token' /etc/obbystreams/obbystreams.yaml
```

Restart after manual config edits:

```bash
sudo systemctl restart obbystreams.service
```

## ArangoDB Offline

Check the app endpoint:

```bash
curl -sS http://127.0.0.1:8529/_api/version
```

Check app status through the dashboard API:

```bash
curl -sS https://s.obby.ca/api/arango -H 'x-obbystreams-token: TOKEN'
```

The dashboard can operate with ArangoDB degraded, but durable history will be incomplete.

## Frontend Looks Old

Rebuild and restart:

```bash
npm ci
npm run build
sudo rsync -a static /opt/obbystreams/
sudo systemctl restart obbystreams.service
```

Then hard-refresh the browser. The build uses hashed asset names, so a stale `static/index.html` usually means the production copy did not update.
