---
layout: page
title: Obbystreams Security
description: Security model for Obbystreams dashboard access, session tokens, ArangoDB credentials, HLS routes, and production hardening.
---

# Security

Obbystreams controls a local streaming process. Treat dashboard access as privileged infrastructure access.

## Secrets

The live config contains:

- dashboard password
- session token
- ArangoDB password
- stream source URLs
- private IPTV provider cookies and playlist keys

Keep `/etc/obbystreams/obbystreams.yaml` mode `640` or tighter.

## Public Surface

Public unauthenticated routes:

- `/`
- `/static/*`
- `/api/health`
- `/hls/*`

Guarded API routes require the session token. Do not run production with an empty `dashboard.session_token`.

## Recommended Deployment

- Bind Starlette to `127.0.0.1`.
- Put nginx in front of the app.
- Enable HTTPS.
- Keep ArangoDB on localhost or a private network.
- Run as an unprivileged user.
- Restrict write paths in systemd.
- Rotate tokens after exposure.

## Private IPTV Secrets

`private_iptv.cookies`, provider playlist keys, `Cookie`, `Authorization`, and token-like headers are operational secrets. They are required only on the server side and are redacted from public config/status responses.

Do not commit the live provider cookie, direct private playlist key, or generated source manifest. Keep `/tmp/obbystreams-sources.json` readable only by the service user because it may contain per-source request headers.

## Reporting

Report vulnerabilities through GitHub private advisories:

`https://github.com/obnoxiousmods/obbystreams/security/advisories/new`

Do not disclose exploitable vulnerabilities in public issues.
