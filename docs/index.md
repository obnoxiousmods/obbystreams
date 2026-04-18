---
layout: home
title: Obbystreams Documentation
---

# Obbystreams Documentation

Obbystreams runs a production HLS stream from a browser-accessible control plane. It combines a Starlette backend, a React/Tailwind frontend, a resilient transcoder wrapper, ArangoDB operational history, systemd process supervision, and nginx TLS termination.

## What To Read First

- [Installation](installation.md): host setup, frontend build, service install, nginx, ArangoDB, verification, and rollback.
- [Configuration](configuration.md): every YAML section and practical production defaults.
- [Operations](operations.md): start/stop/restart, watchdog behavior, HLS health, process telemetry, and deploy routines.
- [API](api.md): authentication, endpoint payloads, health checks, HLS proxy behavior, and examples.
- [Frontend](frontend.md): React, Tailwind, Video.js controls, build pipeline, and local UI workflow.
- [Release](release.md): versioning, artifacts, GitHub Releases, GitHub Pages, and wiki publishing.
- [Security](security.md): threat model, secrets, service hardening, and reporting.
- [Troubleshooting](troubleshooting.md): common production failures and commands that narrow them quickly.
- [Changelog](changelog.md): release history.

## Runtime Architecture

```text
Browser
  |
  | HTTPS
  v
nginx at s.obby.ca
  |
  | http://127.0.0.1:8767
  v
Starlette app.py
  |-- serves static React build
  |-- exposes JSON API
  |-- proxies /hls/*
  |-- manages local stream process
  |-- writes operational records to ArangoDB
  v
bin/obbystreams / ufc -> ffmpeg -> HLS output directory
```

The dashboard and transcoder are separate responsibilities. The dashboard starts and observes the configured command, while the command handles source selection, encoder choice, ffmpeg execution, and HLS output.

## Production Defaults

```text
/opt/obbystreams/                  Application files
/etc/obbystreams/obbystreams.yaml  Live config and secrets
/usr/bin/obbystreams               Transcoder wrapper
/var/www/live.obnoxious.lol/stream HLS output directory
127.0.0.1:8767                     Starlette bind address
s.obby.ca                          Public nginx vhost
```

## Release 0.2.0

Version `0.2.0` is the frontend and documentation release. It adds the React/Tailwind dashboard, custom Video.js controls, purple accent theming, GitHub Pages docs, release bundles, and a GitHub wiki publishing pass.

Read the full notes in [releases/v0.2.0.md](releases/v0.2.0.md).
